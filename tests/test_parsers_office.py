"""Engine 0.1.2 document types: .pptx, .xlsx, OpenDocument, .epub, .rtf, .ipynb and the extra plain-text
extensions, all read with the standard library only (src/locus/ingest/docformats.py, parsers.py).

Every fixture is built here with zipfile (minimal valid OOXML / ODF / EPUB parts) or written as RTF
text, so no binary file is committed. Offline: indexing tests use HashEmbedder, never a paid API.

1. Happy paths per format, including the ordering rules (slides, spine, workbook sheets).
2. xlsx: shared strings, sparse cells, empty rows, and the row / column truncation lines.
3. RTF escapes: \\par, \\tab, \\'hh with code pages, \\uN with fallbacks, skipped destinations.
4. Safety: zip bombs (declared and streamed size, per member and in total), DOCTYPE / ENTITY
   members, password-protected and DRM files, corrupt zips.
5. A bad file is counted in IndexStats.errors and the run goes on, like a bad PDF or DOCX.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from locus.config import DEFAULT_EXTENSIONS, Settings
from locus.ingest import docformats
from locus.ingest.chunker import chunk_text
from locus.ingest.docformats import (
    MAX_MEMBER_BYTES,
    MAX_SHEET_COLUMNS,
    MAX_SHEET_ROWS,
    SHEET_GROUP_CHARS,
    UnreadableFile,
)
from locus.ingest.embedder import HashEmbedder
from locus.ingest.estimator import kind_of
from locus.ingest.indexer import index_folder
from locus.ingest.parsers import TEXT_EXTENSIONS, parse_file

chromadb = pytest.importorskip("chromadb")

from locus.ingest.store import ChromaStore  # noqa: E402

XML_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
NS_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
BOMB = '<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;">]>'


def _zip(path: Path, members: dict[str, str | bytes]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in members.items():
            zf.writestr(name, content)
    return path


def _rels(*rels: tuple[str, str, str]) -> str:
    items = "".join(f'<Relationship Id="{i}" Type="{t}" Target="{target}"/>' for i, t, target in rels)
    return f'{XML_HEAD}<Relationships xmlns="{NS_PKG_REL}">{items}</Relationships>'


# --- .pptx fixtures -------------------------------------------------------------------------------


def _shape(lines: list[str], ph: str | None = None) -> str:
    placeholder = f'<p:ph type="{ph}"/>' if ph else ""
    paras = "".join(f"<a:p><a:r><a:rPr/><a:t>{line}</a:t></a:r></a:p>" for line in lines)
    return (
        f'<p:sp><p:nvSpPr><p:cNvPr id="2" name="Shape"/><p:cNvSpPr/><p:nvPr>{placeholder}</p:nvPr></p:nvSpPr>'
        f"<p:spPr/><p:txBody><a:bodyPr/>{paras}</p:txBody></p:sp>"
    )


def _slide(*shapes: str) -> str:
    return (
        f'{XML_HEAD}<p:sld xmlns:a="{NS_A}" xmlns:p="{NS_P}" xmlns:r="{NS_R}"><p:cSld><p:spTree>'
        f'<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>'
        f"{''.join(shapes)}</p:spTree></p:cSld></p:sld>"
    )


def _make_pptx(
    path: Path,
    slides: list[str],
    notes: dict[int, list[str]] | None = None,
    order: list[int] | None = None,
    extra: dict[str, str | bytes] | None = None,
) -> Path:
    """slides[i] is slide{i+1}.xml; `order` (1-based) is the presentation order, default file order."""
    order = order or list(range(1, len(slides) + 1))
    members: dict[str, str | bytes] = {
        "_rels/.rels": _rels(("rId1", f"{REL}/officeDocument", "ppt/presentation.xml")),
        "ppt/presentation.xml": (
            f'{XML_HEAD}<p:presentation xmlns:p="{NS_P}" xmlns:r="{NS_R}"><p:sldIdLst>'
            + "".join(f'<p:sldId id="{256 + n}" r:id="rId{n}"/>' for n in order)
            + "</p:sldIdLst></p:presentation>"
        ),
        "ppt/_rels/presentation.xml.rels": _rels(
            *[(f"rId{n}", f"{REL}/slide", f"slides/slide{n}.xml") for n in range(1, len(slides) + 1)]
        ),
    }
    for n, xml in enumerate(slides, start=1):
        members[f"ppt/slides/slide{n}.xml"] = xml
    for n, lines in (notes or {}).items():
        members[f"ppt/slides/_rels/slide{n}.xml.rels"] = _rels(
            ("rId1", f"{REL}/notesSlide", f"../notesSlides/notesSlide{n}.xml")
        )
        members[f"ppt/notesSlides/notesSlide{n}.xml"] = _slide(
            _shape([], ph="sldImg"),
            _shape(lines, ph="body"),
            _shape([str(n)], ph="sldNum"),
        )
    members.update(extra or {})
    return _zip(path, members)


def test_pptx_reads_slides_in_presentation_order_with_notes(tmp_path: Path):
    path = _make_pptx(
        tmp_path / "deck.pptx",
        [
            _slide(_shape(["Intro title"], ph="title"), _shape(["Welcome everyone"])),
            _slide(_shape(["Pricing"], ph="title"), _shape(["Starter is $5", "Pro is $10"])),
            _slide(_shape(["Roadmap"], ph="title")),
        ],
        notes={2: ["Mention the annual discount", "Ask about Tranzila"]},
        order=[2, 3, 1],  # presentation order differs from the file numbering
    )
    text = parse_file(path)
    assert text is not None
    blocks = text.split("\n\n")
    assert (
        blocks[0]
        == "Slide 1\nPricing\nStarter is $5\nPro is $10\nNotes:\nMention the annual discount\nAsk about Tranzila"
    )
    assert blocks[1] == "Slide 2\nRoadmap"
    assert blocks[2] == "Slide 3\nIntro title\nWelcome everyone"
    # The slide-number field on the notes page and the slide-image placeholder are not notes text.
    assert "Ask about Tranzila" in blocks[0] and not blocks[0].rstrip().endswith("2")


def test_pptx_skips_footer_placeholders_reads_tables_and_numbers_empty_slides(tmp_path: Path):
    table = (
        "<p:graphicFrame><a:graphic><a:graphicData><a:tbl><a:tblGrid/>"
        "<a:tr><a:tc><a:txBody><a:p><a:r><a:t>Plan</a:t></a:r></a:p></a:txBody></a:tc>"
        "<a:tc><a:txBody><a:p><a:r><a:t>Price</a:t></a:r></a:p></a:txBody></a:tc></a:tr>"
        "<a:tr><a:tc><a:txBody><a:p><a:r><a:t>Pro</a:t></a:r></a:p></a:txBody></a:tc>"
        "<a:tc><a:txBody><a:p><a:r><a:t>$10</a:t></a:r></a:p></a:txBody></a:tc></a:tr>"
        "</a:tbl></a:graphicData></a:graphic></p:graphicFrame>"
    )
    path = _make_pptx(
        tmp_path / "deck.pptx",
        [
            _slide(_shape(["Only a footer"], ph="ftr"), _shape(["7"], ph="sldNum")),  # nothing worth indexing
            _slide(_shape(["Plans"], ph="title"), table, _shape(["Confidential"], ph="ftr")),
        ],
    )
    assert parse_file(path) == "Slide 2\nPlans\nPlan | Price\nPro | $10"  # the empty slide still counted as 1


def test_pptx_without_a_slide_list_falls_back_to_numbered_files(tmp_path: Path):
    path = _make_pptx(
        tmp_path / "deck.pptx",
        [_slide(_shape(["one"])), _slide(_shape(["two"])), _slide(_shape(["three"]))],
        extra={"ppt/presentation.xml": f'{XML_HEAD}<p:presentation xmlns:p="{NS_P}"/>'},
    )
    assert parse_file(path) == "Slide 1\none\n\nSlide 2\ntwo\n\nSlide 3\nthree"


# --- .xlsx fixtures -------------------------------------------------------------------------------


def _letters(index: int) -> str:
    out = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        out = chr(65 + rem) + out
    return out


def _sheet_xml(rows: list[list], strings: list[str]) -> str:
    """rows hold str (shared string), int/float, bool, ("inline", text) or None (no cell at that column)."""
    out = []
    for r, row in enumerate(rows, start=1):
        cells = []
        for c, value in enumerate(row):
            ref = f"{_letters(c)}{r}"
            if value is None:
                continue
            if isinstance(value, bool):
                cells.append(f'<c r="{ref}" t="b"><v>{int(value)}</v></c>')
            elif isinstance(value, str):
                if value not in strings:
                    strings.append(value)
                cells.append(f'<c r="{ref}" t="s"><v>{strings.index(value)}</v></c>')
            elif isinstance(value, tuple):
                cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{value[1]}</t></is></c>')
            else:
                cells.append(f'<c r="{ref}"><v>{value}</v></c>')
        out.append(f'<row r="{r}">{"".join(cells)}</row>')
    return f'{XML_HEAD}<worksheet xmlns="{NS_S}"><sheetData>{"".join(out)}</sheetData></worksheet>'


def _make_xlsx(path: Path, sheets: list[tuple[str, list[list]]], extra: dict[str, str | bytes] | None = None) -> Path:
    """Sheets are stored as sheet{n}.xml in reverse of workbook order, so the rels have to be followed."""
    strings: list[str] = []
    members: dict[str, str | bytes] = {
        "_rels/.rels": _rels(("rId1", f"{REL}/officeDocument", "xl/workbook.xml")),
    }
    workbook_sheets = []
    rels = []
    total = len(sheets)
    for n, (name, rows) in enumerate(sheets, start=1):
        file_no = total - n + 1
        members[f"xl/worksheets/sheet{file_no}.xml"] = _sheet_xml(rows, strings)
        workbook_sheets.append(f'<sheet name="{name}" sheetId="{n}" r:id="rIdS{n}"/>')
        rels.append((f"rIdS{n}", f"{REL}/worksheet", f"worksheets/sheet{file_no}.xml"))
    members["xl/workbook.xml"] = (
        f'{XML_HEAD}<workbook xmlns="{NS_S}" xmlns:r="{NS_R}"><sheets>{"".join(workbook_sheets)}</sheets></workbook>'
    )
    rels.append(("rIdSS", f"{REL}/sharedStrings", "sharedStrings.xml"))
    members["xl/_rels/workbook.xml.rels"] = _rels(*rels)
    members["xl/sharedStrings.xml"] = (
        f'{XML_HEAD}<sst xmlns="{NS_S}">' + "".join(f"<si><t>{s}</t></si>" for s in strings) + "</sst>"
    )
    members.update(extra or {})
    return _zip(path, members)


def test_xlsx_sheets_shared_strings_sparse_cells_and_empty_rows(tmp_path: Path):
    path = _make_xlsx(
        tmp_path / "book.xlsx",
        [
            (
                "Budget",
                [
                    ["Item", "Cost", "Paid"],
                    ["Hosting", 12.5, True],
                    [None, None, None],  # empty row: skipped
                    ["Domain", None, False],  # a gap in the middle keeps its column
                    [None, None, ("inline", "late note")],  # leading empties kept, trailing ones trimmed
                ],
            ),
            ("Empty", [[None]]),  # a sheet with nothing in it is left out
            ("Notes", [["Tab\there", "line\nbreak"]]),  # whitespace inside a cell cannot split the row
        ],
    )
    assert parse_file(path) == (
        "Sheet: Budget\n"
        "Item\tCost\tPaid\n"
        "Hosting\t12.5\tTRUE\n"
        "Domain\t\tFALSE\n"
        "\t\tlate note\n"
        "\n"
        "Sheet: Notes\n"
        "Tab here\tline break"
    )


def test_xlsx_rich_text_and_phonetic_runs_in_shared_strings(tmp_path: Path):
    path = _make_xlsx(tmp_path / "book.xlsx", [("S", [["x"]])])
    with zipfile.ZipFile(path) as zf:
        members = {n: zf.read(n) for n in zf.namelist()}
    members["xl/sharedStrings.xml"] = (
        f'{XML_HEAD}<sst xmlns="{NS_S}"><si><r><t>Hello </t></r><r><rPr/><t>world</t></r>'
        "<rPh><t>furigana</t></rPh></si></sst>"
    ).encode()
    _zip(path, members)
    assert parse_file(path) == "Sheet: S\nHello world"


def test_xlsx_truncates_rows_and_columns_and_says_so(tmp_path: Path):
    rows = [[f"row{i}", i] for i in range(MAX_SHEET_ROWS + 5)]
    rows.insert(0, [f"c{i}" for i in range(MAX_SHEET_COLUMNS + 3)])
    path = _make_xlsx(tmp_path / "big.xlsx", [("Big", rows)])
    text = parse_file(path)
    assert text is not None
    lines = [line for line in text.split("\n") if line]  # (blank lines only group rows, see below)
    assert lines[0] == "Sheet: Big"
    assert lines[1].count("\t") == MAX_SHEET_COLUMNS - 1 and lines[1].endswith(f"c{MAX_SHEET_COLUMNS - 1}")
    assert f"c{MAX_SHEET_COLUMNS}" not in text
    assert f"row{MAX_SHEET_ROWS - 2}\t{MAX_SHEET_ROWS - 2}" in text  # the last row inside the limit
    assert f"row{MAX_SHEET_ROWS}\t" not in text
    assert lines[MAX_SHEET_ROWS] == f"row{MAX_SHEET_ROWS - 2}\t{MAX_SHEET_ROWS - 2}"  # header + 4,999 rows
    assert lines[-2] == f"[truncated: rows after the first {MAX_SHEET_ROWS} were not indexed]"
    assert lines[-1] == f"[truncated: columns after the first {MAX_SHEET_COLUMNS} were not indexed]"


def test_long_sheets_get_blank_lines_between_whole_rows_so_chunks_do_not_cut_rows(tmp_path: Path):
    rows = [[f"SKU-{i:05d}", i * 3, "Customer asked about the invoice."] for i in range(1, 900)]
    text = parse_file(_make_xlsx(tmp_path / "long.xlsx", [("Data", rows)]))
    assert text is not None
    groups = text.split("\n\n")
    assert len(groups) > 10 and groups[0].startswith("Sheet: Data\nSKU-00001\t3\t")
    for group in groups:
        lines = group.split("\n")
        assert sum(len(line) + 1 for line in lines) < SHEET_GROUP_CHARS + 100  # one group is a fraction of a chunk
        assert all(line.startswith("Sheet: ") or line.count("\t") == 2 for line in lines)  # no row is ever split
    first = chunk_text(text, 700, 100)[0].text
    assert first.startswith("Sheet: Data\nSKU-00001") and first.count("\n\n") >= 1


def test_xlsx_a_sheet_exactly_at_the_row_limit_is_not_marked_truncated(tmp_path: Path):
    path = _make_xlsx(tmp_path / "full.xlsx", [("Full", [[i] for i in range(MAX_SHEET_ROWS)])])
    text = parse_file(path)
    assert text is not None and "truncated" not in text
    assert len([line for line in text.split("\n") if line]) == MAX_SHEET_ROWS + 1  # the name line + every row


def test_xlsx_cell_far_to_the_right_is_cut_without_building_a_wide_row(tmp_path: Path):
    path = _make_xlsx(tmp_path / "wide.xlsx", [("W", [[None] * 16000 + ["far"], ["near"]])])
    assert (
        parse_file(path) == f"Sheet: W\nnear\n[truncated: columns after the first {MAX_SHEET_COLUMNS} were not indexed]"
    )


# --- OpenDocument fixtures ------------------------------------------------------------------------

ODF_NS = (
    'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
    'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
    'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
    'xmlns:presentation="urn:oasis:names:tc:opendocument:xmlns:presentation:1.0"'
)


def _make_odf(path: Path, kind: str, body: str, extra: dict[str, str | bytes] | None = None) -> Path:
    mime = {"text": "text", "spreadsheet": "spreadsheet", "presentation": "presentation"}[kind]
    members: dict[str, str | bytes] = {
        "mimetype": f"application/vnd.oasis.opendocument.{mime}",
        "content.xml": (
            f"{XML_HEAD}<office:document-content {ODF_NS}><office:automatic-styles/>"
            f"<office:body><office:{kind}>{body}</office:{kind}></office:body></office:document-content>"
        ),
        "META-INF/manifest.xml": (
            f'{XML_HEAD}<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0"/>'
        ),
    }
    members.update(extra or {})
    return _zip(path, members)


def test_odt_headings_paragraphs_lists_tables_and_what_it_leaves_out(tmp_path: Path):
    body = (
        "<text:tracked-changes><text:changed-region><text:deletion>"
        "<text:p>deleted wording</text:p></text:deletion></text:changed-region></text:tracked-changes>"
        '<text:h text:outline-level="1">Refund policy</text:h>'
        '<text:p>Refunds take<text:s text:c="2"/>5 business<text:tab/>days.<text:line-break/>No exceptions.'
        "<text:note><text:note-citation>1</text:note-citation><text:note-body><text:p>See section 4</text:p>"
        "</text:note-body></text:note></text:p>"
        "<text:list><text:list-item><text:p>first item</text:p></text:list-item>"
        "<text:list-item><text:p>second item</text:p></text:list-item></text:list>"
        '<office:annotation><dc:creator xmlns:dc="http://purl.org/dc/elements/1.1/">Dana</dc:creator>'
        "<text:p>reviewer comment</text:p></office:annotation>"
        '<table:table table:name="T"><table:table-header-rows><table:table-row>'
        "<table:table-cell><text:p>Plan</text:p></table:table-cell><table:table-cell><text:p>Price</text:p></table:table-cell>"
        "</table:table-row></table:table-header-rows><table:table-row>"
        "<table:table-cell><text:p>Pro</text:p></table:table-cell><table:table-cell><text:p>$10</text:p></table:table-cell>"
        "</table:table-row></table:table>"
        "<text:p/>"  # an empty paragraph adds nothing
    )
    text = parse_file(_make_odf(tmp_path / "policy.odt", "text", body))
    assert text == (
        "Refund policy\n\n"
        "Refunds take  5 business\tdays.\nNo exceptions. [See section 4]\n\n"
        "first item\n\nsecond item\n\n"
        "Plan | Price\n\nPro | $10"
    )
    assert "deleted wording" not in text and "reviewer comment" not in text and "Dana" not in text


def test_ods_sheets_repeated_cells_and_rows_are_never_expanded(tmp_path: Path):
    body = (
        '<table:table table:name="Sales">'
        "<table:table-row><table:table-cell><text:p>Region</text:p></table:table-cell>"
        '<table:table-cell office:value-type="float" office:value="0.1"><text:p>10%</text:p></table:table-cell>'
        '<table:table-cell table:number-columns-repeated="1048576"/></table:table-row>'
        '<table:table-row table:number-rows-repeated="1048576"><table:table-cell table:number-columns-repeated="1024"/>'
        "</table:table-row>"
        "<table:table-row><table:table-cell/><table:table-cell><text:p>EMEA</text:p></table:table-cell>"
        '<table:covered-table-cell/><table:table-cell office:value-type="float" office:value="42"/></table:table-row>'
        "</table:table>"
        '<table:table table:name="Blank"><table:table-row><table:table-cell/></table:table-row></table:table>'
        '<table:table table:name="Fill"><table:table-row table:number-rows-repeated="3">'
        '<table:table-cell table:number-columns-repeated="2"><text:p>x</text:p></table:table-cell>'
        "</table:table-row></table:table>"
    )
    text = parse_file(_make_odf(tmp_path / "sales.ods", "spreadsheet", body))
    assert text == "Sheet: Sales\nRegion\t10%\n\tEMEA\t\t42\n\nSheet: Fill\nx\tx\nx\tx\nx\tx"


def test_ods_wide_filled_row_and_row_limit_are_truncated(tmp_path: Path):
    wide = '<table:table-cell table:number-columns-repeated="100000"><text:p>w</text:p></table:table-cell>'
    tall = (
        '<table:table-row table:number-rows-repeated="100000">'
        "<table:table-cell><text:p>t</text:p></table:table-cell></table:table-row>"
    )
    path = _make_odf(
        tmp_path / "big.ods",
        "spreadsheet",
        f'<table:table table:name="Wide"><table:table-row>{wide}</table:table-row></table:table>'
        f'<table:table table:name="Tall">{tall}</table:table>',
    )
    text = parse_file(path)
    assert text is not None
    wide_part, tall_part = text.split("\n\nSheet: Tall")  # (long sheets also hold blank lines between row groups)
    assert wide_part.split("\n")[1] == "\t".join(["w"] * MAX_SHEET_COLUMNS)
    assert wide_part.endswith(f"[truncated: columns after the first {MAX_SHEET_COLUMNS} were not indexed]")
    assert tall_part.count("\nt") == MAX_SHEET_ROWS
    assert tall_part.endswith(f"[truncated: rows after the first {MAX_SHEET_ROWS} were not indexed]")


def test_odp_slides_and_speaker_notes(tmp_path: Path):
    def frame(text: str, cls: str | None = None) -> str:
        attr = f' presentation:class="{cls}"' if cls else ""
        return f"<draw:frame{attr}><draw:text-box><text:p>{text}</text:p></draw:text-box></draw:frame>"

    body = (
        '<draw:page draw:name="page1">'
        + frame("Kickoff", "title")
        + frame("1", "page-number")
        + "<presentation:notes>"
        + frame("Say hello", "notes")
        + "</presentation:notes></draw:page>"
        + '<draw:page draw:name="page2"><draw:frame><draw:image/></draw:frame></draw:page>'
        + '<draw:page draw:name="page3">'
        + frame("Wrap up")
        + "</draw:page>"
    )
    assert parse_file(_make_odf(tmp_path / "deck.odp", "presentation", body)) == (
        "Slide 1\nKickoff\nNotes:\nSay hello\n\nSlide 3\nWrap up"
    )


# --- .epub fixtures -------------------------------------------------------------------------------


def _make_epub(
    path: Path, chapters: dict[str, str], spine: list[str], extra: dict[str, str | bytes] | None = None
) -> Path:
    """`chapters` maps file name -> body markup, listed in the manifest in the order given;
    `spine` is the reading order (file names)."""
    manifest = (
        "".join(f'<item id="id-{name}" href="{name}" media-type="application/xhtml+xml"/>' for name in chapters)
        + '<item id="cover" href="cover.jpg" media-type="image/jpeg"/>'
    )
    itemrefs = "".join(f'<itemref idref="id-{name}"/>' for name in spine)
    members: dict[str, str | bytes] = {
        "mimetype": "application/epub+zip",
        "META-INF/container.xml": (
            f'{XML_HEAD}<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0"><rootfiles>'
            '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
            "</rootfiles></container>"
        ),
        "OEBPS/content.opf": (
            f'{XML_HEAD}<package xmlns="http://www.idpf.org/2007/opf" version="3.0"><metadata/>'
            f"<manifest>{manifest}</manifest><spine>{itemrefs}</spine></package>"
        ),
        "OEBPS/cover.jpg": b"\xff\xd8\xff not an image",
    }
    for name, body in chapters.items():
        members[f"OEBPS/{name}"] = body
    members.update(extra or {})
    return _zip(path, members)


XHTML_HEAD = (
    f'{XML_HEAD}<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">'
    '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Ignored title</title>'
    "<style>p { color: red }</style><script>var hidden = 1;</script></head><body>"
)


def test_epub_reads_chapters_in_spine_order_and_strips_markup(tmp_path: Path):
    path = _make_epub(
        tmp_path / "book.epub",
        {  # manifest order is not reading order, and ch3 is not in the spine at all
            "ch2.xhtml": f"{XHTML_HEAD}<h1>Chapter Two</h1><p>Second &amp; last.</p></body></html>",
            "ch1.xhtml": (
                f"{XHTML_HEAD}<h1>Chapter One</h1><p>It was a <em>dark</em> night.<br/>Really&nbsp;dark.</p>"
                "<ul><li>alpha</li><li>beta</li></ul><table><tr><td>a</td><td>b</td></tr></table></body></html>"
            ),
            "ch3.xhtml": f"{XHTML_HEAD}<p>Not in the spine</p></body></html>",
        },
        spine=["ch1.xhtml", "ch2.xhtml"],
    )
    assert parse_file(path) == (
        "Chapter One\n\nIt was a dark night.\nReally dark.\n\nalpha\n\nbeta\n\na b\n\nChapter Two\n\nSecond & last."
    )


def test_epub_percent_encoded_href_and_unquoted_head(tmp_path: Path):
    path = _make_epub(
        tmp_path / "book.epub",
        {"my chapter.xhtml": "<html><head><title>T</title><body><p>kept</p></body></html>"},
        spine=["my chapter.xhtml"],
    )
    with zipfile.ZipFile(path) as zf:
        members = {n: zf.read(n) for n in zf.namelist()}
    members["OEBPS/content.opf"] = members["OEBPS/content.opf"].replace(b"my chapter", b"my%20chapter")
    _zip(path, members)
    assert parse_file(path) == "kept"  # %20 resolved, and the <head> with no end tag did not swallow the body


def test_epub_font_obfuscation_is_fine_but_drm_is_refused(tmp_path: Path):
    def encryption(algorithm: str) -> str:
        return (
            f'{XML_HEAD}<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            f'<EncryptedData xmlns="http://www.w3.org/2001/04/xmlenc#"><EncryptionMethod Algorithm="{algorithm}"/>'
            "</EncryptedData></encryption>"
        )

    chapters = {"c.xhtml": "<html><body><p>readable</p></body></html>"}
    fonts_only = _make_epub(
        tmp_path / "fonts.epub",
        chapters,
        ["c.xhtml"],
        extra={"META-INF/encryption.xml": encryption("http://www.idpf.org/2008/embedding")},
    )
    assert parse_file(fonts_only) == "readable"
    drm = _make_epub(
        tmp_path / "drm.epub",
        chapters,
        ["c.xhtml"],
        extra={"META-INF/encryption.xml": encryption("http://www.w3.org/2001/04/xmlenc#aes128-cbc")},
    )
    with pytest.raises(UnreadableFile, match="DRM"):
        parse_file(drm)


# --- .rtf -----------------------------------------------------------------------------------------


def _rtf(path: Path, body: str, head: str = r"\ansi\ansicpg1252\deff0") -> Path:
    path.write_bytes((r"{\rtf1" + head + body + "}").encode("latin-1"))
    return path


def test_rtf_paragraphs_tabs_hex_and_unicode_escapes(tmp_path: Path):
    # Real writers put a line break after \par, which also ends the control word.
    body = "\n".join(
        [
            r"{\fonttbl{\f0\fnil\fcharset0 Calibri;}}{\colortbl;\red255\green0\blue0;}",
            r"{\stylesheet{\s1 heading 1;}}{\info{\title Secret title}{\author Someone}}",
            r"{\*\generator Msftedit 5.41;}{\*\listtable{\list\listid1}}",
            r"\pard\f0\fs22 Caf\'e9 menu\tab 5\'80\par",
            r"Price: \u8364? and \u1513?\u1500?\u1493?\u1501? shalom\par",
            r"Escapes: \{braces\} and a \\ backslash, \~nbsp, a\_b, soft\-hyphen\par",
            r"{\pict\wmetafile8\picw100 0102030405060708090a}",
            r"{\b Bold} and {\i italic}\line next line\par",
            r"\emdash  \lquote quoted\rquote  \bullet  end",  # each control word eats one following space
        ]
    )
    text = parse_file(_rtf(tmp_path / "note.rtf", body))
    assert text is not None
    assert text.split("\n\n") == [
        "Café menu\t5€",
        "Price: € and שלום shalom",
        "Escapes: {braces} and a \\ backslash,  nbsp, a-b, softhyphen",
        "Bold and italic\nnext line",
        "— ‘quoted’ • end",
    ]
    for dropped in ("Calibri", "Secret title", "Someone", "Msftedit", "heading 1", "0102030405", "255"):
        assert dropped not in text


def test_rtf_unicode_fallback_length_surrogate_pairs_and_binary_data(tmp_path: Path):
    body = (
        r"\uc2 a\u233\'e9\'e9b "  # \uc2: two fallback bytes follow, both swallowed
        r"\uc0 c\u233 d "  # \uc0: nothing follows, so the next character is real text
        r"\uc1 \u-10179?\u-8704? emoji "  # U+1F600 as a surrogate pair, one fallback byte each
        r"\u-10179? broken\par"  # a lone surrogate half becomes U+FFFD, never an unencodable string
        r"{\*\unknown dropped text}{\*\shpinst kept text}"
        r"{\pict\bin8 {}\\\'zz}after"  # raw binary that looks like RTF syntax is skipped by length
    )
    text = parse_file(_rtf(tmp_path / "u.rtf", body))
    assert text is not None
    text.encode("utf-8")  # must be storable
    assert "a\u00e9b c\u00e9d " in text  # \uc2 swallowed both fallback bytes
    assert "\U0001f600 emoji " in text
    assert "\ufffd broken" in text
    assert "dropped text" not in text and "kept text" in text and text.endswith("kept textafter")


def test_rtf_hebrew_through_font_charset_and_ansicpg(tmp_path: Path):
    # WordPad style: bytes under a font with \fcharset177 are Windows-1255 (Hebrew).
    shalom = r"\'f9\'ec\'e5\'ed"
    body = r"{\fonttbl{\f0\fcharset0 Arial;}{\f1\fcharset177 David;}}" rf"\f1 {shalom}\f0  hello \f1 {shalom}\par"
    text = parse_file(_rtf(tmp_path / "he.rtf", body))
    assert text == "שלום hello שלום"
    # No per-font charset: the document's \ansicpg decides.
    only_cp = _rtf(tmp_path / "he2.rtf", rf"{shalom}", head=r"\ansi\ansicpg1255\deff0")
    assert parse_file(only_cp) == "שלום"


def test_rtf_multibyte_code_page_pair_split_by_a_line_break(tmp_path: Path):
    # Shift-JIS bytes 93 FA 96 7B are two characters; writers may wrap the line between \'hh tokens.
    body = "\\'93\n\\'fa\\'96\n\\'7b\\par"
    assert parse_file(_rtf(tmp_path / "ja.rtf", body, head=r"\ansi\ansicpg932")) == "日本"


def test_rtf_table_cells_become_tabs_and_rows_become_lines(tmp_path: Path):
    body = r"\trowd\cellx1000\cellx2000\intbl A\cell B\cell\row\pard C\par"
    assert parse_file(_rtf(tmp_path / "t.rtf", body)) == "A\tB\nC"


def test_rtf_that_is_not_rtf_or_too_deep_is_refused(tmp_path: Path):
    plain = tmp_path / "plain.rtf"
    plain.write_text("just some text, not RTF")
    with pytest.raises(UnreadableFile, match="not an RTF"):
        parse_file(plain)
    deep = tmp_path / "deep.rtf"
    deep.write_bytes(b"{\\rtf1" + b"{" * 5000)
    with pytest.raises(UnreadableFile, match="nested too deeply"):
        parse_file(deep)


# --- .ipynb and the extra plain-text extensions ---------------------------------------------------


def test_ipynb_reads_markdown_and_code_sources_but_not_outputs(tmp_path: Path):
    notebook = {
        "nbformat": 4,
        "metadata": {"language_info": {"name": "python"}},
        "cells": [
            {"cell_type": "markdown", "source": ["# Churn analysis\n", "We compare **cohorts**."]},
            {
                "cell_type": "code",
                "source": "df = load()\ndf.describe()",
                "outputs": [{"output_type": "stream", "text": ["SECRET OUTPUT\n"]}],
            },
            {"cell_type": "raw", "source": "raw cell"},
            {"cell_type": "code", "source": ["  \n"], "outputs": []},  # blank cells are dropped
        ],
    }
    path = tmp_path / "analysis.ipynb"
    path.write_text(json.dumps(notebook), encoding="utf-8")
    assert parse_file(path) == "# Churn analysis\nWe compare **cohorts**.\n\n```python\ndf = load()\ndf.describe()\n```"


def test_ipynb_nbformat3_and_invalid_json(tmp_path: Path):
    old = {"nbformat": 3, "worksheets": [{"cells": [{"cell_type": "code", "input": ["x = 1"], "language": "python"}]}]}
    path = tmp_path / "old.ipynb"
    path.write_text(json.dumps(old), encoding="utf-8")
    assert parse_file(path) == "```python\nx = 1\n```"
    bad = tmp_path / "bad.ipynb"
    bad.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ValueError):
        parse_file(bad)


@pytest.mark.parametrize("ext", [".markdown", ".mdx", ".org", ".tex", ".tsv", ".log", ".ini", ".cfg"])
def test_extra_plain_text_extensions_are_parsed_as_text(tmp_path: Path, ext: str):
    path = tmp_path / f"file{ext}"
    path.write_bytes("héllo\tworld\n".encode("cp1252"))
    assert ext in TEXT_EXTENSIONS
    assert parse_file(path) == "héllo\tworld\n"


def test_default_extensions_include_pptx_and_xlsx():
    assert {".pptx", ".xlsx"} <= set(DEFAULT_EXTENSIONS)


def test_default_extensions_include_odf_epub_and_rtf():
    assert {".odt", ".ods", ".odp", ".epub", ".rtf"} <= set(DEFAULT_EXTENSIONS)


def test_default_extensions_include_notebooks_and_text_but_not_noisy_text():
    assert {".ipynb", ".markdown", ".mdx", ".org", ".tex", ".tsv"} <= set(DEFAULT_EXTENSIONS)
    # Parseable, but opt-in through LOCUS_EXTENSIONS: ini/cfg files often hold credentials. .log is default
    # since W2 (size-capped, repetitive logs skipped; see tests/test_file_type_coverage.py).
    assert not {".ini", ".cfg"} & set(DEFAULT_EXTENSIONS)


@pytest.mark.parametrize(
    "name", ["old.doc", "old.xls", "old.ppt", "page.pages", "slides.key", "sheet.numbers", "x.png"]
)
def test_unsupported_types_still_return_none(tmp_path: Path, name: str):
    path = tmp_path / name
    path.write_bytes(b"\xd0\xcf\x11\xe0 whatever")
    assert parse_file(path) is None


def test_estimator_counts_pptx_and_xlsx_as_their_own_kind():
    assert {kind_of(".pptx"), kind_of(".xlsx")} == {"office"}


def test_estimator_counts_odf_epub_and_rtf_as_their_own_kind():
    assert {kind_of(e) for e in (".odt", ".ods", ".odp", ".epub", ".rtf")} == {"office"}


def test_estimator_kinds_for_notebooks_and_text():
    assert kind_of(".ipynb") == "data" and kind_of(".tsv") == "data" and kind_of(".markdown") == "prose"


# --- safety ---------------------------------------------------------------------------------------

OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # the wrapper around a password-protected Office file
PADDING = "<!-- " + "x" * 2500 + " -->"


def _pptx_with_slide_xml(path: Path, slide_xml: str | bytes) -> Path:
    return _make_pptx(path, [_slide(_shape(["ok"]))], extra={"ppt/slides/slide1.xml": slide_xml})


def test_member_over_the_size_limit_is_refused_by_its_declared_size(tmp_path: Path):
    path = _pptx_with_slide_xml(tmp_path / "bomb.pptx", " " * (MAX_MEMBER_BYTES + 1))
    assert path.stat().st_size < 1_000_000  # a real bomb: tiny on disk, 50 MB+ unpacked
    with pytest.raises(UnreadableFile, match=r"unpacks to \d+ MB, over the 50 MB read limit"):
        parse_file(path)


def test_member_limit_is_applied_to_pptx_and_xlsx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(docformats, "MAX_MEMBER_BYTES", 2000)
    fixtures = [
        _pptx_with_slide_xml(tmp_path / "a.pptx", _slide(_shape(["s"])) + PADDING),
        _make_xlsx(
            tmp_path / "b.xlsx",
            [("S", [["v"]])],
            extra={"xl/sharedStrings.xml": f'{XML_HEAD}<sst xmlns="{NS_S}"/>{PADDING}'},
        ),
    ]
    for path in fixtures:
        with pytest.raises(UnreadableFile, match="read limit"):
            parse_file(path)


def test_member_limit_is_applied_to_odf_epub_and_rtf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(docformats, "MAX_MEMBER_BYTES", 2000)
    fixtures = [
        _make_odf(tmp_path / "c.odt", "text", "<text:p>t</text:p>" + PADDING),
        _make_epub(tmp_path / "d.epub", {"c.xhtml": "<p>x</p>" + PADDING}, ["c.xhtml"]),
    ]
    big_rtf = tmp_path / "e.rtf"
    big_rtf.write_bytes(b"{\\rtf1 " + b"a" * 3000 + b"}")
    for path in [*fixtures, big_rtf]:
        with pytest.raises(UnreadableFile, match="read limit"):
            parse_file(path)


def test_total_unpacked_size_across_members_is_limited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(docformats, "MAX_TOTAL_BYTES", 3000)
    slides = [_slide(_shape([f"slide {i}"])) + "<!-- " + "y" * 700 + " -->" for i in range(1, 8)]
    path = _make_pptx(tmp_path / "many.pptx", slides)
    with pytest.raises(UnreadableFile, match="read limit"):
        parse_file(path)


def test_a_stream_longer_than_declared_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # zipfile itself stops at the declared size; this checks the second line of defence by faking a
    # stream that keeps going.
    monkeypatch.setattr(docformats, "MAX_MEMBER_BYTES", 1000)
    path = _pptx_with_slide_xml(tmp_path / "lie.pptx", "<p/>")
    monkeypatch.setattr(zipfile.ZipFile, "open", lambda self, info, *a, **k: io.BytesIO(b"x" * 5000))
    with pytest.raises(UnreadableFile, match="more than"):
        parse_file(path)


DOCTYPE_VARIANTS = [
    pytest.param(BOMB, id="doctype+entity"),
    pytest.param('<!DOCTYPE foo SYSTEM "http://example.invalid/x.dtd">', id="doctype-only"),
    pytest.param('<!doctype foo [<!entity x "y">]>', id="lowercase"),
]


def _inject_dtd(path: Path, member: str, dtd: str) -> Path:
    """Rewrite *member* of the zip at *path* with *dtd* right after its XML declaration."""
    with zipfile.ZipFile(path) as zf:
        members = {n: zf.read(n) for n in zf.namelist()}
    assert members[member].startswith(XML_HEAD.encode())
    members[member] = members[member].replace(b"?>", b"?>" + dtd.encode(), 1)
    return _zip(path, members)


@pytest.mark.parametrize("dtd", DOCTYPE_VARIANTS)
def test_xml_with_doctype_or_entity_is_refused_in_pptx_and_xlsx(tmp_path: Path, dtd: str):
    def xlsx(name: str) -> Path:
        return _make_xlsx(tmp_path / name, [("S", [["v"]])])

    cases = [
        _inject_dtd(_make_pptx(tmp_path / "a.pptx", [_slide(_shape(["&lol2;"]))]), "ppt/slides/slide1.xml", dtd),
        _inject_dtd(_make_pptx(tmp_path / "a2.pptx", [_slide(_shape(["x"]))]), "ppt/presentation.xml", dtd),
        _inject_dtd(xlsx("b.xlsx"), "xl/sharedStrings.xml", dtd),
        _inject_dtd(xlsx("c.xlsx"), "xl/worksheets/sheet1.xml", dtd),
        _inject_dtd(xlsx("d.xlsx"), "xl/workbook.xml", dtd),
    ]
    for path in cases:
        with pytest.raises(UnreadableFile, match="DOCTYPE or ENTITY"):
            parse_file(path)


@pytest.mark.parametrize("dtd", DOCTYPE_VARIANTS)
def test_xml_with_doctype_or_entity_is_refused_in_odf_and_epub(tmp_path: Path, dtd: str):
    cases = [
        _inject_dtd(_make_odf(tmp_path / "e.odt", "text", "<text:p>&lol2;</text:p>"), "content.xml", dtd),
        _inject_dtd(_make_odf(tmp_path / "f.ods", "spreadsheet", "<table:table/>"), "content.xml", dtd),
        _inject_dtd(_make_odf(tmp_path / "g.odp", "presentation", "<draw:page/>"), "content.xml", dtd),
        _inject_dtd(_make_epub(tmp_path / "h.epub", {"c.xhtml": "<p>x</p>"}, ["c.xhtml"]), "OEBPS/content.opf", dtd),
        _inject_dtd(
            _make_epub(tmp_path / "i.epub", {"c.xhtml": "<p>x</p>"}, ["c.xhtml"]), "META-INF/container.xml", dtd
        ),
    ]
    for path in cases:
        with pytest.raises(UnreadableFile, match="DOCTYPE or ENTITY"):
            parse_file(path)


def test_doctype_in_utf16_xml_is_refused_too(tmp_path: Path):
    slide = (_slide(_shape(["x"])).replace("<p:sld ", f"{BOMB}<p:sld ", 1)).replace("UTF-8", "UTF-16")
    path = _pptx_with_slide_xml(tmp_path / "u16.pptx", slide.encode("utf-16"))
    with pytest.raises(UnreadableFile, match="DOCTYPE or ENTITY"):
        parse_file(path)


def test_text_that_merely_mentions_a_doctype_is_fine(tmp_path: Path):
    # Escaped, so the bytes hold "&lt;!DOCTYPE", not a declaration.
    path = _make_pptx(tmp_path / "ok.pptx", [_slide(_shape(["start with &lt;!DOCTYPE html&gt; and &amp; more"]))])
    assert parse_file(path) == "Slide 1\nstart with <!DOCTYPE html> and & more"


@pytest.mark.parametrize("ext", [".pptx", ".xlsx"])
def test_password_protected_ooxml_file_is_refused_with_a_clear_message(tmp_path: Path, ext: str):
    path = tmp_path / f"locked{ext}"
    path.write_bytes(OLE_MAGIC + b"\x00" * 600)  # an OLE compound file, not a zip
    with pytest.raises(UnreadableFile, match="password-protected"):
        parse_file(path)


@pytest.mark.parametrize("ext", [".odt", ".ods", ".odp", ".epub"])
def test_ole_wrapped_file_with_an_odf_or_epub_name_is_refused_too(tmp_path: Path, ext: str):
    path = tmp_path / f"locked{ext}"
    path.write_bytes(OLE_MAGIC + b"\x00" * 600)
    with pytest.raises(UnreadableFile, match="password-protected"):
        parse_file(path)


def test_odf_with_encryption_data_is_refused(tmp_path: Path):
    manifest = (
        f'{XML_HEAD}<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0">'
        '<manifest:file-entry manifest:full-path="content.xml"><manifest:encryption-data/></manifest:file-entry>'
        "</manifest:manifest>"
    )
    path = _make_odf(tmp_path / "locked.odt", "text", "<text:p>x</text:p>", extra={"META-INF/manifest.xml": manifest})
    with pytest.raises(UnreadableFile, match="password-protected"):
        parse_file(path)


def _assert_corrupt_inputs_raise(tmp_path: Path, ext: str) -> None:
    truncated = tmp_path / f"truncated{ext}"
    truncated.write_bytes(b"PK\x03\x04 this is not a complete zip file")
    with pytest.raises(zipfile.BadZipFile):
        parse_file(truncated)
    empty = tmp_path / f"empty{ext}"
    empty.write_bytes(b"")
    with pytest.raises(zipfile.BadZipFile):
        parse_file(empty)
    not_the_format = _zip(tmp_path / f"other{ext}", {"readme.txt": "a perfectly good zip with no document in it"})
    with pytest.raises(UnreadableFile):
        parse_file(not_the_format)


@pytest.mark.parametrize("ext", [".pptx", ".xlsx"])
def test_corrupt_or_wrong_ooxml_container_raises_like_a_bad_docx(tmp_path: Path, ext: str):
    _assert_corrupt_inputs_raise(tmp_path, ext)


@pytest.mark.parametrize("ext", [".odt", ".ods", ".odp", ".epub"])
def test_corrupt_or_wrong_odf_or_epub_container_raises_like_a_bad_docx(tmp_path: Path, ext: str):
    _assert_corrupt_inputs_raise(tmp_path, ext)


def test_malformed_xml_inside_a_valid_zip_raises(tmp_path: Path):
    path = _pptx_with_slide_xml(tmp_path / "bad.pptx", "<p:sld><unclosed>")
    with pytest.raises(ET.ParseError):
        parse_file(path)


# --- through the indexer ----------------------------------------------------------------------------


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


def _index(tmp_path: Path, root: Path):
    settings = _settings(tmp_path)
    embedder = HashEmbedder()
    store = ChromaStore(settings.data_dir, settings.collection, embedder.name)
    stats = index_folder(root, embedder, store, settings, log=lambda _: None)
    return settings, embedder, store, stats


def test_indexer_indexes_pptx_and_xlsx_and_counts_bad_files_without_stopping(tmp_path: Path):
    root = tmp_path / "docs"
    root.mkdir()
    _make_pptx(
        root / "deck.pptx", [_slide(_shape(["Quarterly pricing review"], ph="title"))], notes={1: ["zebra budget"]}
    )
    _make_xlsx(root / "budget.xlsx", [("Q3", [["Hosting", 120], ["Domain", 12]])])
    (root / "zzz-broken.xlsx").write_bytes(b"PK\x03\x04 truncated")
    (root / "locked.pptx").write_bytes(OLE_MAGIC + b"\x00" * 100)
    (root / "plain.md").write_text("A plain markdown note.")

    settings, embedder, store, stats = _index(tmp_path, root)

    assert stats.files_seen == 5 and stats.files_indexed == 3
    assert len(stats.errors) == 2
    assert any("zzz-broken.xlsx" in e and "BadZipFile" in e for e in stats.errors)
    assert any("locked.pptx" in e and "password-protected" in e for e in stats.errors)
    assert {Path(p).name for p in store.list_paths()} == {"deck.pptx", "budget.xlsx", "plain.md"}
    hits = store.query(embedder.embed_query("zebra budget"), k=1)
    assert hits and hits[0].path.endswith("deck.pptx")

    # A second run skips the unchanged good files and reports the same two bad ones again.
    again = index_folder(root, embedder, store, settings, log=lambda _: None)
    assert again.files_indexed == 0 and again.files_skipped == 3 and len(again.errors) == 2


def test_indexer_indexes_odf_epub_and_rtf(tmp_path: Path):
    root = tmp_path / "docs"
    root.mkdir()
    _make_odf(root / "policy.odt", "text", "<text:p>Refunds are paid within five business days.</text:p>")
    _make_epub(
        root / "book.epub",
        {"c.xhtml": "<html><body><p>The lighthouse keeper sailed north.</p></body></html>"},
        ["c.xhtml"],
    )
    _rtf(root / "memo.rtf", r"\pard Memo about the offsite\par")
    (root / "broken.epub").write_bytes(b"PK\x03\x04 truncated")

    _settings_, embedder, store, stats = _index(tmp_path, root)

    assert stats.files_seen == 4 and stats.files_indexed == 3 and len(stats.errors) == 1
    assert {Path(p).name for p in store.list_paths()} == {"policy.odt", "book.epub", "memo.rtf"}
    hits = store.query(embedder.embed_query("lighthouse keeper sailed north"), k=1)
    assert hits and hits[0].path.endswith("book.epub")


def test_file_that_became_corrupt_keeps_its_old_chunks(tmp_path: Path):
    root = tmp_path / "docs"
    root.mkdir()
    deck = _make_pptx(root / "deck.pptx", [_slide(_shape(["Original slide text"]))])
    settings, embedder, store, _stats = _index(tmp_path, root)
    assert store.list_paths() == [str(deck.resolve())]

    deck.write_bytes(b"PK\x03\x04 corrupted later")
    stats = index_folder(root, embedder, store, settings, log=lambda _: None)
    assert len(stats.errors) == 1 and stats.files_indexed == 0
    assert store.list_paths() == [str(deck.resolve())]  # same as a bad PDF: the old chunks stay


def test_a_large_workbook_is_capped_per_file_on_a_line_boundary(tmp_path: Path, monkeypatch) -> None:
    from locus.ingest import parsers

    monkeypatch.delenv(parsers.SPREADSHEET_CAP_ENV, raising=False)
    monkeypatch.setitem(parsers._DOCUMENT_READERS, ".xlsx", lambda _p: "Sheet: S\n" + "a\tb\n" * 200_000)
    f = tmp_path / "big.xlsx"
    f.write_bytes(b"x")
    text = parsers.parse_file(f)
    assert text is not None
    body, _, last = text.rpartition("\n")
    assert last.startswith("[truncated: only the first 200,000 characters of this workbook were indexed")
    assert parsers.SPREADSHEET_CAP_ENV in last  # the note says how to raise it
    assert len(body) <= parsers.DEFAULT_SPREADSHEET_MAX_CHARS
    assert body.endswith("a\tb")  # cut on a row boundary, never mid-row


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1000", 1000), ("1_000_000", 1_000_000), ("0", None), ("lots", 200_000), ("", 200_000), ("-5", 200_000)],
)
def test_the_user_can_raise_or_remove_the_workbook_cap(monkeypatch, value: str, expected: int | None) -> None:
    from locus.ingest import parsers

    monkeypatch.setenv(parsers.SPREADSHEET_CAP_ENV, value)
    assert parsers.spreadsheet_char_cap() == expected


def test_no_cap_keeps_the_whole_workbook(tmp_path: Path, monkeypatch) -> None:
    from locus.ingest import parsers

    monkeypatch.setenv(parsers.SPREADSHEET_CAP_ENV, "0")
    whole = "Sheet: S\n" + "a\tb\n" * 200_000
    monkeypatch.setitem(parsers._DOCUMENT_READERS, ".ods", lambda _p: whole)
    f = tmp_path / "big.ods"
    f.write_bytes(b"x")
    assert parsers.parse_file(f) == whole


def test_prose_documents_are_not_capped(tmp_path: Path, monkeypatch) -> None:
    from locus.ingest import parsers

    monkeypatch.setitem(parsers._DOCUMENT_READERS, ".epub", lambda _p: "word " * 200_000)
    f = tmp_path / "book.epub"
    f.write_bytes(b"x")
    text = parsers.parse_file(f)
    assert text is not None and "[truncated" not in text and len(text) == 1_000_000
