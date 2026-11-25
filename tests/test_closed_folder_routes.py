"""Routes tried against a closed folder: none of them may hand over a Downloads file the user has not opened.

The index below holds Downloads rows (an index made before the folder was closed) and `policy.toml` has no entry for
it, so the folder is closed. Each test is one way of asking for those bytes through one door: the MCP tools, the engine
behind the HTTP API, the JSON commands, the older text command, a link, a `..`, a different case, a decomposed name, a
hard link, the watcher, the freshness step. Offline: HashEmbedder, a fake home.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import threading
import time
import unicodedata
from pathlib import Path

import pytest

pytest.importorskip("chromadb")

from locus import cli, closed_folders, service  # noqa: E402
from locus import mcp_server as srv  # noqa: E402
from locus.config import DEFAULT_EXTENSIONS, Settings  # noqa: E402
from locus.ingest.embedder import HashEmbedder  # noqa: E402
from locus.ingest.freshness import freshen_for_serve  # noqa: E402
from locus.ingest.indexer import index_folder  # noqa: E402
from locus.ingest.store import ChromaStore  # noqa: E402

SECRET = "the statement balance is nine hundred and fourteen quokka dollars"
QUERY = "statement balance quokka dollars"
QUIET = lambda _m: None  # noqa: E731


class World:
    def __init__(self, tmp_path: Path, monkeypatch):
        self.home = (tmp_path / "home").resolve()
        self.downloads = self.home / "Downloads"
        self.settings = Settings(
            data_dir=tmp_path / "data", collection="routes", embedder="hash", voyage_model="", openai_model="",
            local_model="", chunk_tokens=100, overlap_tokens=10, max_file_mb=5, extensions=DEFAULT_EXTENSIONS,
        )  # fmt: skip
        for rel, text in {"Downloads/statement.md": SECRET, "Documents/plan.md": "the launch plan"}.items():
            f = self.home / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text, encoding="utf-8")
        self.statement = self.downloads / "statement.md"
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: self.home))
        monkeypatch.setenv("HOME", str(self.home))
        monkeypatch.setenv("LOCUS_DATA_DIR", str(self.settings.data_dir))
        monkeypatch.setenv("LOCUS_EMBEDDER", "hash")
        monkeypatch.setenv("LOCUS_COLLECTION", "routes")
        from locus.config import get_settings

        get_settings.cache_clear()
        self.embedder = HashEmbedder()
        # The migration has run already (it would have opened Downloads for this old index and said so): the folder is
        # closed because the user closed it again, which is the state every route below is tried against.
        closed_folders._write_marker(self.settings.data_dir, {"version": 1, "collections": {"routes": {"opened": []}}})
        self.store = ChromaStore(self.settings.data_dir, self.settings.collection, self.embedder.name)
        index_folder(self.home, self.embedder, self.store, self.settings, log=QUIET)  # the old way: no closed folder
        assert str(self.statement) in self.store.list_paths()
        components = (self.embedder, self.store, self.settings)
        monkeypatch.setattr(srv, "_components", lambda: components)
        monkeypatch.setattr(srv, "freshen_for_serve", lambda *a, **k: None)
        monkeypatch.setattr(service, "freshen_for_serve", lambda *a, **k: None)
        monkeypatch.setattr(service, "_open_default", lambda: components)
        monkeypatch.setattr(cli, "get_settings", lambda: self.settings)
        monkeypatch.setattr(cli, "_components", lambda _s: (self.embedder, self.store))
        self.engine = service.Engine(lambda: components)

    def names(self) -> set[str]:
        return {Path(p).name for p in ChromaStore(self.settings.data_dir, "routes", self.embedder.name).list_paths()}


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def clean(out: str, echo: str = "") -> bool:
    """No text of the file and no mention of its name. A caller's own path or prefix is echoed back in a message, so
    that exact string is taken out before the name is looked for."""
    shown = out.replace(repr(echo), "").replace(echo, "") if echo else out
    return SECRET not in out and "nine hundred" not in out and "statement.md" not in shown


nameless = clean


def run(capsys, *argv):
    capsys.readouterr()
    with pytest.raises(SystemExit) as stop:
        cli.main(list(argv))
    captured = capsys.readouterr()
    return stop.value.code, captured.out + captured.err


def spellings(w: World) -> list[str]:
    """Other ways to write the path of the file or its folder."""
    d, f = str(w.downloads), str(w.statement)
    return [
        f,
        f.replace("/Downloads/", "/DOWNLOADS/"),
        f.replace("/Downloads/", "/downloads/"),
        unicodedata.normalize("NFD", f),
        f.replace("/Downloads/", "/Documents/../Downloads/"),
        f.replace("/Downloads/", "//Downloads/"),
        d,
        d + "/",
        d.replace("/Downloads", "/Documents/../Downloads"),
        d.upper(),
        unicodedata.normalize("NFD", d),
    ]


# --- the MCP tools --------------------------------------------------------------------------------------------


def test_search_by_text_by_prefix_and_by_date_window(world):
    assert nameless(asyncio.run(srv.locus_search(query=QUERY)))
    assert nameless(asyncio.run(srv.locus_search(modified_after="2000-01-01")))
    for prefix in spellings(world):
        assert clean(asyncio.run(srv.locus_search(query=QUERY, path_prefix=prefix)), prefix), prefix
        assert clean(asyncio.run(srv.locus_search(modified_after="2000-01-01", path_prefix=prefix)), prefix), prefix


def test_listing_with_and_without_a_prefix_names_nothing_and_counts_nothing(world):
    out = asyncio.run(srv.locus_list_sources())
    assert nameless(out) and "total: 1 file(s)" in out
    for prefix in spellings(world):
        listed = asyncio.run(srv.locus_list_sources(path_prefix=prefix))
        assert clean(listed, prefix) and "total: 0 file(s)" in listed, prefix
        assert clean(asyncio.run(srv.locus_list_sources(prefix=prefix)), prefix), prefix  # the deprecated alias


def test_read_by_every_spelling_of_the_path(world):
    for path in spellings(world):
        out = asyncio.run(srv.locus_read_file(path=path))
        assert out.startswith("Error:") and SECRET not in out, path


def test_a_search_for_the_exact_file_returns_no_chunk(world):
    out = asyncio.run(srv.locus_search(query="statement balance quokka", path_prefix=str(world.statement)))
    assert "nine hundred" not in out and clean(out, str(world.statement))


# --- the engine behind the API, and the command line ----------------------------------------------------------


def test_the_engine_for_every_spelling(world):
    assert all(h.path != str(world.statement) for h in world.engine.search(QUERY).results)
    for prefix in spellings(world):
        assert world.engine.search(QUERY, path_prefix=prefix).results == []
        assert world.engine.list_sources(prefix).paths == []
    for path in spellings(world):
        with pytest.raises(service.ServiceError):
            world.engine.read_file(path)


def test_the_json_commands_and_the_text_command(world, capsys):
    assert nameless(run(capsys, "query", QUERY, "--json")[1])
    assert nameless(run(capsys, "query", QUERY)[1])
    assert nameless(run(capsys, "sources", "--json")[1]) and nameless(run(capsys, "sources")[1])
    for prefix in spellings(world):
        assert clean(run(capsys, "query", QUERY, "--prefix", prefix, "--json")[1], prefix), prefix
        code, out = run(capsys, "read", prefix, "--json")
        assert code == cli.EXIT_REFUSED and SECRET not in out, prefix


# --- links ----------------------------------------------------------------------------------------------------


def test_a_link_in_documents_that_points_into_downloads_is_never_indexed_nor_readable(world, capsys):
    link = world.home / "Documents" / "via-link"
    link.symlink_to(world.downloads, target_is_directory=True)
    file_link = world.home / "Documents" / "statement-link.md"
    file_link.symlink_to(world.statement)
    index_folder(world.home / "Documents", world.embedder, world.store, world.settings, log=QUIET)
    assert "statement-link.md" not in world.names()
    for path in (str(link / "statement.md"), str(file_link)):
        assert asyncio.run(srv.locus_read_file(path=path)).startswith("Error:")
    assert clean(asyncio.run(srv.locus_search(query=QUERY, path_prefix=str(link))))
    assert run(capsys, "index", str(link))[0] == cli.EXIT_NEEDS_YES  # naming the link names the closed folder


def test_a_hard_link_is_skipped_by_default(world):
    twin = world.home / "Documents" / "twin.md"
    os.link(world.statement, twin)
    index_folder(world.home / "Documents", world.embedder, world.store, world.settings, log=QUIET)
    assert "twin.md" not in world.names()


def test_a_hard_link_is_indexed_when_the_user_allows_hard_links_and_that_is_the_one_path_rule_that_cannot_stop(
    world, monkeypatch
):
    """KNOWN LIMIT, documented: the rules are path rules. With LOCUS_ALLOW_HARD_LINKS=1 the second name of a file that
    also lives in Downloads is just a file in Documents. This test pins that, so the docs and the code agree."""
    monkeypatch.setenv("LOCUS_ALLOW_HARD_LINKS", "1")
    twin = world.home / "Documents" / "twin.md"
    os.link(world.statement, twin)
    index_folder(world.home / "Documents", world.embedder, world.store, world.settings, log=QUIET)
    assert "twin.md" in world.names()


# --- the ways in ----------------------------------------------------------------------------------------------


def test_indexing_a_parent_in_every_way_leaves_downloads_out(world, capsys, tmp_path, monkeypatch):
    fresh = ChromaStore(tmp_path / "fresh-data", "routes", world.embedder.name)  # a new index: nothing in it yet
    fresh_settings = dataclasses.replace(world.settings, data_dir=tmp_path / "fresh-data")
    monkeypatch.setattr(cli, "get_settings", lambda: fresh_settings)
    monkeypatch.setattr(cli, "_components", lambda _s: (world.embedder, fresh))
    for argv in (
        ("index", str(world.home), "--all", "--yes"),
        ("index", "--roots", str(world.home), "--yes", "--all"),
        ("index", str(world.home / "Documents" / ".."), "--all", "--yes"),
    ):
        assert run(capsys, *argv)[0] == 0
        assert {Path(p).name for p in fresh.list_paths()} == {"plan.md"}, argv


def test_a_prune_over_the_parent_removes_the_hidden_rows_for_good_and_says_how_many_files_are_closed(world, capsys):
    code, out = run(capsys, "index", str(world.home), "--all", "--prune", "--yes")
    assert code == 0 and world.names() == {"plan.md"}
    assert "file(s) in ~/Downloads are closed" in out


def test_the_watcher_on_the_parent_does_not_pick_up_a_new_downloads_file(world, capsys):
    def watch() -> None:
        try:
            cli.main(["watch", str(world.home), "--all", "--yes"])
        except BaseException:  # noqa: BLE001 - abandoned when the test ends, as in test_watcher.py
            pass

    threading.Thread(target=watch, daemon=True).start()
    time.sleep(3.0)  # the initial pass (which prunes) and the observer start
    (world.downloads / "fresh.md").write_text("a brand new downloaded file about marmots", encoding="utf-8")
    (world.home / "Documents" / "fresh-doc.md").write_text("a brand new document about marmots", encoding="utf-8")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and "fresh-doc.md" not in world.names():
        time.sleep(0.1)
    assert "fresh-doc.md" in world.names()  # the watcher works...
    assert "fresh.md" not in world.names()  # ...and leaves Downloads alone


def test_the_freshness_step_does_not_index_a_new_downloads_file(world):
    (world.downloads / "fresh.md").write_text("a brand new downloaded file about marmots", encoding="utf-8")
    (world.home / "Documents" / "fresh-doc.md").write_text("a brand new document about marmots", encoding="utf-8")
    freshen_for_serve(world.embedder, world.store, world.settings)
    assert "fresh-doc.md" in world.names() and "fresh.md" not in world.names()


def test_opening_then_closing_by_hand_hides_it_again_at_once(world, capsys):
    policy = world.settings.data_dir / "policy.toml"
    closed_folders.append_opt_in(policy, world.downloads, "test", world.home)
    assert SECRET in asyncio.run(srv.locus_search(query=QUERY))
    policy.write_text("version = 1\n", encoding="utf-8")
    assert clean(asyncio.run(srv.locus_search(query=QUERY)))


def test_a_downloads_that_became_a_link_after_the_rules_were_loaded_is_still_closed(world, tmp_path):
    """The guard is cached per policy file; where `~/Downloads` leads is part of what it is cached for."""
    assert nameless(asyncio.run(srv.locus_search(query=QUERY)))  # loads and keeps the guard
    moved = tmp_path / "moved-away"
    world.downloads.rename(moved)
    world.downloads.symlink_to(moved, target_is_directory=True)
    store = world.store
    store.upsert(
        ids=["moved:0"],
        embeddings=[world.embedder.embed_query("x")],
        documents=["a row whose path is where the link now leads: moved-away " + SECRET],
        metadatas=[
            {"path": str(moved / "late.md"), "chunk_index": 0, "n_chunks": 1, "file_hash": "x", "mtime": 1.0,
             "root": str(moved)}
        ],
    )  # fmt: skip
    assert "late.md" not in asyncio.run(srv.locus_search(query=QUERY))
    assert "late.md" not in asyncio.run(srv.locus_list_sources())
