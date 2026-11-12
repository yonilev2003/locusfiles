"""Locus's own help articles, shipped inside the engine, and the offline search behind `locus_help`.

The articles live in `locus/support_kb/*.md` (package data: the wheel carries them because hatchling
packages every file under src/locus). Each starts with a small front-matter block:

    ---
    id: install                     # must equal the file name without .md
    title: Install the Locus engine
    url: /docs/connect              # a path on the Locus website; the host comes from LOCUS_API_BASE
    keywords: install, pipx, ...    # comma-separated words and phrases people use for this topic
    updated: 2026-10-01             # when the text was last checked against the product
    ---

This corpus is separate from the user's index on purpose (docs/design/customer-support-connector-
tools-2026-09-21.md): nothing here reads ChromaStore or locus_search results, so text in an indexed
file can never come back as "official Locus help". It needs no network and no account.

Scoring is BM25 over a weighted bag of words (title and keywords count three times, body once),
with a light suffix stemmer so "indexing" finds "index". Small and dependency-free: the corpus is
about twenty short articles.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Any

KB_DIR = "support_kb"
REQUIRED_FIELDS = ("id", "title", "url", "keywords", "updated")

_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TOKEN_RE = re.compile(r"[a-z0-9]+")

# BM25 parameters (the usual defaults) and field weights.
_K1 = 1.2
_B = 0.75
_TITLE_WEIGHT = 3
_KEYWORD_WEIGHT = 3
# A result scoring below this share of the best one is noise, not a second answer.
_RELATIVE_CUTOFF = 0.25

# Words that carry no topic. "locus" is here too: every article is about Locus.
_STOPWORDS = frozenset(
    """
    a an and are as at be been but by can could do does did doing for from has have having how i if
    in into is it its me my of on or our so than that the their them then there these they this to
    too was we were what when where which who why will with would you your yours am any all also
    get got just not no should want need please help locus s t don doesn didn isn wasn won aren
    cannot
    """.split()
)
# Words whose -ing/-ed ending is part of the word, not a suffix.
_NO_SUFFIX = frozenset(
    "embed need speed feed seed bed red shed bring string thing ring nothing something anything everything".split()
)
_KEEP_DOUBLE = ("ll", "ss", "zz")


@dataclass(frozen=True)
class Article:
    id: str
    title: str
    path: str  # site path, e.g. "/docs/connect#cursor"
    keywords: tuple[str, ...]
    updated: str  # YYYY-MM-DD
    body: str


def _stem(token: str) -> str:
    """A deliberately small suffix stemmer, applied to queries and articles alike: plurals, -ing,
    -ed and a final -e fold together ("indexing"/"indexes"/"index", "revoked"/"revoke",
    "embedding"/"embedded"/"embed")."""
    t = token
    if len(t) > 4 and t.endswith("ies"):
        t = t[:-3] + "y"
    elif len(t) > 3 and t.endswith("s") and not t.endswith(("ss", "us", "is")):
        t = t[:-1]
    if t not in _NO_SUFFIX:
        for suffix in ("ing", "ed"):
            if t.endswith(suffix) and len(t) - len(suffix) >= 3:
                t = t[: -len(suffix)]
                if len(t) >= 4 and t[-1] == t[-2] and t[-2:] not in _KEEP_DOUBLE:
                    t = t[:-1]  # "stopped" -> "stop", "embedding" -> "embed"
                break
    if len(t) > 3 and t.endswith("e") and not t.endswith("ee"):
        t = t[:-1]
    return t


def tokenize(text: str) -> list[str]:
    """Lower-case word tokens, stop words removed, lightly stemmed. `LOCUS_API_KEY` -> api, key."""
    return [_stem(t) for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS]


def parse_article(name: str, text: str) -> Article:
    """One article from its file name and text. Raises ValueError on a malformed file."""
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"{name}: must start with a '---' front-matter line")
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        raise ValueError(f"{name}: front matter is not closed with '---'") from None
    meta: dict[str, str] = {}
    for line in lines[1:end]:
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        if not sep:
            raise ValueError(f"{name}: front-matter line without ':': {line!r}")
        meta[key.strip()] = value.strip()
    missing = [f for f in REQUIRED_FIELDS if not meta.get(f)]
    if missing:
        raise ValueError(f"{name}: missing front-matter field(s) {missing}")
    stem = name.removesuffix(".md")
    if meta["id"] != stem or not _ID_RE.match(meta["id"]):
        raise ValueError(f"{name}: id {meta['id']!r} must equal the file name and be lower-case-with-dashes")
    if not meta["url"].startswith("/") or "://" in meta["url"]:
        raise ValueError(f"{name}: url must be a site path such as /docs/connect, got {meta['url']!r}")
    if not _DATE_RE.match(meta["updated"]):
        raise ValueError(f"{name}: updated must be YYYY-MM-DD, got {meta['updated']!r}")
    body = "\n".join(lines[end + 1 :]).strip()
    if not body:
        raise ValueError(f"{name}: empty body")
    keywords = tuple(k.strip() for k in meta["keywords"].split(",") if k.strip())
    return Article(meta["id"], meta["title"], meta["url"], keywords, meta["updated"], body)


@lru_cache(maxsize=1)
def load_articles() -> tuple[Article, ...]:
    """Every bundled article, sorted by id. Read through importlib.resources, so it works from a
    wheel, a zip or a source checkout alike."""
    root = resources.files("locus").joinpath(KB_DIR)
    articles = [
        parse_article(entry.name, entry.read_text(encoding="utf-8"))
        for entry in root.iterdir()
        if entry.is_file() and entry.name.endswith(".md")
    ]
    if not articles:
        raise RuntimeError(f"no help articles found in locus/{KB_DIR}")
    return tuple(sorted(articles, key=lambda a: a.id))


class HelpIndex:
    """BM25 over the articles. Built once per process (help_index())."""

    def __init__(self, articles: tuple[Article, ...]) -> None:
        self.articles = articles
        self._tf: list[Counter[str]] = []
        for a in articles:
            tf: Counter[str] = Counter()
            for _ in range(_TITLE_WEIGHT):
                tf.update(tokenize(a.title))
            for _ in range(_KEYWORD_WEIGHT):
                tf.update(tokenize(" ".join(a.keywords)))
            tf.update(tokenize(a.body))
            self._tf.append(tf)
        self._len = [sum(tf.values()) for tf in self._tf]
        self._avg_len = (sum(self._len) / len(self._len)) if self._len else 1.0
        df: Counter[str] = Counter()
        for tf in self._tf:
            df.update(tf.keys())
        n = len(articles)
        self._idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}

    def score(self, query: str) -> list[tuple[Article, float]]:
        """Every article with a positive score, best first (ties: by id)."""
        terms = set(tokenize(query))
        scored = []
        for article, tf, length in zip(self.articles, self._tf, self._len, strict=True):
            total = 0.0
            for t in terms:
                f = tf.get(t, 0)
                if f:
                    norm = f + _K1 * (1 - _B + _B * length / self._avg_len)
                    total += self._idf[t] * f * (_K1 + 1) / norm
            if total > 0:
                scored.append((article, total))
        scored.sort(key=lambda pair: (-pair[1], pair[0].id))
        return scored

    def search(self, query: str, limit: int = 3) -> list[tuple[Article, float]]:
        """At most `limit` articles: the best match, plus others that score within reach of it."""
        scored = self.score(query)
        if not scored:
            return []
        best = scored[0][1]
        return [pair for pair in scored if pair[1] >= best * _RELATIVE_CUTOFF][:limit]


@lru_cache(maxsize=1)
def help_index() -> HelpIndex:
    return HelpIndex(load_articles())


def article_url(article: Article, base: str) -> str:
    """The article's page on the Locus website, under `base` (Settings.api_base, OPS-007). With no
    usable base (an invalid LOCUS_API_BASE), the bare site path is still a useful pointer."""
    return f"{base}{article.path}" if base else article.path


# Founder proposal 2026-10-01 §5: the help and account tools answer in English, and the assistant
# relays them in the user's language. Product terms stay in English so they match the site and UI.
RELAY_LANGUAGE_LINE = (
    "Relay this to the user in the language of the conversation; keep product terms in English in brackets."
)


def with_language_line(text: str) -> str:
    """`text` followed by RELAY_LANGUAGE_LINE as its own last line."""
    return f"{text.rstrip()}\n\n{RELAY_LANGUAGE_LINE}"


def format_help(query: str, results: list[tuple[Article, float]], *, base: str, version: str) -> str:
    """Markdown for locus_help: the matching articles in full, each with its source page."""
    return with_language_line(_format_help(query, results, base=base, version=version))


def _format_help(query: str, results: list[tuple[Article, float]], *, base: str, version: str) -> str:
    articles = load_articles()
    reviewed = max(a.updated for a in articles)
    provenance = (
        f"From Locus's own help articles, bundled with Locus {version} (last reviewed {reviewed}). "
        "They are not from the user's indexed files. Where they describe a status that changes "
        "(plans, trial, remote connection), the linked page is current."
    )
    if not results:
        lines = [
            f"# Locus help: no article matches {query!r}",
            "",
            provenance,
            "",
            "Topics covered (call locus_help again with one of these, or with other words; the articles "
            "are written in English, so ask in English even when the user wrote in another language):",
        ]
        lines += [f"- {a.title}: {article_url(a, base)}" for a in articles]
        lines += [
            "",
            "For this computer's setup, run locus_diagnose_connection; for the user's own plan and "
            f"usage, run locus_account_status. A person can help too: {article_url(_by_id('contact-support'), base)}",
        ]
        return "\n".join(lines)
    lines = [f"# Locus help: {query}", "", provenance, ""]
    for i, (article, _score) in enumerate(results, 1):
        lines += [
            f"## {i}. {article.title}",
            f"Source: {article_url(article, base)} (updated {article.updated})",
            "",
            article.body,
            "",
        ]
    lines.append("Sources: " + "; ".join(article_url(a, base) for a, _ in results))
    shown = {a.id for a, _ in results}
    if "contact-support" not in shown:
        lines.append(
            "Not solved? locus_diagnose_connection checks this computer's setup, and a person can help: "
            + article_url(_by_id("contact-support"), base)
        )
    return "\n".join(lines)


# Subscription states in which the relays serve an account (their own entitlement gate), unless the
# account-status answer also says the trial has ended.
ENTITLED_STATUSES = frozenset({"active", "trialing"})


def account_is_entitled(data: dict[str, Any]) -> bool:
    """Whether an account-status answer describes an account the relays serve."""
    return str(data.get("status") or "") in ENTITLED_STATUSES and data.get("trial_end") != "ended"


def fetch_account_status(url: str, api_key: str, *, timeout: float = 15) -> dict[str, Any]:
    """GET Locus's account-status endpoint with *api_key* and return its JSON object.

    The one account check the engine makes: `locus_account_status` reports what it returns, and
    `locus setup --managed` uses it to check a key before saving it. It only reads the account and
    spends nothing (no embedding call, no budget). Raises urllib.error.HTTPError (401: key not
    recognized or revoked; 404: no plan record), urllib.error.URLError when the server cannot be
    reached, and ValueError when the answer is not a JSON object. The key goes only into the
    Authorization header; no exception raised here carries it.
    """
    import json
    import urllib.request

    req = urllib.request.Request(url, method="GET", headers={"Authorization": f"Bearer {api_key}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    if not isinstance(data, dict):
        raise ValueError("the account-status answer is not a JSON object")
    return data


def _by_id(article_id: str) -> Article:
    for article in load_articles():
        if article.id == article_id:
            return article
    raise KeyError(article_id)
