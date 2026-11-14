"""Stdlib-only text extraction for office-style documents: .pptx, .xlsx, OpenDocument (.odt, .ods,
.odp), .epub and .rtf. parsers.py dispatches here; unsupported types never reach this module.

The first four are zip containers of XML, so they are read with zipfile + ElementTree instead of one
third-party package per format (the engine's install stays small and nothing new needs a licence
review). RTF is a small control-word stripper over the raw bytes.

These files come from arbitrary folders on the user's machine, so every read is defensive:
- zip bombs: a member whose declared size is over MAX_MEMBER_BYTES, a file whose reads would pass
  MAX_TOTAL_BYTES, and a member that unpacks to more than it declared are all refused; reads carry a
  byte limit, nothing is extracted to disk;
- entity expansion: an XML member containing a DOCTYPE or ENTITY declaration is refused before it
  reaches the parser (none of these formats needs one). EPUB chapters are the exception: they are
  read with html.parser, which never expands entities, and XHTML chapters normally start with a DOCTYPE;
- protected files: a password-protected Office file (an OLE wrapper, not a zip), ODF encryption data
  and DRM-protected EPUBs are refused with a plain message instead of being parsed into garbage.

A refusal raises UnreadableFile (a ValueError); a corrupt zip raises zipfile.BadZipFile. The indexer
catches any exception per file and counts it in IndexStats.errors, exactly as for a bad PDF or DOCX,
so one bad file never stops a run and its previous chunks stay in the index.
"""

from __future__ import annotations

import codecs
import io
import posixpath
import re
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from functools import lru_cache
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree as ET

# Zip-bomb limits: per member, and for everything read from one file. A real slide deck or workbook
# has XML parts of a few MB; media (images, video) is never read at all.
MAX_MEMBER_BYTES = 50 * 1024 * 1024
MAX_TOTAL_BYTES = 200 * 1024 * 1024
# A sheet is cut here (and says so) so one giant export cannot turn into millions of chunks.
MAX_SHEET_ROWS = 5000
MAX_SHEET_COLUMNS = 200
# chunker.py splits on blank lines first and only then on sentences and spaces, so a sheet written as
# one block would be cut in the middle of rows (and its first chunk could be just "Sheet:"). A blank
# line every ~1,200 characters (a few hundred tokens, under a chunk) gives it whole-row boundaries.
SHEET_GROUP_CHARS = 1200

_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # the wrapper Office puts around a password-protected file
_DTD = re.compile(rb"<!(?:DOCTYPE|ENTITY)", re.IGNORECASE)


class UnreadableFile(ValueError):
    """A file Locus refuses to parse: protected, over a safety limit, or not what its extension says."""


def _mb(n: int) -> str:
    return f"{n / (1024 * 1024):.0f} MB"


class _Package:
    """A zip container opened read-only. Every read is capped; nothing is extracted to disk."""

    def __init__(self, path: Path) -> None:
        with open(path, "rb") as fh:
            if fh.read(len(_OLE_MAGIC)) == _OLE_MAGIC:
                # "Encrypt with password" wraps the whole package in an OLE compound file, and so
                # do the legacy .doc/.xls/.ppt formats: neither is a zip.
                raise UnreadableFile("password-protected or legacy binary Office file (not a zip package)")
        self._zip = zipfile.ZipFile(path)  # BadZipFile for a corrupt or truncated file
        self._names = set(self._zip.namelist())
        self._remaining = MAX_TOTAL_BYTES

    def __enter__(self) -> _Package:
        return self

    def __exit__(self, *exc: object) -> None:
        self._zip.close()

    def has(self, name: str) -> bool:
        return name in self._names

    def names(self) -> list[str]:
        return sorted(self._names)

    def read(self, name: str) -> bytes:
        if name not in self._names:
            raise UnreadableFile(f"{name} is missing from the package")
        info = self._zip.getinfo(name)
        if info.flag_bits & 0x1:
            raise UnreadableFile(f"{name} is encrypted")
        limit = min(MAX_MEMBER_BYTES, self._remaining)
        if info.file_size > limit:
            raise UnreadableFile(f"{name} unpacks to {_mb(info.file_size)}, over the {_mb(limit)} read limit")
        with self._zip.open(info) as fh:
            data = fh.read(limit + 1)  # one byte past the limit: a stream longer than it declared is refused too
        if len(data) > limit:
            raise UnreadableFile(f"{name} unpacks to more than the {_mb(limit)} read limit")
        self._remaining -= len(data)
        return data


