"""RET-002: a collection records how its vectors were made (with or without Voyage's document/query `input_type`) and
the embedder follows it, so one collection never mixes the two. This applies to `voyage` (your own key) and to the
legacy managed collections; local, OpenAI and the test embedder have no such distinction. Offline, no API calls."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("chromadb")

from locus.ingest.embedder import (  # noqa: E402
    INPUT_TYPE_NONE,
    INPUT_TYPE_V1,
    HashEmbedder,
    check_collection_policy,
    follow_collection_policy,
    input_type_status,
)
from locus.ingest.store import INPUT_TYPE_POLICY_KEY, ChromaStore  # noqa: E402


class _Policy:
    """An embedder that has an input_type policy: `unrecorded` is what a collection built before policies were
    recorded was made with (Voyage: v1; the old managed relay: none)."""

    def __init__(self, policy: str, unrecorded: str, switchable: bool = True) -> None:
        self.name = "fake:policy"
        self.input_type_policy = policy
        self.unrecorded_input_type_policy = unrecorded
        if switchable:
            self.use_input_type_policy = self._use

    def _use(self, policy: str) -> None:
        self.input_type_policy = policy


def test_an_embedder_without_a_policy_follows_nothing():
    assert follow_collection_policy(HashEmbedder(), "v1", "docs") is None
    assert input_type_status(HashEmbedder()).startswith("n/a")


def test_a_collection_built_the_same_way_needs_no_change_and_no_warning():
    embedder = _Policy(INPUT_TYPE_V1, INPUT_TYPE_V1)
    assert follow_collection_policy(embedder, INPUT_TYPE_V1, "docs") is None
    assert embedder.input_type_policy == INPUT_TYPE_V1 and input_type_status(embedder).startswith("v1")


def test_a_collection_from_before_policies_were_recorded_is_read_as_its_embedder_built_it():
    voyage_like = _Policy(INPUT_TYPE_V1, unrecorded=INPUT_TYPE_V1)
    assert follow_collection_policy(voyage_like, None, "docs") is None
    legacy_managed = _Policy(INPUT_TYPE_V1, unrecorded=INPUT_TYPE_NONE)
    warning = follow_collection_policy(legacy_managed, None, "docs")
    assert legacy_managed.input_type_policy == INPUT_TYPE_NONE  # it now embeds the old way
    assert warning and "'docs'" in warning and "re-index" in warning


def test_an_embedder_follows_a_legacy_collection_instead_of_mixing_vectors():
    embedder = _Policy(INPUT_TYPE_V1, unrecorded=INPUT_TYPE_V1)
    warning = follow_collection_policy(embedder, INPUT_TYPE_NONE, "old")
    assert embedder.input_type_policy == INPUT_TYPE_NONE and warning
    assert input_type_status(embedder).startswith("none (legacy")


def test_a_policy_this_version_cannot_follow_is_refused():
    with pytest.raises(RuntimeError, match="cannot follow"):
        follow_collection_policy(_Policy(INPUT_TYPE_V1, INPUT_TYPE_V1), "v9", "docs")
    with pytest.raises(RuntimeError, match="cannot follow"):  # an embedder that cannot switch
        follow_collection_policy(_Policy(INPUT_TYPE_V1, INPUT_TYPE_V1, switchable=False), INPUT_TYPE_NONE, "docs")


def test_a_collection_re_created_under_another_policy_while_open_is_refused():
    embedder = _Policy(INPUT_TYPE_NONE, INPUT_TYPE_NONE)
    check_collection_policy(embedder, INPUT_TYPE_NONE, "docs")  # unchanged: fine
    with pytest.raises(RuntimeError, match="re-created it"):
        check_collection_policy(embedder, INPUT_TYPE_V1, "docs")


def test_a_new_collection_records_its_policy_and_a_reopen_does_not_overwrite_it(tmp_path: Path):
    first = ChromaStore(tmp_path / "data", "recorded", "fake:policy", input_type_policy=INPUT_TYPE_V1)
    assert first.input_type_policy == INPUT_TYPE_V1
    first.upsert(["a:0"], [[1.0, 0.0]], ["text"], [{"path": "/v/a.txt", "chunk_index": 0, "root": "/v"}])
    second = ChromaStore(tmp_path / "data", "recorded", "fake:policy", input_type_policy=INPUT_TYPE_NONE)
    assert second.input_type_policy == INPUT_TYPE_V1  # what was recorded wins over what the caller wants
    assert (second._col.metadata or {}).get(INPUT_TYPE_POLICY_KEY) == INPUT_TYPE_V1


def test_recording_the_policy_keeps_cosine_distance(tmp_path: Path):
    """chromadb refuses an hnsw:* key in modify(); the distance function is fixed when the collection is created.
    If recording the policy lost it, an un-normalised vector would no longer score 1.0 against its own direction."""
    store = ChromaStore(tmp_path / "data", "cosine", "fake:policy", input_type_policy=INPUT_TYPE_V1)
    store.upsert(["a:0"], [[3.0, 0.0]], ["text"], [{"path": "/v/a.txt", "chunk_index": 0, "root": "/v"}])
    hits = store.query([1.0, 0.0], k=1)
    assert len(hits) == 1 and hits[0].score == pytest.approx(1.0, abs=1e-6)
