"""One big file must not fill the whole top-k: at most two chunks per file while other files match. Offline."""

from __future__ import annotations

from locus.collapse import PER_FILE_MAX, visible_collapsed_hits
from locus.ingest.store import Hit


class _Open:
    def allowed(self, _path):
        return True


def _hits(spec: list[tuple[str, int]]) -> list[Hit]:
    return [
        Hit(path=p, chunk_index=i, text=f"{p} chunk {i} words", score=1.0 - n / 1000) for n, (p, i) in enumerate(spec)
    ]


def _fetcher(all_hits: list[Hit]):
    def fetch(n: int) -> list[Hit]:
        return all_hits[:n]

    return fetch


def test_a_big_file_cannot_fill_the_top_k_when_other_files_match():
    others = [(f"/note{n}.md", 0) for n in range(10)]
    ranked = [("/big.csv", i) for i in range(30)] + others
    rows, _ = visible_collapsed_hits(_Open(), _fetcher(_hits(ranked)), 10)
    paths = [r.path for r in rows]
    assert len(rows) == 10 and paths.count("/big.csv") == PER_FILE_MAX == 2
    assert {p for p, _ in others[:8]} <= set(paths)
    assert [r.chunk_index for r in rows if r.path == "/big.csv"] == [0, 1]  # the best-ranked ones, rank order kept
    assert all("path" in r.__dataclass_fields__ and r.chunk_index is not None for r in rows)


def test_when_no_other_file_matches_the_page_is_still_filled_from_that_file():
    ranked = [("/big.csv", i) for i in range(30)]
    rows, _ = visible_collapsed_hits(_Open(), _fetcher(_hits(ranked)), 10)
    assert len(rows) == 10 and {r.path for r in rows} == {"/big.csv"}
    assert [r.chunk_index for r in rows] == list(range(10))


def test_a_topped_up_page_keeps_the_other_file_and_rank_order():
    ranked = [("/big.csv", i) for i in range(6)] + [("/a.md", 0)]
    rows, _ = visible_collapsed_hits(_Open(), _fetcher(_hits(ranked)), 5)
    assert [(r.path, r.chunk_index) for r in rows] == [
        ("/big.csv", 0),
        ("/big.csv", 1),
        ("/big.csv", 2),
        ("/big.csv", 3),
        ("/a.md", 0),
    ]
