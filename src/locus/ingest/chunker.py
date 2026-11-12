"""Split text into overlapping chunks on paragraph, then sentence, then hard boundaries.

Sizes are expressed in approximate tokens (1 token ≈ 4 characters). Deterministic and
dependency-free so it can be tuned with the eval set without touching the embedder.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n+")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_CHARS_PER_TOKEN = 4


@dataclass(frozen=True)
class Chunk:
    text: str
    index: int
    # 1-based page the chunk's own text starts on (the overlap tail copied from the previous chunk is not
    # counted); None for text that has no pages.
    page: int | None = None


def _split_long(text: str, max_chars: int) -> list[str]:
    """Split one over-long paragraph on sentence boundaries, hard-wrapping if needed."""
    parts: list[str] = []
    buf = ""
    for sentence in _SENTENCE_SPLIT.split(text):
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(buf) + len(sentence) + 1 <= max_chars:
            buf = f"{buf} {sentence}".strip()
            continue
        if buf:
            parts.append(buf)
        while len(sentence) > max_chars:
            cut = sentence.rfind(" ", 0, max_chars)
            if cut <= 0:  # no space to break on (e.g. one very long token/URL) — hard-cut
                cut = max_chars
            parts.append(sentence[:cut].rstrip())
            sentence = sentence[cut:].lstrip()
        buf = sentence
    if buf:
        parts.append(buf)
    return parts


def _tail(text: str, overlap_chars: int) -> str:
    """Last *overlap_chars* of text, trimmed to start on a word boundary."""
    if overlap_chars <= 0 or len(text) <= overlap_chars:
        return ""
    tail = text[-overlap_chars:]
    cut = tail.find(" ")
    return tail[cut + 1 :] if cut != -1 else tail


def _limits(chunk_tokens: int, overlap_tokens: int) -> tuple[int, int]:
    """(max_chars of one unit/merged buffer, max chars of the overlap tail) for these settings."""
    max_chars = max(chunk_tokens, 50) * _CHARS_PER_TOKEN
    overlap_chars = min(max(overlap_tokens, 0) * _CHARS_PER_TOKEN, max_chars // 3)
    return max_chars, overlap_chars


def max_chunk_chars(chunk_tokens: int = 700, overlap_tokens: int = 100) -> int:
    """Upper bound on len(chunk.text) for chunk_text(text, chunk_tokens, overlap_tokens).

    A chunk after the first is an overlap tail of up to overlap_chars, a 2-char "\\n\\n" separator
    and a unit of up to max_chars, with no length check on the sum: 2,800 + 2 + 400 = 3,202 chars
    at the defaults. Text with no spaces to trim the tail on (CJK, base64, minified JSON, long
    URLs) reaches it exactly. Consumers that budget whole chunks (mcp_server's
    SEARCH_CHAR_BUDGET, scripts/eval_header.py) size against this, not against max_chars.
    """
    max_chars, overlap_chars = _limits(chunk_tokens, overlap_tokens)
    return max_chars + (2 + overlap_chars if overlap_chars else 0)


def _units(text: str, max_chars: int) -> list[str]:
    units: list[str] = []
    for para in _PARAGRAPH_SPLIT.split(text):
        para = para.strip()
        if not para:
            continue
        units.extend(_split_long(para, max_chars) if len(para) > max_chars else [para])
    return units


def _merge(units: list[tuple[str, int | None]], max_chars: int, overlap_chars: int) -> list[Chunk]:
    chunks: list[Chunk] = []
    buf = ""
    buf_page: int | None = None
    for unit, page in units:
        if not buf:
            buf, buf_page = unit, page
        elif len(buf) + 2 + len(unit) <= max_chars:
            buf = f"{buf}\n\n{unit}"
        else:
            chunks.append(Chunk(buf, len(chunks), buf_page))
            tail = _tail(buf, overlap_chars)
            buf = f"{tail}\n\n{unit}" if tail else unit
            buf_page = page
    if buf:
        chunks.append(Chunk(buf, len(chunks), buf_page))
    return chunks


def chunk_text(text: str, chunk_tokens: int = 700, overlap_tokens: int = 100) -> list[Chunk]:
    """Chunk *text* into pieces of at most ~chunk_tokens with ~overlap_tokens of overlap."""
    if not text or not text.strip():
        return []
    max_chars, overlap_chars = _limits(chunk_tokens, overlap_tokens)
    return _merge([(u, None) for u in _units(text, max_chars)], max_chars, overlap_chars)


def chunk_pages(pages: list[tuple[int, str]], chunk_tokens: int = 700, overlap_tokens: int = 100) -> list[Chunk]:
    """Like chunk_text for a paged document ((page number, text) in order). A chunk may run across pages;
    its `page` is the page its first own paragraph is on. With one page the chunks equal chunk_text's."""
    max_chars, overlap_chars = _limits(chunk_tokens, overlap_tokens)
    units = [(u, number) for number, text in pages for u in _units(text, max_chars)]
    return _merge(units, max_chars, overlap_chars)
