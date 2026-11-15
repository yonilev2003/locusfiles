"""Pre-index size and token estimate (PROD-012; master plan Ch.6 PROD-012 and Ch.13.7.4).

`scan()` walks a folder with the indexer's own `iter_files()`, so the files it counts are exactly
the files `locus index` would consider, and it counts every exclusion by reason. `estimate()` turns
bytes into embedding tokens: it parses and chunks a random sample of the files (all of them for a
small folder), within a time budget, measures tokens per byte for each file type, and applies that
to the files it did not sample. Types with no usable sample fall back to fixed per-type ratios.

Nothing here embeds anything or opens a network connection (tests assert it). All ratios are
ASSUMPTIONS until calibrated against real managed runs (Voyage's usage.total_tokens on the Ch.7
corpora; PROD-012 closure asks for ±25%). They carry RATIOS_VERSION so a stored estimate can be
compared with the ratios that produced it.

No prices or plan budgets live here yet, so plan fit is reported as not available (in neutral user
wording: user-facing copy never names an internal ADR). PLAN_TOKEN_BUDGETS_PENDING_A1 is the one
table that turns it on. The pricing decision was since made (monthly budgets plus a
one-time first-index allowance); filling this table from it, and deciding how the allowance is shown,
is an open follow-up (engine-pipeline pack report, review F6).
"""

from __future__ import annotations

import math
import random
import re
import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Settings
from ..policy import DENIED_SKIP_REASONS, Matcher
from .chunker import chunk_text
from .indexer import iter_files
from .parsers import parse_document

RATIOS_VERSION = "2026-10-02.1"


@dataclass(frozen=True)
class TypeRatio:
    chars_per_byte: float  # extracted text per file byte, used only when no file of the type was sampled
    chars_per_token: float  # Latin-script characters per embedding token


# ASSUMPTION (RATIOS_VERSION): not yet measured against the Voyage tokenizer. Prose follows the
# chunker's own 4 chars/token (chunker._CHARS_PER_TOKEN); code, data and markup tokenize denser.
# PDF and DOCX files are mostly non-text bytes (fonts, images, zip), so their bytes say little;
# sampling replaces these fallbacks with measured values whenever a file of the type parses.
RATIOS: dict[str, TypeRatio] = {
    "prose": TypeRatio(chars_per_byte=1.0, chars_per_token=4.0),
    "code": TypeRatio(chars_per_byte=1.0, chars_per_token=3.2),
    "data": TypeRatio(chars_per_byte=1.0, chars_per_token=3.0),
    "markup": TypeRatio(chars_per_byte=1.0, chars_per_token=3.3),
    "pdf": TypeRatio(chars_per_byte=0.08, chars_per_token=4.0),
    "docx": TypeRatio(chars_per_byte=0.35, chars_per_token=4.0),
    # .pptx, .xlsx, OpenDocument, .epub and .rtf (engine 0.1.2): zipped XML or control-word markup like
    # DOCX, so the DOCX bytes-to-text ratio is reused until a calibration run measures them.
    "office": TypeRatio(chars_per_byte=0.35, chars_per_token=4.0),
    "other": TypeRatio(chars_per_byte=1.0, chars_per_token=3.5),
}
KIND_OF_EXT: dict[str, str] = {
    ".md": "prose",
    ".txt": "prose",
    ".rst": "prose",
    ".py": "code",
    ".js": "code",
    ".ts": "code",
    ".sh": "code",
    ".sql": "code",
    ".json": "data",
    ".yaml": "data",
    ".yml": "data",
    ".toml": "data",
    ".csv": "data",
    ".xml": "data",
    ".html": "markup",
    ".htm": "markup",
    ".pdf": "pdf",
    ".docx": "docx",
    ".pptx": "office",
    ".xlsx": "office",
    ".odt": "office",
    ".ods": "office",
    ".odp": "office",
    ".epub": "office",
    ".rtf": "office",
    ".markdown": "prose",
    ".mdx": "prose",
    ".org": "prose",
    ".tex": "prose",
    ".log": "prose",
    ".ini": "data",
    ".cfg": "data",
    ".tsv": "data",
    ".ipynb": "data",
}
# ASSUMPTION: scripts outside Latin (Hebrew, Arabic, Cyrillic, CJK...) take far more tokens per
# character; counted separately so a Hebrew corpus is not estimated like English.
NON_LATIN_CHARS_PER_TOKEN = 1.6
_NON_LATIN = re.compile(r"[^\x00-\u024f\s]")  # anything past Latin Extended-B
# ASSUMPTION: the spread of real tokens around the point estimate (Ch.13.7.4: 0.8x-1.5x).
RANGE_LOW = 0.8
RANGE_HIGH = 1.5
# ASSUMPTION: LOCUS_EMBEDDER=local (bge-small) throughput on a recent laptop CPU, tokens/second.
LOCAL_TOKENS_PER_SECOND = (1500.0, 6000.0)

