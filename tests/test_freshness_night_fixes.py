"""Regression tests for the night fixes to freshness (day-3 review F1, F2, F3, F7, F8). HashEmbedder, tmp folders."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import locus.ingest.freshness as fr
from locus.config import DEFAULT_EXTENSIONS, Settings
from locus.ingest.embedder import HashEmbedder
from locus.ingest.freshness import freshen, render_freshness_note
from locus.ingest.indexer import index_folder
from locus.ingest.multiroot import Stage, index_roots
from locus.ingest.store import ChromaStore
from locus.policy import Matcher, Policy

QUIET = lambda _msg: None  # noqa: E731


class Counting(HashEmbedder):
    def __init__(self) -> None:
        super().__init__()
        self.documents = 0

    def embed_documents(self, texts):
        self.documents += len(texts)
        return super().embed_documents(texts)


@pytest.fixture
def world(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data", collection="night", embedder="hash", voyage_model="", openai_model="",
        local_model="", chunk_tokens=100, overlap_tokens=10, max_file_mb=5, extensions=DEFAULT_EXTENSIONS,
    )  # fmt: skip
    embedder = Counting()
    return settings, embedder, ChromaStore(settings.data_dir, settings.collection, embedder.name), tmp_path


def _matcher(roots, home):
    return Matcher(Policy(roots=tuple(str(r) for r in roots)), home)


def _folder(base: Path, name: str, files: dict[str, str]) -> Path:
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    for n, text in files.items():
        (d / n).write_text(text)
    return d


# F1 -------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("embedder_name", ["voyage", "openai", "managed"])
def test_hosted_embedder_switches_freshness_off_entirely_and_says_so(world, embedder_name):
    settings, embedder, store, tmp = world
    chosen = _folder(tmp, "chosen", {"a.md": "alpha plan for the zebra release", "b.md": "second", "c.md": "third"})
    other = _folder(tmp, "other", {"diary.md": "never indexed before"})
    index_folder(chosen, embedder, store, settings, log=QUIET)
    (chosen / "new.md").write_text("added in an indexed folder")  # even this is not embedded now
    (chosen / "b.md").unlink()  # nor is a single deletion applied
    before = sorted(store.list_paths())
    embedder.documents = 0
    hosted = replace(settings, embedder=embedder_name)
    report = freshen([chosen, other], _matcher([chosen, other], tmp), embedder, store, hosted, budget_s=5.0)
    assert embedder.documents == 0 and report.indexed == 0 and report.removed == 0 and report.hosted_off
    assert sorted(store.list_paths()) == before
    note = render_freshness_note(report, 5.0) or ""
    assert note.startswith("Freshness note:") and "hosted" in note and "`locus index <folder>`" in note
    assert not any(ch.isdigit() for ch in note) and "diary" not in note and str(other) not in note  # counts-free


def test_local_embedder_has_no_hosted_note(world):
    settings, embedder, store, tmp = world
    chosen = _folder(tmp, "chosen", {"a.md": "alpha plan for the zebra release"})
    report = freshen([chosen], _matcher([chosen], tmp), embedder, store, settings, budget_s=5.0)
    assert not report.hosted_off and render_freshness_note(report, 2.0) is None


def test_local_embedder_still_indexes_a_new_root(world):
    settings, embedder, store, tmp = world
    chosen = _folder(tmp, "chosen", {"a.md": "alpha plan for the zebra release"})
    freshen([chosen], _matcher([chosen], tmp), embedder, store, settings, budget_s=5.0)
    assert len(store.list_paths()) == 1


# F2 -------------------------------------------------------------------------------------------------------------
def test_no_chunk_marker_survives_a_new_process_and_resets_when_the_file_changes(world, monkeypatch):
    import locus.ingest.indexer as indexer_module

    settings, embedder, store, tmp = world
    root = _folder(tmp, "docs", {"empty.txt": "", "real.md": "Alpha plan: ship the zebra release in March."})
    (root / "broken.docx").write_bytes(b"not a zip")
    matcher = _matcher([root], tmp)
    freshen([root], matcher, embedder, store, settings, budget_s=5.0)
    assert (settings.data_dir / "freshness-seen-night.json").exists()
    parses: list[str] = []
    real = indexer_module.parse_document
    monkeypatch.setattr(indexer_module, "parse_document", lambda p, **kw: (parses.append(p.name), real(p, **kw))[1])
    again = freshen([root], matcher, embedder, store, settings, budget_s=5.0)  # a fresh pass reads the marker file
    assert parses == [] and again.errors == 0 and not again.cut_short
    (root / "empty.txt").write_text("now it has words")
    freshen([root], matcher, embedder, store, settings, budget_s=5.0)
    assert parses == ["empty.txt"] and any(p.endswith("empty.txt") for p in store.list_paths())


def test_one_slow_empty_file_is_parsed_once_not_on_every_call(world, monkeypatch):
    import os
    import time

    import locus.ingest.indexer as indexer_module

    settings, embedder, store, tmp = world
    root = _folder(tmp, "docs", {"a_scan.md": "  ", "z_real.md": "Alpha plan: ship the zebra release in March."})
    newer = time.time() + 60  # changed files are indexed newest first: make the slow one the newest
    os.utime(root / "a_scan.md", (newer, newer))
    ticks = {"now": 0.0}
    real = indexer_module.parse_document

    def slow(path, **kw):
        if path.name == "a_scan.md":
            ticks["now"] += 10.0  # eats the whole budget, like a scanned PDF
        return real(path, **kw)

    monkeypatch.setattr(indexer_module, "parse_document", slow)
    matcher = _matcher([root], tmp)
    first = freshen([root], matcher, embedder, store, settings, 2.0, clock=lambda: ticks["now"])
    assert first.pending == 1  # the real file was not reached on the first call
    second = freshen([root], matcher, embedder, store, settings, 2.0, clock=lambda: ticks["now"])
    assert second.pending == 0 and second.indexed == 1  # ...but is on the second: the slow one is not retried


def test_marker_is_not_written_for_a_run_stopping_embed_error(world):
    from locus.ingest.errors import RateLimited

    settings, _embedder, store, tmp = world

    class Failing(HashEmbedder):
        def embed_documents(self, texts):
            raise RateLimited("provider", retry_after=1)

    root = _folder(tmp, "docs", {"a.md": "Alpha plan: ship the zebra release in March."})
    report = freshen([root], _matcher([root], tmp), Failing(), store, settings, budget_s=5.0)
    assert report.errors == 1
    assert not (settings.data_dir / "freshness-seen-night.json").exists()  # retried next time, as it should


# F3 -------------------------------------------------------------------------------------------------------------
def test_freshness_skips_aliases_even_after_they_are_touched(world):
    settings, embedder, store, tmp = world
    root = tmp / "home"
    body = "Lease agreement between the parties, term twelve months, rent due on the first."
    _folder(root, "contracts", {"lease.md": body})
    _folder(root, "downloads", {"lease.md": body})
    assert index_roots([root], embedder, store, settings, _matcher([root], tmp), Stage(), log=QUIET).duplicates == 1
    matcher = _matcher([root], tmp)
    freshen([root], matcher, embedder, store, settings, budget_s=5.0)
    assert len(store.list_paths()) == 1
    for p in root.rglob("lease.md"):  # touch both copies: mtime moves, bytes do not
        p.write_text(body)
    freshen([root], matcher, embedder, store, settings, budget_s=5.0)
    assert len(store.list_paths()) == 1


def test_an_alias_that_really_changed_is_indexed(world):
    settings, embedder, store, tmp = world
    root = tmp / "home"
    body = "Lease agreement between the parties, term twelve months, rent due on the first."
    _folder(root, "contracts", {"lease.md": body})
    dup = _folder(root, "downloads", {"lease.md": body})
    index_roots([root], embedder, store, settings, _matcher([root], tmp), Stage(), log=QUIET)
    (dup / "lease.md").write_text("A different document now, about the zebra release.")
    freshen([root], _matcher([root], tmp), embedder, store, settings, budget_s=5.0)
    assert len(store.list_paths()) == 2


# F7 -------------------------------------------------------------------------------------------------------------
def test_explicitly_indexed_library_folder_is_kept_and_counted(world):
    settings, embedder, store, tmp = world
    home = tmp / "home"
    root = _folder(home / "Library" / "Mobile Documents", "notes", {"n.md": "Notes the user pointed index at."})
    index_folder(root, embedder, store, settings, log=QUIET)
    report = freshen([root], _matcher([root], home), embedder, store, settings, budget_s=5.0)
    assert store.list_paths() and report.removed == 0 and report.kept_denied == 1
    note = render_freshness_note(report, 2.0)
    assert note and "1 indexed file(s) now match a deny rule and were kept" in note and "n.md" not in note
    # S-01: the advice must be a command that works: `locus index --prune` without a folder is refused (it falls into
    # the --roots path, which has no --prune), so the note names the folder form.
    assert "`locus index <folder> --prune`" in note and "`locus index --prune`" not in note


def test_a_newly_denied_secret_folder_is_kept_not_purged(world):
    settings, embedder, store, tmp = world
    root = _folder(tmp, "root", {"a.md": "keep me", "b.md": "also keep"})
    secret = _folder(root, "private", {"s1.md": "secret one", "s2.md": "secret two", "s3.md": "secret three"})
    index_folder(root, embedder, store, settings, log=QUIET)
    assert len(store.list_paths()) == 5
    deny = Matcher(Policy(roots=(str(root),), deny_paths=(str(secret),)), tmp)
    report = freshen([root], deny, embedder, store, settings, budget_s=5.0)
    assert report.removed == 0 and report.kept_denied == 3 and len(store.list_paths()) == 5  # (HYPOTHESIS)
    note = render_freshness_note(report, 2.0) or ""
    assert "3 indexed file(s) now match a deny rule" in note and "s1.md" not in note and str(secret) not in note


def test_every_deny_reason_keeps_its_rows(world):
    settings, embedder, store, tmp = world
    root = _folder(tmp, "root", {"ok.md": "fine", "credentials.json": '{"k": "v"}', "server.pem": "-----BEGIN-----"})
    permissive = Matcher(Policy(roots=(str(root),)), tmp)
    assert permissive.decide(root / "credentials.json").allowed is False  # the default secret-file rule applies now
    # indexed before the rule existed: index without the matcher
    index_folder(root, embedder, store, settings, log=QUIET)
    before = sorted(store.list_paths())
    report = freshen([root], permissive, embedder, store, settings, budget_s=5.0)
    assert sorted(store.list_paths()) == before and report.removed == 0 and report.kept_denied >= 1


def test_a_denied_row_whose_file_is_really_gone_is_removed(world):
    settings, embedder, store, tmp = world
    root = _folder(tmp, "root", {"a.md": "keep me", "b.md": "also keep", "c.md": "and me", "d.md": "me too"})
    secret = _folder(root, "private", {"s1.md": "secret one"})
    index_folder(root, embedder, store, settings, log=QUIET)
    (secret / "s1.md").unlink()
    deny = Matcher(Policy(roots=(str(root),), deny_paths=(str(secret),)), tmp)
    report = freshen([root], deny, embedder, store, settings, budget_s=5.0)
    assert report.removed == 1 and report.kept_denied == 0 and len(store.list_paths()) == 4


def test_a_denied_file_is_not_refreshed_when_it_changes(world):
    settings, embedder, store, tmp = world
    root = _folder(tmp, "root", {"a.md": "keep me"})
    secret = _folder(root, "private", {"s.md": "old secret text"})
    index_folder(root, embedder, store, settings, log=QUIET)
    (secret / "s.md").write_text("brand new text about zebras")
    deny = Matcher(Policy(roots=(str(root),), deny_paths=(str(secret),)), tmp)
    before = embedder.documents
    freshen([root], deny, embedder, store, settings, budget_s=5.0)
    assert embedder.documents == before and len(store.list_paths()) == 2


def test_no_freshness_pass_deletes_most_of_a_root_even_when_the_files_are_denied(world):
    settings, embedder, store, tmp = world
    root = _folder(tmp, "vol", {f"f{i}.md": f"file number {i} about zebras" for i in range(4)})
    index_folder(root, embedder, store, settings, log=QUIET)
    root.rename(tmp / "elsewhere")
    root.mkdir()
    deny = Matcher(Policy(roots=(str(root),), deny_paths=(str(tmp / "elsewhere"),)), tmp)
    report = freshen([root], deny, embedder, store, settings, budget_s=5.0)
    assert len(store.list_paths()) == 4 and report.removed == 0


# F8 -------------------------------------------------------------------------------------------------------------
def test_emptied_root_keeps_its_rows_and_says_so(world):
    settings, embedder, store, tmp = world
    root = _folder(tmp, "vol", {f"f{i}.md": f"file number {i} about zebras" for i in range(4)})
    index_folder(root, embedder, store, settings, log=QUIET)
    root.rename(tmp / "elsewhere")
    root.mkdir()
    report = freshen([root], _matcher([root], tmp), embedder, store, settings, budget_s=5.0)
    assert len(store.list_paths()) == 4 and report.removed == 0 and report.removals_refused == 4
    assert "kept in the index" in (render_freshness_note(report, 2.0) or "")


def test_a_minority_of_deleted_files_is_still_removed(world):
    settings, embedder, store, tmp = world
    root = _folder(tmp, "vol", {f"f{i}.md": f"file number {i} about zebras" for i in range(4)})
    index_folder(root, embedder, store, settings, log=QUIET)
    (root / "f0.md").unlink()
    report = freshen([root], _matcher([root], tmp), embedder, store, settings, budget_s=5.0)
    assert report.removed == 1 and len(store.list_paths()) == 3


def test_one_pass_removes_at_most_the_cap(world, monkeypatch):
    monkeypatch.setattr(fr, "_MAX_REMOVALS_PER_PASS", 2)
    settings, embedder, store, tmp = world
    root = _folder(tmp, "vol", {f"f{i}.md": f"file number {i} about zebras" for i in range(10)})
    index_folder(root, embedder, store, settings, log=QUIET)
    for i in range(4):  # 4 of 10: under half, over the cap
        (root / f"f{i}.md").unlink()
    first = freshen([root], _matcher([root], tmp), embedder, store, settings, budget_s=5.0)
    assert first.removed == 2 and first.removals_refused == 2
    second = freshen([root], _matcher([root], tmp), embedder, store, settings, budget_s=5.0)
    assert second.removed == 2 and len(store.list_paths()) == 6


def test_an_unstatable_file_blocks_deletion_like_an_unreadable_folder(world, monkeypatch):
    settings, embedder, store, tmp = world
    root = _folder(tmp, "vol", {f"f{i}.md": f"file number {i} about zebras" for i in range(4)})
    index_folder(root, embedder, store, settings, log=QUIET)
    (root / "f0.md").unlink()
    real_iter = fr.iter_files

    def flaky(r, s, on_skip=None, policy=None):
        yield from real_iter(r, s, on_skip, policy)
        on_skip("unreadable", root / "f1.md")  # as if stat() had failed on one file

    monkeypatch.setattr(fr, "iter_files", flaky)
    report = freshen([root], _matcher([root], tmp), embedder, store, settings, budget_s=5.0)
    assert not report.scan_complete and report.removed == 0


def test_freshen_for_serve_returns_the_hosted_note(world):
    settings, embedder, store, tmp = world
    chosen = _folder(tmp, "chosen", {"a.md": "alpha plan for the zebra release"})
    index_folder(chosen, embedder, store, settings, log=QUIET)
    note = fr.freshen_for_serve(embedder, store, replace(settings, embedder="voyage"), env={})
    assert note and note.startswith("Freshness note:") and "hosted" in note
