"""Local lexical (BM25) leg: a prototype, off by default and wired into nothing.

A SQLite FTS5 index next to the vector index (`<data_dir>/<collection>.lexical.sqlite`) that ranks chunks by
BM25, plus a reciprocal-rank-fusion helper to combine its ranking with the dense one. SQLite and FTS5 are in
the Python standard library, so this adds no dependency, no model and almost no memory.

Two tokenisations, fixed in advance by the 2026-10-25 retrieval experiment and reproduced here unchanged:
  `uni`  SQLite's `porter unicode61` on the lower-cased text.
  `heb`  Hebrew-aware: niqqud removed, final letters normalised, and for words of four or more letters an extra
         token with a leading prefix letter (ו ב ל כ מ ה ש) or two-letter prefix (וה שה מה לה בה כש וב ול ומ וכ)
         stripped, so "בשולחן" is found by "שולחן". The same function builds the index text and the query terms.

Opt-in: `lexical_enabled()` reads `LOCUS_LEXICAL` (1/true/yes/on). Nothing in the engine calls this module yet:
`index_file`, `locus query` and `locus_search` neither write nor read it, with or without the variable. Wiring it in
(an index-time write beside the Chroma upsert, a query-time fusion) is further work that needs its
own change, a re-index of existing collections, and a measurement on real files.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
from collections import defaultdict
from collections.abc import Hashable, Sequence
from dataclasses import dataclass
from pathlib import Path

LEXICAL_ENV = "LOCUS_LEXICAL"
FLAVOURS = ("uni", "heb")
SCHEMA_VERSION = "1"
RRF_K = 60

_TOKENIZER = {"uni": "porter unicode61 remove_diacritics 2", "heb": "porter unicode61"}

NIQQUD = re.compile(r"[֑-ׇ]")
FINAL = str.maketrans("ךםןףץ", "כמנפצ")
PREF1 = set("ובלכמהש")
PREF2 = {"וה", "שה", "מה", "לה", "בה", "כש", "וב", "ול", "ומ", "וכ"}
STOP = set(
    (
        "a an the of in on at to for and or is are was were be by with from as that this it its what which who "
        "how many much does do did "
        "של את על עם או גם לא כי זה זו הוא היא הם אתה אני מה מי איך כמה איזה איזו אילו האם יש אין היה היו "
        "בין לפי מן אל"
    ).split()
)


def lexical_enabled(env: dict[str, str] | None = None) -> bool:
    """True when the user opted in with LOCUS_LEXICAL=1 (also true/yes/on). Off by default."""
    value = (os.environ if env is None else env).get(LEXICAL_ENV, "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def lexical_path(data_dir: Path, collection: str) -> Path:
    return Path(data_dir).expanduser() / f"{collection}.lexical.sqlite"


def words(text: str) -> list[str]:
    return re.findall(r"[^\W_]+", text.lower())


def heb_tokens(text: str) -> list[str]:
    """Words of *text* with niqqud removed and final letters normalised; a Hebrew word also yields its
    prefix-stripped form (see the module docstring)."""
    out: list[str] = []
    # Niqqud comes off the whole text first: the word splitter treats a combining mark as a separator, so
    # stripping it per word (as the 2026-10-25 experiment script did) left a pointed word in pieces.
    for w in words(NIQQUD.sub("", text)):
        w = w.translate(FINAL)
        out.append(w)
        if re.match(r"[א-ת]", w):
            if len(w) > 3 and w[0] in PREF1:
                out.append(w[1:])
            if len(w) > 4 and w[:2] in PREF2:
                out.append(w[2:])
    return out


def query_terms(text: str, flavour: str) -> list[str]:
    """The distinct query terms of *text* for *flavour*: stop words and one-letter tokens dropped."""
    tokens = heb_tokens(text) if flavour == "heb" else words(text)
    seen: set[str] = set()
    out: list[str] = []
    for t in tokens:
        if t in STOP or len(t) < 2 or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


@dataclass(frozen=True)
class LexicalHit:
    chunk_id: str
    path: str
    score: float  # higher is better (the negated FTS5 bm25())


class LexicalIndex:
    """An FTS5 BM25 index of chunk text keyed by the same chunk ids the vector store uses."""

    def __init__(self, path: Path, flavour: str = "heb") -> None:
        if flavour not in FLAVOURS:
            raise ValueError(f"flavour must be one of {FLAVOURS}, not {flavour!r}")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        with self._lock:
            self._db.execute("create table if not exists meta(key text primary key, value text)")
            self._db.execute(
                "create table if not exists chunks(rowid integer primary key, chunk_id text unique, path text)"
            )
            self._db.execute("create index if not exists chunks_path on chunks(path)")
            recorded = self._meta("flavour")
            if recorded is None:
                self._db.execute(
                    f"create virtual table if not exists t using fts5(body, tokenize='{_TOKENIZER[flavour]}')"
                )
                self._db.executemany(
                    "insert or replace into meta(key, value) values (?, ?)",
                    [("flavour", flavour), ("schema_version", SCHEMA_VERSION)],
                )
                self._db.commit()
            elif recorded != flavour:
                self._db.close()
                raise RuntimeError(
                    f"{self.path.name} was built with the {recorded!r} tokenisation, not {flavour!r}: "
                    "delete it and rebuild (the tokenisation is fixed when the index is created)."
                )
        self.flavour = flavour

    def _meta(self, key: str) -> str | None:
        row = self._db.execute("select value from meta where key = ?", (key,)).fetchone()
        return row[0] if row else None

    def _body(self, text: str) -> str:
        return " ".join(heb_tokens(text)) if self.flavour == "heb" else text.lower()

    # -- writes ----------------------------------------------------------------------------------------
    def upsert(self, chunk_ids: Sequence[str], paths: Sequence[str], texts: Sequence[str]) -> None:
        """Insert or replace chunks; a chunk id already present is overwritten, never duplicated."""
        if not (len(chunk_ids) == len(paths) == len(texts)):
            raise ValueError("chunk_ids, paths and texts must have the same length")
        with self._lock, self._db:
            for cid, path, text in zip(chunk_ids, paths, texts, strict=True):
                row = self._db.execute("select rowid from chunks where chunk_id = ?", (cid,)).fetchone()
                if row:
                    self._db.execute("delete from t where rowid = ?", (row[0],))
                    self._db.execute("delete from chunks where rowid = ?", (row[0],))
                cur = self._db.execute("insert into chunks(chunk_id, path) values (?, ?)", (cid, path))
                self._db.execute("insert into t(rowid, body) values (?, ?)", (cur.lastrowid, self._body(text)))

    def delete_path(self, path: str) -> int:
        """Remove every chunk of *path* (the Chroma store's `delete_path` counterpart). Returns the count."""
        with self._lock, self._db:
            rows = self._db.execute("select rowid from chunks where path = ?", (path,)).fetchall()
            for (rowid,) in rows:
                self._db.execute("delete from t where rowid = ?", (rowid,))
            self._db.execute("delete from chunks where path = ?", (path,))
        return len(rows)

    # -- reads -----------------------------------------------------------------------------------------
    def query(self, text: str, n: int = 50, path_prefix: str | None = None) -> list[LexicalHit]:
        """The best *n* chunks for *text* by BM25, best first; [] when the query has no usable term."""
        terms = query_terms(text, self.flavour)
        if not terms:
            return []
        expr = " OR ".join('"' + t.replace('"', "") + '"' for t in terms)
        sql = (
            "select c.chunk_id, c.path, bm25(t) as s from t join chunks c on c.rowid = t.rowid "
            "where t match ? order by s limit ?"
        )
        with self._lock:
            try:
                rows = self._db.execute(sql, (expr, n if path_prefix is None else max(n * 20, 1000))).fetchall()
            except sqlite3.OperationalError:
                return []
        hits = [LexicalHit(cid, path, -score) for cid, path, score in rows]
        if path_prefix is not None:
            prefix = path_prefix.rstrip("/\\")
            hits = [h for h in hits if h.path == prefix or h.path.startswith(prefix + os.sep)][:n]
        return hits

    def count(self) -> int:
        with self._lock:
            return int(self._db.execute("select count(*) from chunks").fetchone()[0])

    def close(self) -> None:
        with self._lock:
            self._db.close()


def open_if_enabled(data_dir: Path, collection: str, flavour: str = "heb") -> LexicalIndex | None:
    """The collection's lexical index when LOCUS_LEXICAL is on, else None, creating nothing on disk."""
    if not lexical_enabled():
        return None
    return LexicalIndex(lexical_path(data_dir, collection), flavour)


def rrf_fuse(
    rankings: Sequence[Sequence[Hashable]], k: int = RRF_K, weights: Sequence[float] | None = None
) -> list[Hashable]:
    """Reciprocal rank fusion: each ranking contributes weight / (k + rank) to an item; best first, ties by
    first appearance order of the item across the rankings (stable and deterministic)."""
    if weights is not None and len(weights) != len(rankings):
        raise ValueError("one weight per ranking")
    score: dict[Hashable, float] = defaultdict(float)
    first_seen: dict[Hashable, int] = {}
    for li, ranking in enumerate(rankings):
        w = 1.0 if weights is None else weights[li]
        for rank, item in enumerate(ranking):
            score[item] += w / (k + rank + 1)
            first_seen.setdefault(item, len(first_seen))
    return sorted(score, key=lambda item: (-score[item], first_seen[item]))
