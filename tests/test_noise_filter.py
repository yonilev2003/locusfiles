"""Base64 noise: payloads in notebooks and markdown never reach the index, and rows written by an older parser
are re-indexed once. Offline, HashEmbedder; payloads are generated, not stored."""

from __future__ import annotations

import base64
import json
import os
import textwrap
from pathlib import Path

import pytest

pytest.importorskip("chromadb")

from locus.config import DEFAULT_EXTENSIONS, Settings  # noqa: E402
from locus.ingest.embedder import HashEmbedder  # noqa: E402
from locus.ingest.indexer import IndexStats, index_file  # noqa: E402
from locus.ingest.noise import PARSER_VERSION, REMOVED, drop_base64_lines, strip_base64  # noqa: E402
from locus.ingest.parsers import parse_file  # noqa: E402
from locus.ingest.store import ChromaStore  # noqa: E402

PAYLOAD = base64.b64encode(os.urandom(3000)).decode()  # about 4000 characters, one line
WRAPPED = textwrap.fill(PAYLOAD, 76)  # the same payload wrapped like MIME: no line is 200 characters


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data", collection="noise", embedder="hash", voyage_model="", openai_model="",
        local_model="", chunk_tokens=400, overlap_tokens=50, max_file_mb=5, extensions=DEFAULT_EXTENSIONS,
    )  # fmt: skip


def _store(tmp_path: Path) -> tuple[ChromaStore, HashEmbedder, Settings]:
    settings, embedder = _settings(tmp_path), HashEmbedder()
    return ChromaStore(settings.data_dir, settings.collection, embedder.name), embedder, settings


def _notebook(path: Path) -> Path:
    cells = [
        {
            "cell_type": "markdown",
            "source": [
                f"# Quarterly churn model\n\nThe attached chart: ![chart](attachment:c.png)\n\n"
                f"data:image/png;base64,{PAYLOAD}\n"
            ],
        },
        {"cell_type": "code", "source": [f'image = "{PAYLOAD}"\nprint("churn drivers are pricing and onboarding")']},
    ]
    path.write_text(json.dumps({"cells": cells, "metadata": {}, "nbformat": 4}), encoding="utf-8")
    return path


def test_strip_base64_removes_data_uri_and_long_run_but_keeps_prose():
    text = f"Intro line.\n![x](data:image/png;base64,{PAYLOAD})\nkey = '{PAYLOAD}'\nOutro line."
    out = strip_base64(text)
    assert PAYLOAD[:50] not in out
    assert out.count(REMOVED) == 2
    assert "Intro line." in out and "Outro line." in out


@pytest.mark.parametrize("separator", ["=" * 300, "/" * 300, "a" * 300, "-" * 300])
def test_strip_base64_keeps_separators_and_repeated_characters(separator: str):
    assert strip_base64(f"head\n{separator}\ntail") == f"head\n{separator}\ntail"


def test_drop_base64_lines_removes_wrapped_payload_and_keeps_the_prose_around_it():
    assert drop_base64_lines(WRAPPED).strip() == ""  # a chunk of nothing but payload is empty afterwards
    prose = "We decided on three pricing tiers and a yearly discount."
    mixed = f"Intro paragraph about the launch plan.\n\n{WRAPPED}\n\n{prose}"
    out = drop_base64_lines(mixed)
    assert "Intro paragraph about the launch plan." in out and prose in out
    assert not any(line in out for line in WRAPPED.splitlines())
    assert drop_base64_lines(prose * 5) == prose * 5


def test_notebook_text_has_no_payload_in_markdown_or_code_cell(tmp_path: Path):
    text = parse_file(_notebook(tmp_path / "a.ipynb"), strip=True) or ""
    assert PAYLOAD[:50] not in text
    assert "Quarterly churn model" in text and "pricing and onboarding" in text


def test_notebook_index_holds_no_payload_and_is_still_searchable(tmp_path: Path):
    store, embedder, settings = _store(tmp_path)
    nb = _notebook(tmp_path / "a.ipynb")
    stats = IndexStats()
    assert index_file(nb, tmp_path, embedder, store, settings, stats, log=lambda _m: None) > 0
    got = store._col.get(include=["documents", "metadatas"])
    assert got["documents"] and all(PAYLOAD[:50] not in d for d in got["documents"])
    assert any("churn" in d for d in got["documents"])
    assert all(m["parser_version"] == PARSER_VERSION for m in got["metadatas"])


def test_a_chunk_that_mixes_prose_and_wrapped_base64_keeps_the_prose(tmp_path: Path):
    store, embedder, settings = _store(tmp_path)
    note = tmp_path / "n.md"
    note.write_text(
        "# Notes\n\nShort intro about the project.\n\n" + WRAPPED + "\n\nClosing remark about the budget.\n",
        encoding="utf-8",
    )
    index_file(note, tmp_path, embedder, store, settings, IndexStats(), log=lambda _m: None)
    docs = store._col.get(include=["documents"])["documents"]
    joined = "\n".join(docs)
    assert "Short intro" in joined and "Closing remark about the budget" in joined  # the prose survives
    assert not any(line in joined for line in WRAPPED.splitlines())
    got = store._col.get(include=["metadatas"])["metadatas"]
    assert sorted(m["chunk_index"] for m in got) == list(range(len(got)))  # no gaps


