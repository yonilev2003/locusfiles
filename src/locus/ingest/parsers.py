"""File → plain text. One function per format; unsupported types return None."""

from __future__ import annotations

import dataclasses
import json
import os
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from . import docformats
from .hebrew_text_order import normalise_hebrew_pdf_text
from .noise import strip_base64

TEXT_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".md",
        ".txt",
        ".rst",
        ".py",
        ".js",
        ".ts",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".csv",
        ".html",
        ".htm",
        ".xml",
        ".sh",
        ".sql",
        # Source and subtitle text added in engine 0.1.2; noise guards below apply.
        ".c",
        ".h",
        ".cc",
        ".cpp",
        ".hpp",
        ".go",
        ".rs",
        ".swift",
        ".java",
        ".kt",
        ".rb",
        ".php",
        ".srt",
        ".vtt",
        ".tsx",
        ".jsx",
        ".mjs",
        ".mts",
        ".cjs",
        # Plain text that engine 0.1.2 added. .ini and .cfg parse but are not in
        # config.DEFAULT_EXTENSIONS (ini/cfg files often hold credentials); .log is default but size-capped.
        ".markdown",
        ".mdx",
        ".org",
        ".tex",
        ".tsv",
        ".log",
        ".ini",
        ".cfg",
    }
)

# Stdlib readers for office-style documents (engine 0.1.2); see docformats.py for the safety limits.
_DOCUMENT_READERS: dict[str, Callable[[Path], str]] = {
    ".pptx": docformats.read_pptx,
    ".xlsx": docformats.read_xlsx,
    ".odt": docformats.read_odt,
    ".ods": docformats.read_ods,
    ".odp": docformats.read_odp,
    ".epub": docformats.read_epub,
    ".rtf": docformats.read_rtf,
}

# A workbook's text grows with every sheet (each is cut at 5,000 rows on its own), so one large
# file could eat a large share of a monthly managed-token budget (2M on Starter). Spreadsheets
# are capped per file by default (low unless the user raises it);
# prose formats (books, decks) are left whole. The user opts in to more with
# LOCUS_SPREADSHEET_MAX_CHARS (a higher number, or 0 for no limit) and re-indexes.
DEFAULT_SPREADSHEET_MAX_CHARS = 200_000
SPREADSHEET_CAP_ENV = "LOCUS_SPREADSHEET_MAX_CHARS"
_SPREADSHEET_EXTENSIONS: frozenset[str] = frozenset({".xlsx", ".ods"})


def spreadsheet_char_cap() -> int | None:
    """The per-workbook character cap: the env override if it is a whole number (0 = no cap), else the default."""
    raw = (os.getenv(SPREADSHEET_CAP_ENV) or "").strip().replace("_", "").replace(",", "")
    if raw.isdigit():
        return int(raw) or None
    return DEFAULT_SPREADSHEET_MAX_CHARS


