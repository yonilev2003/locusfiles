"""R2: the folder rules are checked again on every path a file tool is about to return.

A rule added after a file was indexed applies at once, with no re-index and no restart, to `locus_search`,
`locus_list_sources` (and its `total`), `locus_read_file`, the engine behind the HTTP API and the `locus query`,
`sources` and `read` commands.
Offline: HashEmbedder, a fake home, tmp folders.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import unicodedata
from pathlib import Path

import pytest

pytest.importorskip("chromadb")

from locus import cli, restrictions, service  # noqa: E402
from locus import mcp_server as srv  # noqa: E402
from locus.config import DEFAULT_EXTENSIONS, Settings  # noqa: E402
from locus.ingest.embedder import HashEmbedder  # noqa: E402
from locus.ingest.indexer import index_folder  # noqa: E402
from locus.ingest.store import ChromaStore  # noqa: E402

QUIET = lambda _msg: None  # noqa: E731
DIARY = "a private diary entry about zebras and the secret garden"
PLAN = "the launch plan about zebras and pricing"


class World:
    def __init__(self, tmp_path: Path, monkeypatch):
        self.home = (tmp_path / "home").resolve()
        self.home.mkdir()
        self.settings = Settings(
            data_dir=tmp_path / "data", collection="rules", embedder="hash", voyage_model="", openai_model="",
            local_model="", chunk_tokens=100, overlap_tokens=10, max_file_mb=5, extensions=DEFAULT_EXTENSIONS,
        )  # fmt: skip
        self.embedder = HashEmbedder()
        self.store = ChromaStore(self.settings.data_dir, self.settings.collection, self.embedder.name)
        self.notes = self.home / "notes"
        for rel, text in {
            "plan.md": PLAN,
            "private/diary.md": DIARY,
            "work/report.md": "quarterly report on zebras",
        }.items():
            f = self.notes / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text, encoding="utf-8")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: self.home))
        components = (self.embedder, self.store, self.settings)
        monkeypatch.setattr(srv, "_components", lambda: components)
        monkeypatch.setattr(srv, "freshen_for_serve", lambda *a, **k: None)
        monkeypatch.setattr(service, "freshen_for_serve", lambda *a, **k: None)
        monkeypatch.setattr(service, "_open_default", lambda: components)
        monkeypatch.setattr(cli, "get_settings", lambda: self.settings)
        monkeypatch.setattr(cli, "_components", lambda _s: (self.embedder, self.store))
        self.engine = service.Engine(lambda: components)

    def index(self, folder: Path | None = None) -> None:
        index_folder(folder or self.notes, self.embedder, self.store, self.settings, log=QUIET)

    def policy(self, text: str | None) -> None:
        f = self.settings.data_dir / "policy.toml"
        f.parent.mkdir(parents=True, exist_ok=True)
        if text is None:
            f.unlink(missing_ok=True)
        else:
            f.write_text(text, encoding="utf-8")
            os.utime(f, ns=(time.time_ns() + 5_000_000_000, time.time_ns() + 5_000_000_000))  # a different time, always

    def search(self, query: str, **kw) -> str:
        return asyncio.run(srv.locus_search(query=query, **kw))

    def sources(self, **kw) -> str:
        return asyncio.run(srv.locus_list_sources(**kw))

    def read(self, path: str, **kw) -> str:
        return asyncio.run(srv.locus_read_file(path=path, **kw))


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    w.index()
    return w


def run(capsys, *argv):
    capsys.readouterr()
    with pytest.raises(SystemExit) as stop:
        cli.main(list(argv))
    captured = capsys.readouterr()
    return stop.value.code, captured.out, captured.err


# --- a rule added after indexing ------------------------------------------------------------------------------


def test_a_rule_added_after_indexing_hides_the_file_everywhere_and_removing_it_brings_it_back(world):
    diary = str(world.notes / "private" / "diary.md")
    before = world.sources()
    assert "total: 3 file(s)" in before and diary in before and diary in world.search("diary")

    world.policy(f'[deny]\npaths = ["{world.notes / "private"}"]\n')  # the rows are still in the index
    found = world.search("diary zebras garden")
    assert str(world.notes / "private") not in found and DIARY not in found
    assert "1 result(s) hidden by your folder rules" in found
    listing = world.sources()
    assert diary not in listing and "total: 2 file(s)" in listing
    assert "1 indexed file(s) hidden by your folder rules" in listing
    refused = world.read(diary)
    assert refused.startswith("Error:") and "a path the user denied" in refused and DIARY not in refused
    assert str(world.notes / "plan.md") in world.search("launch plan")  # the rest answers as before

    world.policy(None)  # the rule goes: the same calls work again, no re-index, no restart
    assert diary in world.search("diary zebras garden") and "total: 3 file(s)" in world.sources()
    assert DIARY in world.read(diary)
    assert len(world.store.list_paths()) == 3  # nothing was ever removed from the index


def test_an_excluded_folder_is_hidden_at_once(world):
    world.policy(f'[[folder]]\npath = "{(world.notes / "work").as_posix()}"\nprofile = "excluded"\n')
    assert str(world.notes / "work") not in world.sources() and "quarterly report" not in world.search("quarterly")
    assert "a folder the user excluded" in world.read(str(world.notes / "work" / "report.md"))


def test_a_file_the_deny_list_would_refuse_today_is_hidden_even_if_it_was_indexed_earlier(world):
    secret = world.notes / "passwords.txt"
    secret.write_text("bank login hunter2 zebras", encoding="utf-8")
    world.index()  # indexed with no deny-list, as an older version did
    assert str(secret) in world.store.list_paths()
    assert str(secret) not in world.sources() and "hunter2" not in world.search("bank login zebras")
    assert "built-in deny-list" in world.read(str(secret))


def test_files_outside_the_rule_come_back_with_their_path_and_chunk_index(world):
    world.policy(f'[deny]\npaths = ["{world.notes / "private"}"]\n')
    found = world.search("launch plan zebras pricing")
    assert f"{world.notes / 'plan.md'}  (chunk 0" in found and PLAN in found


def test_the_policy_file_is_noticed_without_a_restart_even_when_rewritten_at_once(world):
    diary = str(world.notes / "private" / "diary.md")
    for _ in range(3):
        world.policy(f'[deny]\npaths = ["{world.notes / "private"}"]\n')
        assert diary not in world.sources()
        world.policy("")
        assert diary in world.sources()


def test_a_broken_policy_file_returns_the_error_and_no_text_but_help_and_diagnose_still_answer(world):
    world.policy('[[folder]]\npath = "~/x"\nprofil = "untrusted"\n')
    for out in (world.search("diary"), world.sources(), world.read(str(world.notes / "plan.md"))):
        assert "cannot read its folder rules" in out and "profil" in out
        assert "zebras" not in out and str(world.notes) not in out
    assert "cannot read" not in asyncio.run(srv.locus_help(query="how do I install Locus"))
    assert "# Locus diagnostics" in asyncio.run(srv.locus_diagnose_connection())
    world.policy(None)
    assert PLAN in world.search("launch plan zebras pricing")


def test_the_most_relevant_rows_hidden_do_not_leave_a_short_page(world):
    for i in range(12):  # a dozen rows that match "zebra" best, all under the hidden folder
        (world.notes / "private" / f"z{i}.md").write_text("zebra zebra zebra zebra zebra", encoding="utf-8")
    for i in range(4):  # and a few plainer ones that stay visible
        (world.notes / "work" / f"v{i}.md").write_text(f"a zebra at the zoo number {i}", encoding="utf-8")
    world.index()
    world.policy(f'[deny]\npaths = ["{world.notes / "private"}"]\n')
    found = world.search("zebra zebra zebra zebra zebra", top_k=3)
    assert found.count("\n## ") == 3 and str(world.notes / "private") not in found
    assert "result(s) hidden by your folder rules" in found


def test_a_date_window_listing_hides_the_file_and_counts_only_what_it_shows(world):
    world.policy(f'[deny]\npaths = ["{world.notes / "private"}"]\n')
    out = asyncio.run(srv.locus_search(modified_after="2000-01-01", top_k=1))
    assert str(world.notes / "private") not in out
    assert "(2 file(s) in the window in all: that is the count)" in out


def test_100_hidden_files_do_not_change_the_page_size_of_a_listing(world):
    for i in range(100):
        (world.notes / "private" / f"n{i}.md").write_text(f"note {i}", encoding="utf-8")
    world.index()
    world.policy(f'[deny]\npaths = ["{world.notes / "private"}"]\n')
    assert "total: 2 file(s)" in world.sources(limit=1000)


# --- the other doors ------------------------------------------------------------------------------------------


def test_the_engine_behind_the_api_and_json_commands_applies_the_same_rules(world):
    diary = str(world.notes / "private" / "diary.md")
    world.policy(f'[deny]\npaths = ["{world.notes / "private"}"]\n')
    result = world.engine.search("diary zebras garden")
    assert diary not in [h.path for h in result.results] and "hidden by your folder rules" in (result.note or "")
    sources = world.engine.list_sources()
    assert diary not in sources.paths and sources.total == 2 and sources.indexed_total == 2
    with pytest.raises(service.ServiceError) as err:
        world.engine.read_file(diary)
    assert err.value.code == "confinement_refused" and "a path the user denied" in err.value.message
    assert DIARY not in err.value.message
    world.policy(None)
    assert DIARY in world.engine.read_file(diary).text


def test_a_broken_policy_stops_the_engine_with_a_stable_code(world):
    world.policy("this is [not toml")
    for call in (lambda: world.engine.search("x"), world.engine.list_sources, lambda: world.engine.read_file("/x")):
        with pytest.raises(service.ServiceError) as err:
            call()
        assert err.value.code == "policy_error" and "cannot read its folder rules" in err.value.message


def test_the_command_line_applies_the_same_rules(world, capsys):
    diary = str(world.notes / "private" / "diary.md")
    world.policy(f'[deny]\npaths = ["{world.notes / "private"}"]\n')
    code, out, _ = run(capsys, "query", "diary zebras garden", "--json")
    assert code == 0 and diary not in out and DIARY not in out
    code, out, _ = run(capsys, "query", "diary zebras garden")  # the older text form
    assert diary not in out and "diary" not in out.replace("diary zebras garden", "")
    code, out, _ = run(capsys, "sources", "--json")
    assert diary not in json.loads(out)["paths"] and json.loads(out)["total"] == 2
    code, out, err = run(capsys, "read", diary, "--json")
    assert code == cli.EXIT_REFUSED and json.loads(out)["error"]["code"] == "confinement_refused" and DIARY not in out
    world.policy("not toml [")
    for argv in (("query", "x", "--json"), ("query", "x"), ("sources",), ("read", diary)):
        code, out, err = run(capsys, *argv)
        assert code in (cli.EXIT_NOT_CONFIGURED, cli.EXIT_REFUSED) and "zebras" not in out


# --- other ways to the same bytes -----------------------------------------------------------------------------


def test_a_folder_swapped_for_a_link_into_a_hidden_folder_is_refused_even_when_the_target_is_indexed(world):
    """Both rows are in the index; the link makes one name lead to the other. Hiding the second hides the way to it."""
    (world.notes / "work" / "mirror.md").write_text(DIARY, encoding="utf-8")
    world.index()
    world.policy(f'[deny]\npaths = ["{world.notes / "private"}"]\n')
    moved = world.notes / "work-real"
    (world.notes / "work").rename(moved)
    (world.notes / "work").symlink_to(world.notes / "private", target_is_directory=True)
    (world.notes / "private" / "mirror.md").write_text(DIARY, encoding="utf-8")
    out = world.read(str(world.notes / "work" / "mirror.md"))
    assert out.startswith("Error:") and DIARY not in out
    with pytest.raises(service.ServiceError):
        world.engine.read_file(str(world.notes / "work" / "mirror.md"))


def test_a_dot_dot_path_is_not_an_indexed_path(world):
    sneaky = str(world.notes / "work" / ".." / "private" / "diary.md")
    world.policy(f'[deny]\npaths = ["{world.notes / "private"}"]\n')
    assert world.read(sneaky).startswith("Error:") and DIARY not in world.read(sneaky)
    assert DIARY not in world.search("diary", path_prefix=str(world.notes / "work" / ".." / "private"))


def test_a_different_case_or_unicode_spelling_of_a_hidden_path_is_hidden_too(world):
    nested = world.notes / "résumé"
    nested.mkdir()
    (nested / "cv.md").write_text("curriculum vitae of the zebra keeper", encoding="utf-8")
    world.index()
    decomposed = unicodedata.normalize("NFD", str(nested))
    world.policy(f'[deny]\npaths = ["{decomposed}"]\n')  # written decomposed, indexed as the file system spelled it
    assert "of the zebra keeper" not in world.search("curriculum vitae zebra keeper")
    world.policy(f'[deny]\npaths = ["{str(world.notes / "PRIVATE")}"]\n')  # a different case
    assert DIARY not in world.search("diary zebras garden") and "diary.md" not in world.sources()


def test_the_matcher_cost_is_small_enough_to_check_a_thousand_paths_in_a_listing(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    w.index()
    paths = [str(w.notes / "bulk" / f"f{i:04d}.md") for i in range(1000)]  # rows only: no file is needed to list one
    w.store.upsert(
        ids=[f"bulk:{i}" for i in range(1000)],
        embeddings=[w.embedder.embed_query(f"bulk file {i}") for i in range(1000)],
        documents=[f"bulk file {i}" for i in range(1000)],
        metadatas=[
            {"path": p, "chunk_index": 0, "n_chunks": 1, "file_hash": "x", "mtime": 1.0, "root": str(w.notes)}
            for p in paths
        ],
    )
    w.policy(f'[deny]\npaths = ["{w.notes / "private"}"]\n')
    started = time.perf_counter()
    out = w.sources(limit=1000)
    first = time.perf_counter() - started
    assert "total: 1002 file(s)" in out
    started = time.perf_counter()
    w.sources(limit=1000)
    assert first < 1.0 and time.perf_counter() - started < 1.0  # the second call answers from the kept decisions


def test_the_guard_is_not_confused_by_a_policy_with_roots(world):
    """`roots` decide what an index run walks; they must not hide an indexed file that lies outside them."""
    world.policy('roots = ["~/somewhere-else"]\n')
    assert world.search("launch plan zebras pricing").count(str(world.notes / "plan.md")) >= 1
    assert restrictions.open_gate(world.settings.data_dir).allowed(str(world.notes / "plan.md"))
