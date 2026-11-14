"""What a user is told when the relay refuses this computer's channel: their own next steps, never the operator's
configuration (the Supabase JWT secret, row-level-security policies, dashboard settings). The relay is off in 0.2.0
unless LOCUS_ENABLE_HOSTED=1; this message is the only place its failure was worded for an operator."""

from __future__ import annotations

import re

from locus import mcp_server

OPERATOR_WORDS = re.compile(
    r"SUPABASE|JWT|\bRLS\b|row[- ]level|Realtime Authorization|Allow public access|dashboard", re.IGNORECASE
)


def test_the_refusal_text_names_what_the_user_can_check_and_nothing_of_the_operators_setup():
    text = mcp_server._channel_refused_text(Exception("channel closed"))
    assert text.startswith("locus_mcp: the relay refused this computer's channel: ")
    assert "LOCUS_API_KEY" in text and "active" in text and "Restarting won't fix this" in text
    assert not OPERATOR_WORDS.search(text), OPERATOR_WORDS.search(text).group(0)


def test_the_detail_from_the_relay_is_shown_but_cut_to_a_short_printable_line():
    text = mcp_server._channel_refused_text("x" * 2000 + "\x1b[31m")
    assert "x" * 300 in text and "x" * 301 not in text and "\x1b" not in text


def test_no_user_facing_text_in_the_engine_still_mentions_the_operators_secret_or_policies():
    from pathlib import Path

    src = Path(mcp_server.__file__).parent
    for path in src.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "SUPABASE_JWT_SECRET" not in text, path.name
        assert "Realtime Authorization" not in text and "Allow public access" not in text, path.name
