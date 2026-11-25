"""`locus status`: one read-only screen. Offline, HashEmbedder, a tmp data dir; the network is mocked."""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("chromadb")

from locus import cli, clients, killswitch  # noqa: E402
from locus import mcp_server as srv  # noqa: E402
from locus.config import DEFAULT_EXTENSIONS, Settings  # noqa: E402
from locus.ingest.embedder import HashEmbedder  # noqa: E402
from locus.ingest.indexer import index_folder  # noqa: E402
from locus.ingest.store import ChromaStore  # noqa: E402


@pytest.fixture()
def world(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LOCUS_ALLOW_TEST_EMBEDDER", "1")
    monkeypatch.delenv("LOCUS_DISABLED", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # client config files are looked up under HOME
    settings = Settings(
        data_dir=tmp_path / "data", collection="status", embedder="hash", voyage_model="", openai_model="",
        local_model="", chunk_tokens=400, overlap_tokens=50, max_file_mb=5, extensions=DEFAULT_EXTENSIONS,
    )  # fmt: skip
    embedder = HashEmbedder()
    store = ChromaStore(settings.data_dir, settings.collection, embedder.name)
    root = tmp_path / "notes"
    root.mkdir()
    for i in range(6):
        (root / f"f{i}.md").write_text(f"note {i}: decision about topic {i}\n", encoding="utf-8")
    index_folder(root, embedder, store, settings, log=lambda _m: None)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(srv, "_components", lambda: (embedder, store, settings))
    srv._state.update(embedder=embedder, store=store, settings=settings)
    yield root, settings, store
    srv._state.clear()


def _run(capsys, *argv: str) -> str:
    code = cli.cmd_status(cli.build_parser().parse_args(["status", *argv]))
    assert code == 0
    return capsys.readouterr().out


def _no_network(monkeypatch) -> list[str]:
    calls: list[str] = []

    def boom(*a, **k):
        calls.append("urlopen")
        raise AssertionError("network used")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    monkeypatch.setattr(cli, "_latest_version", lambda: calls.append("latest") or None)
    return calls


def test_status_without_flag_makes_no_network_call(world, capsys, monkeypatch):
    calls = _no_network(monkeypatch)
    out = _run(capsys)
    assert calls == []
    assert "latest" not in out
    assert "6 files" in out and "chunks" in out and "paused     : no" in out and "embedder   : hash" in out


def test_status_before_any_index_says_none_yet(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("LOCUS_ALLOW_TEST_EMBEDDER", "1")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    settings = Settings(
        data_dir=tmp_path / "empty", collection="status", embedder="hash", voyage_model="", openai_model="",
        local_model="", chunk_tokens=400, overlap_tokens=50, max_file_mb=5, extensions=DEFAULT_EXTENSIONS,
    )  # fmt: skip
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    out = _run(capsys)
    assert "none yet" in out
    assert not (tmp_path / "empty" / "chroma").exists()  # read-only: nothing created


def test_pending_number_is_the_one_diagnose_reports(world, capsys):
    root, _settings, store = world
    later = time.time() + 120
    for name in ("f0.md", "f1.md", "f2.md"):  # explicit: glob order is filesystem-dependent and could hit f5
        os.utime(root / name, (later, later))
    (root / "f5.md").unlink()
    out = _run(capsys)
    m = re.search(r"pending    : (\d+) changed on disk, (\d+) gone", out)
    d = re.search(r"\((\d+) changed on disk and (\d+) gone", "\n".join(srv._index_state_lines(store)))
    assert m and d and m.groups() == d.groups() == ("3", "1")


def test_paused_is_shown(world, capsys):
    _root, settings, _store = world
    killswitch.pause(settings.data_dir)
    assert "paused     : yes" in _run(capsys)


def test_clients_detected_read_only(world, tmp_path, capsys):
    cfg = Path(
        clients.resolve_path(clients.BY_NAME["cursor"], clients.current_os(), os.environ, str(tmp_path / "home"))
    )
    cfg.parent.mkdir(parents=True)
    text = '{"mcpServers": {"locus": {"command": "locus"}}}'
    cfg.write_text(text)
    out = _run(capsys)
    assert re.search(r"clients    : .*cursor", out) and "not checked" in out
    assert cfg.read_text() == text


def test_check_update_makes_one_get_and_reports(world, capsys, monkeypatch):
    seen = []

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, *a):
            return b'{"info": {"version": "9.9.9"}}'

    def fake(url, timeout=None):
        seen.append((url, timeout))
        return Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    out = _run(capsys, "--check-update")
    assert seen == [("https://pypi.org/pypi/locusfiles/json", 5)]
    assert "latest     : 9.9.9 (newer than the" in out


def _pypi_says(monkeypatch, version):
    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, *a):
            return json.dumps({"info": {"version": version}}).encode()

    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=None: Resp())


@pytest.mark.parametrize(
    ("installed", "latest", "expect"),
    [
        ("0.2.0rc2", "0.1.3", "nothing newer than 0.2.0rc2"),  # a pre-release is not told 0.1.3 is "latest"
        ("0.2.0rc2", "0.2.0", "0.2.0 (newer than the 0.2.0rc2 you run)"),
        ("0.2.0rc2", "0.2.0rc2", "0.2.0rc2 (up to date)"),
        ("0.2.0", "0.2.0", "0.2.0 (up to date)"),
        ("0.2.0", "0.1.3", "yours is newer"),
        ("0.2.0rc2", "0.1.3", "yours is a pre-release"),
        ("0.2.0.dev1", "0.2.0", "0.2.0 (newer than the 0.2.0.dev1 you run)"),
        ("0.2.0-rc.1", "0.2.0", "0.2.0 (newer than the 0.2.0-rc.1 you run)"),
        ("0.2.0rc3+local", "0.2.0rc3", "0.2.0rc3 (up to date)"),
        ("0.2.0.post1", "0.2.0", "nothing newer than 0.2.0.post1"),
        ("0.1.3", "0.2.0", "0.2.0 (newer than the 0.1.3 you run)"),
    ],
)
def test_check_update_compares_versions_and_never_calls_an_older_stable_latest(
    world, capsys, monkeypatch, installed, latest, expect
):
    monkeypatch.setattr("locus.__version__", installed)
    _pypi_says(monkeypatch, latest)
    out = _run(capsys, "--check-update")
    assert expect in out
    if expect.startswith("nothing newer") or "yours is" in expect:
        assert "(up to date)" not in out and f"latest     : {latest}" not in out


def test_check_update_failure_says_could_not_check(world, capsys, monkeypatch):
    def fail(*a, **k):
        raise OSError("offline")

    monkeypatch.setattr(urllib.request, "urlopen", fail)
    assert "latest     : could not check" in _run(capsys, "--check-update")


def test_json_output(world, capsys, monkeypatch):
    _no_network(monkeypatch)
    data = json.loads(_run(capsys, "--json"))
    assert data["index"]["files"] == 6 and data["paused"] is None and "latest_version" not in data
