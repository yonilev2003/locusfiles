# ruff: noqa: E501 - synthetic Hebrew and English strings
"""Night 3 P7: the Hebrew PDF text-order repairs, on synthetic strings built the way the extractor garbles them."""

from __future__ import annotations

import re

import pytest

from locus.ingest.hebrew_text_order import (
    fix_displaced_signs,
    fix_mirrored_brackets,
    fix_reversed_latin_blocks,
    normalise_hebrew_pdf_text,
    unreverse_latin_line,
)

HEBREW_BODY = "זהו טקסט בעברית שמספיק ארוך כדי להיחשב מסמך עברי. "


def garble(sentence: str) -> str:
    """What the extractor printed for a reversed Latin line: words in reverse order, a word's trailing punctuation at
    its front, a hyphenated word and an apostrophe word turned around."""
    out = []
    for tok in reversed(sentence.split()):
        trail = re.search(r"[.,;:!?]+$", tok)
        lead, core = (trail.group(0), tok[: trail.start()]) if trail else ("", tok)
        if re.fullmatch(r"[A-Za-z]+(-[A-Za-z]+)+", core):
            core = "-".join(reversed(core.split("-")))
        m = re.fullmatch(r"([A-Za-z]+)([’'])(s|t|re|ll|d|ve|m)", core)
        if m:
            core = f"{m.group(3)}{m.group(2)}{m.group(1)}"
        out.append(lead + core)
    return " " + " ".join(out)


SENTENCES = [
    "The New Venture Creation Capstone Seminar provides students with a structured, practice-oriented",
    "framework for transforming entrepreneurial ideas into viable ventures.",
    "mapping, financial planning, and prototype development.",
    "creation: ideation, team formation, business model design, market and competitor analysis, ecosystem",
    "With a master’s degree in business administration and a track record of",
    "The startup owner's manual, a step-by-step guide.",
    "Introduction to Econometrics, Second Edition, Addison-Wesley,",
]


@pytest.mark.parametrize("sentence", SENTENCES)
def test_a_garbled_latin_line_is_put_back(sentence):
    assert unreverse_latin_line(garble(sentence)) == sentence


def test_a_block_of_garbled_lines_inside_a_hebrew_document_is_fixed():
    text = HEBREW_BODY + "\n" + "\n".join(garble(s) for s in SENTENCES[:3]) + "\n" + HEBREW_BODY
    fixed, repairs = normalise_hebrew_pdf_text(text)
    assert fixed.split("\n")[1:4] == SENTENCES[:3]
    assert repairs.reversed_latin_lines == 3 and repairs.total == 3
    assert fixed.split("\n")[0] == HEBREW_BODY  # the Hebrew lines are not touched


def test_a_block_is_fixed_as_a_whole_even_when_a_line_has_no_punctuation():
    lines = [garble("Each team will be mentored by both a faculty advisor"), garble("and an industry professional.")]
    assert fix_reversed_latin_blocks(lines)[0] == [
        "Each team will be mentored by both a faculty advisor",
        "and an industry professional.",
    ]


# -- things that must not change ------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "3. What do we learn from paragraph III?\nPUT A MARK BY THE TWO CORRECT ANSWERS.\n....... i) What electronic health records.",
        "Introduction to Data Science\nThis course equips you with practical data science skills, in business and economics.",
        ".)Lusardi, Menkhoff, & Urban, 2020 is a right-order line whose punctuation moved",
    ],
)
def test_english_and_ordinary_lines_are_left_alone(text):
    for with_hebrew in (False, True):
        doc = (HEBREW_BODY + "\n" if with_hebrew else "") + text
        assert normalise_hebrew_pdf_text(doc)[0] == doc


def test_an_english_only_document_is_never_reversed_even_with_a_dot_first():
    doc = "\n".join(garble(s) for s in SENTENCES[:3])  # no Hebrew in the document at all
    assert normalise_hebrew_pdf_text(doc) == (doc, normalise_hebrew_pdf_text(doc)[1])
    assert normalise_hebrew_pdf_text(doc)[1].total == 0


