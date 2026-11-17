"""R5: a hit from a folder marked `untrusted` says so; nothing else about the answer changes.

Downloads (once opened) and the Notion sync folder are `untrusted` without anyone writing it; an entry in policy.toml
overrides that. The label is one more line under a hit's header and in a read's header: the text is returned
verbatim, in the same order. It is a request to the assistant to be careful, not a barrier (SECURITY.md says
so). Offline.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("chromadb")

from locus import mcp_server as srv  # noqa: E402
from locus import restrictions  # noqa: E402
from locus.config import DEFAULT_EXTENSIONS, Settings  # noqa: E402
from locus.ingest.embedder import HashEmbedder  # noqa: E402
from locus.ingest.indexer import index_folder  # noqa: E402
from locus.ingest.store import ChromaStore  # noqa: E402

LINE = restrictions.UNTRUSTED_FOLDER_LINE
TEXT = "the quokka lecture covers marsupial habitats in detail"


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = (tmp_path / "home").resolve()
    settings = Settings(
        data_dir=tmp_path / "data", collection="profile", embedder="hash", voyage_model="", openai_model="",
        local_model="", chunk_tokens=100, overlap_tokens=10, max_file_mb=5, extensions=DEFAULT_EXTENSIONS,
    )  # fmt: skip
    folders = {"mine": home / "mine", "other": home / "other", "notion": settings.data_dir / "sources" / "notion"}
    for name, folder in folders.items():
        folder.mkdir(parents=True)
        (folder / f"{name}.md").write_text(f"{TEXT} ({name})", encoding="utf-8")
    embedder = HashEmbedder()
    store = ChromaStore(settings.data_dir, settings.collection, embedder.name)
    for folder in folders.values():
        index_folder(folder, embedder, store, settings, log=lambda _m: None)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    components = (embedder, store, settings)
    monkeypatch.setattr(srv, "_components", lambda: components)
    monkeypatch.setattr(srv, "freshen_for_serve", lambda *a, **k: None)
    monkeypatch.setenv("LOCUS_DATA_DIR", str(settings.data_dir))
    return home, settings, folders


def policy(settings: Settings, text: str) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "policy.toml").write_text(text, encoding="utf-8")


def search() -> str:
    return asyncio.run(srv.locus_search(query="quokka lecture marsupial habitats", top_k=6))


def block_of(out: str, path: Path) -> str:
    """The lines of one hit: its header (which names the path) up to the next header."""
    chunks = out.split("\n## ")
    return next(c for c in chunks if str(path) in c.splitlines()[0])


def test_a_hit_under_an_untrusted_folder_carries_the_line_and_a_trusted_one_does_not(world):
    home, settings, folders = world
    policy(settings, f'[[folder]]\npath = "{folders["other"]}"\nprofile = "untrusted"\n')
    out = search()
    assert LINE in block_of(out, folders["other"] / "other.md")
    assert LINE not in block_of(out, folders["mine"] / "mine.md")
    assert out.count(LINE) == 2  # the Notion sync folder is untrusted without an entry, and `other` by its entry


def test_the_read_header_carries_it_too(world):
    home, settings, folders = world
    policy(settings, f'[[folder]]\npath = "{folders["other"]}"\nprofile = "untrusted"\n')
    marked = asyncio.run(srv.locus_read_file(path=str(folders["other"] / "other.md")))
    plain = asyncio.run(srv.locus_read_file(path=str(folders["mine"] / "mine.md")))
    assert marked.split("\n\n")[0].endswith(LINE) and LINE not in plain
    assert marked.endswith(f"{TEXT} (other)") and plain.endswith(f"{TEXT} (mine)")


def test_the_notion_sync_folder_is_untrusted_unless_an_entry_says_otherwise(world):
    home, settings, folders = world
    assert LINE in block_of(search(), folders["notion"] / "notion.md")
    policy(settings, f'[[folder]]\npath = "{folders["notion"]}"\nprofile = "trusted"\n')
    assert LINE not in block_of(search(), folders["notion"] / "notion.md")


def test_the_label_changes_no_text_and_no_order(world):
    home, settings, folders = world
    policy(settings, f'[[folder]]\npath = "{folders["other"]}"\nprofile = "untrusted"\n')
    labelled = search()
    policy(
        settings,
        f'[[folder]]\npath = "{folders["other"]}"\nprofile = "trusted"\n'
        f'[[folder]]\npath = "{folders["notion"]}"\nprofile = "trusted"\n',
    )
    plain = search()
    assert LINE not in plain and labelled != plain
    assert labelled.replace(f"{LINE}\n", "") == plain  # the only difference is the added lines
    assert f"{TEXT} (other)" in labelled


def test_an_opened_downloads_is_untrusted_without_anyone_writing_it(world):
    home, settings, folders = world
    downloads = home / "Downloads"
    downloads.mkdir()
    (downloads / "dl.md").write_text(f"{TEXT} (dl)", encoding="utf-8")
    index_folder(downloads, HashEmbedder(), srv._components()[1], settings, log=lambda _m: None)
    from locus import closed_folders

    closed_folders.append_opt_in(settings.data_dir / "policy.toml", downloads, "test", home)
    assert LINE in block_of(search(), downloads / "dl.md")
