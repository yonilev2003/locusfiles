"""locus_search's evidence budget and the deprecated `prefix` alias of locus_list_sources. Offline, HashEmbedder."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("chromadb")

from locus import mcp_server as srv  # noqa: E402
from locus.config import DEFAULT_EXTENSIONS, Settings  # noqa: E402
from locus.ingest.chunker import max_chunk_chars  # noqa: E402
from locus.ingest.embedder import HashEmbedder  # noqa: E402
from locus.ingest.indexer import index_folder  # noqa: E402
from locus.ingest.store import ChromaStore, Hit  # noqa: E402


@pytest.fixture()
def server(tmp_path, monkeypatch):
    settings = Settings(
        data_dir=tmp_path / "data", collection="srv", embedder="hash", voyage_model="", openai_model="",
        local_model="", chunk_tokens=100, overlap_tokens=10, max_file_mb=5, extensions=DEFAULT_EXTENSIONS,
    )  # fmt: skip
    embedder = HashEmbedder()
    store = ChromaStore(settings.data_dir, settings.collection, embedder.name)
    root = tmp_path / "notes"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "a.md").write_text("alpha note about the zebra release")
    (root / "b.md").write_text("beta note about pricing")
    index_folder(root, embedder, store, settings, log=lambda _m: None)
    monkeypatch.setattr(srv, "_components", lambda: (embedder, store, settings))
    monkeypatch.setattr(srv, "freshen_for_serve", lambda *a, **k: None)
    return root.resolve()


# --- the evidence budget ---------------------------------------------------------------------------------------


def test_the_default_top_k_always_fits_whole_inside_the_budget():
    """If the chunker's bound outgrows the budget, a default search would silently lose the text of its last hits."""
    assert srv.SEARCH_DEFAULT_TOP_K * max_chunk_chars() <= srv.SEARCH_CHAR_BUDGET


def test_the_search_description_states_the_budget_the_code_uses():
    tools = {t.name: t for t in asyncio.run(srv.mcp.list_tools())}
    description = " ".join(tools["locus_search"].description.split())
    assert f"{srv.SEARCH_CHAR_BUDGET:,} characters of chunk text" in description


def test_the_lowest_ranked_hits_lose_their_text_first_and_are_not_listed_as_sources():
    hits = [Hit(f"/a/{i}.md", 0, "x" * 400, 0.9 - i / 100) for i in range(5)]
    out = srv._format_search_results("q", hits, budget=1000)
    assert out.count("text not included") == 3  # 400 + 400 fit; the other three do not
    assert "Sources: /a/0.md; /a/1.md" in out and "Also matched, text not delivered" in out
    for i in (2, 3, 4):
        assert f"/a/{i}.md" in out  # every path stays listed (constraint 2)


def test_a_single_top_hit_over_the_whole_budget_is_cut_and_says_where_to_read_on():
    out = srv._format_search_results("q", [Hit("/a/big.md", 0, "y" * 3000, 0.9)], budget=1000)
    assert "first 1,000 shown" in out and "2,000 more chars" in out and "locus_read_file on /a/big.md" in out


def test_a_search_never_returns_more_chunk_text_than_the_budget(server):
    out = asyncio.run(srv.locus_search("note", top_k=25))
    assert len(out) < srv.SEARCH_CHAR_BUDGET + 4000  # chunk text is bounded; headers and notes are small


# --- the deprecated alias --------------------------------------------------------------------------------------


def test_the_prefix_alias_is_still_marked_deprecated_in_the_tool_schema():
    tools = {t.name: t for t in asyncio.run(srv.mcp.list_tools())}
    schema = tools["locus_list_sources"].input_schema
    assert schema["properties"]["prefix"].get("deprecated") is True
    assert "prefix" not in schema.get("required", [])


def test_the_alias_scopes_the_listing_like_path_prefix_and_says_it_is_deprecated(server):
    via_alias = asyncio.run(srv.locus_list_sources(prefix=str(server / "sub")))
    via_name = asyncio.run(srv.locus_list_sources(path_prefix=str(server / "sub")))
    assert "a.md" in via_alias and "b.md" not in via_alias and "total: 1 file(s) in scope" in via_alias
    assert "`prefix` is deprecated" in via_alias and "deprecated" not in via_name


def test_two_different_folders_in_the_two_names_are_refused(server):
    out = asyncio.run(srv.locus_list_sources(path_prefix=str(server / "sub"), prefix=str(server)))
    assert out.startswith("Error: path_prefix and prefix") and "a.md" not in out


def test_the_same_folder_in_both_names_is_accepted_whatever_its_trailing_slash(server):
    out = asyncio.run(srv.locus_list_sources(path_prefix=str(server / "sub"), prefix=str(server / "sub") + "/"))
    assert out.startswith("# Indexed files under") and "total: 1 file(s) in scope" in out
