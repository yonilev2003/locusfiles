"""Machine-made noise inside otherwise readable files: base64 payloads.

A notebook attachment, an inline `data:image/png;base64,...` image or a pasted key blob is thousands of
characters of random-looking text. Embedded, it matches nothing, wastes the embedding window and shows up
as a nonsense search hit. `strip_base64` removes such runs from a parsed file's text, `is_noise_chunk`
drops a chunk that is still mostly base64 (wrapped at 64 or 76 characters, so no single line is long), and
`PARSER_VERSION` is stamped on every chunk so a file indexed by an older parser is re-indexed once.
"""

from __future__ import annotations

import re

# Bump when what the parsers or the noise filters extract changes in a way old rows should not keep.
# 1 = before the noise filter (rows carry no stamp); 2 = base64 stripped, base64 chunks dropped.
PARSER_VERSION = 2
PARSER_VERSION_KEY = "parser_version"

_DATA_URI = re.compile(r"data:[A-Za-z0-9.+/-]+(?:;[A-Za-z0-9=.+-]+)*;base64,[A-Za-z0-9+/_=-]+")
_LONG_RUN = re.compile(r"[A-Za-z0-9+/]{200,}={0,2}")
_B64_LINE = re.compile(r"[A-Za-z0-9+/]{60,}={0,2}")
_MIN_DISTINCT = 16  # a run of one repeated character ("////", "aaaa") is not a payload
_MIN_NOISE_SHARE = 0.5  # share of a chunk's characters that sit on base64-looking lines
REMOVED = "[base64 data removed]"


def _is_payload(run: str) -> bool:
    return len(set(run)) >= _MIN_DISTINCT


def strip_base64(text: str) -> str:
    """*text* with data: URIs and base64-looking runs of 200+ characters replaced by a short marker."""
    if "base64" not in text and not _LONG_RUN.search(text):
        return text
    text = _DATA_URI.sub(REMOVED, text)
    return _LONG_RUN.sub(lambda m: REMOVED if _is_payload(m.group()) else m.group(), text)


def is_noise_chunk(text: str) -> bool:
    """True when most of *text* is base64-looking lines (60+ characters of the base64 alphabet, no spaces)."""
    total = len(text.strip())
    if total == 0:
        return True
    noisy = sum(
        len(line)
        for line in (ln.strip() for ln in text.splitlines())
        if _B64_LINE.fullmatch(line) and _is_payload(line)
    )
    return noisy / total >= _MIN_NOISE_SHARE
