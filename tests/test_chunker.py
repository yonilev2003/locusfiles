import pytest

from locus.ingest.chunker import chunk_text, max_chunk_chars


def test_empty_text_yields_no_chunks():
    assert chunk_text("") == []
    assert chunk_text("   \n\n  ") == []


def test_short_text_is_one_chunk():
    chunks = chunk_text("Hello world.\n\nSecond paragraph.", chunk_tokens=100, overlap_tokens=10)
    assert len(chunks) == 1
    assert chunks[0].index == 0
    assert "Second paragraph." in chunks[0].text


def test_long_text_splits_with_overlap_and_bounded_size():
    paragraphs = [f"Paragraph {i} " + ("lorem ipsum dolor sit amet " * 12) for i in range(40)]
    text = "\n\n".join(paragraphs)
    chunks = chunk_text(text, chunk_tokens=200, overlap_tokens=30)
    assert len(chunks) > 3
    max_chars = 200 * 4 + 30 * 4 + 2
    assert all(len(c.text) <= max_chars for c in chunks)
    assert [c.index for c in chunks] == list(range(len(chunks)))
    # overlap: the head of chunk i+1 comes from the tail of chunk i
    for prev, nxt in zip(chunks, chunks[1:], strict=False):
        head = nxt.text.split("\n\n", 1)[0]
        assert head and head in prev.text


def test_single_huge_paragraph_is_split_on_sentences():
    text = " ".join(f"Sentence number {i} ends here." for i in range(300))
    chunks = chunk_text(text, chunk_tokens=100, overlap_tokens=0)
    assert len(chunks) > 5
    assert all(len(c.text) <= 100 * 4 for c in chunks)
    assert chunks[0].text.startswith("Sentence number 0")


def test_a_single_run_on_sentence_never_cuts_mid_word():
    """Regression: bullet-list-style text with no sentence punctuation used to hard-wrap
    at an exact character offset, slicing words in half (e.g. 'quarterly mark' / 'mark\n\neting')."""
    words = [f"item{i:04d}bullet" for i in range(400)]  # one long run, no '.', '!' or '?' at all
    text = " ".join(words)
    chunks = chunk_text(text, chunk_tokens=50, overlap_tokens=0)
    assert len(chunks) > 3
    rejoined = " ".join(c.text for c in chunks)
    for word in words:
        assert word in rejoined, f"{word!r} was split across a chunk boundary"


@pytest.mark.parametrize(
    "text",
    [
        "中文" * 12000,  # no spaces: the overlap tail cannot be trimmed on a word boundary
        "\n\n".join("x" * 2800 for _ in range(5)),  # max-size units, no spaces
        " ".join(f"w{i:05d}" for i in range(6000)),  # spaced words: tail trimmed to a boundary
    ],
)
def test_max_chunk_chars_bounds_every_chunk_and_is_reached_without_spaces(text):
    """max_chunk_chars is what SEARCH_CHAR_BUDGET and the eval header size against (RET-004):
    at the defaults a chunk can be 2,800 + 2 + 400 = 3,202 chars, not 2,800 or 3,200."""
    bound = max_chunk_chars()
    assert bound == 3202
    sizes = [len(c.text) for c in chunk_text(text)]
    assert max(sizes) <= bound
    if " " not in text:
        assert max(sizes) == bound


def test_max_chunk_chars_follows_the_settings():
    assert max_chunk_chars(200, 30) == 200 * 4 + 2 + 30 * 4
    assert max_chunk_chars(100, 0) == 400  # no overlap: no tail and no separator
    assert max_chunk_chars(300, 1000) == 1200 + 2 + 1200 // 3  # overlap capped at a third
