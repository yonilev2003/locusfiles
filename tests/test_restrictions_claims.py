"""The help articles about closed folders, hidden results and the kill switch quote what the code says.

Like tests/test_public_claims.py, these do not prove a sentence true; they stop the article and the message from
drifting apart (a renamed message, a changed command, a different block).
"""

from __future__ import annotations

from pathlib import Path

from locus import closed_folders, killswitch, restrictions
from locus.config import get_settings
from locus.policy import parse_policy

KB = Path(__file__).resolve().parents[1] / "src" / "locus" / "support_kb"


def test_no_search_results_quotes_the_messages_the_tools_give():
    article = (KB / "no-search-results.md").read_text(encoding="utf-8")
    assert "hidden by your folder rules" in article
    assert "hidden by your folder rules" in restrictions.HIDDEN_RESULTS
    assert "hidden by your folder rules" in restrictions.HIDDEN_FILES
    assert "is in a closed folder" in article and "closed folder" in restrictions._RULE_WORDS["closed_folder"]
    assert "Locus is paused by the user" in article and killswitch.PAUSED_SENTENCE.startswith(
        "Locus is paused by the user"
    )
    assert "Locus cannot read its folder rules (policy.toml)" in article
    assert restrictions.policy_problem_text("x").startswith("Locus cannot read its folder rules (policy.toml)")
    assert "locus resume" in article and "locus resume" in killswitch.PAUSED_SENTENCE


def test_the_index_article_names_the_command_and_the_block_the_command_line_writes():
    article = (KB / "index-a-folder.md").read_text(encoding="utf-8")
    assert "`locus index ~/Downloads`" in article and 'path = "~/Downloads"' in article
    assert 'profile = "untrusted"' in article
    block = closed_folders.opt_in_block("~/Downloads", "note")
    assert 'path = "~/Downloads"' in block and f'profile = "{closed_folders.OPEN_PROFILE}"' in block
    assert parse_policy(block).folders[0].path == "~/Downloads"  # the block the article describes is a valid policy


def test_the_run_summary_hint_gives_the_command_the_article_gives(tmp_path):
    home = tmp_path / "home"
    (home / "Downloads").mkdir(parents=True)
    (home / "Downloads" / "a.md").write_text("a", encoding="utf-8")
    settings = get_settings()
    lines = closed_folders.hint_lines(home, settings, parse_policy(""), home)
    assert lines == ["1 file(s) in ~/Downloads are closed: run `locus index ~/Downloads` to open (it asks first)."]