# Sampling (Ch.13.7.4: everything below 2,000 files, else a random sample), bounded in time.
SAMPLE_ALL_BELOW = 2000
SAMPLE_FRACTION = 0.02
MIN_SAMPLE_PER_KIND = 20
DEFAULT_SAMPLE_SECONDS = 60.0

# Monthly managed-token budget per plan, {plan name: tokens}. Empty on purpose: fill it only from
# the pricing decision (made after this module was written), together with how the first-index allowance is
# shown; never from the master plan's draft numbers. Follow-up from review F6.
PLAN_TOKEN_BUDGETS_PENDING_A1: dict[str, int] = {}


def kind_of(ext: str) -> str:
    return KIND_OF_EXT.get(ext.lower(), "other")


def text_tokens(text: str, kind: str) -> float:
    """Estimated embedding tokens for *text* of file kind *kind* (ASSUMPTION ratios)."""
    non_latin = len(_NON_LATIN.findall(text))
    latin = len(text) - non_latin
    return latin / RATIOS[kind].chars_per_token + non_latin / NON_LATIN_CHARS_PER_TOKEN


def overlap_factor(settings: Settings) -> float:
    """Extra text the chunker repeats between neighbouring chunks, as a multiplier (mirrors
    chunk_text's own clamp: overlap is at most a third of a chunk)."""
    chunk = max(settings.chunk_tokens, 50)
    overlap = min(max(settings.overlap_tokens, 0), chunk // 3)
    return 1.0 + overlap / chunk


@dataclass(frozen=True)
class FileEntry:
    path: Path
    size: int

    @property
    def ext(self) -> str:
        return self.path.suffix.lower()

    @property
    def kind(self) -> str:
        return kind_of(self.ext)


@dataclass
class Scan:
    """What `locus index <root>` would look at, and what it would leave out and why."""

    root: Path
    files: list[FileEntry]
    excluded: Counter[str] = field(default_factory=Counter)  # reason (indexer.SKIP_REASONS) -> count
    skipped_dir_names: Counter[str] = field(default_factory=Counter)  # e.g. {"node_modules": 3}

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.files)

    def by_ext(self) -> dict[str, tuple[int, int]]:
        """{ext: (files, bytes)}, largest first."""
        acc: dict[str, list[int]] = {}
        for f in self.files:
            row = acc.setdefault(f.ext or "(none)", [0, 0])
            row[0] += 1
            row[1] += f.size
        return {k: (v[0], v[1]) for k, v in sorted(acc.items(), key=lambda kv: (-kv[1][1], kv[0]))}


def scan(root: Path | str, settings: Settings, matcher: Matcher | None = None) -> Scan:
    """Walk *root* as `locus index` does; with a *matcher* (the deny-list), refused files are counted, not listed."""
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(root)
    result = Scan(root=root, files=[])

    def on_skip(reason: str, path: Path) -> None:
        result.excluded[reason] += 1
        if reason in ("skipped_dir", "hidden_dir"):
            result.skipped_dir_names[path.name] += 1

    for path in iter_files(root, settings, on_skip, matcher):
        try:
            size = path.stat().st_size
        except OSError:
            result.excluded["unreadable"] += 1
            continue
        result.files.append(FileEntry(path, size))
    return result


@dataclass
class KindEstimate:
    kind: str
    files: int = 0
    bytes: int = 0
    sampled_files: int = 0
    sampled_bytes: int = 0
    sampled_tokens: float = 0.0
    unparsed: int = 0  # sampled but empty or failed to parse
    no_text: int = 0  # sampled PDFs under the characters-per-page threshold (scanned)
    truncated: int = 0  # sampled PDFs cut at the page or time limit
    tokens: float = 0.0

    @property
    def measured(self) -> bool:
        return self.sampled_bytes > 0