def _check_no_dtd(data: bytes, name: str) -> None:
    # UTF-16/32 XML has NULs between the ASCII bytes; drop them so the scan still sees "<!DOCTYPE".
    scan = data.replace(b"\x00", b"") if b"\x00" in data[:8] else data
    if _DTD.search(scan):
        raise UnreadableFile(f"{name} declares a DOCTYPE or ENTITY (refused to avoid entity expansion)")


def _xml(pkg: _Package, name: str) -> ET.Element:
    data = pkg.read(name)
    _check_no_dtd(data, name)
    return ET.fromstring(data)


def check_docx_package(path: Path) -> None:
    """Hold a .docx to the limits the other containers get, before python-docx opens it (S-09). python-docx loads
    every member of the package into memory (media included) and parses every XML part, with no limit of its own, so a
    zip bomb or a part with an entity declaration would reach it unchecked. Refused with UnreadableFile: a member whose
    declared size is over MAX_MEMBER_BYTES, a package whose members declare more than MAX_TOTAL_BYTES together, an
    encrypted member, an XML or relationship part that unpacks to more than it declared, and an XML part with a
    DOCTYPE or ENTITY declaration. Also refuses a password-protected file (an OLE wrapper) in plain words."""
    with _Package(path) as pkg:
        total = 0
        for info in pkg._zip.infolist():
            if info.flag_bits & 0x1:
                raise UnreadableFile(f"{info.filename} is encrypted")
            if info.file_size > MAX_MEMBER_BYTES:
                raise UnreadableFile(
                    f"{info.filename} unpacks to {_mb(info.file_size)}, over the {_mb(MAX_MEMBER_BYTES)} read limit"
                )
            total += info.file_size
        if total > MAX_TOTAL_BYTES:
            raise UnreadableFile(f"the package unpacks to {_mb(total)}, over the {_mb(MAX_TOTAL_BYTES)} read limit")
        for name in pkg.names():
            if name.endswith((".xml", ".rels")):
                _check_no_dtd(pkg.read(name), name)


def _local(tag: str) -> str:
    """Element name without its namespace, so the same code reads transitional and strict OOXML."""
    return tag.rsplit("}", 1)[-1]


def _rel_id(el: ET.Element) -> str:
    """The relationship id an element points at: its namespaced `r:id` attribute, not the plain `id`
    some of them also carry (a <p:sldId id="256" r:id="rId2"/> has both)."""
    return next((value for key, value in el.attrib.items() if key.endswith("}id")), "")


def _child(el: ET.Element, local: str) -> ET.Element | None:
    return next((c for c in el if _local(c.tag) == local), None)


def _text_of(el: ET.Element | None) -> str:
    return (el.text or "") if el is not None else ""


def _resolve(base: str, target: str) -> str | None:
    """A relationship/href target as a zip member name: relative to *base*, fragment dropped,
    percent-escapes undone. None when it is empty or climbs out of the package."""
    target = unquote(target.split("#", 1)[0].split("?", 1)[0])
    if not target:
        return None
    name = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join(base, target))
    return None if name.startswith("..") else name


def _relationships(pkg: _Package, part: str | None) -> list[tuple[str, str, str]]:
    """(Id, Type, member name) for each internal relationship of *part* (None: the package itself)."""
    if part is None:
        base, rels = "", "_rels/.rels"
    else:
        base = posixpath.dirname(part)
        rels = posixpath.join(base, "_rels", posixpath.basename(part) + ".rels")
    if not pkg.has(rels):
        return []
    found = []
    for el in _xml(pkg, rels):
        if _local(el.tag) != "Relationship" or el.get("TargetMode") == "External":
            continue
        target = _resolve(base, el.get("Target", ""))
        if target:
            found.append((el.get("Id", ""), el.get("Type", ""), target))
    return found


def _main_part(pkg: _Package, default: str) -> str:
    """The package's main document (workbook.xml, presentation.xml), per _rels/.rels."""
    for _id, rel_type, target in _relationships(pkg, None):
        if rel_type.endswith("/officeDocument") and pkg.has(target):
            return target
    return default


