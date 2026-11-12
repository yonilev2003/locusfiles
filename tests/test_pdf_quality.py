"""W4: PDF quality guards: scanned/low-text detection, per-file limits, page numbers, Hebrew text order."""

from __future__ import annotations

import pytest

from locus.ingest import parsers
from locus.ingest.chunker import chunk_pages, chunk_text
from locus.ingest.parsers import parse_document, parse_file, read_pdf

from .pdf_helpers import write_pdf

LONG = "The quarterly budget review covers marketing spend, hiring plans and the launch schedule in detail."


@pytest.fixture(autouse=True)
def _default_pdf_limits(monkeypatch):
    for name in (
        "LOCUS_PDF_MIN_CHARS_PER_PAGE",
        "LOCUS_PDF_MAX_PAGES",
        "LOCUS_PDF_MAX_SECONDS",
        "LOCUS_PDF_HEBREW_FIX",
    ):
        monkeypatch.delenv(name, raising=False)


# --- 1. scanned / low-text detection ---------------------------------------------------------------------------


def test_text_pdf_is_read_with_page_numbers(tmp_path):
    pdf = write_pdf(tmp_path / "a.pdf", [[LONG], [LONG + " Second page."]])
    got = read_pdf(pdf)
    assert [n for n, _ in got.pages] == [1, 2]
    assert not got.no_text and not got.truncated
    assert parse_file(pdf) == got.text and "Second page." in got.text


def test_blank_pages_are_no_text_not_an_empty_file(tmp_path):
    pdf = write_pdf(tmp_path / "scan.pdf", [[], [], []])
    got = parse_document(pdf)
    assert got.no_text and got.text is None
    assert parse_file(pdf) is None


def test_a_few_stray_characters_on_many_pages_is_still_no_text(tmp_path):
    # a scan with a page number typed on each page: 2 characters per page, under the default 20
    pdf = write_pdf(tmp_path / "scan.pdf", [[str(i)] for i in range(10, 20)])
    assert parse_document(pdf).no_text


def test_threshold_is_an_average_over_the_pages_read_and_a_setting(tmp_path, monkeypatch):
    # one dense page and nine blank ones: 100 characters / 10 pages = 10 per page
    pdf = write_pdf(tmp_path / "mixed.pdf", [[LONG]] + [[]] * 9)
    assert parse_document(pdf).no_text
    monkeypatch.setenv("LOCUS_PDF_MIN_CHARS_PER_PAGE", "5")
    got = parse_document(pdf)
    assert not got.no_text and LONG in got.text
    monkeypatch.setenv("LOCUS_PDF_MIN_CHARS_PER_PAGE", "0")  # 0 switches the check off
    assert not parse_document(write_pdf(tmp_path / "b.pdf", [["x"], []])).no_text


def test_bad_threshold_value_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("LOCUS_PDF_MIN_CHARS_PER_PAGE", "lots")
    assert parsers.pdf_min_chars_per_page() == parsers.DEFAULT_PDF_MIN_CHARS_PER_PAGE == 20


# --- 2. per-file limits ----------------------------------------------------------------------------------------