# Files that are text but not what the user wrote: dependency lockfiles, minified bundles and generated
# code. Matched on the file name alone, so the dry run can count them without reading anything.
_LOCKFILE_NAMES = frozenset(
    {"package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "composer.lock", "pipfile.lock", "bun.lock"}
)
_GENERATED_SUFFIXES = (".min.js", ".min.css", ".bundle.js", ".pb.go", "_pb2.py", "_pb2_grpc.py")
_GENERATED_MARKERS = (".generated.",)

# A source or web file with a line this long is minified or machine-written, not read by a person.
# Data formats (json, csv, md, txt...) legitimately have long lines and are not tested.
_MINIFIED_LINE_CHARS = 2_000
_MINIFIED_CHECKED: frozenset[str] = frozenset(
    {".js", ".ts", ".tsx", ".jsx", ".mjs", ".mts", ".cjs", ".html", ".htm", ".xml"}
    | {".c", ".h", ".cc", ".cpp", ".hpp", ".go", ".rs", ".swift", ".java", ".kt", ".rb", ".php", ".sql", ".sh"}
)


def noise_name_reason(path: Path) -> str | None:
    """Why *path* is skipped by name alone (lockfile or generated code), else None."""
    name = path.name.lower()
    if name in _LOCKFILE_NAMES or name.endswith(".lock"):
        return "lockfile"
    if name.endswith(_GENERATED_SUFFIXES) or any(m in name for m in _GENERATED_MARKERS):
        return "generated"
    return None


def _is_minified(text: str) -> bool:
    return any(len(line) > _MINIFIED_LINE_CHARS for line in text.splitlines())


# .log is default but bounded: a hard size cap, and logs whose lines are almost all the same once digits
# are blanked (timestamps, ids, counters) are skipped as repetitive.
LOG_MAX_BYTES = 1024 * 1024
_LOG_SAMPLE_LINES = 2_000
_LOG_MIN_UNIQUE_SHARE = 0.2
_DIGITS = re.compile(r"\d+")


def _is_repetitive_log(text: str) -> bool:
    lines = [ln for ln in text.splitlines()[:_LOG_SAMPLE_LINES] if ln.strip()]
    if len(lines) < 50:
        return False
    return len({_DIGITS.sub("0", ln) for ln in lines}) < _LOG_MIN_UNIQUE_SHARE * len(lines)


def parse_file(path: Path, *, strip: bool = True) -> str | None:
    """Return the text content of *path*, or None if the format is unsupported or is machine-made noise.
    Base64 payloads (data: URIs, long base64 runs) are removed from the text of every format, unless *strip* is False:
    the credential check must see the text as it is in the file (a JWT's payload can be a 200-character run)."""
    text = _parse_raw(path)
    return strip_base64(text) if strip and text else text


def _parse_raw(path: Path) -> str | None:
    ext = path.suffix.lower()
    if ext in TEXT_EXTENSIONS:
        if noise_name_reason(path):
            return None
        if ext == ".log" and path.stat().st_size > LOG_MAX_BYTES:
            return None
        text = _read_text(path)
        if ext == ".log" and _is_repetitive_log(text):
            return None
        if ext in _MINIFIED_CHECKED and _is_minified(text):
            return None
        return text
    if ext == ".pdf":
        return read_pdf(path).text
    if ext == ".docx":
        return _read_docx(path)
    if ext == ".ipynb":
        return _read_ipynb(path)
    reader = _DOCUMENT_READERS.get(ext)
    if reader is None:
        return None
    text = reader(path)
    if ext in _SPREADSHEET_EXTENSIONS:
        text = _cap_spreadsheet(text, spreadsheet_char_cap())
    return text


def _cap_spreadsheet(text: str, cap: int | None) -> str:
    """Cut a workbook's text at *cap* characters on a line boundary, saying so and how to raise it."""
    if cap is None or len(text) <= cap:
        return text
    cut = text.rfind("\n", 0, cap)
    kept = text[: cut if cut > 0 else cap]
    note = (
        f"[truncated: only the first {cap:,} characters of this workbook were indexed, to protect your "
        f"token budget. To index all of it, set {SPREADSHEET_CAP_ENV} to a higher number (0 = no limit) "
        "and re-index]"
    )
    return f"{kept}\n{note}"


# A file that fails strict UTF-8 is still read as UTF-8 (bad bytes become U+FFFD) when at most this
# share of its non-ASCII characters are invalid sequences; above it, it is treated as legacy cp1252.
_UTF8_MAX_INVALID_SHARE = 0.05


def _read_text(path: Path) -> str:
    # Detect common non-UTF-8 encodings instead of decoding everything as UTF-8 with
    # errors="ignore", which silently drops accented/non-ASCII characters and turns
    # UTF-16 files (common from Windows exports) into NUL-interleaved garbage.
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig", errors="replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        # A file that is UTF-8 apart from a stray byte (a cut-off character, a paste from another
        # tool) must not be decoded as cp1252 as a whole: a Hebrew transcript with one bad byte
        # became mojibake end to end and was unsearchable. The test is how much of the non-ASCII
        # content is *not* valid UTF-8, not the file size: a long, mostly-ASCII cp1252 file has
        # a tiny replacement share too, but nearly all of its accented letters are invalid.
        lenient = raw.decode("utf-8", errors="replace")
        bad = lenient.count("�") - raw.count(b"\xef\xbf\xbd")  # minus genuine U+FFFD in the file
        non_ascii = len(lenient) - len(lenient.encode("ascii", "ignore"))
        if bad <= _UTF8_MAX_INVALID_SHARE * non_ascii:
            return lenient
        # cp1252 (Windows Latin-1 superset) maps every byte to *some* character, so this
        # never raises — a reasonable fallback for legacy-encoded exports; still far better
        # than silently dropping every non-ASCII character.
        return raw.decode("cp1252", errors="replace")


# PDF guards (W4). Environment settings, read where used like the spreadsheet cap above.
# - a PDF whose text averages fewer characters per page than this is treated as scanned: no chunks, counted
#   as `pdf_no_text` (0 switches the check off);
# - at most this many pages, and this many seconds per file, are read; the pages read so far are kept and the
#   file is counted as `pdf_truncated`. The time is checked between pages: one page that never returns is not
#   interrupted;
# - the Hebrew text-order repairs (hebrew_text_order.py) run on each page.
DEFAULT_PDF_MIN_CHARS_PER_PAGE = 20
DEFAULT_PDF_MAX_PAGES = 400
DEFAULT_PDF_MAX_SECONDS = 20.0


def _env_number(name: str, default: float, *, whole: bool = False) -> float:
    raw = (os.getenv(name) or "").strip().replace("_", "")
    try:
        value = int(raw) if whole else float(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def pdf_min_chars_per_page() -> float:
    return _env_number("LOCUS_PDF_MIN_CHARS_PER_PAGE", DEFAULT_PDF_MIN_CHARS_PER_PAGE)


def pdf_max_pages() -> int:
    return int(_env_number("LOCUS_PDF_MAX_PAGES", DEFAULT_PDF_MAX_PAGES, whole=True)) or DEFAULT_PDF_MAX_PAGES


def pdf_max_seconds() -> float:
    return _env_number("LOCUS_PDF_MAX_SECONDS", DEFAULT_PDF_MAX_SECONDS) or DEFAULT_PDF_MAX_SECONDS


def pdf_hebrew_fix_enabled() -> bool:
    return (os.getenv("LOCUS_PDF_HEBREW_FIX") or "1").strip().lower() not in {"0", "false", "off", "no"}


_monotonic = time.monotonic  # replaced in tests


def clean_pdf_page_text(text: str, *, fix_hebrew: bool) -> str:
    """One page's extracted text, with the Hebrew text-order repairs when *fix_hebrew*. The repairs only fire on
    Hebrew lines and on documents with at least 20 Hebrew letters on the page, so a page without Hebrew comes
    back unchanged (tests/test_pdf_quality.py)."""
    return normalise_hebrew_pdf_text(text)[0] if fix_hebrew else text


@dataclass
class PdfRead:
    pages: list[tuple[int, str]] = field(default_factory=list)  # (1-based page number, text) of pages with text
    n_pages: int = 0  # pages in the document
    pages_read: int = 0
    truncated: bool = False  # stopped at the page or time limit before the last page
    no_text: bool = False  # under the characters-per-page threshold: scanned or image-only

    @property
    def text(self) -> str | None:
        """The pages joined by blank lines; None for a no-text PDF."""
        if self.no_text or not self.pages:
            return None
        return "\n\n".join(t for _, t in self.pages)


def read_pdf(path: Path, only_pages: Iterable[int] | None = None) -> PdfRead:
    """Extract a PDF's text page by page. *only_pages* (1-based) reads just those pages and skips the page limit
    and the no-text check; the time budget always applies, after the first page."""
    # pypdfium2 wraps Google's PDFium (the engine behind Chrome's PDF viewer) via
    # ctypes bindings under Apache-2.0/BSD-3-Clause — no AGPL/commercial-license question
    # and real-engine text extraction quality close to PyMuPDF's MuPDF, unlike
    # pure-Python parsers (pypdf/pdfminer) which garble multi-column layouts and spacing.
    # This is plain text extraction only, matching the one call this parser ever made to
    # PyMuPDF (page.get_text()) — Locus is a RAG pipeline over text chunks, not an OCR tool.
    import pypdfium2 as pdfium

    fix = pdf_hebrew_fix_enabled()
    deadline = _monotonic() + pdf_max_seconds()
    result = PdfRead()
    doc = pdfium.PdfDocument(str(path))
    try:
        result.n_pages = len(doc)
        wanted = sorted({n for n in only_pages if 1 <= n <= result.n_pages}) if only_pages is not None else None
        numbers = wanted if wanted is not None else range(1, min(result.n_pages, pdf_max_pages()) + 1)
        for number in numbers:
            if result.pages_read and _monotonic() > deadline:
                break
            page = doc[number - 1]
            try:
                textpage = page.get_textpage()
                try:
                    text = textpage.get_text_range()
                finally:
                    textpage.close()
            finally:
                page.close()
            result.pages_read += 1
            if text.strip():
                result.pages.append((number, clean_pdf_page_text(text, fix_hebrew=fix)))
        if wanted is None:
            result.truncated = result.pages_read < result.n_pages
            chars = sum(len(t.strip()) for _, t in result.pages)
            result.no_text = result.pages_read > 0 and chars < pdf_min_chars_per_page() * result.pages_read
    finally:
        doc.close()
    return result


# Longest page list parse_page_spec expands; the caller still checks pages against the document (PDF page cap is 400).
MAX_PAGE_SPEC_PAGES = 2000


def parse_page_spec(spec: str) -> list[int]:
    """ "7", "3-5", "1,6" or "1-3,7" as sorted 1-based page numbers. Raises ValueError (message fit to show a
    user) for anything else: empty, 0, a reversed range, letters. Whether the pages exist is up to the caller."""
    wanted: set[int] = set()
    for part in (spec or "").split(","):
        m = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+)\s*)?", part)
        if m is None:
            raise ValueError(
                f"pages={spec!r} is not a page list; use a page like 7, a range like 3-5, or both like 1-3,7"
            )
        first, last = int(m[1]), int(m[2] or m[1])
        if first < 1 or last < first:
            raise ValueError(f"pages={spec!r} is not a valid range: pages count from 1 and a range runs upward (3-5)")
        if last - first + 1 > MAX_PAGE_SPEC_PAGES:
            raise ValueError(
                f"pages={spec!r} asks for more than {MAX_PAGE_SPEC_PAGES} pages at once; use a smaller range"
            )
        wanted.update(range(first, last + 1))
        if len(wanted) > MAX_PAGE_SPEC_PAGES:
            raise ValueError(f"pages={spec!r} asks for more than {MAX_PAGE_SPEC_PAGES} pages at once")
    return sorted(wanted)


