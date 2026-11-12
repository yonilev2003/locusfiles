"""Open the configured collection together with the embedder that reads and writes it.

The one place the CLI and the MCP server pair an embedder with its collection, so both follow the
same rules: the embedder name must match the collection's (store.py), and the embedder embeds the
way the collection was built, with or without Voyage's document/query input_type (RET-002).
"""

from __future__ import annotations

from ..config import Settings
from .chunk_policy import chunk_tokens_for_new_collection
from .embedder import (
    Embedder,
    check_collection_policy,
    follow_collection_policy,
    get_embedder,
    input_type_policy_of,
)
from .store import ChromaStore


def open_index(settings: Settings) -> tuple[Embedder, ChromaStore, str | None]:
    """The embedder, its collection, and a warning for the user when the collection is a legacy one
    built without input_type (search keeps working; a re-index is recommended), else None.

    Raises RuntimeError for a configuration problem: a missing key, a collection built with another
    embedder, or an input_type policy this version cannot follow."""
    embedder = get_embedder(settings)
    store = ChromaStore(
        settings.data_dir,
        settings.collection,
        embedder.name,
        input_type_policy=input_type_policy_of(embedder),
        chunk_tokens=chunk_tokens_for_new_collection(settings, embedder),
    )
    warning = follow_collection_policy(embedder, store.input_type_policy, settings.collection)
    # A long-lived serve/watch reopens the collection after another process writes it: if that
    # process re-created it under another policy, refuse rather than mix (store._open calls this).
    store.input_type_policy_guard = lambda recorded: check_collection_policy(embedder, recorded, settings.collection)
    return embedder, store, warning