def test_max_pages_keeps_the_pages_read_and_says_truncated(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCUS_PDF_MAX_PAGES", "3")
    pdf = write_pdf(tmp_path / "long.pdf", [[f"{LONG} page {i}"] for i in range(1, 8)])
    got = read_pdf(pdf)
    assert got.truncated and got.n_pages == 7 and [n for n, _ in got.pages] == [1, 2, 3]
    assert "page 3" in got.text and "page 4" not in got.text


def test_max_pages_default_is_about_400():
    assert parsers.pdf_max_pages() == 400


def test_time_budget_stops_between_pages_and_keeps_what_was_read(tmp_path, monkeypatch):
    pdf = write_pdf(tmp_path / "slow.pdf", [[f"{LONG} page {i}"] for i in range(1, 6)])
    ticks = iter(range(0, 1000, 10))  # every clock read is 10 s later than the last
    monkeypatch.setattr(parsers, "_monotonic", lambda: next(ticks))
    monkeypatch.setenv("LOCUS_PDF_MAX_SECONDS", "25")
    got = read_pdf(pdf)
    assert got.truncated and 1 <= len(got.pages) < 5
    assert got.pages[0][0] == 1


def test_time_budget_always_reads_at_least_one_page(tmp_path, monkeypatch):
    pdf = write_pdf(tmp_path / "slow.pdf", [[LONG], [LONG]])
    ticks = iter(range(0, 1000, 100))
    monkeypatch.setattr(parsers, "_monotonic", lambda: next(ticks))
    monkeypatch.setenv("LOCUS_PDF_MAX_SECONDS", "1")
    got = read_pdf(pdf)
    assert len(got.pages) == 1 and got.truncated


def test_time_budget_default_is_about_20_seconds():
    assert parsers.pdf_max_seconds() == 20


def test_a_pdf_that_fits_the_limits_is_not_truncated(tmp_path):
    assert not read_pdf(write_pdf(tmp_path / "ok.pdf", [[LONG]] * 3)).truncated


# --- 3. page numbers through chunking --------------------------------------------------------------------------


def test_chunk_text_is_unchanged_and_has_no_page():
    text = "\n\n".join(f"Paragraph number {i} " + "word " * 40 for i in range(30))
    chunks = chunk_text(text, 100, 10)
    assert all(c.page is None for c in chunks)
    assert [(c.text, c.index) for c in chunks] == [(c.text, c.index) for c in chunk_pages([(1, text)], 100, 10)]


def test_chunk_pages_tags_each_chunk_with_the_page_its_own_text_starts_on():
    pages = [(p, "\n\n".join(f"Page {p} paragraph {i} " + "word " * 30 for i in range(3))) for p in (1, 2, 3, 4)]
    chunks = chunk_pages(pages, 100, 10)
    assert chunks[0].page == 1
    assert [c.page for c in chunks] == sorted(c.page for c in chunks)  # never goes back
    assert {c.page for c in chunks} == {1, 2, 3, 4}
    for c in chunks:  # the text after any overlap tail belongs to the tagged page
        assert f"Page {c.page} " in c.text


def test_chunk_pages_keeps_original_page_numbers_when_pages_are_skipped():
    chunks = chunk_pages([(3, "alpha beta"), (7, "gamma delta")], 700, 0)
    assert len(chunks) == 1 and chunks[0].page == 3
    chunks = chunk_pages([(3, "alpha " * 600), (7, "gamma " * 600)], 100, 0)
    assert chunks[0].page == 3 and chunks[-1].page == 7


# --- 4. Hebrew text order --------------------------------------------------------------------------------------

ENGLISH_ONLY = [
    "Budget (approx.) was -5 percent; see item (b) and (c).",
    ".NET and .github are not sentence punctuation. ,leading comma, odd order: planning, and prototype development.",
    "a) first b) second\n)reversed( brackets in English stay\n1,500- stays",
    "Revenue grew 3,200- units; the text ,planning and ;prototype appear on one line.",
    "\n".join(["development prototype and ,planning", ".creation of value", "the quick brown fox jumps"] * 3),
    "x1,500- and y2- then (open ) mismatch ) (",
]


@pytest.mark.parametrize("text", ENGLISH_ONLY)
def test_hebrew_fix_never_changes_english_only_text(text):
    assert parsers.clean_pdf_page_text(text, fix_hebrew=True) == text


def test_hebrew_fix_repairs_a_hebrew_page_and_leaves_its_english_alone():
    heb = 'הסכום ששולם עבור השירותים היה )500 ש"ח( בסך הכל, ב1,500- לחודש'
    page = f"{heb}\nThe agreed total stays as written here today."
    out = parsers.clean_pdf_page_text(page, fix_hebrew=True)
    assert "(500" in out and "ב-1,500" in out
    assert out.endswith("The agreed total stays as written here today.")


def test_hebrew_fix_off_returns_the_text_untouched():
    heb = 'הסכום ששולם עבור השירותים היה )500 ש"ח( בסך הכל'
    assert parsers.clean_pdf_page_text(heb, fix_hebrew=False) == heb


def test_hebrew_fix_setting_defaults_on_and_can_be_turned_off(monkeypatch):
    assert parsers.pdf_hebrew_fix_enabled() is True
    for off in ("0", "false", "off", "no"):
        monkeypatch.setenv("LOCUS_PDF_HEBREW_FIX", off)
        assert parsers.pdf_hebrew_fix_enabled() is False
    monkeypatch.setenv("LOCUS_PDF_HEBREW_FIX", "1")
    assert parsers.pdf_hebrew_fix_enabled() is True


def test_read_pdf_applies_the_cleaner_per_page_when_enabled(tmp_path, monkeypatch):
    pdf = write_pdf(tmp_path / "a.pdf", [[LONG], [LONG + " two"]])
    seen = []
    monkeypatch.setattr(parsers, "clean_pdf_page_text", lambda t, fix_hebrew: seen.append((fix_hebrew, t[:10])) or t)
    read_pdf(pdf)
    assert [f for f, _ in seen] == [True, True]
    monkeypatch.setenv("LOCUS_PDF_HEBREW_FIX", "0")
    seen.clear()
    read_pdf(pdf)
    assert [f for f, _ in seen] == [False, False]


def test_an_english_pdf_reads_the_same_with_the_fix_on_or_off(tmp_path, monkeypatch):
    pdf = write_pdf(tmp_path / "a.pdf", [[LONG, "Net (a) -5 ,odd"], [LONG]])
    on = read_pdf(pdf).text
    monkeypatch.setenv("LOCUS_PDF_HEBREW_FIX", "0")
    assert read_pdf(pdf).text == on


# --- indexer, store, search and read_file ----------------------------------------------------------------------

chromadb = pytest.importorskip("chromadb")

import asyncio  # noqa: E402

from locus import mcp_server as srv  # noqa: E402
from locus.config import DEFAULT_EXTENSIONS, Settings  # noqa: E402
from locus.ingest.embedder import HashEmbedder  # noqa: E402
from locus.ingest.estimator import estimate, render_estimate, scan  # noqa: E402
from locus.ingest.indexer import index_folder  # noqa: E402
from locus.ingest.store import ChromaStore, Hit  # noqa: E402


def _settings(tmp_path, chunk_tokens=100):
    return Settings(
        data_dir=tmp_path / "data",
        collection="test",
        embedder="hash",
        voyage_model="",
        openai_model="",
        local_model="",
        chunk_tokens=chunk_tokens,
        overlap_tokens=10,
        max_file_mb=5,
        extensions=DEFAULT_EXTENSIONS,
    )


def _run(tmp_path, root, **kw):
    settings = _settings(tmp_path)
    embedder = HashEmbedder()
    store = ChromaStore(settings.data_dir, "test", embedder.name)
    logs: list[str] = []
    stats = index_folder(root, embedder, store, settings, log=logs.append, **kw)
    return stats, store, logs, settings, embedder


def _paged_pdf(path, n=6):
    return write_pdf(path, [[f"Page {i} about topic{i}. {LONG}"] for i in range(1, n + 1)])


def test_scanned_pdf_yields_no_chunks_and_is_counted(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    write_pdf(root / "scan.pdf", [[], []])
    (root / "note.md").write_text("A plain note about the budget.")
    stats, store, _, _, _ = _run(tmp_path, root)
    assert stats.pdf_no_text == 1 and stats.files_indexed == 1
    assert store.list_paths() == [str(root / "note.md")]
    assert "pdf_no_text=1" in stats.summary()


def test_summary_is_unchanged_when_nothing_is_flagged(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "note.md").write_text("A plain note about the budget.")
    stats, *_ = _run(tmp_path, root)
    assert "pdf_" not in stats.summary()


def test_a_file_that_becomes_a_scan_loses_its_old_chunks_with_a_note(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    pdf = _paged_pdf(root / "a.pdf", 2)
    _, store, _, settings, embedder = _run(tmp_path, root)
    assert store.list_paths() == [str(pdf)]
    write_pdf(pdf, [[], []])  # replaced by a scan
    logs: list[str] = []
    stats = index_folder(root, embedder, store, settings, log=logs.append)
    assert stats.pdf_no_text == 1 and store.list_paths() == []
    assert any("no extractable text" in m and "earlier index entries for it were removed" in m for m in logs)


def test_truncated_pdf_indexes_the_pages_read_and_counts_it(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCUS_PDF_MAX_PAGES", "2")
    root = tmp_path / "docs"
    root.mkdir()
    _paged_pdf(root / "long.pdf", 6)
    stats, store, logs, _, _ = _run(tmp_path, root)
    assert stats.pdf_truncated == 1 and stats.files_indexed == 1 and "pdf_truncated=1" in stats.summary()
    docs = store._col.get(include=["documents"])["documents"]
    assert any("topic2" in d for d in docs) and not any("topic3" in d for d in docs)
    assert any("only the first 2 of 6 pages" in m for m in logs)


def test_chunks_store_the_first_page_and_non_pdf_chunks_store_none(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    _paged_pdf(root / "a.pdf", 6)
    (root / "note.md").write_text("A plain note about the budget.")
    _, store, _, _, _ = _run(tmp_path, root)
    metas = store._col.get(include=["metadatas"])["metadatas"]
    pdf_pages = {m["page"] for m in metas if m["ext"] == ".pdf"}
    assert pdf_pages and pdf_pages <= {1, 2, 3, 4, 5, 6} and 1 in pdf_pages
    assert all("page" not in m for m in metas if m["ext"] == ".md")


def test_query_returns_the_page(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    _paged_pdf(root / "a.pdf", 6)
    (root / "note.md").write_text("A plain note about the budget.")
    _, store, _, _, embedder = _run(tmp_path, root)
    hits = store.query(embedder.embed_query("Page 4 about topic4"), k=20)
    pdf_hits = [h for h in hits if h.path.endswith(".pdf")]
    assert pdf_hits and all(h.page is not None for h in pdf_hits)
    assert all(h.page is None for h in hits if h.path.endswith(".md"))


def test_search_format_shows_the_page_only_for_pdf_chunks():
    hits = [
        Hit("/a/x.pdf", 2, "alpha", 0.9, page=7),
        Hit("/a/y.md", 0, "beta", 0.8),
    ]
    out = srv._format_search_results("q", hits)
    assert "## 1. /a/x.pdf  (chunk 2, score 0.900, p.7, " in out
    assert "## 2. /a/y.md  (chunk 0, score 0.800, 4 chars)" in out
    unscored = srv._format_search_results(None, hits[:1], scored=False)
    assert "(chunk 2, p.7, " in unscored


@pytest.fixture()
def pdf_server(tmp_path, monkeypatch):
    root = tmp_path / "docs"
    root.mkdir()
    _paged_pdf(root / "a.pdf", 6)
    (root / "note.md").write_text("A plain note about the budget.")
    outside = tmp_path / "outside.pdf"
    _paged_pdf(outside, 3)
    _, store, _, settings, embedder = _run(tmp_path, root)
    srv._state.clear()
    srv._state.update({"settings": settings, "embedder": embedder, "store": store})
    monkeypatch.setattr(srv, "_components", lambda: (embedder, store, settings))
    yield root, outside
    srv._state.clear()


def _read(path, **kw):
    return asyncio.run(srv.locus_read_file(path=str(path), **kw))


def test_read_file_pages_returns_only_those_pages(pdf_server):
    root, _ = pdf_server
    out = _read(root / "a.pdf", pages="3-4")
    assert "topic3" in out and "topic4" in out and "topic2" not in out and "topic5" not in out
    assert "pages 3-4 of 6" in out
    single = _read(root / "a.pdf", pages="5")
    assert "topic5" in single and "topic4" not in single
    several = _read(root / "a.pdf", pages="1,6")
    assert "topic1" in several and "topic6" in several and "topic3" not in several


def test_read_file_without_pages_is_unchanged(pdf_server):
    root, _ = pdf_server
    out = _read(root / "a.pdf")
    assert all(f"topic{i}" in out for i in range(1, 7)) and "pages " not in out.splitlines()[0]
    assert out.startswith(f"# {root / 'a.pdf'} (chars 0–")


@pytest.mark.parametrize("bad", ["0", "7", "4-2", "x", "3-", "1-9", "-2", ""])
def test_read_file_invalid_pages_give_a_clear_error(pdf_server, bad):
    root, _ = pdf_server
    out = _read(root / "a.pdf", pages=bad)
    assert out.startswith("Error:") and "pages" in out and "topic" not in out


def test_read_file_pages_on_a_non_pdf_is_an_error(pdf_server):
    root, _ = pdf_server
    out = _read(root / "note.md", pages="1")
    assert out.startswith("Error:") and "only for PDF" in out and "plain note" not in out


def test_read_file_still_refuses_a_path_that_is_not_indexed_even_with_pages(pdf_server):
    _, outside = pdf_server
    for kw in ({}, {"pages": "1"}):
        out = _read(outside, **kw)
        assert out.startswith("Error:") and "not an indexed file" in out and "topic" not in out


def test_read_file_pages_beyond_the_page_limit_still_work(pdf_server, monkeypatch):
    root, _ = pdf_server
    # the page limit applies to indexing and whole-file reads, not to an explicit page request
    monkeypatch.setenv("LOCUS_PDF_MAX_PAGES", "2")
    assert "topic6" in _read(root / "a.pdf", pages="6")


def test_read_file_tool_name_and_read_only_annotation_stay():
    tools = {t.name: t for t in asyncio.run(srv.mcp.list_tools())}
    tool = tools["locus_read_file"]
    assert tool.annotations.read_only_hint is True
    assert "pages" in tool.input_schema["properties"]
    assert set(tools) >= {"locus_search", "locus_list_sources", "locus_read_file"}


def test_a_scanned_pdf_read_file_says_so(tmp_path, monkeypatch):
    root = tmp_path / "docs"
    root.mkdir()
    write_pdf(root / "scan.pdf", [[LONG]])
    _, store, _, settings, embedder = _run(tmp_path, root)
    write_pdf(root / "scan.pdf", [[], []])
    monkeypatch.setattr(srv, "_components", lambda: (embedder, store, settings))
    out = _read(root / "scan.pdf")
    assert "no extractable text" in out


# --- dry-run / estimate ----------------------------------------------------------------------------------------


def test_estimate_counts_scanned_and_truncated_pdfs(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCUS_PDF_MAX_PAGES", "2")
    root = tmp_path / "docs"
    root.mkdir()
    write_pdf(root / "scan.pdf", [[], []])
    _paged_pdf(root / "long.pdf", 5)
    write_pdf(root / "ok.pdf", [[LONG]])
    settings = _settings(tmp_path)
    result = scan(root, settings)
    est = estimate(result.files, settings)
    assert (est.pdf_no_text, est.pdf_truncated, est.pdf_sampled) == (1, 1, 3)
    text = render_estimate(result, est, settings)
    assert "PDFs with no extractable text: 1 of 3" in text and "PDFs cut at the page or time limit: 1" in text
    assert "scan.pdf" not in text and "long.pdf" not in text  # counts only, no file names
