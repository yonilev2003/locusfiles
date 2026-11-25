"""The tool list is exactly what the server registers: four tools, whether or not a Locus account key is set.

Decided 2026-11-21 (L6, "One MCP, four tools"): `locus_diagnose_connection` and `locus_account_status` are folded into
`locus_help(check=true)`; the server instructions, the README and these tests list exactly the tools registered, and
the instructions never send the model to a tool that is not there.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import replace
from pathlib import Path

import pytest

from locus import mcp_server as srv
from locus import support
from locus.config import get_settings

ROOT = Path(__file__).resolve().parents[1]
FILE_TOOLS = {"locus_search", "locus_list_sources", "locus_read_file"}
DEFAULT_TOOLS = FILE_TOOLS | {"locus_help"}
KEY = "locus_live_TESTkey0123456789abcdefghijklmnop"


def _tools() -> dict:
    return {t.name: t for t in asyncio.run(srv.mcp.list_tools())}


def _with_key(on: bool):
    base = get_settings()
    return replace(base, managed_api_key=KEY if on else None)


@pytest.fixture(autouse=True)
def _default_instructions_afterwards(monkeypatch):
    monkeypatch.delenv("LOCUS_ANNOUNCE", raising=False)
    srv.apply_tool_set()
    yield
    srv.apply_tool_set()


@pytest.mark.parametrize("with_key", [False, True])
def test_the_server_registers_four_tools_with_or_without_a_key(monkeypatch, with_key):
    monkeypatch.setattr(srv, "get_settings", lambda: _with_key(with_key))
    srv.apply_tool_set()
    assert set(_tools()) == DEFAULT_TOOLS


@pytest.mark.parametrize("old", ["locus_diagnose_connection", "locus_account_status"])
def test_the_two_folded_tools_are_gone(old):
    assert old not in _tools()
    assert old not in (srv.mcp.instructions or "")


def test_locus_help_says_it_can_reach_the_network_and_stays_read_only():
    tool = _tools()["locus_help"]
    annotations = tool.annotations
    assert annotations.read_only_hint is True and annotations.destructive_hint is False
    assert annotations.open_world_hint is True
    description = " ".join((tool.description or "").split())  # the docstring wraps over lines
    assert "check=true" in description and "never contains API key values, file contents or file paths" in description
    properties = tool.input_schema["properties"]
    assert {"query", "max_articles", "check", "check_remote"} <= set(properties)
    assert tool.input_schema.get("required", []) == []  # check=true needs no question


def test_the_instructions_name_only_tools_that_exist():
    named = set(re.findall(r"\blocus_[a-z_]+\b", srv.mcp.instructions or ""))
    assert named <= set(_tools()), named - set(_tools())
    assert FILE_TOOLS | {"locus_help"} <= named


def test_the_instructions_send_the_model_to_the_check_only_for_the_users_own_account():
    text = srv.mcp.instructions or ""
    assert "locus_help with check=true first" in text
    assert "Only when the user asks about their own Locus account" in text
    assert "Do not use check for general questions about Locus" in text
    # plans and trials are no longer a topic the help articles are sent to
    for word in ("plans, trial", "plan, trial end"):
        assert word not in text


def test_the_instructions_stay_inside_the_budget_with_the_announce_paragraph(monkeypatch):
    monkeypatch.setenv("LOCUS_ANNOUNCE", "1")
    srv.apply_tool_set()
    text = srv.mcp.instructions or ""
    assert text.endswith(srv.ANNOUNCE_INSTRUCTIONS) and len(text) < 4096


def test_every_tool_description_stays_inside_the_2048_character_budget_Claude_Code_keeps():
    for name, tool in _tools().items():
        assert len(tool.description or "") <= 2048, (name, len(tool.description or ""))


def test_main_serves_four_tools_with_a_key(monkeypatch):
    monkeypatch.setenv("LOCUS_API_KEY", KEY)
    get_settings.cache_clear()
    seen: list[set[str]] = []
    monkeypatch.setattr(srv, "start_warmup", lambda: None)
    monkeypatch.setattr(srv.mcp, "run", lambda *a, **k: seen.append(set(_tools())))
    try:
        srv.main("stdio")
    finally:
        monkeypatch.delenv("LOCUS_API_KEY")
        get_settings.cache_clear()
    assert seen == [DEFAULT_TOOLS]


def test_main_without_a_key_serves_four_tools(monkeypatch):
    monkeypatch.delenv("LOCUS_API_KEY", raising=False)
    get_settings.cache_clear()
    seen: list[set[str]] = []
    monkeypatch.setattr(srv, "start_warmup", lambda: None)
    monkeypatch.setattr(srv.mcp, "run", lambda *a, **k: seen.append(set(_tools())))
    srv.main("stdio")
    assert seen == [DEFAULT_TOOLS]


def test_the_readme_lists_exactly_the_tools_the_server_can_register():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    paragraph = readme.split("The three file tools are", 1)[1].split("\n\n", 1)[0]
    named = set(re.findall(r"`(locus_[a-z_]+)`", paragraph))
    assert named == DEFAULT_TOOLS
    assert "only when `LOCUS_API_KEY` is set" in paragraph


# --- locus_help(check=true): the old diagnose report, plus the account status when a key is set ----------------------


def test_check_returns_the_diagnostics_report_and_no_account_call_without_a_key(monkeypatch):
    monkeypatch.setattr(srv, "get_settings", lambda: _with_key(False))

    def no_network(*a, **k):
        raise AssertionError("the account endpoint must not be called without a key")

    monkeypatch.setattr(support, "fetch_account_status", no_network)
    report = asyncio.run(srv.locus_help(check=True))
    assert report.startswith("# Locus diagnostics") and "managed API key configured: no" in report
    assert "# Locus account status" not in report and "No Locus account configured" not in report
    assert "Reachability" not in report


def test_check_adds_the_account_status_when_a_key_is_set_and_never_prints_the_key(monkeypatch):
    monkeypatch.setattr(srv, "get_settings", lambda: _with_key(True))
    monkeypatch.setattr(
        support, "fetch_account_status", lambda *a, **k: {"tier": "personal", "status": "active", "used_today": 5}
    )
    report = asyncio.run(srv.locus_help(check=True))
    assert report.startswith("# Locus diagnostics")
    assert "# Locus account status" in report and "Plan: personal (active)" in report
    assert KEY not in report
    assert "managed API key configured: yes" in report


def test_check_remote_adds_the_reachability_section(monkeypatch):
    monkeypatch.setattr(srv, "get_settings", lambda: _with_key(False))
    monkeypatch.setattr(srv, "_check_reachability", lambda url: "reachable (1ms)")
    report = asyncio.run(srv.locus_help(check=True, check_remote=True))
    assert "## Remote reachability (check_remote=true)" in report and "reachable (1ms)" in report


def test_without_check_the_help_answers_a_question_and_an_empty_query_is_told_what_to_give():
    answered = asyncio.run(srv.locus_help(query="how do I install Locus"))
    assert "# Locus diagnostics" not in answered and len(answered) > 300
    assert "check=true" in asyncio.run(srv.locus_help())


# --- the account text no longer fails on a field the server left out ---------------------------------------------


@pytest.mark.parametrize(
    "answer",
    [{}, {"status": "past_due"}, {"tier": "personal", "status": "canceled", "used_today": 5}, {"used_today": None}],
)
def test_the_account_text_survives_missing_fields(monkeypatch, answer):
    monkeypatch.setattr(srv, "get_settings", lambda: _with_key(True))
    monkeypatch.setattr(support, "fetch_account_status", lambda *a, **k: dict(answer))
    text = srv._account_status_text()
    assert text.startswith("# Locus account status") and "KeyError" not in text
    assert "unavailable" in text  # something was missing in every one of these answers
