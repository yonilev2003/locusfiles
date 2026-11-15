"""Multi-client groundwork (conformance checklist, "build once, reuse for every client"): what a
client reads first must stand alone, and every tool must stay read-only so Codex and Cursor can
auto-approve them. The verbatim and 2,048/512 budgets are pinned in test_mcp_guidance_sync.py;
this adds the stand-alone and 4,096 ceilings and the read-only annotation on every tool. The account tool exists
only when a Locus account key is set (tests/test_tool_set.py), so the default set is five tools."""

from __future__ import annotations

import asyncio
import re

from locus import mcp_server

LEAD_CHARS = 512  # clients that read only the start of the instructions (OpenAI guide)
TOTAL_CEILING = 4096
EXPECTED_TOOLS = {
    "locus_search",
    "locus_list_sources",
    "locus_read_file",
    "locus_diagnose_connection",
    "locus_help",
}
# Words that only make sense with text the reader may not have: a stand-alone lead uses none of them.
DANGLING = re.compile(
    r"\b(above|below|as described|as mentioned|see (?:the |section )|the following|previously)\b", re.I
)


def _instructions() -> str:
    return mcp_server.mcp.instructions or ""


def test_first_512_characters_state_what_locus_is_and_when_to_use_it():
    lead = _instructions()[:LEAD_CHARS]
    assert "Locus searches the user's own files" in lead  # what it is
    assert "Use locus_search when a question could be answered from that material" in lead  # when to use it
    assert "cite the file paths" in lead  # how to answer
    # The trigger sentence measured to raise how often assistants call the tool.
    assert "it says my, our or we" in lead and "names one of their projects, clients, people or documents" in lead


def test_first_512_characters_end_on_a_complete_paragraph_with_no_dangling_reference():
    instructions = _instructions()
    first_paragraph = instructions.split("\n\n")[0]
    # A cut at 512 loses nothing from the scope paragraph, and it closes with a full stop.
    assert len(first_paragraph) <= LEAD_CHARS
    assert instructions[:LEAD_CHARS].startswith(first_paragraph)
    assert first_paragraph.rstrip().endswith(".")
    assert not DANGLING.search(first_paragraph), DANGLING.search(first_paragraph)


def test_total_instructions_stay_under_4096_characters():
    assert 0 < len(_instructions()) < TOTAL_CEILING


def test_all_default_tools_keep_read_only_hint_true():
    tools = {t.name: t for t in asyncio.run(mcp_server.mcp.list_tools())}
    assert set(tools) == EXPECTED_TOOLS
    for name, tool in tools.items():
        assert tool.annotations is not None, name
        assert tool.annotations.read_only_hint is True, name
