"""The remote audit log's list of indexed paths is cached per index generation.

Listing every path reads the metadata of every chunk (measured 2026-11-17: about 52 microseconds per chunk), so doing it
on every audited call would slow every remote answer on a large index.
"""

from __future__ import annotations

from locus import mcp_server as srv


class FakeStore:
    def __init__(self, generation: str | None = "g1") -> None:
        self.calls = 0
        self.generation = generation
        self.paths = ["/a/x.md", "/a/y.md"]

    def _read_generation(self) -> str | None:
        return self.generation

    def list_paths(self) -> list[str]:
        self.calls += 1
        return list(self.paths)


def _install(monkeypatch, store) -> None:
    srv._KNOWN_PATHS_CACHE.clear()
    monkeypatch.setattr(srv, "_components", lambda: (None, store, None))


def test_the_second_call_reuses_the_list_while_the_generation_is_unchanged(monkeypatch):
    store = FakeStore()
    _install(monkeypatch, store)
    assert srv._known_paths() == {"/a/x.md", "/a/y.md"}
    assert srv._known_paths() == {"/a/x.md", "/a/y.md"}
    assert store.calls == 1


def test_a_new_generation_lists_the_paths_again(monkeypatch):
    store = FakeStore()
    _install(monkeypatch, store)
    srv._known_paths()
    store.paths.append("/a/z.md")
    store.generation = "g2"
    assert "/a/z.md" in srv._known_paths()
    assert store.calls == 2


def test_without_a_generation_token_the_list_is_kept_only_for_the_ttl(monkeypatch):
    store = FakeStore(generation=None)
    _install(monkeypatch, store)
    monkeypatch.setattr(srv, "_KNOWN_PATHS_TTL_S", 3600.0)
    srv._known_paths()
    srv._known_paths()
    assert store.calls == 1
    monkeypatch.setattr(srv, "_KNOWN_PATHS_TTL_S", 0.0)
    srv._known_paths()
    assert store.calls == 2


def test_a_different_store_object_is_never_served_from_the_cache(monkeypatch):
    first, second = FakeStore(), FakeStore()
    second.paths = ["/b/only.md"]
    _install(monkeypatch, first)
    srv._known_paths()
    monkeypatch.setattr(srv, "_components", lambda: (None, second, None))
    assert srv._known_paths() == {"/b/only.md"}


def test_a_failure_gives_an_empty_set_not_an_error(monkeypatch):
    srv._KNOWN_PATHS_CACHE.clear()

    def boom():
        raise RuntimeError("index is gone")

    monkeypatch.setattr(srv, "_components", boom)
    assert srv._known_paths() == set()
