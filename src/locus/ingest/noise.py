"""Machine-made noise inside otherwise readable files: base64 payloads.

A notebook attachment, an inline `data:image/png;base64,...` image or a pasted key blob is thousands of
characters of random-looking text. Embedded, it matches nothing, wastes the embedding window and shows up
as a nonsense search hit. `strip_base64` removes such runs from a parsed file's text, `drop_base64_lines`
removes the lines of a chunk that are still base64 (wrapped at 64 or 76 characters, so no single line is long)
and keeps the prose around them, and
`PARSER_VERSION` is stamped on every chunk so a file indexed by an older parser is re-indexed once.
"""

from __future__ import annotations

import re

# Bump when what the parsers or the noise filters extract changes in a way old rows should not keep.
# 1 = before the noise filter (rows carry no stamp); 2 = base64 stripped, base64 lines of a chunk dropped; 3 = text read as UTF-16, cp1255 or cp1252 when sane.
PARSER_VERSION = 3
PARSER_VERSION_KEY = "parser_version"

_DATA_URI = re.compile(r"data:[A-Za-z0-9.+/-]+(?:;[A-Za-z0-9=.+-]+)*;base64,[A-Za-z0-9+/_=-]+")
_LONG_RUN = re.compile(r"[A-Za-z0-9+/]{200,}={0,2}")
_B64_LINE = re.compile(r"[A-Za-z0-9+/]{60,}={0,2}")
_B64_TAIL = re.compile(r"[A-Za-z0-9+/]{16,}={0,2}")
_MIN_DISTINCT = 16  # a run of one repeated character ("////", "aaaa") is not a payload
REMOVED = "[base64 data removed]"


def _is_payload(run: str) -> bool:
    return len(set(run)) >= _MIN_DISTINCT


def strip_base64(text: str) -> str:
    """*text* with data: URIs and base64-looking runs of 200+ characters replaced by a short marker."""
    if "base64" not in text and not _LONG_RUN.search(text):
        return text
    text = _DATA_URI.sub(REMOVED, text)
    return _LONG_RUN.sub(lambda m: REMOVED if _is_payload(m.group()) else m.group(), text)


def _is_fragment(text: str) -> bool:
    """A chunk cut out of the middle of a wrapped payload: a few tokens of the base64 alphabet and nothing else."""
    tokens = text.split()
    return (
        0 < len(tokens) <= 3
        and all(re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", t) for t in tokens)
        and any(len(t) >= 12 and len(set(t)) >= 10 and (any(c.isdigit() for c in t) or not t.islower()) for t in tokens)
    )


def drop_base64_lines(text: str) -> str:
    """*text* without its base64-looking lines (60+ characters of the base64 alphabet, no spaces: a payload wrapped at
    64 or 76 characters has no long line), plus the shorter last line of such a block. The prose around them is kept;
    an empty result means the whole chunk was payload."""
    lines = text.splitlines()
    kept: list[str] = []
    after_payload = False  # the previous line was payload: a wrapped block ends with a shorter line
    for raw in lines:
        line = raw.strip()
        payload = bool(_B64_LINE.fullmatch(line)) and _is_payload(line)
        tail = after_payload and bool(_B64_TAIL.fullmatch(line)) and _is_payload(line)
        after_payload = payload
        if not (payload or tail):
            kept.append(raw)
    out = text if len(kept) == len(lines) else re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
    return "" if _is_fragment(out) else out
