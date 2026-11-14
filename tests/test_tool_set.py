"""The tool list is exactly what the server registers, and the account tool exists only with a Locus account key.

Founder decision 2026-11-14: `locus_account_status` is registered only when LOCUS_API_KEY is set; the offline help and
diagnose tools stay; the server instructions, the README and these tests list exactly the tools registered, and the
instructions never send the model to a tool that is not there (or to the account tool for general questions).
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
DEFAULT_TOOLS = FILE_TOOLS | {"locus_help", "locus_diagnose_connection"}
ACCOUNT = "locus_account_status"
KEY = "locus_live_TESTkey0123456789abcdefghijklmnop"


def _tools() -> dict:
    return {t.name: t for t in asyncio.run(srv.mcp.list_tools())}


def _with_key(on: bool):
    base = get_settings()
    return replace(base, managed_api_key=KEY if on else None)


@pytest.fixture(autouse=True)
def _default_tool_set_afterwards(monkeypatch):
    monkeypatch.delenv("LOCUS_ANNOUNCE", raising=False)
    srv.apply_tool_set(_with_key(False))
    yield
    srv.apply_tool_set(_with_key(False))


def test_without_a_key_the_server_registers_five_tools_and_no_account_tool():
    assert srv.apply_tool_set(_with_key(False)) is False
    assert set(_tools()) == DEFAULT_TOOLS


def test_with_a_key_the_account_tool_is_added_and_says_it_reaches_the_network():
    assert srv.apply_tool_set(_with_key(True)) is True
    tools = _tools()
    assert set(tools) == DEFAULT_TOOLS | {ACCOUNT}
    annotations = tools[ACCOUNT].annotations
    assert annotations.read_only_hint is True and annotations.destructive_hint is False
    assert annotations.open_world_hint is True
    description = " ".join((tools[ACCOUNT].description or "").split())  # the docstring wraps over lines
    assert "only when the user asks about their own Locus account" in description
    assert "Do not use it for general questions about Locus" in description


def test_removing_the_key_removes_the_tool_and_restores_the_instructions():
    default_text = srv.mcp.instructions
    srv.apply_tool_set(_with_key(True))
    assert srv.mcp.instructions != default_text
    srv.apply_tool_set(_with_key(False))
    assert ACCOUNT not in _tools() and srv.mcp.instructions == default_text


def test_applying_twice_is_harmless():
    srv.apply_tool_set(_with_key(True))
    srv.apply_tool_set(_with_key(True))
    assert sorted(_tools()) == sorted(DEFAULT_TOOLS | {ACCOUNT})


@pytest.mark.parametrize("with_key", [False, True])
def test_the_instructions_name_only_tools_that_exist(with_key):
    srv.apply_tool_set(_with_key(with_key))
    named = set(re.findall(r"\blocus_[a-z_]+\b", srv.mcp.instructions or ""))
    assert named <= set(_tools()), named - set(_tools())
    assert FILE_TOOLS | {"locus_help", "locus_diagnose_connection"} <= named


def test_the_instructions_send_the_model_to_the_account_tool_only_for_the_users_own_account():
    assert ACCOUNT not in (srv.mcp.instructions or "")
    srv.apply_tool_set(_with_key(True))
    text = srv.mcp.instructions or ""
    assert "Only when the user asks about their own Locus account" in text
    assert "Do not call it for general questions about Locus" in text
    # plans and trials are no longer a topic the help tool is sent to
    for word in ("plans, trial", "plan, trial end"):
        assert word not in text


def test_the_instructions_stay_inside_the_budget_with_the_account_tool_and_the_announce_paragraph(monkeypatch):
    monkeypatch.setenv("LOCUS_ANNOUNCE", "1")
    srv.apply_tool_set(_with_key(True))
    text = srv.mcp.instructions or ""
    assert text.endswith(srv.ANNOUNCE_INSTRUCTIONS) and len(text) < 4096


def test_every_tool_description_stays_inside_the_2048_character_budget_Claude_Code_keeps():
    srv.apply_tool_set(_with_key(True))
    for name, tool in _tools().items():
        assert len(tool.description or "") <= 2048, (name, len(tool.description or ""))


def test_main_registers_the_account_tool_before_it_serves(monkeypatch):
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
    assert seen == [DEFAULT_TOOLS | {ACCOUNT}]


def test_main_without_a_key_serves_five_tools(monkeypatch):
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
    assert named == DEFAULT_TOOLS | {ACCOUNT}
    assert "only when `LOCUS_API_KEY` is set" in paragraph


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