def format_pages(numbers: list[int]) -> str:
    """The inverse of parse_page_spec: [1, 2, 3, 7] -> "1-3,7"."""
    runs: list[str] = []
    start = prev = numbers[0]
    for n in [*numbers[1:], None]:
        if n is not None and n == prev + 1:
            prev = n
            continue
        runs.append(str(start) if start == prev else f"{start}-{prev}")
        if n is not None:
            start = prev = n
    return ",".join(runs)


@dataclass
class Parsed:
    """parse_file's text plus what the indexer reports: PDF pages and the no-text / truncated flags."""

    text: str | None
    pages: list[tuple[int, str]] | None = None  # PDFs only
    no_text: bool = False
    truncated: bool = False
    pages_read: int = 0
    n_pages: int = 0


def strip_parsed(parsed: Parsed) -> Parsed:
    """*parsed* with base64 payloads removed from its text and, for a PDF, from each page."""
    if parsed.pages:
        pages = [(n, strip_base64(t)) for n, t in parsed.pages]
        return dataclasses.replace(parsed, text="\n\n".join(t for _, t in pages), pages=pages)
    return dataclasses.replace(parsed, text=strip_base64(parsed.text) if parsed.text else parsed.text)


def parse_document(path: Path, *, strip: bool = True) -> Parsed:
    """parse_file, with PDF page boundaries and flags. Same text as parse_file (including *strip*)."""
    if path.suffix.lower() == ".pdf":
        got = read_pdf(path)
        pages = None if got.no_text else got.pages
        parsed = Parsed(got.text, pages, got.no_text, got.truncated, got.pages_read, got.n_pages)
    else:
        parsed = Parsed(parse_file(path, strip=False))
    return strip_parsed(parsed) if strip else parsed


