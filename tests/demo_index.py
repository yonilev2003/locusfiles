"""The demo vault indexed once with the offline HashEmbedder, shared by the tests of the JSON surfaces
(test_service.py, test_cli_json.py, test_api.py). Read-only: a test that changes files or the index builds its own."""

from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple

from locus.config import DEFAULT_EXTENSIONS, Settings
from locus.ingest.embedder import HashEmbedder
from locus.ingest.indexer import index_folder
from locus.ingest.store import ChromaStore

VAULT = (Path(__file__).resolve().parent / "fixtures" / "demo-vault").resolve()

# Questions with a clear answer in the vault, each different enough to rank different files.
QUERIES = (
    "pricing tiers and what the plans cost",
    "what are the risks for the launch",
    "sync bug postmortem root cause",
    "api rate limits per minute",
)


class DemoIndex(NamedTuple):
    settings: Settings
    embedder: HashEmbedder
    store: ChromaStore
    root: Path


def make_settings(data_dir: Path, collection: str = "demo") -> Settings:
    return Settings(
        data_dir=data_dir, collection=collection, embedder="hash", voyage_model="", openai_model="",
        local_model="", chunk_tokens=400, overlap_tokens=50, max_file_mb=5, extensions=DEFAULT_EXTENSIONS,
    )  # fmt: skip


def build_demo_index(base: Path) -> DemoIndex:
    settings = make_settings(base / "data")
    embedder = HashEmbedder()
    store = ChromaStore(settings.data_dir, settings.collection, embedder.name)
    stats = index_folder(VAULT, embedder, store, settings, log=lambda _m: None)
    assert (stats.files_indexed, len(stats.errors)) == (21, 0)
    return DemoIndex(settings, embedder, store, VAULT)


# What `locus_search` prints for each hit: "## 1. /abs/path.md  (chunk 2, score 0.512, p.3, modified_at ..., 812 chars)"
_HEADER = re.compile(
    r"^## \d+\. (?P<path>.+?)  \(chunk (?P<chunk>\d+)(?:, score (?P<score>-?[0-9.]+))?"
    r"(?:, p\.(?P<page>\d+))?(?:, modified_at (?P<modified>[^,]+))?, [0-9,]+ chars",
    re.M,
)


def mcp_hits(text: str) -> list[dict]:
    """The hits of a locus_search answer, in order: path, chunk_index, score, page and modified_at as printed."""
    return [
        {
            "path": m["path"],
            "chunk_index": int(m["chunk"]),
            "score": None if m["score"] is None else float(m["score"]),
            "page": None if m["page"] is None else int(m["page"]),
            "modified": m["modified"],
        }
        for m in _HEADER.finditer(text)
    ]