@dataclass
class _Sheet:
    """One sheet's rows as tab-separated lines, under the row and column limits."""

    name: str
    lines: list[str] = field(default_factory=list)
    truncated_rows: bool = False
    truncated_cols: bool = False

    def add_row(self, cells: dict[int, str]) -> bool:
        """Add one row ({column index: text}). False once the sheet is full: stop reading it."""
        if not cells:
            return True
        if len(self.lines) >= MAX_SHEET_ROWS:
            self.truncated_rows = True
            return False
        if max(cells) >= MAX_SHEET_COLUMNS:
            self.truncated_cols = True
        width = min(max(cells) + 1, MAX_SHEET_COLUMNS)
        # A tab or newline inside a cell would break the row; fold all whitespace to single spaces.
        values = [re.sub(r"\s+", " ", cells.get(i, "")).strip() for i in range(width)]
        while values and not values[-1]:
            values.pop()
        if values:
            self.lines.append("\t".join(values))
        return True

    def render(self) -> str:
        if not self.lines:
            return ""
        out = [f"Sheet: {self.name}"]
        size = 0
        for line in self.lines:
            if size >= SHEET_GROUP_CHARS:
                out.append("")
                size = 0
            out.append(line)
            size += len(line) + 1
        if self.truncated_rows:
            out.append(f"[truncated: rows after the first {MAX_SHEET_ROWS} were not indexed]")
        if self.truncated_cols:
            out.append(f"[truncated: columns after the first {MAX_SHEET_COLUMNS} were not indexed]")
        return "\n".join(out)


# --- .pptx ------------------------------------------------------------------------------------

# Placeholders that repeat on every slide (footer, date, slide number) or hold no text (slide image).
_SKIP_PLACEHOLDERS = frozenset({"sldNum", "dt", "ftr", "hdr", "sldImg"})


def _placeholder_type(shape: ET.Element) -> str | None:
    """The `type` of a shape's placeholder (title, body, sldNum...), if it is one."""
    for el in shape.iter():
        if _local(el.tag) == "ph":
            return el.get("type")
        if _local(el.tag) in ("txBody", "spPr"):
            break
    return None


def _paragraph_text(p: ET.Element) -> str:
    """One DrawingML paragraph: its runs and fields, with line breaks."""
    parts = []
    for node in p:
        kind = _local(node.tag)
        if kind in ("r", "fld"):
            parts.append(_text_of(_child(node, "t")))
        elif kind == "br":
            parts.append("\n")
    return "".join(parts)


def _drawing_lines(node: ET.Element) -> Iterator[str]:
    """Text of a slide or notes page in document order, one line per paragraph or table row."""
    for child in node:
        kind = _local(child.tag)
        if kind == "p":
            text = _paragraph_text(child)
            if text.strip():
                yield text
        elif kind == "tbl":
            for row in child:
                if _local(row.tag) != "tr":
                    continue
                cells = [" ".join(_drawing_lines(tc)) for tc in row if _local(tc.tag) == "tc"]
                line = " | ".join(c for c in cells if c)
                if line:
                    yield line
        elif kind == "sp" and _placeholder_type(child) in _SKIP_PLACEHOLDERS:
            continue
        elif kind == "AlternateContent":
            # Choice and Fallback carry the same content twice; read the first only.
            branch = _child(child, "Choice") or _child(child, "Fallback")
            if branch is not None:
                yield from _drawing_lines(branch)
        else:
            yield from _drawing_lines(child)


def _slide_parts(pkg: _Package, presentation: str) -> list[str]:
    """Slide members in presentation order (sldIdLst), not in file-name order."""
    by_id = {rid: target for rid, _type, target in _relationships(pkg, presentation)}
    parts = []
    for el in _xml(pkg, presentation).iter():
        if _local(el.tag) == "sldId":
            target = by_id.get(_rel_id(el))
            if target and pkg.has(target):
                parts.append(target)
    if parts:
        return parts
    # No usable slide list: fall back to slide1.xml, slide2.xml... in numeric order.
    numbered = [(int(m.group(1)), n) for n in pkg.names() if (m := re.fullmatch(r"ppt/slides/slide(\d+)\.xml", n))]
    return [n for _, n in sorted(numbered)]


def read_pptx(path: Path) -> str:
    """Slide text in slide order under a `Slide N` line, then the speaker notes under `Notes:`."""
    with _Package(path) as pkg:
        slides = []
        for number, part in enumerate(_slide_parts(pkg, _main_part(pkg, "ppt/presentation.xml")), start=1):
            lines = list(_drawing_lines(_xml(pkg, part)))
            notes: list[str] = []
            for _rid, rel_type, target in _relationships(pkg, part):
                if rel_type.endswith("/notesSlide") and pkg.has(target):
                    notes = list(_drawing_lines(_xml(pkg, target)))
                    break
            if lines or notes:  # a slide with nothing to say is left out, but still counts in the numbering
                slides.append("\n".join([f"Slide {number}", *lines, *(["Notes:", *notes] if notes else [])]))
    return "\n\n".join(slides)


