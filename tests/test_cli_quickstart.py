"""`locus quickstart FOLDER`: doctor, estimate, index with progress, the client entry (printed, or written after
consent with a backup), one sample query, the time of each step. Offline: HashEmbedder, a temp data dir and a temp HOME;
no real client config and no network."""

from __future__ import annotations

import json
import sys

import pytest

pytest.importorskip("chromadb")

from locus import cli, clients  # noqa: E402
from locus.config import get_settings  # noqa: E402

from .demo_index import VAULT  # noqa: E402


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("LOCUS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LOCUS_EMBEDDER", "hash")
    monkeypatch.setenv("LOCUS_COLLECTION", "quickstart")
    monkeypatch.setattr(clients, "detect_command", lambda: sys.executable)
    get_settings.cache_clear()
    return tmp_path / "home"


def run(capsys, *argv):
    capsys.readouterr()
    with pytest.raises(SystemExit) as stop:
        cli.main(["quickstart", str(VAULT), *argv])
    out = capsys.readouterr()
    return stop.value.code, out.out, out.err


def test_a_first_run_prints_every_stage_a_cited_hit_and_the_times(home, capsys):
    code, out, _ = run(capsys)
    assert code == 0, out
    for step in ("1/5 doctor", "2/5 estimate", "3/5 index", "4/5 connect claude-code", "5/5 sample query"):
        assert step in out
    assert "files, about" in out and "seen=21 indexed=21" in out  # progress with an ETA, then the summary
    assert str(VAULT) in out and " #" in out  # a hit carries a path and a chunk number
    assert "Time per stage:" in out and all(f"  {n}" in out for n in ("doctor", "estimate", "index", "query", "total"))
    assert "claude mcp add" in out  # printed, not run
    assert list(home.iterdir()) == []  # nothing was written to the (temp) home


def test_write_asks_first_and_backs_up_the_existing_config(home, capsys, monkeypatch):
    config = home / ".cursor" / "mcp.json"
    config.parent.mkdir()
    config.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
    monkeypatch.setattr(clients, "_interactive", lambda: True)
    monkeypatch.setattr(clients, "_ask", lambda _prompt: "n")
    code, out, _ = run(capsys, "--client", "cursor", "--write")
    assert code == 0 and "Nothing was written." in out
    assert json.loads(config.read_text()) == {"mcpServers": {"other": {"command": "x"}}}

    monkeypatch.setattr(clients, "_ask", lambda _prompt: "y")
    code, out, _ = run(capsys, "--client", "cursor", "--write")
    assert code == 0 and "Backup of the file as it was" in out
    entries = json.loads(config.read_text())["mcpServers"]
    assert set(entries) == {"other", "locus"}
    assert len(list(config.parent.glob("mcp.json.locus-backup-*"))) == 1


def test_write_without_a_terminal_and_without_yes_writes_nothing_but_the_index_is_built(home, capsys):
    config = home / ".cursor" / "mcp.json"
    code, out, err = run(capsys, "--client", "cursor", "--write")
    assert code == 0 and not config.exists()
    assert "there is no terminal to ask on" in err and "seen=21 indexed=21" in out


def test_a_missing_folder_stops_with_the_stage_and_the_rerun_command(home, capsys, tmp_path):
    capsys.readouterr()
    with pytest.raises(SystemExit) as stop:
        cli.main(["quickstart", str(tmp_path / "nope")])
    assert stop.value.code != 0
