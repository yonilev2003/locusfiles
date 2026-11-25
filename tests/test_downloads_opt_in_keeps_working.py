"""The Downloads guarantee: an instance over the user's own Downloads folder for university study keeps working.

One file, so the guarantee is visible and must pass before any change to the closed-folder or query-time rules is
merged. Each test is one invariant:

  A1  after `locus index <Downloads> --yes` every file type indexed before is indexed now (same count as the old code)
  A2  `locus_search` returns Downloads files with path and chunk_index, `locus_read_file` their text, nothing redacted
  A3  rows already under Downloads survive the upgrade, `--prune` and a freshness pass
  A4  `locus watch` on Downloads keeps indexing new files, a file moved in after the watcher started included
  A5  the query-time re-check does not hide an opened Downloads, and hides it at once, reversibly, when the entry goes
  A6  while paused the tools refuse; after `locus resume` they work again in the same process
  A7  a 20-call enumeration across Downloads stays under the volume thresholds (no warning line)
  A8  the labels add lines but leave the chunk text byte-identical

Fake home, HashEmbedder, tmp folders: offline, never the real home directory, never a paid API.
"""

from __future__ import annotations

import asyncio
import dataclasses
import shutil
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("chromadb")

from locus import audit, cli, killswitch, restrictions  # noqa: E402
from locus import mcp_server as srv  # noqa: E402
from locus.config import DEFAULT_EXTENSIONS, Settings, get_settings  # noqa: E402
from locus.ingest.embedder import HashEmbedder  # noqa: E402
from locus.ingest.freshness import freshen_for_serve  # noqa: E402
from locus.ingest.indexer import index_folder  # noqa: E402
from locus.ingest.store import ChromaStore  # noqa: E402

from .pdf_helpers import write_pdf  # noqa: E402

QUIET = lambda _msg: None  # noqa: E731

