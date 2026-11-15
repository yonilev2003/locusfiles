"""A hard link to a file outside the indexed folder is not indexed.

A hard link is the same file under a second name. Someone who can write into an indexed folder can link a file from
elsewhere on the same disk into it under a harmless name, and without this its text became searchable (found in the
security self-assessment of 2026-11-15). No name-based rule sees that, so a file with more than one link is skipped
unless LOCUS_ALLOW_HARD_LINKS is set. Limits, stated in the README: a filesystem that does not report link counts
(FAT, exFAT, some network shares) cannot be checked, and two names inside the folder skip both."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("chromadb")

from locus.config import DEFAULT_EXTENSIONS, Settings  # noqa: E402
from locus.ingest.embedder import HashEmbedder  # noqa: E402
from locus.ingest.estimator import exclusion_lines, scan  # noqa: E402
from locus.ingest.freshness import freshen  # noqa: E402
from locus.ingest.indexer import IndexStats, index_file, index_folder, iter_files  # noqa: E402
from locus.ingest.store import ChromaStore  # noqa: E402
from locus.policy import Matcher, Policy  # noqa: E402

CANARY = "OUTSIDE-CANARY-the-secret-launch-codes"


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.delenv("LOCUS_ALLOW_HARD_LINKS", raising=False)
    root = tmp_path / "notes"
    outside = tmp_path / "elsewhere"
    root.mkdir()
    outside.mkdir()
    (root / "plan.md").write_text("the zebra release plan")
    (outside / "private.md").write_text(CANARY)
    try:
        os.link(outside / "private.md", root / "harmless-name.md")
    except (OSError, NotImplementedError):
        pytest.skip("this filesystem cannot make hard links")
    settings = Settings(
        data_dir=tmp_path / "data", collection="links", embedder="hash", voyage_model="", openai_model="",
        local_model="", chunk_tokens=100, overlap_tokens=10, max_file_mb=5, extensions=DEFAULT_EXTENSIONS,
    )  # fmt: skip
    embedder = HashEmbedder()
    store = ChromaStore(settings.data_dir, settings.collection, embedder.name)
    return root, outside, settings, embedder, store, tmp_path


def test_the_walker_skips_the_linked_file_and_says_why(world):
    root, _outside, settings, *_ = world
    skipped: list[tuple[str, str]] = []
    files = [p.name for p in iter_files(root, settings, lambda reason, p: skipped.append((reason, p.name)))]
    assert files == ["plan.md"] and ("hard_link", "harmless-name.md") in skipped


def test_its_text_never_reaches_the_index_and_the_run_says_how_many_without_names(world):
    root, _outside, settings, embedder, store, _tmp = world
    lines: list[str] = []
    stats = index_folder(root, embedder, store, settings, log=lines.append)
    assert stats.files_indexed == 1 and [Path(p).name for p in store.list_paths()] == ["plan.md"]
    hits = store.query(embedder.embed_query(CANARY), k=5)
    assert not any(CANARY in hit.text for hit in hits)
    line = next(line for line in lines if "hard link" in line)
    assert "1 file(s) with more than one hard link" in line and "LOCUS_ALLOW_HARD_LINKS=1" in line
    assert "harmless-name" not in line  # counts only


def test_the_user_can_allow_hard_links(world, monkeypatch):
    root, _outside, settings, embedder, store, _tmp = world
    monkeypatch.setenv("LOCUS_ALLOW_HARD_LINKS", "1")
    index_folder(root, embedder, store, settings, log=lambda _m: None)
    assert sorted(Path(p).name for p in store.list_paths()) == ["harmless-name.md", "plan.md"]


def test_when_the_other_name_is_gone_the_file_is_an_ordinary_file_again(world):
    root, outside, settings, embedder, store, _tmp = world
    os.unlink(outside / "private.md")  # the link count falls back to one
    index_folder(root, embedder, store, settings, log=lambda _m: None)
    assert sorted(Path(p).name for p in store.list_paths()) == ["harmless-name.md", "plan.md"]


def test_two_names_inside_the_folder_skip_both(world):
    root, _outside, settings, *_ = world
    os.link(root / "plan.md", root / "plan-copy.md")
    files = [p.name for p in iter_files(root, settings)]
    assert files == []  # documented limit: the walker cannot tell which name is "the" file


def test_index_file_on_a_linked_path_is_refused_with_a_reason(world):
    root, _outside, settings, embedder, store, _tmp = world
    stats, lines = IndexStats(), []
    assert index_file(root / "harmless-name.md", root, embedder, store, settings, stats, log=lines.append) == 0
    assert stats.files_skipped == 1 and "more than one hard link" in lines[0] and store.list_paths() == []


def test_the_freshness_step_of_a_search_does_not_pick_it_up(world):
    root, _outside, settings, embedder, store, tmp = world
    matcher = Matcher(Policy(roots=(str(root),)), tmp)
    report = freshen([root], matcher, embedder, store, settings, budget_s=10.0)
    assert [Path(p).name for p in store.list_paths()] == ["plan.md"] and report.errors == 0


def test_estimate_and_dry_run_count_it_under_its_own_label(world):
    root, _outside, settings, *_ = world
    result = scan(root, settings)
    assert result.excluded["hard_link"] == 1
    assert any("1 file with more than one hard link" in line for line in exclusion_lines(result))