# Elements whose children are text that belongs to the paragraph (a hyperlink, a tracked insertion,
# an inline content control). Tracked deletions (w:del, w:moveFrom) are left out on purpose.
_DOCX_INLINE = frozenset({"hyperlink", "ins", "smartTag", "fldSimple", "sdt", "sdtContent", "moveTo"})


def _docx_run_text(node) -> str:
    from docx.oxml.ns import qn

    parts = []
    for child in node:
        tag = child.tag
        if tag == qn("w:r"):
            for item in child:
                if item.tag == qn("w:t"):
                    parts.append(item.text or "")
                elif item.tag == qn("w:tab"):
                    parts.append("\t")
                elif item.tag in (qn("w:br"), qn("w:cr")):
                    parts.append("\n")
        elif tag.rsplit("}", 1)[-1] in _DOCX_INLINE:
            parts.append(_docx_run_text(child))
    return "".join(parts)


def _docx_blocks(container) -> list[str]:
    """Text blocks of a body, cell or content control in document order: one per non-empty paragraph and
    one per table row ("a | b"), so a table stays where it is in the document. A merged cell is one
    cell, not one per column it spans; a content control's paragraphs are read."""
    from docx.oxml.ns import qn

    blocks: list[str] = []
    for child in container:
        if child.tag == qn("w:p"):
            text = _docx_run_text(child)
            if text.strip():
                blocks.append(text)
        elif child.tag == qn("w:tbl"):
            for row in child.iterchildren(qn("w:tr")):
                cells = []
                for cell in row.iterchildren(qn("w:tc")):
                    text = " ".join(b.strip() for b in _docx_blocks(cell) if b.strip())
                    if text:
                        cells.append(text)
                if cells:
                    blocks.append(" | ".join(cells))
        elif child.tag == qn("w:sdt"):
            content = child.find(qn("w:sdtContent"))
            if content is not None:
                blocks.extend(_docx_blocks(content))
    return blocks