LECTURE = "lecture one covers eigenvalues, eigenvectors and a very distinctive phrase about zebras"
TEXT_TYPES = tuple(e for e in DEFAULT_EXTENSIONS if e in {
    ".md", ".txt", ".rst", ".py", ".js", ".ts", ".json", ".yaml", ".yml", ".toml", ".csv", ".html", ".markdown",
    ".mdx", ".org", ".tex", ".tsv", ".htm", ".sql", ".sh", ".c", ".h", ".cc", ".cpp", ".hpp", ".go", ".rs",
})  # fmt: skip


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A fake home: Downloads holds one file of every plain-text type, a PDF and the lecture; Documents a plan."""
    home = (tmp_path / "home").resolve()
    downloads = home / "Downloads"
    downloads.mkdir(parents=True)
    for ext in TEXT_TYPES:
        (downloads / f"paper{ext}").write_text(
            f"study material in a {ext} file about graph colouring", encoding="utf-8"
        )
    (downloads / "lecture1.md").write_text(LECTURE, encoding="utf-8")
    write_pdf(downloads / "slides.pdf", [["slides about spectral graph theory"], ["second page of slides"]])
    (home / "Documents").mkdir()
    (home / "Documents" / "plan.md").write_text("the launch plan", encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("LOCUS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LOCUS_EMBEDDER", "hash")
    monkeypatch.setenv("LOCUS_COLLECTION", "study")
    get_settings.cache_clear()
    return home, get_settings()


def run(capsys, *argv):
    capsys.readouterr()
    with pytest.raises(SystemExit) as stop:
        cli.main(list(argv))
    captured = capsys.readouterr()
    return stop.value.code, captured.out, captured.err


def store_of(settings: Settings) -> ChromaStore:
    return ChromaStore(settings.data_dir, settings.collection, HashEmbedder().name)


def wire_mcp(monkeypatch, settings: Settings, store: ChromaStore):
    monkeypatch.setattr(srv, "_components", lambda: (HashEmbedder(), store, settings))
    monkeypatch.setattr(srv, "freshen_for_serve", lambda *a, **k: None)


def wait_until(predicate, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


# --- A1 -------------------------------------------------------------------------------------------------------


def test_A1_every_file_type_indexed_before_is_indexed_now(world, capsys):
    home, settings = world
    old = dataclasses.replace(settings, collection="before")  # what the code did before Downloads was closed
    index_folder(home / "Downloads", HashEmbedder(), store_of(old), old, log=QUIET)
    before = store_of(old)
    assert {Path(p).suffix for p in before.list_paths()} >= set(TEXT_TYPES) | {".pdf"}

    assert run(capsys, "index", str(home / "Downloads"), "--yes")[0] == 0
    after = store_of(settings)
    assert sorted(Path(p).name for p in after.list_paths()) == sorted(Path(p).name for p in before.list_paths())
    assert after.count() == before.count()  # same files, same chunks


# --- A2 -------------------------------------------------------------------------------------------------------


def test_A2_search_and_read_return_the_downloads_files_untouched(world, capsys, monkeypatch):
    home, settings = world
    assert run(capsys, "index", str(home / "Downloads"), "--yes")[0] == 0
    store = store_of(settings)
    wire_mcp(monkeypatch, settings, store)
    lecture = str(home / "Downloads" / "lecture1.md")

    found = asyncio.run(srv.locus_search(query="eigenvalues zebras lecture", top_k=3))
    assert f"{lecture}  (chunk 0" in found  # the path and the chunk_index, as before
    assert LECTURE in found  # the chunk text, whole

    read = asyncio.run(srv.locus_read_file(path=lecture))
    assert LECTURE in read and "Error" not in read


# --- A3 -------------------------------------------------------------------------------------------------------


def _old_index(home: Path, settings: Settings) -> ChromaStore:
    """An index made before Downloads was closed: the whole home, no closed folder."""
    index_folder(home, HashEmbedder(), store_of(settings), settings, log=QUIET)
    return store_of(settings)


def test_A3_rows_survive_the_upgrade_a_prune_and_a_freshness_pass(world, capsys):
    home, settings = world
    before = _old_index(home, settings)
    rows = set(before.list_paths())
    assert str(home / "Downloads" / "lecture1.md") in rows

    code, _, err = run(capsys, "stats")  # the upgrade: the first command that reads the index
    assert code == 0 and "closed folder from this version on" in err

    assert run(capsys, "index", str(home), "--all", "--prune", "--yes")[0] == 0
    assert set(store_of(settings).list_paths()) == rows  # nothing dropped, nothing duplicated

    embedder, store = HashEmbedder(), store_of(settings)
    freshen_for_serve(embedder, store, settings)
    assert set(store.list_paths()) == rows


# --- A5 -------------------------------------------------------------------------------------------------------


def test_A5_the_recheck_keeps_an_opened_downloads_and_hides_it_when_the_entry_goes_and_back_when_it_returns(
    world, capsys, monkeypatch
):
    home, settings = world
    assert run(capsys, "index", str(home / "Downloads"), "--yes")[0] == 0
    store = store_of(settings)
    wire_mcp(monkeypatch, settings, store)
    lecture = str(home / "Downloads" / "lecture1.md")
    entry = settings.data_dir / "policy.toml"
    opened = entry.read_text(encoding="utf-8")

    def sees_it() -> bool:
        found = asyncio.run(srv.locus_search(query="eigenvalues zebras lecture", top_k=3))
        listed = asyncio.run(srv.locus_list_sources(limit=1000))
        return lecture in found and lecture in listed and LECTURE in asyncio.run(srv.locus_read_file(path=lecture))

    assert sees_it()  # open: nothing is hidden
    entry.write_text("version = 1\n", encoding="utf-8")  # the entry is removed
    assert not sees_it()
    assert "closed folder" in asyncio.run(srv.locus_read_file(path=lecture))
    assert lecture in store.list_paths()  # hidden, not deleted
    entry.write_text(opened, encoding="utf-8")  # and put back
    assert sees_it()


# --- A6 -------------------------------------------------------------------------------------------------------


def test_A6_while_paused_the_tools_refuse_and_after_resume_they_work_in_the_same_process(world, capsys, monkeypatch):
    home, settings = world
    assert run(capsys, "index", str(home / "Downloads"), "--yes")[0] == 0
    wire_mcp(monkeypatch, settings, store_of(settings))
    lecture = str(home / "Downloads" / "lecture1.md")

    def tools() -> list[str]:
        return [
            asyncio.run(srv.locus_search(query="eigenvalues zebras lecture")),
            asyncio.run(srv.locus_list_sources()),
            asyncio.run(srv.locus_read_file(path=lecture)),
        ]

    assert all(lecture in out for out in tools())
    assert run(capsys, "pause")[0] == 0
    assert tools() == [killswitch.PAUSED_SENTENCE] * 3  # nothing else: no path, no text
    assert run(capsys, "resume")[0] == 0
    assert all(lecture in out for out in tools())


# --- A7 -------------------------------------------------------------------------------------------------------


def test_A7_a_twenty_call_enumeration_across_downloads_stays_under_the_volume_thresholds(world, capsys, monkeypatch):
    home, settings = world
    assert run(capsys, "index", str(home / "Downloads"), "--yes")[0] == 0
    wire_mcp(monkeypatch, settings, store_of(settings))
    outputs = [asyncio.run(srv.locus_list_sources(path_prefix=str(home / "Downloads"), limit=1000))]
    for path in sorted(Path(p) for p in store_of(settings).list_paths())[:19]:
        outputs.append(asyncio.run(srv.locus_read_file(path=str(path))))
    summary = audit.summarize(audit.read_lines(settings.data_dir / "audit"))
    assert summary["calls"] == 20 and summary["refused"] == 0
    assert summary["calls"] < audit.WARN_CALLS and summary["chars"] < audit.WARN_CHARS
    assert not any("this session has read" in out for out in outputs)  # no warning line anywhere


# --- A8 -------------------------------------------------------------------------------------------------------


def test_A8_the_labels_add_lines_and_leave_the_chunk_text_byte_identical(world, capsys, monkeypatch):
    home, settings = world
    assert run(capsys, "index", str(home / "Downloads"), "--yes")[0] == 0  # opened as `untrusted`
    wire_mcp(monkeypatch, settings, store_of(settings))
    lecture = str(home / "Downloads" / "lecture1.md")
    policy_file = settings.data_dir / "policy.toml"

    def answers() -> tuple[str, str]:
        found = asyncio.run(srv.locus_search(query="eigenvalues zebras lecture", top_k=3))
        read = asyncio.run(srv.locus_read_file(path=lecture))
        return found, read

    labelled = answers()
    policy_file.write_text('[[folder]]\npath = "~/Downloads"\nprofile = "trusted"\n', encoding="utf-8")
    plain = answers()
    for with_label, without in zip(labelled, plain, strict=True):
        assert restrictions.UNTRUSTED_FOLDER_LINE in with_label and restrictions.UNTRUSTED_FOLDER_LINE not in without
        assert with_label.replace(restrictions.UNTRUSTED_FOLDER_LINE + "\n", "") == without  # added lines, nothing else
        assert LECTURE.encode() in with_label.encode() and LECTURE.encode() in without.encode()


# --- A4 -------------------------------------------------------------------------------------------------------


def test_A4_watch_keeps_indexing_new_files_and_a_file_moved_in_later(world, tmp_path, capsys):
    home, settings = world
    downloads = home / "Downloads"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "moved-in.md").write_text("moved in after the watcher started, about tessellations", encoding="utf-8")

    def watch() -> None:
        try:
            cli.main(["watch", str(downloads), "--yes"])
        except BaseException:  # noqa: BLE001 - the thread is abandoned when the test ends, as in test_watcher.py
            pass

    threading.Thread(target=watch, daemon=True).start()
    assert wait_until(lambda: (settings.data_dir / "chroma").exists() and "lecture1.md" in _names(settings))
    time.sleep(1.5)  # the initial pass is done; give the observer time to start before anything changes
    (downloads / "created-later.md").write_text("created after the watcher started, about matroids", encoding="utf-8")
    shutil.move(str(elsewhere / "moved-in.md"), str(downloads / "moved-in.md"))
    assert wait_until(lambda: {"created-later.md", "moved-in.md"} <= _names(settings))


def _names(settings: Settings) -> set[str]:
    try:
        return {Path(p).name for p in store_of(settings).list_paths()}
    except Exception:  # noqa: BLE001 - the index may be mid-write
        return set()