def test_text_without_defects_is_returned_unchanged_and_the_repair_is_idempotent():
    doc = HEBREW_BODY + '(הערה) ומחיר ב-1,500 ש"ח.\n' + "\n".join(SENTENCES)
    assert normalise_hebrew_pdf_text(doc)[0] == doc
    garbled = HEBREW_BODY + "\n" + garble(SENTENCES[0]) + '\nמחיר ב1,500- ש"ח )הערה(\n'
    once, _ = normalise_hebrew_pdf_text(garbled)
    assert normalise_hebrew_pdf_text(once)[0] == once


# -- mirrored brackets -------------------------------------------------------------------------------------------------
def test_mirrored_brackets_on_hebrew_lines():
    assert (
        fix_mirrored_brackets('עלות העץ )500 ש"ח( ועלות העבודה )1,000 ש"ח( מסתכמות')
        == 'עלות העץ (500 ש"ח) ועלות העבודה (1,000 ש"ח) מסתכמות'
    )
    assert fix_mirrored_brackets("המדגם )ובמקרה הרציף מקסימום לצפיפות(.") == "המדגם (ובמקרה הרציף מקסימום לצפיפות)."
    assert fix_mirrored_brackets('הנראות המקסימלית )אנ"מ( עבור הפרמטר') == 'הנראות המקסימלית (אנ"מ) עבור הפרמטר'
    assert fix_mirrored_brackets("עלויות נוספות )עבודה וכו'( המסתכמות") == "עלויות נוספות (עבודה וכו') המסתכמות"


def test_correct_brackets_unmatched_brackets_and_list_markers_are_not_touched():
    for line in [
        "(הערה) על המחיר",
        "הערה ) בלי פותח",
        "שורה ( בלי סוגר",
        "[א] ו-(ב)",
        "ב) טענה (נכונה) ונמקו",
        "no hebrew )here(",
    ]:
        assert fix_mirrored_brackets(line) == line


def test_square_brackets_mirrored_too():
    assert fix_mirrored_brackets("ראו ]מקור 3[ בספר") == "ראו [מקור 3] בספר"


# -- displaced signs ------------------------------------------------------------------------------------------------------------
def test_a_hyphen_behind_its_number_goes_back_in_front():
    assert fix_displaced_signs('מסתכמות ב1,500- ש"ח') == ('מסתכמות ב-1,500 ש"ח', 1)
    assert fix_displaced_signs("המסתכמות ב50- דולר.") == ("המסתכמות ב-50 דולר.", 1)
    assert fix_displaced_signs("ב-50 דולר, 2432 - סמינר, ב12-13") == ("ב-50 דולר, 2432 - סמינר, ב12-13", 0)


# -- known limits (documented, so a change shows up) -----------------------------------------------------------------------------
def test_known_limit_brackets_inside_a_reversed_run_are_not_repaired():
    fixed = unreverse_latin_line(
        " .startup their to cultures bringing Founders :spawning Cultural .)2024 (.R .H ,Greve"
    )
    assert fixed.startswith("Greve, H. ") and "Founders bringing" in fixed
    assert "(2024)" not in fixed  # the year's brackets stay wrong; see the research note


def test_known_limit_a_dotted_name_in_a_hebrew_document_looks_like_a_reversed_run():
    """A Latin line that starts with ".word" inside a Hebrew document is read as reversed. Rare in course material, but a
    real false positive; an English-only document is safe (previous tests)."""
    line = ".gitignore and .env files are listed in this paragraph about build tools"
    english_only = normalise_hebrew_pdf_text(line)[0]
    assert english_only == line
    assert normalise_hebrew_pdf_text(HEBREW_BODY + "\n" + line)[0] != HEBREW_BODY + "\n" + line
