"""Index-on-demand freshness (sprint 1, criterion 2): before `locus_search` answers, a bounded mtime scan of
the allowed roots indexes added/changed files and drops deleted ones. Never in the background; the clock is
injected so the time budget is tested without sleeping."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from locus import mcp_server as mcp_server_module
from locus.config import DEFAULT_EXTENSIONS, Settings
from locus.ingest.embedder import HashEmbedder
from locus.ingest.indexer import index_folder
from locus.policy import Matcher, Policy

pytest.importorskip("chromadb")

from locus.ingest.freshness import freshen, render_freshness_note  # noqa: E402
from locus.ingest.store import ChromaStore  # noqa: E402


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        collection="test",
        embedder="hash",
        voyage_model="",
        openai_model="",
        local_model="",
        chunk_tokens=100,
        overlap_tokens=10,
        max_file_mb=5,
        extensions=DEFAULT_EXTENSIONS,
    )


class CountingEmbedder(HashEmbedder):
    """Counts the texts sent for embedding; `on_embed` lets a test move a fake clock per call."""

    def __init__(self, on_embed=None):
        super().__init__()
        self.documents = 0
        self.on_embed = on_embed

    def embed_documents(self, texts):
        self.documents += len(texts)
        if self.on_embed:
            self.on_embed()
        return super().embed_documents(texts)


class FakeClock:
    def __init__(self, tick: float = 0.0):
        self.now = 0.0
        self.tick = tick

    def __call__(self) -> float:
        self.now += self.tick
        return self.now


@pytest.fixture()
def world(tmp_path: Path):
    root = tmp_path / "notes"
    root.mkdir()
    (root / "a.md").write_text("Alpha plan: ship the zebra release in March.")
    (root / "b.md").write_text("Budget notes: marketing spend capped at 3k.")
    settings = _settings(tmp_path)
    embedder = CountingEmbedder()
    store = ChromaStore(settings.data_dir, settings.collection, embedder.name)
    index_folder(root, embedder, store, settings, log=lambda _: None)
    embedder.documents = 0
    return root, settings, embedder, store


def _run(world, **kw):
    root, settings, embedder, store = world
    kw.setdefault("budget_s", 2.0)
    kw.setdefault("clock", FakeClock())
    return freshen([root], None, embedder, store, settings, **kw)


def _paths(store) -> set[str]:
    return {Path(p).name for p in store.list_paths()}


def test_added_file_is_searchable_after_freshen(world):
    root, _, embedder, store = world
    (root / "canary.md").write_text("Canary quokka wombat marker sentence.")
    report = _run(world)
    assert (report.indexed, report.removed, report.pending) == (1, 0, 0)
    hits = store.query(embedder.embed_query("canary quokka wombat"), k=1)
    assert hits and hits[0].path.endswith("canary.md") and hits[0].chunk_index == 0
    assert render_freshness_note(report, 2.0) is None  # nothing cut short: no note


def test_modified_file_is_reembedded(world):
    root, _, embedder, store = world
    (root / "b.md").write_text("Budget doubled: marketing spend 6k, platypus initiative.")
    st = (root / "b.md").stat()
    os.utime(root / "b.md", (st.st_atime, st.st_mtime + 5))
    report = _run(world)
    assert report.indexed == 1
    hits = store.query(embedder.embed_query("platypus initiative"), k=1)
    assert hits[0].path.endswith("b.md") and "platypus" in hits[0].text


def test_deleted_file_leaves_the_index(world):
    root, _, _, store = world
    (root / "a.md").unlink()
    report = _run(world)
    assert (report.indexed, report.removed) == (0, 1)
    assert _paths(store) == {"b.md"}


def test_unchanged_files_cost_no_embedding(world):
    _, _, embedder, _ = world
    report = _run(world)
    assert (report.indexed, report.removed, report.pending) == (0, 0, 0)
    assert embedder.documents == 0


def test_touched_but_identical_file_is_not_reembedded(world):
    root, _, embedder, store = world
    st = (root / "a.md").stat()
    os.utime(root / "a.md", (st.st_atime, st.st_mtime + 10))
    report = _run(world)
    assert report.indexed == 0 and embedder.documents == 0
    assert _run(world).indexed == 0  # the new mtime was recorded: the next scan sees it as unchanged
    assert store.file_mtimes()[str(root / "a.md")] == pytest.approx(st.st_mtime + 10)


def test_budget_cuts_indexing_short_and_reports_counts_only(world):
    root, settings, _, store = world
    clock = FakeClock()
    embedder = CountingEmbedder(on_embed=lambda: setattr(clock, "now", clock.now + 1.0))  # each file costs 1 s
    for name in ("n1", "n2", "n3", "n4"):
        (root / f"{name}.md").write_text(f"{name} uniquetoken{name}")
    report = freshen([root], None, embedder, store, settings, budget_s=2.5, clock=clock)
    assert report.indexed == 3 and report.pending == 1 and report.cut_short  # t=0,1,2 start; t=3 stops
    note = render_freshness_note(report, 2.5)
    assert note is not None and "1 changed file" in note and "not yet indexed" in note
    assert "n4" not in note and str(root) not in note  # counts only
    # the next call continues where this one stopped
    report2 = freshen([root], None, CountingEmbedder(), store, settings, budget_s=2.5, clock=FakeClock())
    assert (report2.indexed, report2.pending) == (1, 0)


def test_scan_cut_short_does_not_delete_and_says_so(world):
    root, settings, embedder, store = world
    (root / "a.md").unlink()
    report = freshen([root], None, embedder, store, settings, budget_s=2.0, clock=FakeClock(tick=10.0))
    assert not report.scan_complete and report.cut_short
    assert report.removed == 0 and "a.md" in _paths(store)  # an unfinished scan proves nothing about deletions
    note = render_freshness_note(report, 2.0)
    assert note and "not finished" in note


def test_zero_budget_is_off(world):
    root, _, embedder, store = world
    (root / "new.md").write_text("brand new")
    report = _run(world, budget_s=0)
    assert report.indexed == 0 and report.pending == 0 and "new.md" not in _paths(store)
    assert render_freshness_note(report, 0) is None


def test_policy_denied_files_are_not_indexed_and_leave_the_index(world, tmp_path):
    root, settings, embedder, store = world
    (root / "api-token.txt").write_text("sk-not-a-real-key")
    (root / "ok.md").write_text("fine to index")
    matcher = Matcher(Policy(roots=(str(root),)), tmp_path / "home")
    report = freshen([root], matcher, embedder, store, settings, budget_s=2.0, clock=FakeClock())
    assert report.indexed == 1 and "api-token.txt" not in _paths(store)
    # a file that was indexed earlier and is denied now is dropped
    (root / "b.md").rename(root / "credentials.md")
    report = freshen([root], matcher, embedder, store, settings, budget_s=2.0, clock=FakeClock())
    assert "b.md" not in _paths(store) and "credentials.md" not in _paths(store)
    assert report.removed == 1


def test_root_outside_the_walk_is_untouched(world, tmp_path):
    root, settings, embedder, store = world
    other = tmp_path / "other"
    other.mkdir()
    (other / "x.md").write_text("elsewhere")
    index_folder(other, embedder, store, settings, log=lambda _: None)
    (other / "x.md").unlink()
    report = _run(world)  # only `root` is scanned
    assert report.removed == 0 and "x.md" in _paths(store)


def test_indexed_roots_are_the_fallback(world):
    root, _, _, store = world
    assert store.indexed_roots() == [str(root)]


# --- the hook in the MCP server ------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_state():
    mcp_server_module._state.clear()
    yield
    mcp_server_module._state.clear()


def _serve(world, **env):
    root, settings, embedder, store = world
    mcp_server_module._state.update(embedder=embedder, store=store, settings=settings)
    return root


@pytest.mark.anyio
async def test_search_finds_a_file_added_since_indexing_with_no_command(world, monkeypatch):
    root = _serve(world)
    (root / "canary.md").write_text("Canary quokka wombat marker sentence.")
    out = await mcp_server_module.locus_search("canary quokka wombat", top_k=2)
    assert "canary.md" in out and "chunk" in out.lower()


@pytest.mark.anyio
async def test_search_adds_a_cut_short_note(world, monkeypatch):
    root = _serve(world)
    monkeypatch.setenv("LOCUS_FRESHNESS_BUDGET_S", "0.000001")
    for i in range(3):
        (root / f"late{i}.md").write_text(f"late file {i}")
    out = await mcp_server_module.locus_search("zebra release", top_k=2)
    assert "not yet indexed" in out or "not finished" in out


@pytest.mark.anyio
async def test_search_budget_off_keeps_old_behaviour(world, monkeypatch):
    root = _serve(world)
    monkeypatch.setenv("LOCUS_FRESHNESS_BUDGET_S", "0")
    (root / "canary.md").write_text("Canary quokka wombat marker sentence.")
    out = await mcp_server_module.locus_search("canary quokka wombat", top_k=2)
    assert "canary.md" not in out.split("\n", 3)[-1] or "Stale" in out
    assert "canary.md" not in {Path(p).name for p in world[3].list_paths()}


@pytest.mark.anyio
async def test_list_sources_sees_a_new_file(world):
    root = _serve(world)
    (root / "canary.md").write_text("Canary marker.")
    out = await mcp_server_module.locus_list_sources()
    assert "canary.md" in out and "total: 3" in out


def test_tool_names_and_read_only_hints_unchanged():
    import asyncio

    tools = {t.name: t for t in asyncio.run(mcp_server_module.mcp.list_tools())}
    assert set(tools) >= {
        "locus_search",
        "locus_list_sources",
        "locus_read_file",
        "locus_account_status",
        "locus_diagnose_connection",
        "locus_help",
    }
    assert all(t.annotations.read_only_hint is True for t in tools.values())