# --- .xlsx ------------------------------------------------------------------------------------


def _col_index(ref: str) -> int | None:
    """'C7' -> 2, 'AB1' -> 27 (zero-based column of an A1 cell reference)."""
    n = 0
    for ch in ref:
        if "A" <= ch <= "Z":
            n = n * 26 + ord(ch) - 64
        else:
            break
    return n - 1 if n else None


def _shared_strings(data: bytes, name: str) -> list[str]:
    """The workbook's shared string table, streamed so a large one is never held as a tree."""
    _check_no_dtd(data, name)
    strings = []
    for _event, el in ET.iterparse(io.BytesIO(data), events=("end",)):
        if _local(el.tag) == "si":
            strings.append(_rich_text(el))
            el.clear()
    return strings


def _rich_text(el: ET.Element) -> str:
    """Text of a shared/inline string: its plain `t` or its rich-text runs, without phonetic hints."""
    parts = []
    for node in el:
        kind = _local(node.tag)
        if kind == "t":
            parts.append(node.text or "")
        elif kind == "r":
            parts.append(_text_of(_child(node, "t")))
    return "".join(parts)


def _cell_value(c: ET.Element, strings: list[str]) -> str:
    kind = c.get("t", "n")
    if kind == "inlineStr":
        inline = _child(c, "is")
        return _rich_text(inline) if inline is not None else ""
    v = _child(c, "v")
    raw = (v.text or "").strip() if v is not None else ""
    if kind == "s":
        try:
            return strings[int(raw)]
        except (ValueError, IndexError):
            return ""
    if kind == "b":
        return "TRUE" if raw == "1" else "FALSE"
    return raw  # numbers, formula results, dates (as the stored serial) and error codes as stored


def _xlsx_rows(data: bytes, name: str, strings: list[str]) -> Iterator[dict[int, str]]:
    """Rows of one worksheet as {column index: text}, streamed. Empty cells are left out."""
    _check_no_dtd(data, name)
    cells: dict[int, str] = {}
    next_col = 0
    for _event, el in ET.iterparse(io.BytesIO(data), events=("end",)):
        kind = _local(el.tag)
        if kind == "c":
            ref = el.get("r")
            col = _col_index(ref) if ref else None
            col = next_col if col is None else col
            next_col = col + 1
            value = _cell_value(el, strings)
            if value.strip():
                cells[col] = value
            el.clear()
        elif kind == "row":
            yield cells
            cells, next_col = {}, 0
            el.clear()


def read_xlsx(path: Path) -> str:
    """Each sheet as `Sheet: <name>` then its non-empty rows, cells separated by tabs."""
    with _Package(path) as pkg:
        workbook = _main_part(pkg, "xl/workbook.xml")
        rels = _relationships(pkg, workbook)
        by_id = {rid: target for rid, _type, target in rels}
        sheets: list[tuple[str, str | None]] = []
        for el in _xml(pkg, workbook).iter():
            if _local(el.tag) == "sheet":
                sheets.append((el.get("name") or f"Sheet{len(sheets) + 1}", by_id.get(_rel_id(el))))
        strings: list[str] = []
        shared = next((t for _id, rel_type, t in rels if rel_type.endswith("/sharedStrings")), "xl/sharedStrings.xml")
        if pkg.has(shared):
            strings = _shared_strings(pkg.read(shared), shared)
        blocks = []
        for number, (name, part) in enumerate(sheets, start=1):
            part = part or f"xl/worksheets/sheet{number}.xml"
            if not pkg.has(part):
                continue
            sheet = _Sheet(name)
            for row in _xlsx_rows(pkg.read(part), part, strings):
                if not sheet.add_row(row):
                    break
            if text := sheet.render():
                blocks.append(text)
    return "\n\n".join(blocks)


# --- OpenDocument (.odt, .ods, .odp) ----------------------------------------------------------

_NS = {
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
    "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
    "draw": "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0",
    "presentation": "urn:oasis:names:tc:opendocument:xmlns:presentation:1.0",
}


def _q(prefix: str, local: str) -> str:
    return f"{{{_NS[prefix]}}}{local}"


def _int_attr(el: ET.Element, qname: str, default: int) -> int:
    try:
        return max(int(el.get(qname, default)), 0)
    except ValueError:
        return default


