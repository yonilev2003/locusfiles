"""S-06, S-17: what the public texts may not say, and what they must say, about data and installs.

The README, the PyPI page, SECURITY.md and the bundled help articles are what a stranger reads before running
Locus on their files. These tests do not prove the sentences true (the code review does that); they stop the known
overstatements from coming back and keep the per-mode statement and the pinned installs in place.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from locus import __version__, mcp_server

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_TEXTS = [
    ROOT / "README.md",
    ROOT / "SECURITY.md",
    ROOT / "docs" / "package" / "README.md",
    *sorted((ROOT / "src" / "locus" / "support_kb").glob("*.md")),
]

# Overstatements: "nothing leaves" is false for the complete workflow (the assistant receives excerpts), and the
# local embedder does not contact Hugging Face every time it loads (it asks for the downloaded copy first).
FORBIDDEN = [
    re.compile(r"nothing (ever )?leaves", re.I),
    re.compile(r"never leaves your (computer|machine)", re.I),
    re.compile(r"100\s?% (private|local)", re.I),
    re.compile(r"each time the engine loads its model", re.I),
    re.compile(r"completely (private|offline)", re.I),
]


@pytest.mark.parametrize("path", PUBLIC_TEXTS, ids=lambda p: p.name)
def test_no_public_text_claims_that_nothing_leaves(path):
    text = path.read_text(encoding="utf-8")
    for pattern in FORBIDDEN:
        assert not pattern.search(text), f"{path.name}: {pattern.pattern}"


def test_server_instructions_make_no_such_claim_either():
    for pattern in FORBIDDEN:
        assert not pattern.search(mcp_server.mcp.instructions or "")


def _section(text: str, heading: str) -> str:
    start = text.index(f"## {heading}")
    end = text.find("\n## ", start + 1)
    return text[start : end if end != -1 else len(text)]


def test_readme_says_what_leaves_per_mode_and_when_it_was_checked():
    section = _section((ROOT / "README.md").read_text(encoding="utf-8"), "What leaves your computer")
    for mode in (
        "**Local",
        "**Your own provider key",
        "**Managed embeddings",
        "**Remote connection",
        "**Your own tunnel",
        "**Notion sync",
        "**Account status",
        "**In every mode",
    ):
        assert mode in section, mode
    assert re.search(r"Checked against the code of version \d+\.\d+\.\d+ on \d{4}-\d{2}-\d{2}", section)
    assert "cannot check" in section or "cannot be checked" in section  # server-side claims are marked as such


def test_the_help_article_matches_the_readme_modes():
    article = (ROOT / "src" / "locus" / "support_kb" / "what-leaves-your-computer.md").read_text(encoding="utf-8")
    modes = ("**Local:**", "**Your own key:**", "**Managed (off by default", "**Remote (off by default")
    for mode in modes + ("**Notion (off by default", "**Account status:**"):
        assert mode in article, mode
    assert re.search(r"updated: \d{4}-\d{2}-\d{2}", article)


def test_readme_installs_from_the_repository_are_pinned_to_the_release_tag():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    found = re.findall(r"git\+https://github\.com/[^\s\"@]+(?:@([^\s\"]+))?", text)
    assert found, "the README no longer shows a repository install"
    assert set(found) == {f"v{__version__}"}, found
    assert "tag is created when" in text  # and it says the tag does not exist before the release


def test_readme_tells_how_to_erase_the_index_and_that_it_is_readable_text():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    section = _section(text, "Your data on disk, and how to erase it")
    assert "readable text" in section and "rm -rf ~/.locus" in section
    assert "does not encrypt it" in section
    assert "secure wipe" in section  # what deleting does not do


# --- founder decision 2026-11-14: hosted modes and Notion are off; plan, trial and refund pages are not public ---

REMOVED_PAGES = ("plans-and-limits", "free-trial", "cancel-and-refunds", "api-keys", "teams-and-organizations")


def test_the_hosted_account_pages_are_not_in_the_public_help():
    from locus import support

    ids = {article.id for article in support.load_articles()}
    assert not ids & set(REMOVED_PAGES)
    for name in REMOVED_PAGES:
        assert not (ROOT / "src" / "locus" / "support_kb" / f"{name}.md").exists()


def test_no_public_text_quotes_a_plan_name_a_price_or_a_trial_budget():
    pricing = re.compile(r"\b(Starter|Personal plan|Power plan)\b|US\$|5,000,000|5M-token", re.I)
    dollars = re.compile(r"\$\s?\d")  # not in the README: its demo company has made-up prices
    for path in PUBLIC_TEXTS:
        text = path.read_text(encoding="utf-8")
        assert not pricing.search(text), f"{path.name}: {pricing.search(text).group(0)}"
        if path.name not in ("README.md", "SECURITY.md"):
            assert not dollars.search(text), f"{path.name}: {dollars.search(text).group(0)}"
    for name in REMOVED_PAGES:  # and nothing points a reader to the removed pages
        for path in PUBLIC_TEXTS:
            assert name not in path.read_text(encoding="utf-8"), (path.name, name)


def test_the_help_tool_does_not_answer_an_account_question_from_a_removed_page():
    from locus import support

    questions = (
        "how much does a plan cost",
        "when does my free trial end",
        "how do I get a refund",
        "how do I create an API key",
        "how do I invite a team member to my organization",
    )
    for question in questions:
        ids = [article.id for article, _score in support.help_index().search(question, limit=5)]
        assert not set(ids) & set(REMOVED_PAGES), (question, ids)


def test_the_readme_and_the_pypi_page_say_hosted_modes_and_notion_are_off_by_default():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    pypi = (ROOT / "docs" / "package" / "README.md").read_text(encoding="utf-8")
    for text in (readme, pypi):
        assert "LOCUS_ENABLE_HOSTED=1" in text and "LOCUS_ENABLE_NOTION=1" in text and "plaintext Markdown" in text
    assert 'locusfiles[relay]"' not in pypi  # the PyPI page no longer recommends the relay extra "for a Locus plan"
    assert "A key alone does not switch anything on" in readme


def test_the_notion_copy_is_disclosed_in_every_place_that_says_where_data_sits():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "plaintext Markdown files into `~/.locus/sources/notion/`" in readme
    assert "plaintext Markdown" in _section(readme, "How it works")
    assert "plaintext Markdown" in _section(readme, "Your data on disk, and how to erase it")
    kb = (ROOT / "src" / "locus" / "support_kb" / "what-leaves-your-computer.md").read_text(encoding="utf-8")
    assert "plaintext Markdown files into `~/.locus/sources/notion/`" in kb
