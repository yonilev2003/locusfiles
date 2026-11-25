"""The technical help articles: the README FAQ is answered in the bundled help, and `locus_help` finds them."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from locus import support

ROOT = Path(__file__).resolve().parents[1]


def _plain(text: str) -> str:
    """Markdown links reduced to their text, whitespace collapsed, so a wrapped sentence compares equal."""
    return " ".join(re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text).split())


def _faq_sentences() -> list[str]:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    start = readme.index("\n## FAQ")
    end = readme.index("\n## ", start + 1)
    sentences: list[str] = []
    for line in readme[start:end].splitlines()[1:]:
        if line.startswith("### "):
            sentences.append(_plain(line[4:]))
    body = re.sub(r"^### .*$", "", readme[start:end].split("\n", 2)[2], flags=re.M)
    for paragraph in re.split(r"\n\s*\n", body):
        text = _plain(paragraph)
        sentences.extend(s for s in re.split(r"(?<=[.?!])\s+", text) if s)
    return sentences


def test_every_readme_faq_sentence_is_in_the_bundled_help():
    sentences = _faq_sentences()
    assert len(sentences) >= 20  # the FAQ was found and split
    corpus = _plain("\n".join(a.body for a in support.load_articles()))
    missing = [s for s in sentences if s not in corpus]
    assert not missing, missing


def test_the_technical_articles_exist_and_are_dated():
    ids = {a.id: a for a in support.load_articles()}
    for name in (
        "how-locus-works",
        "models-and-languages",
        "chunk-sizes",
        "timings-and-sizes",
        "limits",
        "privacy-in-brief",
        "file-types",
        "tuning",
        "troubleshooting-recipes",
        "grep-vs-locus",
        "versions-and-updating",
        "clients-matrix",
    ):
        assert ids[name].updated == "2026-11-22", name


SAMPLE_QUESTIONS = [
    ("how does Locus work under the hood, what is the architecture", "how-locus-works"),
    ("which embedding model is the default and does it support Hebrew", "models-and-languages"),
    ("what chunk size does Locus use and what is the overlap", "chunk-sizes"),
    ("how long does indexing take, how many chunks per second", "timings-and-sizes"),
    ("what are the limitations, can it read scanned documents", "limits"),
    ("are my excerpts sent to my assistant, what leaves my computer", "privacy-in-brief"),
    ("can Locus read PDF, DOCX and EPUB files, which file types", "file-types"),
    ("how do I tune the settings to get better results", "tuning"),
    ("when should I use grep instead of Locus", "grep-vs-locus"),
    ("which AI clients are tested, does it work with Cursor or ChatGPT", "clients-matrix"),
]


@pytest.mark.parametrize(("question", "expected"), SAMPLE_QUESTIONS)
def test_locus_help_returns_the_right_article_first(question, expected):
    ids = [a.id for a, _ in support.help_index().search(question, limit=3)]
    assert ids and ids[0] == expected, (question, ids)