_P, _H, _S, _C = _q("text", "p"), _q("text", "h"), _q("text", "s"), _q("text", "c")
_TAB, _BREAK, _NOTE = _q("text", "tab"), _q("text", "line-break"), _q("text", "note")
_TABLE, _ROW, _CELL = _q("table", "table"), _q("table", "table-row"), _q("table", "table-cell")
_COVERED_CELL, _NAME = _q("table", "covered-table-cell"), _q("table", "name")
_ROWS_REPEATED, _COLS_REPEATED = _q("table", "number-rows-repeated"), _q("table", "number-columns-repeated")
_ROW_GROUPS = frozenset(
    _q("table", n) for n in ("table-header-rows", "table-rows", "table-row-group", "table-header-row-group")
)
_PAGE, _FRAME, _NOTES = _q("draw", "page"), _q("draw", "frame"), _q("presentation", "notes")
_FRAME_CLASS = _q("presentation", "class")
_OFFICE_VALUE, _OFFICE_STRING = _q("office", "value"), _q("office", "string-value")
# Left out of the text: deleted text kept for tracked changes, comments, footnote numbers, and
# slide furniture (page number, header, footer, date) that repeats on every page.
_SKIP_INLINE = frozenset({_q("text", "note-citation"), _q("office", "annotation")})
_SKIP_BLOCKS = frozenset({_q("text", "tracked-changes"), _q("office", "annotation"), _NOTES})
_SKIP_FRAME_CLASSES = frozenset({"page-number", "header", "footer", "date-time"})
_ENCRYPTED = re.compile(rb"<[\w.-]*:?encryption-data[\s>/]")


def _odf_inline(el: ET.Element) -> str:
    """Text of one paragraph, with spaces, tabs and line breaks as the format spells them."""
    parts = [el.text or ""]
    for child in el:
        if child.tag == _S:
            parts.append(" " * min(_int_attr(child, _C, 1), 100))
        elif child.tag == _TAB:
            parts.append("\t")
        elif child.tag == _BREAK:
            parts.append("\n")
        elif child.tag == _NOTE:
            parts.append(f" [{_odf_inline(child).strip()}] ")  # footnote or endnote body, inline
        elif child.tag not in _SKIP_INLINE:
            parts.append(_odf_inline(child))
        parts.append(child.tail or "")
    return "".join(parts)


def _odf_table_rows(table: ET.Element) -> Iterator[ET.Element]:
    for child in table:
        if child.tag == _ROW:
            yield child
        elif child.tag in _ROW_GROUPS:
            yield from _odf_table_rows(child)


def _odf_blocks(node: ET.Element) -> Iterator[str]:
    """Paragraphs, headings and table rows (cells joined with ' | ') in document order."""
    for child in node:
        if child.tag in (_P, _H):
            line = _odf_inline(child).strip()
            if line:
                yield line
        elif child.tag == _TABLE:
            for row in _odf_table_rows(child):
                cells = [" ".join(_odf_blocks(c)) for c in row if c.tag in (_CELL, _COVERED_CELL)]
                line = " | ".join(c for c in cells if c)
                if line:
                    yield line
        elif child.tag in _SKIP_BLOCKS or (child.tag == _FRAME and child.get(_FRAME_CLASS) in _SKIP_FRAME_CLASSES):
            continue
        else:
            yield from _odf_blocks(child)


def _odf_body(path: Path, kind: str) -> ET.Element:
    """The `office:<kind>` element of content.xml (kind: text, spreadsheet or presentation)."""
    with _Package(path) as pkg:
        if pkg.has("META-INF/manifest.xml") and _ENCRYPTED.search(pkg.read("META-INF/manifest.xml")):
            raise UnreadableFile("password-protected OpenDocument file")
        root = _xml(pkg, "content.xml")
    for body in root.iter(_q("office", "body")):
        found = body.find(_q("office", kind))
        if found is not None:
            return found
    raise UnreadableFile(f"content.xml has no office:{kind} body")


def read_odt(path: Path) -> str:
    """Paragraphs, headings, lists and tables of a text document, blank-line separated."""
    return "\n\n".join(_odf_blocks(_odf_body(path, "text")))


def read_odp(path: Path) -> str:
    """Slide text under a `Slide N` line, then the speaker notes under `Notes:`."""
    slides = []
    for number, page in enumerate(_odf_body(path, "presentation").iter(_PAGE), start=1):
        lines = list(_odf_blocks(page))
        notes = [line for n in page.iter(_NOTES) for line in _odf_blocks(n)]
        if lines or notes:
            slides.append("\n".join([f"Slide {number}", *lines, *(["Notes:", *notes] if notes else [])]))
    return "\n\n".join(slides)


