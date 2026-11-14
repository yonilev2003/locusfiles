"""The demo vault the README runs (`locus index tests/fixtures/demo-vault`) matches its answer key.

The vault is fictional sample data (Nimbrel Studio and its product Quillwend do not exist). The key sits outside the
vault on purpose: a key inside it would be indexed and would hand the assistant the answers. These tests check every
fact in the key against the files, that the README's demo question finds its files, and that the folder indexes clean.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

pytest.importorskip("chromadb")

from locus.config import DEFAULT_EXTENSIONS, Settings  # noqa: E402
from locus.ingest.embedder import HashEmbedder  # noqa: E402
from locus.ingest.indexer import index_folder  # noqa: E402
from locus.ingest.parsers import parse_file  # noqa: E402
from locus.ingest.store import ChromaStore  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
VAULT = FIXTURES / "demo-vault"
KEY = json.loads((FIXTURES / "demo-vault-key.json").read_text(encoding="utf-8"))
README = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")


def _files() -> list[Path]:
    return sorted(p for p in VAULT.rglob("*") if p.is_file())


def _text(rel: str) -> str:
    return parse_file(VAULT / rel) or ""


def test_the_vault_has_the_number_of_files_the_key_and_the_readme_say():
    assert len(_files()) == KEY["file_count"] == 21
    assert "seen=21 indexed=21" in README


@pytest.mark.parametrize("question", KEY["questions"], ids=lambda q: q["id"])
def test_every_fact_in_the_key_is_in_its_file(question):
    for rel in question["expected_paths"]:
        assert (VAULT / rel).is_file(), rel
    for rel, facts in question["facts"].items():
        text = " ".join(_text(rel).split())
        for fact in facts:
            assert fact in text, (question["id"], rel, fact)


def test_the_exclusive_facts_are_exclusive():
    for entry in KEY["exclusive_facts"]:
        pattern = re.compile(entry["pattern"])
        rels = [str(p.relative_to(VAULT)) for p in _files()]
        holders = sorted(rel for rel in rels if pattern.search(_text(rel)))
        assert holders == sorted(entry["only_in"]), (entry["why"], holders)


def test_the_folder_that_the_demo_removes_has_the_files_the_key_lists():
    removable = KEY["removable_folder"]
    on_disk = sorted(str(p.relative_to(VAULT)) for p in (VAULT / removable["path"]).rglob("*") if p.is_file())
    assert on_disk == sorted(removable["files"])


def test_the_readme_demo_question_finds_files_that_exist():
    for rel in (
        "meetings/2026-08-19-pricing-review.md",
        "pricing/pricing-v3.md",
        "pricing/pricing-v1.md",
        "research/survey-results.csv",
    ):
        assert (VAULT / rel).is_file(), rel
        assert rel in README


def test_the_invented_names_the_key_checked_are_the_ones_the_vault_uses():
    corpus = " ".join(_text(str(p.relative_to(VAULT))) for p in _files()).lower()
    for name in KEY["name_check"]["names"]:
        assert name.lower() in corpus, name


def test_the_vault_indexes_without_errors(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data", collection="demo", embedder="hash", voyage_model="", openai_model="",
        local_model="", chunk_tokens=400, overlap_tokens=50, max_file_mb=5, extensions=DEFAULT_EXTENSIONS,
    )  # fmt: skip
    store = ChromaStore(settings.data_dir, settings.collection, HashEmbedder().name)
    stats = index_folder(VAULT, HashEmbedder(), store, settings, log=lambda _m: None)
    assert (stats.files_seen, stats.files_indexed, len(stats.errors)) == (21, 21, 0)
