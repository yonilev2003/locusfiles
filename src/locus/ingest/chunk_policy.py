"""Which chunk size a collection is built with (ADR-087 item 1).

A 700-token chunk does not fit a 512-token embedding window: the model reads only the head of it. On the
2026-10-25 synthetic-course experiment, 300-token chunks beat 700 for multilingual-e5-small by +0.16 (same-
language recall@5). So a NEW collection built with a model whose window is 512 tokens or less defaults
to 300 tokens, and records that in its metadata. A collection that records nothing was built before this
rule and keeps the legacy 700, so an upgrade never changes what an existing index holds or how new files
are cut next to the old ones. `LOCUS_CHUNK_TOKENS` overrides both, as it always has.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from ..config import Settings

# Collection metadata key: the chunk size (in the chunker's ~4-characters-per-token units) a collection
# was created with. Absent on a collection created before ADR-087.
CHUNK_TOKENS_KEY = "chunk_tokens"

LEGACY_CHUNK_TOKENS = 700
WINDOW_AWARE_CHUNK_TOKENS = 300
SMALL_WINDOW_TOKENS = 512  # a model whose window is this or smaller gets WINDOW_AWARE_CHUNK_TOKENS


def window_aware_chunk_tokens(embedder: Any) -> int | None:
    """The default chunk size for *embedder*, or None when it has no known small window (hosted and
    large-window models keep 700). An embedder reports its window as `window_tokens`."""
    window = getattr(embedder, "window_tokens", None)
    if isinstance(window, int) and 0 < window <= SMALL_WINDOW_TOKENS:
        return WINDOW_AWARE_CHUNK_TOKENS
    return None


def chunk_tokens_for_new_collection(settings: Settings, embedder: Any) -> int | None:
    """The chunk size to record when a collection is created now; None records nothing (legacy 700)."""
    if settings.chunk_tokens_explicit:
        return settings.chunk_tokens
    return window_aware_chunk_tokens(embedder)


def effective_chunk_tokens(settings: Settings, store: Any) -> int:
    """The chunk size to cut the next file with: `LOCUS_CHUNK_TOKENS` if set, else what the collection
    records, else `settings.chunk_tokens` (700 unless a test or caller built Settings with another)."""
    if settings.chunk_tokens_explicit:
        return settings.chunk_tokens
    recorded = getattr(store, "chunk_tokens", None)
    if isinstance(recorded, int) and recorded > 0:
        return recorded
    return settings.chunk_tokens


# Windows (tokens) of the local models Locus knows by name, for previews that must not load a model
# (`locus index --dry-run`, `locus estimate`). The real embedder reports its window itself
# (sentence-transformers' max_seq_length). UNVERIFIED offline: that these numbers match what each model reports.
KNOWN_LOCAL_WINDOWS: dict[str, int] = {
    "bge-small-en-v1.5": 512,
    "bge-small-en": 512,
    "bge-base-en-v1.5": 512,
    "bge-large-en-v1.5": 512,
    "multilingual-e5-small": 512,
    "multilingual-e5-base": 512,
    "multilingual-e5-large": 512,
    "bge-m3": 8192,
}


def preview_chunk_tokens(settings: Settings, store: Any = None) -> tuple[int, str]:
    """(chunk size, why) that `locus index` would use for *settings*, without building the embedder.

    `store` is the existing collection opened read-only, or None when there is none. The answer follows the
    same order as the indexer: LOCUS_CHUNK_TOKENS, the size the collection records, the legacy 700 for a
    populated collection that records none, and for a collection about to be created the window-aware
    default of a known local model."""
    if settings.chunk_tokens_explicit:
        return settings.chunk_tokens, "LOCUS_CHUNK_TOKENS"
    recorded = getattr(store, "chunk_tokens", None)
    if isinstance(recorded, int) and recorded > 0:
        return recorded, "recorded by the existing collection"
    populated = store is not None and store.count() > 0
    if populated:
        return settings.chunk_tokens, "legacy default: the existing collection records no chunk size"
    if settings.embedder == "local":
        window = KNOWN_LOCAL_WINDOWS.get(settings.local_model.rstrip("/").rsplit("/", 1)[-1].lower())
        if window is not None and window <= SMALL_WINDOW_TOKENS:
            return WINDOW_AWARE_CHUNK_TOKENS, f"default for a new collection with a {window}-token model"
    return settings.chunk_tokens, "default"


def settings_for_preview(settings: Settings, store: Any = None) -> tuple[Settings, str]:
    """*settings* with `chunk_tokens` set to what the collection will really use, for the estimator and the
    preflight report, and the one-line reason to show the user."""
    tokens, why = preview_chunk_tokens(settings, store)
    return replace(
        settings, chunk_tokens=tokens, chunk_tokens_explicit=True
    ), f"Chunk size used: {tokens} tokens ({why})."
