"""S-05: locus_read_file reads only paths the index lists, never through a link, and never returns text that came
from a path swapped for a link while the read was under way. Offline, HashEmbedder, tmp folders, POSIX symlinks."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

pytest.importorskip("chromadb")

from locus import mcp_server as srv  # noqa: E402
from locus.config import DEFAULT_EXTENSIONS, Settings  # noqa: E402
from locus.ingest.embedder import HashEmbedder  # noqa: E402
from locus.ingest.indexer import index_folder  # noqa: E402
from locus.ingest.store import ChromaStore  # noqa: E402

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="creating a symlink needs a privilege on Windows; the checks themselves are portable"
)

SECRET = "OUTSIDE-THE-INDEX-SECRET-4711"


@pytest.fixture
def world(tmp_path, monkeypatch):
    settings = Settings(
        data_dir=tmp_path / "data", collection="confine", embedder="hash", voyage_model="", openai_model="",
        local_model="", chunk_tokens=100, overlap_tokens=10, max_file_mb=5, extensions=DEFAULT_EXTENSIONS,
    )  # fmt: skip
    embedder = HashEmbedder()
    store = ChromaStore(settings.data_dir, settings.collection, embedder.name)
    root = tmp_path / "corpus"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "a.md").write_text("indexed content about the zebra release " * 5)
    (root / "b.md").write_text("another indexed note about pricing " * 5)
    index_folder(root, embedder, store, settings, log=lambda _m: None)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text(SECRET)
    (outside / "a.md").write_text(SECRET)
    monkeypatch.setattr(srv, "_components", lambda: (embedder, store, settings))
    return root.resolve(), outside.resolve()


def _read(path: str | Path, **kwargs) -> str:
    return asyncio.run(srv.locus_read_file(path=str(path), **kwargs))


# --- a path the index does not list --------------------------------------------------------------------------------


def test_a_file_that_exists_but_is_not_indexed_is_refused(world):
    _root, outside = world
    out = _read(outside / "secret.md")
    assert "is not an indexed file" in out and SECRET not in out


@pytest.mark.parametrize(
    "spelling",
    [
        "{root}/sub/../b.md",  # dot-dot that lands on an indexed file: not the indexed spelling
        "{root}/./b.md",
        "{root}//b.md",
        "b.md",  # relative
        "{root}/sub/../../outside/secret.md",  # dot-dot out of the corpus
        "{root}",  # a folder, not a file
        "{data}/chroma",  # Locus's own index folder
    ],
)
def test_other_spellings_and_other_places_are_refused(world, tmp_path, spelling):
    root, _outside = world
    out = _read(spelling.format(root=root, data=tmp_path / "data"))
    assert out.startswith("Error:") and "is not an indexed file" in out and SECRET not in out


def test_an_indexed_file_is_read(world):
    root, _outside = world
    assert "indexed content about the zebra release" in _read(root / "sub" / "a.md")


# --- a link ---------------------------------------------------------------------------------------------------------


def test_an_indexed_file_replaced_by_a_symlink_is_refused(world):
    root, outside = world
    target = root / "sub" / "a.md"
    target.unlink()
    target.symlink_to(outside / "secret.md")
    out = _read(target)
    assert "now a symlink" in out and SECRET not in out


def test_an_indexed_file_replaced_by_a_link_to_another_indexed_file_is_refused_too(world):
    root, _outside = world
    target = root / "sub" / "a.md"
    target.unlink()
    target.symlink_to(root / "b.md")
    assert "now a symlink" in _read(target)


def test_an_indexed_folder_replaced_by_a_symlink_is_refused(world):
    root, outside = world
    (root / "sub" / "a.md").unlink()
    (root / "sub").rmdir()
    (root / "sub").symlink_to(outside)  # outside/a.md now answers to the indexed path root/sub/a.md
    out = _read(root / "sub" / "a.md")
    assert "resolves outside the indexed files" in out and SECRET not in out


# --- the race between the check and the read -----------------------------------------------------------------------


def test_a_file_swapped_for_a_symlink_after_the_check_is_never_returned(world, monkeypatch):
    """The checks pass, then the file is replaced by a link to a secret before the bytes are read. The text read
    through the link must be dropped."""
    root, outside = world
    target = root / "sub" / "a.md"
    real_parse = srv.parse_file

    def swapped_then_read(path):
        path.unlink()
        path.symlink_to(outside / "secret.md")  # the attacker wins the race right after the check
        return real_parse(path)

    monkeypatch.setattr(srv, "parse_file", swapped_then_read)
    out = _read(target)
    assert SECRET not in out
    assert "changed while it was being read" in out and "now a symlink" in out


def test_a_folder_swapped_for_a_symlink_after_the_check_is_never_returned(world, monkeypatch):
    root, outside = world
    target = root / "sub" / "a.md"
    real_parse = srv.parse_file

    def folder_swapped_then_read(path):
        (root / "sub" / "a.md").unlink()
        (root / "sub").rmdir()
        (root / "sub").symlink_to(outside)
        return real_parse(path)

    monkeypatch.setattr(srv, "parse_file", folder_swapped_then_read)
    out = _read(target)
    assert SECRET not in out and "changed while it was being read" in out


def test_a_read_that_nothing_interferes_with_is_unchanged(world, monkeypatch):
    root, _outside = world
    calls: list[str] = []
    real_parse = srv.parse_file

    def counting(path):
        calls.append(str(path))
        return real_parse(path)

    monkeypatch.setattr(srv, "parse_file", counting)
    out = _read(root / "b.md", max_chars=200)
    assert "another indexed note about pricing" in out and "changed while" not in out and len(calls) == 1