def _ods_cell_text(cell: ET.Element) -> str:
    text = " ".join(_odf_blocks(cell))  # the text the user sees, e.g. "10%" rather than 0.1
    return text or cell.get(_OFFICE_VALUE) or cell.get(_OFFICE_STRING) or ""


def read_ods(path: Path) -> str:
    """Each sheet as `Sheet: <name>` then its non-empty rows, cells separated by tabs."""
    blocks = []
    for table in _odf_body(path, "spreadsheet"):
        if table.tag != _TABLE:
            continue
        sheet = _Sheet(table.get(_NAME, ""))
        for row in _odf_table_rows(table):
            cells: dict[int, str] = {}
            col = 0
            for cell in row:
                if cell.tag not in (_CELL, _COVERED_CELL):
                    continue
                repeat = _int_attr(cell, _COLS_REPEATED, 1)
                text = _ods_cell_text(cell)
                if text:
                    # Repeat counts can be in the millions (trailing empty cells, filled columns):
                    # never expand past the column limit.
                    for i in range(min(repeat, MAX_SHEET_COLUMNS - col)):
                        cells[col + i] = text
                    if col + repeat > MAX_SHEET_COLUMNS:
                        sheet.truncated_cols = True
                col += repeat
            if not cells:
                continue  # an empty row, however many times it repeats
            if not all(sheet.add_row(cells) for _ in range(min(_int_attr(row, _ROWS_REPEATED, 1), MAX_SHEET_ROWS + 1))):
                break
        if text := sheet.render():
            blocks.append(text)
    return "\n\n".join(blocks)


# --- .epub ------------------------------------------------------------------------------------

_EPUB_DOC_TYPES = frozenset({"application/xhtml+xml", "text/html"})
# Algorithms that only obfuscate embedded fonts (IDPF's and Adobe's), named by what follows the URI
# scheme: they are identifiers, never fetched. Any other EncryptionMethod means DRM.
_FONT_OBFUSCATION = frozenset({"www.idpf.org/2008/embedding", "ns.adobe.com/pdf/enc#RC"})
_DROPPED_TAGS = frozenset({"head", "script", "style"})
_BLOCK_TAGS = frozenset(
    "address article aside blockquote body dd details div dl dt fieldset figcaption figure footer form "
    "h1 h2 h3 h4 h5 h6 header hr li main nav ol p pre section summary table tr ul".split()
)
_XML_ENCODING = re.compile(rb"""^\s*<\?xml[^>]*encoding=["']([A-Za-z0-9_.:-]+)["']""")


