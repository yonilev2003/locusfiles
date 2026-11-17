"""R3: `locus pause` / `locus resume`, the flag file `<data dir>/PAUSED` and LOCUS_DISABLED=1.

While paused the three file tools (and the engine behind the HTTP API and the JSON commands) answer with one fixed
sentence: no path, no text from a file. Help and diagnose keep answering and say so. Offline: HashEmbedder, tmp folders.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path

import pytest

pytest.importorskip("chromadb")

from locus import cli, killswitch, service  # noqa: E402
from locus import mcp_server as srv  # noqa: E402
from locus.config import DEFAULT_EXTENSIONS, Settings  # noqa: E402
from locus.ingest.embedder import HashEmbedder  # noqa: E402
from locus.ingest.indexer import index_folder  # noqa: E402
from locus.ingest.store import ChromaStore  # noqa: E402

SENTINEL = "ZXQ-sentinel-vermilion-hippopotamus"


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = (tmp_path / "home").resolve()
    notes = home / "notes"
    notes.mkdir(parents=True)
    (notes / "plan.md").write_text(f"the launch plan mentions {SENTINEL} twice", encoding="utf-8")
    settings = Settings(
        data_dir=tmp_path / "data", collection="paused", embedder="hash", voyage_model="", openai_model="",
        local_model="", chunk_tokens=100, overlap_tokens=10, max_file_mb=5, extensions=DEFAULT_EXTENSIONS,
    )  # fmt: skip
    embedder = HashEmbedder()
    store = ChromaStore(settings.data_dir, settings.collection, embedder.name)
    index_folder(notes, embedder, store, settings, log=lambda _m: None)
    components = (embedder, store, settings)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(srv, "_components", lambda: components)
    monkeypatch.setattr(srv, "freshen_for_serve", lambda *a, **k: None)
    monkeypatch.setattr(service, "freshen_for_serve", lambda *a, **k: None)
    monkeypatch.setattr(service, "_open_default", lambda: components)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "_components", lambda _s: (embedder, store))
    monkeypatch.setenv("LOCUS_DATA_DIR", str(settings.data_dir))
    return notes, settings


def tools(notes: Path) -> list[str]:
    plan = str(notes / "plan.md")
    return [
        asyncio.run(srv.locus_search(query="launch plan")),
        asyncio.run(srv.locus_search(modified_after="2000-01-01")),
        asyncio.run(srv.locus_list_sources()),
        asyncio.run(srv.locus_read_file(path=plan)),
    ]


def run(capsys, *argv):
    capsys.readouterr()
    with pytest.raises(SystemExit) as stop:
        cli.main(list(argv))
    captured = capsys.readouterr()
    return stop.value.code, captured.out, captured.err


def test_while_paused_every_file_tool_answers_with_one_fixed_sentence_and_nothing_else(world, capsys):
    notes, settings = world
    assert SENTINEL in tools(notes)[0]  # sanity: it is found when running
    killswitch.pause(settings.data_dir)
    capsys.readouterr()
    for out in tools(notes):
        assert out == killswitch.PAUSED_SENTENCE == "Locus is paused by the user. Ask them to run `locus resume`."
        assert SENTINEL not in out and str(notes) not in out
    assert capsys.readouterr().out == ""  # nothing on stdout (it is the MCP protocol channel)


def test_help_and_diagnose_keep_answering_and_say_paused(world):
    notes, settings = world
    killswitch.pause(settings.data_dir)
    helped = asyncio.run(srv.locus_help(query="how do I install Locus"))
    assert "Locus is paused" in helped and len(helped) > 300
    diagnosed = asyncio.run(srv.locus_diagnose_connection())
    assert "# Locus diagnostics" in diagnosed and "kill switch: paused" in diagnosed
    assert SENTINEL not in diagnosed and str(notes) not in diagnosed


def test_resume_restores_search_in_the_same_process(world):
    notes, settings = world
    killswitch.pause(settings.data_dir)
    assert tools(notes)[0] == killswitch.PAUSED_SENTENCE
    assert killswitch.resume(settings.data_dir) is True
    found = tools(notes)
    assert SENTINEL in found[0] and str(notes / "plan.md") in found[2] and SENTINEL in found[3]
    assert "not paused" in asyncio.run(srv.locus_diagnose_connection())


def test_the_environment_variable_does_the_same_without_a_file(world, monkeypatch):
    notes, settings = world
    monkeypatch.setenv("LOCUS_DISABLED", "1")
    for out in tools(notes):
        assert out == killswitch.DISABLED_SENTENCE and "LOCUS_DISABLED" in out and SENTINEL not in out
    assert "kill switch: off: LOCUS_DISABLED is set" in asyncio.run(srv.locus_diagnose_connection())
    monkeypatch.setenv("LOCUS_DISABLED", "0")
    assert SENTINEL in tools(notes)[0]


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_the_flag_is_created_owner_only(world):
    _, settings = world
    killswitch.pause(settings.data_dir)
    assert stat.S_IMODE((settings.data_dir / "PAUSED").stat().st_mode) == 0o600


def test_pause_and_resume_commands(world, capsys, monkeypatch):
    notes, settings = world
    code, out, _ = run(capsys, "pause")
    assert code == 0 and "Locus is paused." in out and (settings.data_dir / "PAUSED").exists()
    assert "Locus was already paused." in run(capsys, "pause")[1]
    code, out, _ = run(capsys, "resume")
    assert code == 0 and "Locus is running again." in out and not (settings.data_dir / "PAUSED").exists()
    assert "Locus was not paused." in run(capsys, "resume")[1]
    monkeypatch.setenv("LOCUS_DISABLED", "1")
    code, out, _ = run(capsys, "resume")
    assert code == 1 and "still set" in out


def test_the_engine_the_api_and_the_json_commands_stop_too_with_a_stable_code(world, capsys):
    notes, settings = world
    killswitch.pause(settings.data_dir)
    engine = service.Engine(
        lambda: (HashEmbedder(), ChromaStore(settings.data_dir, "paused", HashEmbedder().name), settings)
    )
    for call in (
        lambda: engine.search("launch plan"),
        engine.list_sources,
        lambda: engine.read_file(str(notes / "plan.md")),
    ):
        with pytest.raises(service.ServiceError) as err:
            call()
        assert err.value.code == "paused" and err.value.message == killswitch.PAUSED_SENTENCE
    code, out, _ = run(capsys, "query", "launch plan", "--json")
    assert code == cli.EXIT_REFUSED and json.loads(out)["error"] == {
        "code": "paused",
        "message": killswitch.PAUSED_SENTENCE,
    }
    assert SENTINEL not in out
    code, out, err = run(capsys, "query", "launch plan")  # the older text form
    assert code == cli.EXIT_REFUSED and SENTINEL not in out and "paused" in err
    assert run(capsys, "sources", "--json")[0] == cli.EXIT_REFUSED
    assert run(capsys, "read", str(notes / "plan.md"), "--json")[0] == cli.EXIT_REFUSED
    assert engine.health().files == 1  # counts only: health keeps answering


def test_stats_and_doctor_show_the_state(world, capsys):
    _, settings = world
    killswitch.pause(settings.data_dir)
    assert "state      : Locus is paused" in run(capsys, "stats")[1]
    assert "[OK  ] kill switch — paused: run `locus resume`" in run(capsys, "doctor")[1]
