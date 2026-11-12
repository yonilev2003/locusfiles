"""Regression tests for the night-fix pass: --prune and unreadable folders (F9), page-range cap (F10),
the hot-set budget and the walk (F11), a swapped parent folder in locus_read_file (F16). HashEmbedder, tmp folders."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

pytest.importorskip("chromadb")

from locus import mcp_server as mcp_server_module  # noqa: E402
from locus.config import DEFAULT_EXTENSIONS, Settings  # noqa: E402
from locus.ingest.embedder import HashEmbedder  # noqa: E402
from locus.ingest.indexer import index_folder  # noqa: E402
from locus.ingest.multiroot import Stage, index_roots, render_report  # noqa: E402
from locus.ingest.parsers import MAX_PAGE_SPEC_PAGES, parse_page_spec  # noqa: E402
from locus.ingest.store import ChromaStore  # noqa: E402
from locus.policy import Matcher, Policy  # noqa: E402

QUIET = lambda _msg: None  # noqa: E731


def _world(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path / "data", collection="nightfix", embedder="hash", voyage_model="", openai_model="",
        local_model="", chunk_tokens=100, overlap_tokens=10, max_file_mb=5, extensions=DEFAULT_EXTENSIONS,
    )  # fmt: skip
    embedder = HashEmbedder()
    return settings, embedder, ChromaStore(settings.data_dir, settings.collection, embedder.name)


def _two_folders(tmp_path: Path):
    settings, embedder, store = _world(tmp_path)
    root = tmp_path / "docs"
    for sub in ("private", "open"):
        (root / sub).mkdir(parents=True)
        (root / sub / f"{sub}.md").write_text(f"the {sub} folder note about zebras")
    index_folder(root, embedder, store, settings, log=QUIET)
    return settings, embedder, store, root


@pytest.mark.skipif(getattr(os, "geteuid", lambda: 1)() == 0, reason="root can read a mode-0 folder")
def test_prune_skips_only_the_unreadable_subtree_and_says_so(tmp_path):
    settings, embedder, store, root = _two_folders(tmp_path)
    (root / "open" / "open.md").unlink()  # genuinely gone: prune must still remove it
    os.chmod(root / "private", 0)
    lines: list[str] = []
    try:
        stats = index_folder(root, embedder, store, settings, prune=True, log=lines.append)
    finally:
        os.chmod(root / "private", 0o755)
    paths = store.list_paths()
    assert any(p.endswith("private.md") for p in paths)
    assert not any(p.endswith("open.md") for p in paths)
    assert stats.files_removed == 1
    assert any("--prune kept the index of 1 file(s)" in line for line in lines)


def test_prune_still_removes_vanished_files_when_everything_is_readable(tmp_path):
    settings, embedder, store, root = _two_folders(tmp_path)
    (root / "private" / "private.md").unlink()
    lines: list[str] = []
    index_folder(root, embedder, store, settings, prune=True, log=lines.append)
    assert not any(p.endswith("private.md") for p in store.list_paths())
    assert not any("could not read" in line for line in lines)


@pytest.mark.parametrize("spec", [f"1-{MAX_PAGE_SPEC_PAGES + 1}", "1-999999999", "1-1000,2000-3500"])
def test_f10_huge_page_lists_are_refused_quickly(spec):
    with pytest.raises(ValueError, match="more than"):
        parse_page_spec(spec)


def test_f10_ordinary_page_specs_still_parse():
    assert parse_page_spec("1-3,7") == [1, 2, 3, 7]
    assert len(parse_page_spec(f"1-{MAX_PAGE_SPEC_PAGES}")) == MAX_PAGE_SPEC_PAGES


def test_f11_walk_time_is_outside_the_budget_and_reported(tmp_path, monkeypatch):
    import locus.ingest.multiroot as multiroot_module

    settings, embedder, store = _world(tmp_path)
    root = tmp_path / "home"
    root.mkdir()
    (root / "a.md").write_text("hot file about the zebra release")
    now = [0.0]
    real_iter = multiroot_module.iter_files

    def slow_walk(*args, **kwargs):
        now[0] += 120.0
        yield from real_iter(*args, **kwargs)

    monkeypatch.setattr(multiroot_module, "iter_files", slow_walk)
    report = index_roots(
        [root], embedder, store, settings, Matcher(Policy(roots=(str(root),)), tmp_path),
        Stage(order="recent", budget_seconds=60.0), log=QUIET, clock=lambda: now[0],
    )  # fmt: skip
    assert report.stats.files_indexed == 1
    assert report.walk_seconds == 120.0
    assert "Listing the files first took 120 s, not counted" in render_report(report)


def test_f16_a_symlinked_parent_is_refused_but_a_normal_read_works(tmp_path):
    settings, embedder, store = _world(tmp_path)
    root = tmp_path / "corpus"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "a.md").write_text("indexed content " * 20)
    index_folder(root, embedder, store, settings, log=QUIET)
    indexed = (root / "sub" / "a.md").resolve()

    def read(p: Path) -> str:
        mcp_server_module._state.clear()
        mcp_server_module._state.update(embedder=embedder, store=store, settings=settings)
        try:
            return asyncio.run(mcp_server_module.locus_read_file(str(p)))
        finally:
            mcp_server_module._state.clear()

    assert "indexed content" in read(indexed)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a.md").write_text("OUTSIDE-SECRET")
    (root / "sub" / "a.md").unlink()
    (root / "sub").rmdir()
    (root / "sub").symlink_to(outside)
    out = read(indexed)
    assert "OUTSIDE-SECRET" not in out
    assert "resolves outside the indexed files" in out