def test_a_file_that_is_only_wrapped_base64_is_not_indexed(tmp_path: Path):
    store, embedder, settings = _store(tmp_path)
    note = tmp_path / "blob.md"
    note.write_text("\n\n".join([WRAPPED] * 6), encoding="utf-8")
    index_file(note, tmp_path, embedder, store, settings, IndexStats(), log=lambda _m: None)
    assert store._col.get()["ids"] == []


def _jwt(payload_len: int) -> str:
    letters = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    body = "".join(letters[(i * 7 + 3) % len(letters)] for i in range(payload_len))
    return f"eyJhbGciOiJIUzI1NiJ9.eyJ{body}.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV"


def test_a_jwt_with_a_long_payload_is_still_refused_as_a_credential(tmp_path: Path):
    store, embedder, settings = _store(tmp_path)
    note = tmp_path / "session.md"
    note.write_text(f"# Debug notes\n\nthe failing request carried this token: {_jwt(400)}\n", encoding="utf-8")
    stats = IndexStats()
    messages: list[str] = []
    index_file(note, tmp_path, embedder, store, settings, stats, log=messages.append)
    assert store._col.get()["ids"] == []  # nothing of the file was indexed
    assert stats.denied == 1 and any("credential" in m for m in messages)


def test_the_read_path_checks_the_credential_on_the_unstripped_text(tmp_path: Path):
    note = tmp_path / "session.md"
    note.write_text(f"token: {_jwt(400)}\n", encoding="utf-8")
    from locus.policy import secret_content

    assert secret_content(parse_file(note) or "")  # what a read sees: the file as it is
    assert not secret_content(parse_file(note, strip=True) or "")  # the stripped text hides it: never check that


def test_old_row_is_reindexed_once_even_if_the_file_is_unchanged(tmp_path: Path):
    store, embedder, settings = _store(tmp_path)
    note = tmp_path / "n.md"
    note.write_text("# Plan\n\nPricing decision and launch risks.\n", encoding="utf-8")
    log = lambda _m: None  # noqa: E731
    assert index_file(note, tmp_path, embedder, store, settings, IndexStats(), log=log) > 0
    assert store.parser_version(str(note)) == PARSER_VERSION
    assert index_file(note, tmp_path, embedder, store, settings, IndexStats(), log=log) == 0  # unchanged: skipped

    got = store._col.get(include=["metadatas", "documents", "embeddings"])
    old = [{k: v for k, v in m.items() if k != "parser_version"} for m in got["metadatas"]]
    store._col.delete(ids=got["ids"])  # a pre-stamp row: same chunk, no parser_version key at all
    store._col.upsert(ids=got["ids"], embeddings=got["embeddings"], documents=got["documents"], metadatas=old)
    assert store.parser_version(str(note)) == 1
    assert index_file(note, tmp_path, embedder, store, settings, IndexStats(), log=log) > 0
    assert store.parser_version(str(note)) == PARSER_VERSION
    assert index_file(note, tmp_path, embedder, store, settings, IndexStats(), log=log) == 0


def test_freshness_and_index_roots_redo_a_file_an_older_parser_indexed_without_touching_it(tmp_path: Path):
    from locus.ingest.freshness import freshen
    from locus.policy import Matcher, Policy

    store, embedder, settings = _store(tmp_path)
    root = tmp_path / "notes"
    root.mkdir()
    note = root / "n.md"
    note.write_text("# Plan\n\nPricing decision and launch risks.\n", encoding="utf-8")
    matcher = Matcher(Policy(roots=(str(root),)), tmp_path)
    freshen([root], matcher, embedder, store, settings, budget_s=5.0)
    assert store.parser_version(str(note)) == PARSER_VERSION
    assert str(note) not in store.outdated_paths()

    got = store._col.get(include=["metadatas", "documents", "embeddings"])
    old = [{k: v for k, v in m.items() if k != "parser_version"} for m in got["metadatas"]]
    store._col.delete(ids=got["ids"])
    store._col.upsert(ids=got["ids"], embeddings=got["embeddings"], documents=got["documents"], metadatas=old)
    store = ChromaStore(settings.data_dir, settings.collection, embedder.name)  # a new process: no cached summary
    assert store.parser_version(str(note)) == 1
    assert str(note) in store.outdated_paths()  # an old row: nothing may skip it
    assert store.file_states()[str(note)][1] is None

    freshen([root], matcher, embedder, store, settings, budget_s=5.0)  # the file itself did not change
    assert store.parser_version(str(note)) == PARSER_VERSION
    assert str(note) not in store.outdated_paths()
