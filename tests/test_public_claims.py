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
    assert f"@v{__version__}" in text  # the tag exists since the release; the pin names it


def test_readme_tells_how_to_erase_the_index_and_that_it_is_readable_text():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    section = _section(text, "Your data on disk, and how to erase it")
    assert "readable text" in section and "rm -rf ~/.locus" in section
    assert "does not encrypt it" in section
    assert "secure wipe" in section  # what deleting does not do
    assert "~/.cache/locus/onnx" in section  # the model cache is not inside the data folder
    kb = (ROOT / "src" / "locus" / "support_kb" / "delete-account-or-data.md").read_text(encoding="utf-8")
    assert "~/.cache/locus/onnx" in kb


# --- decided 2026-11-14: hosted modes and Notion are off; plan, trial and refund pages are not public ---

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


def test_the_readme_and_security_policy_state_the_hard_link_rule_and_its_limits():
    readme = " ".join((ROOT / "README.md").read_text(encoding="utf-8").split())
    security = " ".join((ROOT / "SECURITY.md").read_text(encoding="utf-8").split())
    assert "more than one hard link" in readme and "LOCUS_ALLOW_HARD_LINKS=1" in readme
    assert "does not report link counts" in readme and "both are skipped" in readme
    assert "named pipe" in readme
    assert "more than one hard link" in security and "LOCUS_ALLOW_HARD_LINKS=1" in security


# --- decided 2026-11-17 after the claims audit: what the first lines, numbers and help articles may say ---


def test_the_first_lines_promise_search_results_that_name_a_file_not_answers_or_every_client():
    import tomllib

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    pypi = (ROOT / "docs" / "package" / "README.md").read_text(encoding="utf-8")
    description = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["description"]
    for text in (readme, pypi, description):
        assert "Every answer names" not in text and "MCP clients you already use" not in text
        assert "such as Claude Desktop, Claude Code and Cursor" not in text
    assert "Search results name the file they came from." in readme
    assert "Search results name the file they came from." in pypi


def test_the_readme_gives_the_chunk_size_a_new_default_collection_gets():
    from locus.ingest.chunk_policy import LEGACY_CHUNK_TOKENS, WINDOW_AWARE_CHUNK_TOKENS

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert f'{WINDOW_AWARE_CHUNK_TOKENS} "tokens"' in readme and f"{WINDOW_AWARE_CHUNK_TOKENS} tokens" in readme
    assert "default 700" not in readme
    assert f"{LEGACY_CHUNK_TOKENS} applies to collections made before 0.1.4" in " ".join(readme.split())


def test_the_api_doc_exit_code_rows_say_what_help_says():
    from locus import cli

    doc = " ".join((ROOT / "docs" / "api.md").read_text(encoding="utf-8").split())
    help_text = " ".join(cli.EXIT_CODES_EPILOG.split())
    for phrase in ("a folder rule closes it", "or Locus is paused", "the confinement check failed"):
        assert phrase in help_text and phrase in doc, phrase
    assert "folder rules" in help_text and "folder rules (`policy.toml`) cannot be read" in doc


def test_no_help_article_sends_an_ordinary_user_to_a_hosted_service_or_a_person_by_telephone():
    from locus import support

    forbidden = re.compile(
        r"your dashboard|Delete your account|telephone|Sunday to Thursday|00:00 UTC|Locus plan", re.I
    )
    for article in support.load_articles():
        assert not forbidden.search(article.body), (article.id, forbidden.search(article.body).group(0))


def test_the_changelog_dates_the_versions_that_are_on_pypi():
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## [0.1.4] - 2026-10-07" in text and "## [0.1.3] - 2026-10-04 (yanked)" in text
    assert "still marked unreleased" not in text
