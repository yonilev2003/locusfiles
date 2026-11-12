"""LocalEmbedder serialises encode calls (Lane M, 2026-10-04).

The MCP server's warm-up thread and the first tool call can both reach one SentenceTransformer; two
concurrent encode calls hang or abort on some devices. These tests use a model-shaped double that
records overlap, so they run offline with no model download.
"""

from __future__ import annotations

import threading
import time

from locus.ingest.embedder import LocalEmbedder


class _Vec:
    def __init__(self, rows: int) -> None:
        self._rows = rows

    def tolist(self) -> list[list[float]]:
        return [[0.0, 1.0] for _ in range(self._rows)]


class _TrapModel:
    """Fails loudly if encode is entered by two threads at once."""

    def __init__(self, delay: float = 0.05) -> None:
        self.delay = delay
        self.active = 0
        self.max_active = 0
        self._guard = threading.Lock()

    def encode(self, texts, **_kwargs):  # noqa: ANN001
        with self._guard:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(self.delay)
        with self._guard:
            self.active -= 1
        return _Vec(len(texts))


def _embedder(model: _TrapModel) -> LocalEmbedder:
    emb = object.__new__(LocalEmbedder)
    emb._model = model
    emb.model = "test-model"
    emb.name = "local:test-model"
    emb._query_prefix = ""
    emb._encode_lock = threading.Lock()
    return emb


def test_concurrent_queries_never_overlap_in_encode() -> None:
    model = _TrapModel()
    emb = _embedder(model)
    threads = [threading.Thread(target=emb.embed_query, args=("q",)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert model.max_active == 1


def test_warmup_style_query_and_document_batch_do_not_overlap() -> None:
    model = _TrapModel()
    emb = _embedder(model)
    t1 = threading.Thread(target=emb.embed_query, args=("warm-up",))
    t2 = threading.Thread(target=emb.embed_documents, args=(["a", "b", "c"],))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)
    assert model.max_active == 1


def test_results_are_unchanged_by_the_lock() -> None:
    emb = _embedder(_TrapModel(delay=0))
    assert emb.embed_query("x") == [0.0, 1.0]
    assert emb.embed_documents(["a", "b"]) == [[0.0, 1.0], [0.0, 1.0]]