def _read_docx(path: Path) -> str:
    import docx  # python-docx

    docformats.check_docx_package(path)  # S-09: the zip-bomb and entity guards every other container already has
    document = docx.Document(str(path))
    return "\n\n".join(_docx_blocks(document.element.body))


def _read_ipynb(path: Path) -> str:
    # Markdown and code cell sources only: outputs (plots, tracebacks, base64 images) are noise for
    # retrieval and often most of the file's bytes.
    notebook = json.loads(_read_text(path))
    if not isinstance(notebook, dict):
        raise ValueError("not a Jupyter notebook")
    cells = notebook.get("cells")
    if cells is None:  # nbformat 3 keeps its cells under worksheets
        cells = [c for ws in notebook.get("worksheets", []) for c in ws.get("cells", [])]
    metadata = notebook.get("metadata")
    info = (metadata.get("language_info") if isinstance(metadata, dict) else None) or {}
    language = info.get("name") if isinstance(info, dict) else None
    parts = []
    for cell in cells:
        if not isinstance(cell, dict) or cell.get("cell_type") not in ("markdown", "code"):
            continue
        source = cell.get("source", cell.get("input", ""))  # nbformat 3 code cells call it "input"
        text = ("".join(source) if isinstance(source, list) else str(source)).strip()
        if not text:
            continue
        parts.append(text if cell["cell_type"] == "markdown" else f"```{language or 'python'}\n{text}\n```")
    return "\n\n".join(parts)
