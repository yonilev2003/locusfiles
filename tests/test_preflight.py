"""The consent screen (UX-005): before a hosted embedder receives any chunk text, `locus index` and `locus watch` show
what would be sent and where, and ask. Offline: nothing is embedded or sent; the screen only walks and estimates."""

from __future__ import annotations

import io
from dataclasses import replace
from pathlib import Path

import pytest

from locus import preflight
from locus.config import DEFAULT_EXTENSIONS, Settings
from locus.ingest.indexer import file_sha256


def _settings(tmp_path: Path, embedder: str = "voyage") -> Settings:
    return Settings(
        data_dir=tmp_path / "data", collection="consent", embedder=embedder, voyage_model="voyage-4-lite",
        openai_model="text-embedding-3-small", local_model="x", chunk_tokens=100, overlap_tokens=10, max_file_mb=5,
        extensions=DEFAULT_EXTENSIONS,
    )  # fmt: skip


@pytest.fixture
def folder(tmp_path):
    root = tmp_path / "notes"
    root.mkdir()
    (root / "a.md").write_text("the zebra release plan " * 40)
    (root / "b.md").write_text("pricing notes " * 40)
    return root


class _Tty(io.StringIO):
    def __init__(self, answer: str = "", tty: bool = True) -> None:
        super().__init__(answer)
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def test_only_the_hosted_embedders_need_consent(tmp_path):
    for name, needs in (("local", False), ("hash", False), ("voyage", True), ("openai", True), ("managed", True)):
        assert preflight.needs_consent(_settings(tmp_path, name)) is needs, name


def test_the_data_flow_sentence_matches_the_mode(tmp_path):
    assert preflight.data_flow_sentence(_settings(tmp_path, "local")) == preflight.LOCAL
    assert preflight.data_flow_sentence(_settings(tmp_path, "voyage")) == preflight.OWN_KEY
    assert preflight.data_flow_sentence(_settings(tmp_path, "openai")) == preflight.OWN_KEY
    assert preflight.data_flow_sentence(_settings(tmp_path, "managed")) == preflight.MANAGED


def test_no_data_flow_sentence_claims_that_nothing_leaves(tmp_path):
    for sentence in (preflight.LOCAL, preflight.OWN_KEY, preflight.MANAGED, preflight.ALWAYS):
        assert "nothing leaves" not in sentence.lower() and "never leaves" not in sentence.lower()
    assert "excerpts go to that assistant" in preflight.LOCAL  # the local mode says what the assistant receives


def test_the_destination_names_where_the_text_goes(tmp_path):
    assert "Voyage AI, with your own key" in preflight.destination(_settings(tmp_path, "voyage"))
    assert "OpenAI, with your own key" in preflight.destination(_settings(tmp_path, "openai"))
    assert preflight.destination(_settings(tmp_path, "local")).startswith("nowhere")


def test_a_hosted_screen_lists_what_would_be_sent_and_where(folder, tmp_path):
    pre = preflight.build_preflight(folder, _settings(tmp_path), sample_seconds=1)
    text = pre.render()
    assert "LOCUS_EMBEDDER=voyage" in text and "To embed and send: 2 file(s)" in text
    assert "Where the text goes: Voyage AI, with your own key" in text
    assert "Your searches later send each query the same way." in text
    assert preflight.ALWAYS in text and pre.sends_anything


def test_a_local_screen_says_embed_not_send(folder, tmp_path):
    pre = preflight.build_preflight(folder, _settings(tmp_path, "local"), sample_seconds=1)
    text = pre.render()
    assert "To embed: 2 file(s)" in text and "To embed and send" not in text
    assert "Your searches later send each query" not in text
    assert not pre.sends_anything  # nothing to confirm when nothing leaves for embedding


def test_unchanged_files_are_not_counted_as_sent(folder, tmp_path):
    known = {str(p.resolve()): file_sha256(p) for p in folder.iterdir() if p.name == "a.md"}
    pre = preflight.build_preflight(
        folder, _settings(tmp_path), known_hash=lambda path: known.get(path), sample_seconds=1
    )
    assert pre.unchanged == 1 and [e.path.name for e in pre.to_send] == ["b.md"]
    assert "1 unchanged since the last index: skipped, nothing sent for them" in pre.render()


def test_confirm_asks_nothing_when_nothing_would_be_sent(folder, tmp_path):
    pre = preflight.build_preflight(folder, _settings(tmp_path, "local"), sample_seconds=1)
    out = io.StringIO()
    assert preflight.confirm(pre, assume_yes=False, stdin=_Tty("", tty=False), out=out) == preflight.PROCEED
    assert out.getvalue() == ""


def test_yes_confirms_without_a_terminal(folder, tmp_path):
    pre = preflight.build_preflight(folder, _settings(tmp_path), sample_seconds=1)
    out = io.StringIO()
    assert preflight.confirm(pre, assume_yes=True, stdin=_Tty("", tty=False), out=out) == preflight.PROCEED
    assert "Confirmed with --yes." in out.getvalue()


def test_without_a_terminal_and_without_yes_nothing_is_sent_and_the_rerun_command_is_named(folder, tmp_path):
    pre = preflight.build_preflight(folder, _settings(tmp_path), sample_seconds=1)
    out = io.StringIO()
    result = preflight.confirm(
        pre, assume_yes=False, stdin=_Tty("y\n", tty=False), out=out, rerun="locus index notes --yes"
    )
    assert result == preflight.NEEDS_YES
    assert "Not sent" in out.getvalue() and "`locus index notes --yes`" in out.getvalue()


@pytest.mark.parametrize(
    ("answer", "outcome"),
    [
        ("y\n", "proceed"),
        ("YES\n", "proceed"),
        ("n\n", "declined"),
        ("\n", "declined"),
        ("maybe\n", "declined"),
        ("", "declined"),
    ],
)
def test_an_interactive_answer_decides(folder, tmp_path, answer, outcome):
    pre = preflight.build_preflight(folder, _settings(tmp_path), sample_seconds=1)
    out = io.StringIO()
    assert preflight.confirm(pre, assume_yes=False, stdin=_Tty(answer), out=out) == outcome
    assert ("Cancelled. Nothing was sent." in out.getvalue()) == (outcome == "declined")


def test_ctrl_c_at_the_question_declines(folder, tmp_path):
    class Interrupted(_Tty):
        def readline(self, *a):
            raise KeyboardInterrupt

    pre = preflight.build_preflight(folder, _settings(tmp_path), sample_seconds=1)
    assert preflight.confirm(pre, assume_yes=False, stdin=Interrupted(), out=io.StringIO()) == preflight.DECLINED


def test_the_managed_destination_names_the_relay_host(tmp_path):
    managed = replace(_settings(tmp_path, "managed"), managed_relay_url="https://example.test/api/embed-relay")
    assert "example.test" in preflight.destination(managed) and "Voyage AI through Locus's relay" in (
        preflight.destination(managed)
    )
