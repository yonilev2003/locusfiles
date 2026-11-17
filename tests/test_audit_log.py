"""R4: the audit log of what the file tools handed over: paths, counts and the client's name, never file text.

`<data dir>/audit/mcp-YYYY-MM-DD.jsonl` (one JSON line per call, owner-only, 90 days). The query text is not in it
unless `[audit] queries = true`. A line that cannot be written never stops an answer. Offline: HashEmbedder.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("chromadb")

from mcp import Client  # noqa: E402
from mcp.types import Implementation  # noqa: E402

from locus import audit, cli, killswitch, service  # noqa: E402
from locus import mcp_server as srv  # noqa: E402
from locus.config import DEFAULT_EXTENSIONS, Settings  # noqa: E402
from locus.ingest.embedder import HashEmbedder  # noqa: E402
from locus.ingest.indexer import index_folder  # noqa: E402
from locus.ingest.store import ChromaStore  # noqa: E402

SENTINEL = "ZXQ-sentinel-vermilion-hippopotamus"
QUERY = "what is the launch plan for quokkas"


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = (tmp_path / "home").resolve()
    notes = home / "notes"
    (notes / "private").mkdir(parents=True)
    (notes / "plan.md").write_text(f"the launch plan for quokkas mentions {SENTINEL} twice", encoding="utf-8")
    (notes / "private" / "diary.md").write_text("a diary about quokkas", encoding="utf-8")
    settings = Settings(
        data_dir=tmp_path / "data", collection="audit", embedder="hash", voyage_model="", openai_model="",
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
    audit._LOGS.clear()
    return notes, settings


def lines(settings: Settings, source: str = "mcp") -> list[dict]:
    files = sorted((settings.data_dir / "audit").glob(f"{source}-*.jsonl"))
    return [json.loads(line) for f in files for line in f.read_text(encoding="utf-8").splitlines()]


def raw(settings: Settings) -> str:
    return "".join(f.read_text(encoding="utf-8") for f in (settings.data_dir / "audit").glob("*.jsonl"))


def test_a_search_and_a_read_leave_two_lines_with_the_paths_and_the_characters_returned(world):
    notes, settings = world
    plan = str(notes / "plan.md")
    found = asyncio.run(srv.locus_search(query=QUERY))
    read = asyncio.run(srv.locus_read_file(path=plan))
    first, second = lines(settings)
    assert (first["tool"], second["tool"]) == ("locus_search", "locus_read_file")
    assert plan in first["paths"] and second["paths"] == [plan]
    assert first["chars"] == len(found) and second["chars"] == len(read)
    assert first["hidden"] == 0 and first["truncated"] is False and first["client"] == "unknown"
    assert datetime.fromisoformat(first["ts"]).tzinfo is not None


def test_no_file_text_and_no_query_text_is_written_by_default(world):
    notes, settings = world
    asyncio.run(srv.locus_search(query=QUERY))
    asyncio.run(srv.locus_read_file(path=str(notes / "plan.md")))
    asyncio.run(srv.locus_list_sources())
    text = raw(settings)
    assert SENTINEL not in text and "quokkas" not in text and QUERY not in text
    assert "launch plan" not in text


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_the_files_are_owner_only_and_so_is_the_folder(world):
    notes, settings = world
    asyncio.run(srv.locus_search(query=QUERY))
    folder = settings.data_dir / "audit"
    file = next(folder.glob("mcp-*.jsonl"))
    assert stat.S_IMODE(file.stat().st_mode) == 0o600 and stat.S_IMODE(folder.stat().st_mode) == 0o700


def test_the_query_is_recorded_only_when_the_policy_asks_for_it(world):
    notes, settings = world
    (settings.data_dir / "policy.toml").write_text("[audit]\nqueries = true\n", encoding="utf-8")
    asyncio.run(srv.locus_search(query=QUERY))
    asyncio.run(srv.locus_list_sources())
    searched, listed = lines(settings)
    assert searched["query"] == QUERY and "query" not in listed
    assert SENTINEL not in raw(settings)  # still never file text


def test_results_hidden_by_the_folder_rules_are_counted_and_their_paths_are_not_logged(world):
    notes, settings = world
    (settings.data_dir / "policy.toml").write_text(f'[deny]\npaths = ["{notes / "private"}"]\n', encoding="utf-8")
    asyncio.run(srv.locus_search(query="a diary about quokkas"))
    row = lines(settings)[0]
    assert row["hidden"] == 1 and str(notes / "private") not in raw(settings)


def test_a_refused_call_is_logged_with_no_paths(world):
    notes, settings = world
    killswitch.pause(settings.data_dir)
    asyncio.run(srv.locus_search(query=QUERY))
    row = lines(settings)[0]
    assert row["refused"] == "paused" and row["paths"] == [] and row["chars"] == len(killswitch.PAUSED_SENTENCE)


def test_the_client_name_the_app_announced_is_recorded_and_the_tools_schema_is_unchanged(world):
    notes, settings = world

    async def go():
        async with Client(srv.mcp, client_info=Implementation(name="Test Desktop", version="9.8")) as client:
            listed = await client.list_tools()
            await client.call_tool("locus_search", {"query": QUERY})
            return listed

    listed = asyncio.run(go())
    row = lines(settings)[0]
    assert (row["client"], row["client_version"]) == ("Test Desktop", "9.8")
    for tool in listed.tools:
        assert "ctx" not in json.dumps(tool.input_schema)  # the SDK's own argument is not part of the tool's schema


def test_a_log_that_cannot_be_written_never_stops_a_search_and_says_so_on_stderr_only(world, capsys):
    notes, settings = world
    (settings.data_dir / "audit").write_text("not a folder", encoding="utf-8")  # the audit folder cannot be created
    capsys.readouterr()
    found = asyncio.run(srv.locus_search(query=QUERY))
    out, err = capsys.readouterr()
    assert SENTINEL in found and out == "" and "audit log could not be written" in err


def test_files_older_than_90_days_are_deleted_when_a_line_is_written(world):
    notes, settings = world
    folder = settings.data_dir / "audit"
    folder.mkdir()
    today = datetime.now(UTC).date()
    keep = [today - timedelta(days=d) for d in (0, 1, 89)]
    drop = [today - timedelta(days=d) for d in (91, 400)]
    for day in keep + drop:
        for source in ("mcp", "api", "cli"):
            (folder / f"{source}-{day.isoformat()}.jsonl").write_text('{"ts":"x"}\n', encoding="utf-8")
    asyncio.run(srv.locus_search(query=QUERY))
    left = {p.name for p in folder.iterdir()}
    for day in keep:
        assert {f"{s}-{day.isoformat()}.jsonl" for s in ("mcp", "api", "cli")} <= left
    for day in drop:
        assert not any(name.endswith(f"{day.isoformat()}.jsonl") for name in left)


def test_the_command_line_json_commands_are_counted_as_the_cli(world, capsys):
    notes, settings = world
    capsys.readouterr()
    with pytest.raises(SystemExit):
        cli.main(["query", QUERY, "--json"])
    capsys.readouterr()
    row = lines(settings, "cli")[0]
    assert row["tool"] == "search" and row["client"] == "locus-cli" and str(notes / "plan.md") in row["paths"]
    assert SENTINEL not in raw(settings) and QUERY not in raw(settings)


def test_the_summary_counts_calls_characters_and_paths(world):
    notes, settings = world
    for _ in range(3):
        asyncio.run(srv.locus_search(query=QUERY))
    asyncio.run(srv.locus_read_file(path=str(notes / "plan.md")))
    summary = audit.summarize(audit.read_lines(settings.data_dir / "audit"))
    assert summary["calls"] == 4 and summary["by_tool"] == {"locus_search": 3, "locus_read_file": 1}
    assert summary["top_paths"][0][0] == str(notes / "plan.md") and summary["chars"] > 0
    since = datetime.now(UTC) + timedelta(hours=1)
    assert audit.summarize(audit.read_lines(settings.data_dir / "audit", since))["calls"] == 0
    time.sleep(0)  # (the clock is not faked: "since an hour from now" is simply empty)


def test_the_audit_command_prints_counts_and_paths_only(world, capsys):
    notes, settings = world
    asyncio.run(srv.locus_search(query=QUERY))
    asyncio.run(srv.locus_read_file(path=str(notes / "plan.md")))
    capsys.readouterr()
    with pytest.raises(SystemExit) as stop:
        cli.main(["audit", "--since", "1d"])
    out = capsys.readouterr().out
    assert stop.value.code == 0
    assert (
        "calls: 2 (refused 0)" in out and "locus_read_file 1, locus_search 1" in out and str(notes / "plan.md") in out
    )
    assert SENTINEL not in out and QUERY not in out
    with pytest.raises(SystemExit) as stop:
        cli.main(["audit", "--since", "yesterday"])
    assert stop.value.code == 2
