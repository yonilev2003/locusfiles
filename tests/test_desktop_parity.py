"""Claude Desktop parity: the when-to-search cue leads the search tool's description, setup prints the line to paste
into Claude's profile, and doctor names the Desktop log. Offline."""

from __future__ import annotations

import argparse
import io
import sys

import pytest

from locus import cli, clients
from locus import mcp_server as srv

PROFILE = (
    "When I ask about my own documents, notes, files or projects, search my computer's files with the Locus tools "
    "first, then answer and cite the file paths."
)


def _tool_description() -> str:
    import asyncio

    tools = asyncio.run(srv.mcp.list_tools())
    return next(t.description for t in tools if t.name == "locus_search")


def test_the_first_line_of_the_search_description_carries_the_cue():
    first = _tool_description().strip().splitlines()[0]
    for word in ("my", "our", "we", "projects", "people", "documents", "decisions"):
        assert word in first.lower(), word


def _setup(*extra: str) -> str:
    out = io.StringIO()
    args = cli.build_parser().parse_args(
        ["setup", "--client", "claude-desktop", "--command", "/usr/local/bin/locus", *extra]
    )
    assert clients.run_client_setup(args, out, io.StringIO()) == 0
    return out.getvalue()


def test_setup_for_claude_desktop_prints_the_line_to_paste_into_the_profile():
    text = _setup()
    assert PROFILE in text and "Settings > Profile" in text


def test_the_line_is_not_printed_for_other_clients_or_on_remove():
    out = io.StringIO()
    args = cli.build_parser().parse_args(["setup", "--client", "cursor", "--command", "/usr/local/bin/locus"])
    clients.run_client_setup(args, out, io.StringIO())
    assert PROFILE not in out.getvalue()
    assert PROFILE not in _setup("--remove")


def test_the_line_is_printed_after_a_write(tmp_path, monkeypatch):
    monkeypatch.setattr(clients, "_interactive", lambda: True)
    monkeypatch.setattr(clients, "_ask", lambda _p: "y")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(sys, "platform", sys.platform)
    cmd = tmp_path / "locus"
    cmd.write_text("#!/bin/sh\n")
    cmd.chmod(0o755)
    out = io.StringIO()
    args = cli.build_parser().parse_args(
        ["setup", "--client", "claude-desktop", "--write", "--yes", "--command", str(cmd)]
    )
    code = clients.run_client_setup(args, out, io.StringIO())
    assert code == 0 and PROFILE in out.getvalue()


def test_doctor_names_the_claude_desktop_log_on_macos(monkeypatch, capsys, tmp_path):
    if sys.platform != "darwin":
        pytest.skip("macOS only")
    monkeypatch.setenv("LOCUS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LOCUS_EMBEDDER", "hash")
    monkeypatch.setenv("LOCUS_ALLOW_TEST_EMBEDDER", "1")
    cli.cmd_doctor(argparse.Namespace(remote=None, token_env=None))
    assert "~/Library/Logs/Claude/mcp-server-locus.log" in capsys.readouterr().out
