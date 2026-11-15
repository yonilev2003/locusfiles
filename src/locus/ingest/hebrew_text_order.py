"""Hebrew PDF text-order repairs: a prototype, wired into nothing (night 3, P7).

A PDF text layer stores glyphs in drawing order, and some Hebrew documents come out of the text extractor in visual
order instead of reading order. Three defects were reproduced on 15 course PDFs:

1. **A Latin phrase inside a Hebrew document comes out reversed**: the words in reverse order, each sentence's
   punctuation at the front of the next word, hyphenated names and apostrophe words turned around
   (".development prototype and ,planning" for "planning, and prototype development.").
2. **Brackets are mirrored on Hebrew lines**: ")500 ש"ח(" for "(500 ש"ח)".
3. **A minus or hyphen sign is displaced behind its number**: "ב1,500-" for "ב-1,500".

`normalise_hebrew_pdf_text` applies the three repairs line by line. It is conservative: a repair fires only on a pattern
that ordinary text does not produce, and `Repairs` counts what it changed so a caller can log it. It cannot fix what
needs the glyph positions: a variable such as `p` that lands on its own line in the middle of a sentence, a currency
sign displaced next to its number, or a reversed Latin line with no punctuation at all in a block that has none.
Brackets inside a reversed Latin run are not repaired (see the research note). Those would need the character boxes
(pypdfium2 `get_charbox`) and a real bidi reorder.

Wired into the PDF reader (`parsers.read_pdf`, one page at a time) behind LOCUS_PDF_HEBREW_FIX, default on. A page
without Hebrew letters comes back unchanged (tests/test_pdf_quality.py). Evidence on real Hebrew PDFs is by eye on 46
files, with no ground truth: label questions and check the answers before trusting it. A PDF
already indexed is not re-read until its bytes change, so turning this on needs a re-index of those files.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_HEBREW = re.compile(r"[א-ת]")
_LATIN_WORD = re.compile(r"[A-Za-z]{2,}")
_LEADING_PUNCT = re.compile(r"^([.,;:!?]+)(.*)$", re.DOTALL)
# sentence punctuation glued to the front of a word (" .development", " ,planning", " :creation"). Leader dots, a list
# number ("3. What") and a bracket after the period (".)Lusardi, 2020": right order, only the punctuation moved)
# are not it
_BLOCK_TRIGGER = re.compile(r"^\s*[.,;:](?=[A-Za-z])")
MIN_HEBREW_LETTERS = 20  # fewer Hebrew letters than this: treat the text as not Hebrew
_APOSTROPHE_SUFFIX = {"s", "t", "re", "ll", "d", "ve", "m"}
_LIST_MARKER = re.compile(r"^\s*[א-תA-Za-z0-9]{1,2}\)")
_SIGN_AFTER_NUMBER = re.compile(r"(?<![\d,.])([א-ת])(\d[\d,.]*\d|\d)-(?=\s|$)")


@dataclass
class Repairs:
    reversed_latin_lines: int = 0
    mirrored_bracket_lines: int = 0
    displaced_signs: int = 0

    @property
    def total(self) -> int:
        return self.reversed_latin_lines + self.mirrored_bracket_lines + self.displaced_signs


def _is_latin_line(line: str) -> bool:
    return not _HEBREW.search(line) and len(_LATIN_WORD.findall(line)) >= 3


def _unreverse_token(tok: str) -> str:
    m = _LEADING_PUNCT.match(tok)
    lead, body = (m.group(1), m.group(2)) if m else ("", tok)
    apos = re.match(r"^([A-Za-z]+)([’'])([A-Za-z]+)$", body)
    if apos and apos.group(1).lower() in _APOSTROPHE_SUFFIX:
        body = f"{apos.group(3)}{apos.group(2)}{apos.group(1)}"
    elif re.match(r"^[A-Za-z]+(-[A-Za-z]+)+$", body):
        body = "-".join(reversed(body.split("-")))
    return body + lead


def unreverse_latin_line(line: str) -> str:
    """Put one reversed Latin line back in reading order (words reversed, punctuation moved to the word's end)."""
    tokens = line.split()
    return " ".join(_unreverse_token(t) for t in reversed(tokens))


def fix_reversed_latin_blocks(lines: list[str]) -> tuple[list[str], int]:
    """Reverse every Latin-only line of a block (consecutive Latin lines) when one line of the block starts with
    sentence punctuation, which ordinary English never does. Returns (lines, number of lines changed)."""
    out = list(lines)
    changed = 0
    i = 0
    while i < len(out):
        if not _is_latin_line(out[i]):
            i += 1
            continue
        j = i
        while j < len(out) and (_is_latin_line(out[j]) or not out[j].strip()):
            j += 1
        block = range(i, j)
        if any(_BLOCK_TRIGGER.match(out[k]) for k in block if out[k].strip()):
            for k in block:
                if out[k].strip():
                    out[k] = unreverse_latin_line(out[k])
                    changed += 1
        i = j
    return out, changed


def fix_mirrored_brackets(line: str) -> str:
    """On a Hebrew line, swap a bracket pair that appears as `)` ... `(` (mirrored by the bidi step). A list marker
    such as `ב)` at the start is not a bracket. Unmatched brackets are left alone."""
    if not _HEBREW.search(line):
        return line
    chars = list(line)
    marker = _LIST_MARKER.match(line)
    skip = marker.end() - 1 if marker else -1  # index of the marker's ")"
    for open_c, close_c in (("(", ")"), ("[", "]")):
        stack: list[int] = []
        for idx, c in enumerate(chars):
            if idx == skip or c not in (open_c, close_c):
                continue
            if stack and chars[stack[-1]] != c:
                first = stack.pop()
                if chars[first] == close_c:  # `)` before `(`: mirrored
                    chars[first], chars[idx] = open_c, close_c
            else:
                stack.append(idx)
    return "".join(chars)


def fix_displaced_signs(line: str) -> tuple[str, int]:
    """`ב1,500-` -> `ב-1,500`: a hyphen or minus that landed behind the number it belongs to, after a one-letter
    Hebrew prefix."""
    return _SIGN_AFTER_NUMBER.subn(r"\1-\2", line)


def normalise_hebrew_pdf_text(text: str) -> tuple[str, Repairs]:
    """Apply the three repairs to *text*; returns (text, what changed). Text without Hebrew or reversed Latin
    blocks comes back unchanged."""
    repairs = Repairs()
    lines = text.split("\n")
    if len(_HEBREW.findall(text)) >= MIN_HEBREW_LETTERS:  # a reversed Latin run is a Hebrew-document defect
        lines, repairs.reversed_latin_lines = fix_reversed_latin_blocks(lines)
    out = []
    for line in lines:
        fixed = fix_mirrored_brackets(line)
        if fixed != line:
            repairs.mirrored_bracket_lines += 1
        fixed, n = fix_displaced_signs(fixed)
        repairs.displaced_signs += n
        out.append(fixed)
    return "\n".join(out), repairs