class _HTMLText(HTMLParser):
    """Visible text of an (X)HTML chapter: block elements become newlines, head/script/style are dropped."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "body":
            self._skip = 0  # a <head> whose end tag was left out must not swallow the chapter
        if tag in _DROPPED_TAGS:
            self._skip += 1
        elif tag in _BLOCK_TAGS or tag == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROPPED_TAGS:
            self._skip = max(self._skip - 1, 0)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")
        elif tag in ("td", "th"):
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def _decode_markup(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    data = data.removeprefix(b"\xef\xbb\xbf")
    declared = _XML_ENCODING.match(data[:200])
    for encoding in ([declared.group(1).decode("ascii")] if declared else []) + ["utf-8"]:
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("cp1252", errors="replace")


def _html_to_text(markup: str) -> str:
    parser = _HTMLText()
    parser.feed(markup)
    parser.close()
    lines = [re.sub(r"\s+", " ", line).strip() for line in "".join(parser.parts).split("\n")]
    # One blank line between blocks, none at the edges.
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def read_epub(path: Path) -> str:
    """Chapter text in spine (reading) order, tags stripped, block elements as line breaks."""
    with _Package(path) as pkg:
        container = _xml(pkg, "META-INF/container.xml")
        rootfile = next((el.get("full-path") for el in container.iter() if _local(el.tag) == "rootfile"), None)
        opf_path = _resolve("", rootfile or "")
        if not opf_path or not pkg.has(opf_path):
            raise UnreadableFile("EPUB has no package (OPF) document")
        if pkg.has("META-INF/encryption.xml"):
            for el in _xml(pkg, "META-INF/encryption.xml").iter():
                algorithm = el.get("Algorithm", "").partition("://")[2]
                if _local(el.tag) == "EncryptionMethod" and algorithm not in _FONT_OBFUSCATION:
                    raise UnreadableFile("DRM-protected EPUB")
        manifest: dict[str, tuple[str, str]] = {}
        spine: list[str] = []
        for section in _xml(pkg, opf_path):
            if _local(section.tag) == "manifest":
                for item in section:
                    manifest[item.get("id", "")] = (item.get("href", ""), item.get("media-type", ""))
            elif _local(section.tag) == "spine":
                spine = [ref.get("idref", "") for ref in section if _local(ref.tag) == "itemref"]
        chapters = []
        for idref in spine:
            href, media_type = manifest.get(idref, ("", ""))
            name = _resolve(posixpath.dirname(opf_path), href)
            if (media_type and media_type not in _EPUB_DOC_TYPES) or not name or not pkg.has(name):
                continue
            if text := _html_to_text(_decode_markup(pkg.read(name))):
                chapters.append(text)
    return "\n\n".join(chapters)


# --- .rtf -------------------------------------------------------------------------------------

# Destinations whose content is not body text: styles, colour tables, metadata, pictures and other
# embedded objects, field instructions, headers/footers, list definitions, shape properties.
_RTF_SKIP = frozenset(
    "stylesheet colortbl info pict object objdata header headerl headerr headerf footer footerl footerr "
    "footerf fldinst listtable listoverridetable rsidtbl generator themedata colorschememapping latentstyles "
    "datastore xmlnstbl revtbl filetbl pgptbl userprops nonshppict sp sn sv template".split()
)
# `{\*\name ...}` groups are optional destinations and are skipped, except those that carry text.
_RTF_KEEP_STAR = frozenset({"shpinst"})
_RTF_WORDS = {
    "par": "\n\n",
    "sect": "\n\n",
    "page": "\n\n",
    "line": "\n",
    "row": "\n",
    "nestrow": "\n",
    "tab": "\t",
    "cell": "\t",
    "nestcell": "\t",
    "emdash": "\u2014",
    "endash": "\u2013",
    "emspace": " ",
    "enspace": " ",
    "qmspace": " ",
    "bullet": "\u2022",
    "lquote": "\u2018",
    "rquote": "\u2019",
    "ldblquote": "\u201c",
    "rdblquote": "\u201d",
}
# Control symbols: \~ (no-break space), \_ (no-break hyphen), \- (optional hyphen), escaped braces
# and backslash, and a backslash before a line break, which RTF defines as \par.
_RTF_SYMBOLS = {
    b"~": " ",
    b"_": "-",
    b"-": "",
    b"\\": "\\",
    b"{": "{",
    b"}": "}",
    b"\n": "\n\n",
    b"\r": "\n\n",
}
# \fcharsetN -> code page, for \'hh bytes written under a font with its own charset (e.g. Hebrew text
# in a WordPad file). Charsets 0 and 1 follow the document's \ansicpgN (cp1252 when it has none).
_RTF_CHARSETS = {
    77: "mac_roman",
    128: "cp932",
    129: "cp949",
    130: "cp1361",
    134: "cp936",
    136: "cp950",
    161: "cp1253",
    162: "cp1254",
    163: "cp1258",
    177: "cp1255",
    178: "cp1256",
    186: "cp1257",
    204: "cp1251",
    222: "cp874",
    238: "cp1250",
    254: "cp437",
    255: "cp850",
}
_RTF_MAX_DEPTH = 1000
_RTF_TOKEN = re.compile(
    rb"\\(?:([A-Za-z]{1,32})(-?\d{1,10})? ?|'([0-9A-Fa-f]{2})|(.))"  # \word[N][ ]  \'hh  \symbol
    rb"|([{}])"
    rb"|([^\\{}\r\n]+)"  # text
    rb"|[\r\n]+",  # raw line breaks in the file mean nothing
    re.DOTALL,
)


@dataclass
class _RtfState:
    skip: bool = False  # inside a destination whose text is dropped
    star: bool = False  # the group's first token was \*
    fonttbl: bool = False  # inside \fonttbl: \f and \fcharset are read, text is dropped
    uc: int = 1  # bytes of fallback text after each \uN
    font: int | None = None


@lru_cache(maxsize=32)
def _codec(name: str) -> str:
    try:
        return codecs.lookup(name).name
    except LookupError:
        return "cp1252"


def _rtf_text(data: bytes) -> str:
    if not data[:64].lstrip().startswith(b"{\\rtf"):
        raise UnreadableFile("not an RTF document")
    out: list[str] = []
    pending = bytearray()  # consecutive \'hh bytes, decoded together (multi-byte code pages)
    pending_codec = "cp1252"
    doc_codec = "cp1252"
    font_codec: dict[int, str] = {}
    stack: list[_RtfState] = []
    state = _RtfState()
    unicode_skip = 0  # fallback characters still to drop after a \uN
    pos, end = 0, len(data)

    def codec_now() -> str:
        return font_codec.get(state.font, doc_codec) if state.font is not None else doc_codec

    def flush() -> None:
        if pending:
            out.append(pending.decode(pending_codec, errors="replace"))
            pending.clear()

    while pos < end:
        m = _RTF_TOKEN.match(data, pos)
        if m is None:  # a lone backslash at the very end
            pos += 1
            continue
        pos = m.end()
        if m.lastindex is None:  # a raw line break: it means nothing, and may fall between two \'hh bytes
            continue
        word, param, hexpair, symbol, brace, text = m.groups()
        if hexpair is not None:
            if unicode_skip:
                unicode_skip -= 1
            elif not state.skip:
                if pending and pending_codec != codec_now():
                    flush()
                pending_codec = codec_now()
                pending.append(int(hexpair, 16))
            continue
        flush()
        if brace is not None:
            unicode_skip = 0
            if brace == b"{":
                if len(stack) >= _RTF_MAX_DEPTH:
                    raise UnreadableFile("RTF groups are nested too deeply")
                stack.append(state)
                state = replace(state, star=False)
            elif stack:
                state = stack.pop()
        elif word is not None:
            name, arg = word.decode("ascii"), int(param) if param else None
            if state.star:
                state.star = False
                state.skip = state.skip or name not in _RTF_KEEP_STAR
            if name == "fonttbl":
                state.skip = state.fonttbl = True
            elif name in _RTF_SKIP:
                state.skip = True
            elif name == "f" and arg is not None:
                state.font = arg
            elif name == "fcharset" and state.fonttbl and state.font is not None and arg in _RTF_CHARSETS:
                font_codec[state.font] = _codec(_RTF_CHARSETS[arg])
            elif name == "ansicpg" and arg:
                doc_codec = _codec(f"cp{arg}")
            elif name in ("ansi", "mac", "pc", "pca"):
                doc_codec = {"ansi": "cp1252", "mac": "mac_roman", "pc": "cp437", "pca": "cp850"}[name]
            elif name == "uc" and arg is not None:
                state.uc = max(arg, 0)
            elif name == "bin":
                pos += arg or 0  # raw binary follows (inside a picture): never parsed as text
            elif state.skip:
                continue
            elif name == "u" and arg is not None:
                out.append(chr(arg & 0xFFFF))  # RTF writes code points above 32767 as negative numbers
                unicode_skip = state.uc
            elif name in _RTF_WORDS:
                out.append(_RTF_WORDS[name])
        elif symbol is not None:
            if symbol == b"*":
                state.star = True
            elif not state.skip:
                out.append(_RTF_SYMBOLS.get(symbol, ""))
        elif text is not None:
            if unicode_skip:
                cut = min(unicode_skip, len(text))
                text, unicode_skip = text[cut:], unicode_skip - cut
            if text and not state.skip:
                out.append(text.decode(codec_now(), errors="replace"))
    flush()
    # \uN escapes for characters above U+FFFF arrive as two surrogate halves: join them, and replace
    # any lone half (which could not be stored as UTF-8).
    joined = "".join(out).encode("utf-16-le", "surrogatepass").decode("utf-16-le", errors="replace")
    lines = [line.rstrip() for line in joined.replace("\x00", "").split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _read_capped(path: Path) -> bytes:
    """The whole file, unless it is over MAX_MEMBER_BYTES (the walk's own size cap is far lower)."""
    with open(path, "rb") as fh:
        data = fh.read(MAX_MEMBER_BYTES + 1)
    if len(data) > MAX_MEMBER_BYTES:
        raise UnreadableFile(f"file is over the {_mb(MAX_MEMBER_BYTES)} read limit")
    return data


def read_rtf(path: Path) -> str:
    """Body text of an RTF file; \\par and \\line as line breaks, \\tab/\\cell as tabs, \\uN and
    \\'hh escapes decoded, and styles, fonts, metadata and pictures dropped."""
    return _rtf_text(_read_capped(path))