@dataclass
class Estimate:
    files: int
    bytes: int
    tokens: float
    kinds: dict[str, KindEstimate]
    sample_seconds: float
    sample_cut_short: bool
    ratios_version: str = RATIOS_VERSION

    @property
    def tokens_low(self) -> float:
        return self.tokens * RANGE_LOW

    @property
    def tokens_high(self) -> float:
        return self.tokens * RANGE_HIGH

    @property
    def sampled_files(self) -> int:
        return sum(k.sampled_files for k in self.kinds.values())

    @property
    def pdf_sampled(self) -> int:
        return sum(k.sampled_files for k in self.kinds.values() if k.kind == "pdf")

    @property
    def pdf_no_text(self) -> int:
        return sum(k.no_text for k in self.kinds.values())

    @property
    def pdf_truncated(self) -> int:
        return sum(k.truncated for k in self.kinds.values())

    def local_cpu_seconds(self) -> tuple[float, float]:
        slow, fast = LOCAL_TOKENS_PER_SECOND
        return self.tokens_low / fast, self.tokens_high / slow

    def plan_fit(self, budgets: dict[str, int] | None = None) -> dict[str, float] | None:
        """Share of each plan's monthly budget the point estimate would use; None while pending A1."""
        table = PLAN_TOKEN_BUDGETS_PENDING_A1 if budgets is None else budgets
        if not table:
            return None
        return {plan: self.tokens / budget for plan, budget in table.items() if budget > 0}

    def to_dict(self) -> dict[str, Any]:
        return {
            "ratios_version": self.ratios_version,
            "assumption": True,
            "files": self.files,
            "bytes": self.bytes,
            "tokens": round(self.tokens),
            "tokens_low": round(self.tokens_low),
            "tokens_high": round(self.tokens_high),
            "sampled_files": self.sampled_files,
            "sample_cut_short": self.sample_cut_short,
            "kinds": {
                k.kind: {
                    "files": k.files,
                    "bytes": k.bytes,
                    "tokens": round(k.tokens),
                    "sampled_files": k.sampled_files,
                    "measured": k.measured,
                }
                for k in self.kinds.values()
            },
            "local_cpu_seconds": [round(s) for s in self.local_cpu_seconds()],
            "plan_fit": self.plan_fit(),
        }


def _sample(entries: list[FileEntry], rng: random.Random) -> list[FileEntry]:
    if len(entries) < SAMPLE_ALL_BELOW:
        chosen = list(entries)
    else:
        by_kind: dict[str, list[FileEntry]] = {}
        for e in entries:
            by_kind.setdefault(e.kind, []).append(e)
        chosen = []
        for group in by_kind.values():
            k = min(len(group), max(MIN_SAMPLE_PER_KIND, math.ceil(SAMPLE_FRACTION * len(group))))
            chosen.extend(rng.sample(group, k))
    rng.shuffle(chosen)  # a time cut-off then still leaves a random subset
    return chosen


def estimate(
    entries: Iterable[FileEntry],
    settings: Settings,
    *,
    sample_seconds: float = DEFAULT_SAMPLE_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    seed: int = 0,
) -> Estimate:
    """Estimate embedding tokens for *entries* by sampling (see module docstring)."""
    entries = list(entries)
    kinds: dict[str, KindEstimate] = {}
    for e in entries:
        k = kinds.setdefault(e.kind, KindEstimate(e.kind))
        k.files += 1
        k.bytes += e.size

    started = clock()
    cut_short = False
    sampled: set[Path] = set()
    for e in _sample(entries, random.Random(seed)):
        if clock() - started >= sample_seconds:
            cut_short = True
            break
        k = kinds[e.kind]
        try:
            parsed = parse_document(e.path)
            text = parsed.text
            k.no_text += parsed.no_text
            k.truncated += parsed.truncated
        except Exception:  # noqa: BLE001 - a file the parser cannot read is counted, not fatal
            text = None
        chunks = chunk_text(text, settings.chunk_tokens, settings.overlap_tokens) if text else []
        k.sampled_files += 1
        sampled.add(e.path)
        if not chunks:
            k.unparsed += 1
            k.sampled_bytes += e.size  # it is part of what was measured: it yields ~0 tokens
            continue
        k.sampled_bytes += e.size
        k.sampled_tokens += sum(text_tokens(c.text, e.kind) for c in chunks)

    ovl = overlap_factor(settings)
    for k in kinds.values():
        unsampled_bytes = k.bytes - k.sampled_bytes
        if k.measured:
            per_byte = k.sampled_tokens / k.sampled_bytes
        else:
            ratio = RATIOS[k.kind]
            per_byte = ratio.chars_per_byte * ovl / ratio.chars_per_token
        k.tokens = k.sampled_tokens + unsampled_bytes * per_byte

    return Estimate(
        files=len(entries),
        bytes=sum(e.size for e in entries),
        tokens=sum(k.tokens for k in kinds.values()),
        kinds=dict(sorted(kinds.items(), key=lambda kv: -kv[1].tokens)),
        sample_seconds=clock() - started,
        sample_cut_short=cut_short,
    )


# --- rendering ----------------------------------------------------------------------------------

