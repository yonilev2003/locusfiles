"""S-09: a .docx goes through the same zip-bomb and entity-expansion guards as the other zip containers, before
python-docx (which loads every member and parses every XML part with no limit of its own) opens it."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

docx = pytest.importorskip("docx")
pytest.importorskip("chromadb")

from locus.config import DEFAULT_EXTENSIONS, Settings  # noqa: E402
from locus.ingest import docformats  # noqa: E402
from locus.ingest.docformats import UnreadableFile  # noqa: E402
from locus.ingest.embedder import HashEmbedder  # noqa: E402
from locus.ingest.indexer import index_folder  # noqa: E402
from locus.ingest.parsers import parse_file  # noqa: E402
from locus.ingest.store import ChromaStore  # noqa: E402

CONTENT_TYPES = (
    '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="xml" ContentType="application/xml"/></Types>'
)


def _real_docx(path: Path, text: str = "Quarterly plan: the zebra release ships Monday.") -> Path:
    document = docx.Document()
    document.add_paragraph(text)
    document.save(str(path))
    return path


def _package(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES)
        for name, data in members.items():
            zf.writestr(name, data)
    return path


@pytest.fixture
def never_opened(monkeypatch):
    """python-docx must not be reached for a refused file."""

    def refuse(*_a, **_k):
        raise AssertionError("python-docx opened a file the guard should have refused")

    monkeypatch.setattr(docx, "Document", refuse)


def test_a_normal_docx_is_read_as_before(tmp_path):
    assert "zebra release ships Monday" in (parse_file(_real_docx(tmp_path / "plan.docx")) or "")


def test_a_member_over_the_per_member_limit_is_refused(tmp_path, monkeypatch, never_opened):
    monkeypatch.setattr(docformats, "MAX_MEMBER_BYTES", 10_000)
    bomb = _package(tmp_path / "bomb.docx", {"word/media/huge.bin": b"\0" * 200_000})  # 200 KB deflates to ~200 B
    assert bomb.stat().st_size < 5_000
    with pytest.raises(UnreadableFile, match="read limit"):
        parse_file(bomb)


def test_many_members_that_are_small_alone_but_big_together_are_refused(tmp_path, monkeypatch, never_opened):
    monkeypatch.setattr(docformats, "MAX_MEMBER_BYTES", 10_000)
    monkeypatch.setattr(docformats, "MAX_TOTAL_BYTES", 25_000)
    parts = {f"word/media/part{i}.bin": b"\0" * 8_000 for i in range(4)}  # 32 KB declared in all
    with pytest.raises(UnreadableFile, match="package unpacks to"):
        parse_file(_package(tmp_path / "many.docx", parts))


def test_an_entity_declaration_in_any_xml_part_is_refused(tmp_path, never_opened):
    evil = b'<?xml version="1.0"?><!DOCTYPE w [<!ENTITY a "aaaa">]><w:document xmlns:w="urn:w">&a;</w:document>'
    with pytest.raises(UnreadableFile, match="DOCTYPE or ENTITY"):
        parse_file(_package(tmp_path / "xxe.docx", {"word/document.xml": evil}))


def test_a_password_protected_docx_is_refused_in_plain_words(tmp_path, never_opened):
    wrapped = tmp_path / "locked.docx"
    wrapped.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 512)  # the OLE wrapper Word puts around it
    with pytest.raises(UnreadableFile, match="password-protected"):
        parse_file(wrapped)


def test_the_real_limits_are_the_ones_the_other_containers_use():
    assert docformats.MAX_MEMBER_BYTES == 50 * 1024 * 1024 and docformats.MAX_TOTAL_BYTES == 200 * 1024 * 1024


def test_a_refused_docx_is_one_file_error_and_does_not_stop_the_run(tmp_path, monkeypatch):
    monkeypatch.setattr(docformats, "MAX_MEMBER_BYTES", 10_000)
    root = tmp_path / "docs"
    root.mkdir()
    _package(root / "bomb.docx", {"word/media/huge.bin": b"\0" * 200_000})
    (root / "note.md").write_text("A plain note about the zebra release.")
    settings = Settings(
        data_dir=tmp_path / "data", collection="docx", embedder="hash", voyage_model="", openai_model="",
        local_model="", chunk_tokens=100, overlap_tokens=10, max_file_mb=5, extensions=DEFAULT_EXTENSIONS,
    )  # fmt: skip
    store = ChromaStore(settings.data_dir, settings.collection, HashEmbedder().name)
    stats = index_folder(root, HashEmbedder(), store, settings, log=lambda _m: None)
    assert stats.files_indexed == 1 and len(stats.errors) == 1 and "bomb.docx" in stats.errors[0]
