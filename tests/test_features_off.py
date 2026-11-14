"""Hosted modes and Notion sync are off in 0.2.0 unless switched on explicitly (founder decision 2026-11-14).

LOCUS_ENABLE_HOSTED=1 is needed for managed embeddings, `locus setup --managed` and `locus serve --transport relay`;
LOCUS_ENABLE_NOTION=1 for `locus sync-notion`. A key or a credential that happens to be present does not switch
anything on. The refusal is exit code 2 with a message that names the switch and what the feature sends or writes.
"""

from __future__ import annotations

import os
from dataclasses import replace

import pytest

from locus import cli, config, configure, mcp_server
from locus.config import get_settings
from locus.ingest.embedder import ManagedEmbedder, get_embedder

KEY = "locus_live_TESTkey0123456789abcdefghijklmnop"


def _exit(argv: list[str]) -> int:
    """The exit code of `locus <argv>` (cli.main always ends in sys.exit)."""
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    return int(exc.value.code or 0)


@pytest.fixture(autouse=True)
def _clean_env(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith("LOCUS_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("LOCUS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(config, "_SOURCE_CHECKOUT", None)
    work = tmp_path / "proj"
    work.mkdir()
    monkeypatch.chdir(work)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " 1 "])
def test_the_switch_accepts_the_usual_spellings(monkeypatch, value):
    monkeypatch.setenv("LOCUS_ENABLE_HOSTED", value)
    assert config.feature_enabled("LOCUS_ENABLE_HOSTED")


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "2", "enabled"])
def test_anything_else_leaves_it_off(monkeypatch, value):
    monkeypatch.setenv("LOCUS_ENABLE_HOSTED", value)
    assert not config.feature_enabled("LOCUS_ENABLE_HOSTED")


def test_a_key_alone_does_not_switch_the_hosted_modes_on(monkeypatch):
    monkeypatch.setenv("LOCUS_API_KEY", KEY)
    monkeypatch.setenv("LOCUS_EMBEDDER", "managed")
    settings = get_settings()
    with pytest.raises(RuntimeError, match="LOCUS_ENABLE_HOSTED=1"):
        get_embedder(settings)


def test_managed_embeddings_work_once_switched_on(monkeypatch):
    monkeypatch.setenv("LOCUS_API_KEY", KEY)
    monkeypatch.setenv("LOCUS_EMBEDDER", "managed")
    monkeypatch.setenv("LOCUS_ENABLE_HOSTED", "1")
    assert isinstance(get_embedder(get_settings()), ManagedEmbedder)


def test_the_default_embedder_is_local_and_needs_no_switch():
    assert get_settings().embedder == "local"


def test_the_hosted_message_says_what_would_leave_the_computer():
    with pytest.raises(RuntimeError) as err:
        config.require_hosted("LOCUS_EMBEDDER=managed")
    text = str(err.value)
    assert "off in this version" in text and "LOCUS_ENABLE_HOSTED=1" in text and "sent to Locus's servers" in text


def test_setup_managed_is_refused_and_writes_nothing(tmp_path, capsys):
    assert _exit(["setup", "--managed"]) == 2
    err = capsys.readouterr().err
    assert "nothing was saved" in err and "LOCUS_ENABLE_HOSTED=1" in err
    assert not (tmp_path / "proj" / ".env").exists()


def test_setup_local_needs_no_switch(tmp_path):
    assert _exit(["setup", "--local", "--skip-check"]) == 0
    assert "LOCUS_EMBEDDER=local" in (tmp_path / "proj" / ".env").read_text()


def test_serve_relay_is_refused_before_anything_starts(monkeypatch, capsys):
    started = []
    monkeypatch.setattr(mcp_server, "_run_relay", lambda *a, **k: started.append(1))
    monkeypatch.setenv("LOCUS_API_KEY", KEY)  # a key does not switch it on
    assert _exit(["serve", "--transport", "relay"]) == 2
    assert "LOCUS_ENABLE_HOSTED=1" in capsys.readouterr().err and started == []


def test_the_server_entry_point_refuses_relay_too(monkeypatch):
    monkeypatch.setattr(mcp_server, "_run_relay", lambda *a, **k: pytest.fail("the relay worker started"))
    with pytest.raises(RuntimeError, match="LOCUS_ENABLE_HOSTED=1"):
        mcp_server.main("relay")


def test_serve_relay_goes_on_when_switched_on(monkeypatch):
    monkeypatch.setenv("LOCUS_ENABLE_HOSTED", "1")
    monkeypatch.setenv("LOCUS_API_KEY", KEY)
    reached = []
    monkeypatch.setattr(mcp_server, "_run_relay", lambda settings: reached.append(settings.managed_api_key))
    monkeypatch.setattr(mcp_server, "start_warmup", lambda: None)
    try:
        assert _exit(["serve", "--transport", "relay"]) == 0
    finally:
        mcp_server.apply_tool_set(replace(get_settings(), managed_api_key=None))
    assert reached == [KEY]


def test_sync_notion_is_refused_and_says_where_the_pages_would_go(monkeypatch, capsys):
    import locus.ingest.notion_source as notion

    monkeypatch.setattr(notion, "sync_notion", lambda *a, **k: pytest.fail("Notion was contacted"))
    monkeypatch.setenv("NOTION_CLIENT_ID", "x")  # credentials alone do not switch it on
    assert _exit(["sync-notion"]) == 2
    err = capsys.readouterr().err
    assert "plaintext Markdown" in err and "sources/notion" in err and "LOCUS_ENABLE_NOTION=1" in err


def test_sync_notion_is_reached_when_switched_on(monkeypatch):
    import locus.ingest.notion_source as notion

    monkeypatch.setenv("LOCUS_ENABLE_NOTION", "1")

    class Reached(Exception):
        pass

    def stop(*_a, **_k):
        raise Reached

    monkeypatch.setattr(notion, "sync_notion", stop)
    with pytest.raises(Reached):
        cli.cmd_sync_notion(cli.build_parser().parse_args(["sync-notion"]))


def test_configure_run_setup_managed_is_refused_when_off():
    result = configure.run_setup("managed", skip_check=True)
    assert result.code == configure.EXIT_CONFIG
