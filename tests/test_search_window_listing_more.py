"""Night 2 (T6, 2026-10-25): a date-window listing without a query shows `top_k` files (default 6). It used to
say "Complete chunk text for all 6 result(s)" even when more files had changed, so "what did I add this week" was
answered with the newest six. The result now says when more files are in the window."""

from __future__ import annotations

import asyncio
import os
import re
import time

import pytest

from locus import mcp_server as srv
from locus.config import DEFAULT_EXTENSIONS, Settings
from locus.ingest.embedder import HashEmbedder
from locus.ingest.indexer import index_folder
from locus.ingest.store import ChromaStore

pytest.importorskip("chromadb")


@pytest.fixture(autouse=True)
def _fresh_state():
    srv._state.clear()
    yield
    srv._state.clear()


def _index_files(tmp_path, count: int) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    base = time.mktime((2026, 10, 20, 12, 0, 0, 0, 0, -1))
    for i in range(count):
        path = root / f"file{i:02d}.md"
        path.write_text(f"note number {i}\n")
        os.utime(path, (base + 3600 * i, base + 3600 * i))
    settings = Settings(
        data_dir=tmp_path / "data",
        collection="test",
        embedder="hash",
        voyage_model="",
        openai_model="",
        local_model="",
        chunk_tokens=800,
        overlap_tokens=10,
        max_file_mb=5,
        extensions=DEFAULT_EXTENSIONS,
    )
    embedder = HashEmbedder()
    store = ChromaStore(settings.data_dir, "test", embedder.name)
    index_folder(root, embedder, store, settings, log=lambda _: None)
    srv._state.update(embedder=embedder, store=store, settings=settings)


def _listing(**kwargs) -> str:
    return asyncio.run(srv.locus_search(modified_after="2026-10-19", **kwargs))


def test_the_listing_says_when_more_files_are_in_the_window(tmp_path):
    _index_files(tmp_path, 9)
    out = _listing(top_k=6)
    assert len(re.findall(r"^## \d+\. ", out, re.MULTILINE)) == 6
    assert re.search(r"More file\(s\) .*top_k", out), out[-400:]
    assert "(9 file(s) in the window in all" in out, out[-400:]  # the total, so a count question needs one call


def test_no_note_when_every_file_in_the_window_is_shown(tmp_path):
    _index_files(tmp_path, 9)
    out = _listing(top_k=9)
    assert len(re.findall(r"^## \d+\. ", out, re.MULTILINE)) == 9
    assert "More file(s)" not in out
    assert "More file(s)" not in _listing(top_k=25)