_REASON_LABELS = {  # (one, many)
    "unsupported_type": ("file of an unsupported type", "files of unsupported types"),
    "too_large": ("file over the size limit (LOCUS_MAX_FILE_MB)", "files over the size limit (LOCUS_MAX_FILE_MB)"),
    "hidden_file": ("hidden file", "hidden files"),
    "not_regular": ("named pipe, socket or device", "named pipes, sockets or devices"),
    "symlink": ("symbolic link", "symbolic links"),
    "unreadable": ("unreadable file", "unreadable files"),
}


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def human_tokens(n: float) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return f"{n:.0f}"


def human_duration(seconds: float) -> str:
    if seconds < 60:
        return "under a minute"
    minutes = seconds / 60
    if minutes < 90:
        return f"{minutes:.0f} min"
    return f"{minutes / 60:.1f} h"


def exclusion_lines(result: Scan) -> list[str]:
    parts = [
        f"{n:,} {one if n == 1 else many}"
        for r, (one, many) in _REASON_LABELS.items()
        if (n := result.excluded.get(r, 0))
    ]
    denied = sum(n for r, n in result.excluded.items() if r in DENIED_SKIP_REASONS)
    if denied:
        parts.append(f"{denied:,} file(s) or folder(s) refused by the deny-list (keys, passwords, browser profiles)")
    folders = result.excluded.get("skipped_dir", 0) + result.excluded.get("hidden_dir", 0)
    lines = [f"Not indexed: {', '.join(parts) if parts else 'no files'}"]
    if folders:
        names = ", ".join(name for name, _ in result.skipped_dir_names.most_common(5))
        lines.append(f"Folders not walked: {folders:,} ({names}{', …' if len(result.skipped_dir_names) > 5 else ''})")
    lines.append(
        "Secret scan: not built yet. File contents are not checked for passwords or keys; hidden files "
        "such as .env are skipped."
    )
    return lines


def pdf_lines(est: Estimate) -> list[str]:
    """Counts of scanned and over-limit PDFs among the sampled ones (no file names)."""
    lines = []
    if est.pdf_no_text:
        lines.append(
            f"PDFs with no extractable text: {est.pdf_no_text:,} of {est.pdf_sampled:,} sampled "
            "(scanned or image-only; they would not be indexed)"
        )
    if est.pdf_truncated:
        lines.append(
            f"PDFs cut at the page or time limit: {est.pdf_truncated:,} of {est.pdf_sampled:,} sampled "
            "(the pages read are indexed)"
        )
    return lines


def render_estimate(result: Scan, est: Estimate, settings: Settings) -> str:
    lines = [
        f"Locus estimate for {result.root}",
        "Nothing was embedded or sent anywhere to make this estimate.",
        "",
        f"Files that would be indexed: {est.files:,} ({human_bytes(est.bytes)})",
    ]
    by_ext = result.by_ext()
    if by_ext:
        lines.append(f"  {'type':<8}{'files':>8}{'size':>11}")
        for ext, (count, size) in list(by_ext.items())[:12]:
            lines.append(f"  {ext:<8}{count:>8,}{human_bytes(size):>11}")
        if len(by_ext) > 12:
            lines.append(f"  … and {len(by_ext) - 12} more types")
    lines.extend(exclusion_lines(result))
    lines.extend(pdf_lines(est))
    lines.append("")
    sampled = f"{est.sampled_files:,} of {est.files:,} files parsed to measure" + (
        " (sampling stopped at the time limit)" if est.sample_cut_short else ""
    )
    lines.append(
        f"Estimated embedding tokens: about {human_tokens(est.tokens)} "
        f"(range {human_tokens(est.tokens_low)}–{human_tokens(est.tokens_high)}; {sampled})"
    )
    lines.append(
        f"  ASSUMPTION: ratios v{est.ratios_version}, not yet calibrated against real embedding runs. "
        "Real counts can fall outside the range, especially for code and non-English text."
    )
    low, high = est.local_cpu_seconds()
    lines.append("")
    lines.append(
        f"On this computer (LOCUS_EMBEDDER=local): no embedding fees. Estimated CPU time "
        f"{human_duration(low)} to {human_duration(high)} on a recent laptop (ASSUMPTION)."
    )
    fit = est.plan_fit()
    if fit is None:
        lines.append("Managed plans: a comparison with each plan's budget is not available in this version.")
    else:
        shares = ", ".join(f"{plan} ≈ {share:.0%} of a month" for plan, share in fit.items())
        lines.append(f"Managed plans: {shares}.")
    lines.append("With your own Voyage or OpenAI key, these tokens are billed to your account by that provider.")
    if settings.embedder not in ("local", "hash"):
        lines.append(f"Current setting: LOCUS_EMBEDDER={settings.embedder}.")
    return "\n".join(lines)
