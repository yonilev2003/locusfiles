"""`locus index --roots ... --dry-run`: what a multi-root index would cover, as counts only (ADR-092).

Walks every root with the indexer's own `iter_files` and a policy Matcher, parses and embeds nothing,
opens no store. The report holds counts by extension, profile and skip reason and never a file or
folder name, so it can be pasted into a chat or a handoff note.
"""

from __future__ import annotations

import re
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings
from .ingest.indexer import iter_files
from .ingest.parsers import TEXT_EXTENSIONS, noise_name_reason
from .policy import Matcher

_PLAIN_EXT = re.compile(r"\.[a-z0-9_+-]{1,12}")


def _reportable_ext(path: Path) -> str:
    """The extension for the unsupported breakdown; anything that is not a short plain token (a dotted
    sentence in a file name) is folded into one label so the report cannot carry part of a file name."""
    ext = path.suffix.lower()
    if not ext:
        return "(none)"
    return ext if _PLAIN_EXT.fullmatch(ext) else "(odd)"


# Modified-age buckets for the hot-set profile: (label, upper bound in days); the last one is open-ended.
AGE_BUCKETS: tuple[tuple[str, float], ...] = (
    ("<= 7 d", 7),
    ("<= 30 d", 30),
    ("<= 90 d", 90),
    ("<= 365 d", 365),
    ("older", float("inf")),
)
_DAY = 86400.0

# Kind groups for the age table. Prose and office documents; structured data and logs; source code and
# markup (everything else the text parser reads). Anything the walker lets through that is in none of
# them (a spreadsheet-like format added later) lands in "other" rather than being dropped.
DOC_EXTENSIONS = frozenset(
    {
        ".md",
        ".markdown",
        ".mdx",
        ".txt",
        ".rst",
        ".org",
        ".tex",
        ".docx",
        ".pdf",
        ".rtf",
        ".odt",
        ".pptx",
        ".odp",
        ".epub",
    }
)
DATA_EXTENSIONS = frozenset(
    {".csv", ".tsv", ".json", ".yaml", ".yml", ".toml", ".xml", ".xlsx", ".ods", ".log", ".ini", ".cfg", ".srt", ".vtt"}
)
KIND_GROUPS = ("docs", "code", "data", "other")


def kind_group(ext: str) -> str:
    if ext in DOC_EXTENSIONS:
        return "docs"
    if ext in DATA_EXTENSIONS:
        return "data"
    return "code" if ext in TEXT_EXTENSIONS else "other"


def age_bucket(mtime: float, now: float) -> int:
    """Index into AGE_BUCKETS for a file last modified at *mtime* (a future mtime counts as the newest)."""
    days = max(0.0, now - mtime) / _DAY
    for i, (_, upper) in enumerate(AGE_BUCKETS):
        if days <= upper:
            return i
    return len(AGE_BUCKETS) - 1


@dataclass
class RootsReport:
    roots: int
    files: Counter[str] = field(default_factory=Counter)  # extension -> files that would be indexed
    bytes: Counter[str] = field(default_factory=Counter)  # extension -> bytes
    by_profile: Counter[str] = field(default_factory=Counter)  # profile -> files
    excluded: Counter[str] = field(default_factory=Counter)  # skip reason -> files or folders
    unsupported: Counter[str] = field(default_factory=Counter)  # extension -> files left out as unsupported_type
    unsupported_bytes: Counter[str] = field(default_factory=Counter)  # extension -> bytes of those files
    # kind group -> [files, bytes] per AGE_BUCKETS entry (counts only, no paths)
    age: dict[str, list[list[int]]] = field(default_factory=dict)
    seconds: float = 0.0
    home_root: bool = False

    @property
    def total_files(self) -> int:
        return sum(self.files.values())


def scan_roots(roots: list[Path], settings: Settings, matcher: Matcher, now: float | None = None) -> RootsReport:
    start = time.monotonic()
    now = time.time() if now is None else now
    report = RootsReport(roots=len(roots), home_root=any(Path(r) == matcher.home for r in roots))

    def on_skip(reason: str, path: Path) -> None:
        report.excluded[reason] += 1
        if reason == "unsupported_type":
            # Extension name and size only, never the path: this is what explains the biggest bucket.
            ext = _reportable_ext(path)
            report.unsupported[ext] += 1
            try:
                report.unsupported_bytes[ext] += path.stat().st_size
            except OSError:
                pass

    for root in roots:
        for path in iter_files(root, settings, on_skip, matcher):
            ext = path.suffix.lower() or "(none)"
            if noise_name_reason(path):
                report.excluded["noise_name"] += 1  # lockfile, *.min.js, generated code: the parser skips it
                continue
            try:
                info = path.stat()
            except OSError:
                report.excluded["unreadable"] += 1
                continue
            size = info.st_size
            cell = report.age.setdefault(kind_group(ext), [[0, 0] for _ in AGE_BUCKETS])[age_bucket(info.st_mtime, now)]
            cell[0] += 1
            cell[1] += size
            report.files[ext] += 1
            report.bytes[ext] += size
            report.by_profile[matcher.profile_of(path)] += 1
    report.seconds = time.monotonic() - start
    return report


HOME_EXPLANATION = (
    "Your home directory is a root. Locus indexes what is under it except a built-in deny-list (keys, "
    "keychains, browser profiles, ~/Library, package caches, .env and key files, security-evidence-*). "
    "Excerpts of indexed files that an assistant retrieves are sent to the assistant you chose. "
    "Edit ~/.locus/policy.toml to exclude more. Read the counts below before indexing."
)


UNSUPPORTED_TOP_N = 25


def render_unsupported(report: RootsReport, top_n: int = UNSUPPORTED_TOP_N) -> list[str]:
    """The top *top_n* unsupported extensions by file count (files, MB); the rest are summed on one line."""
    if not report.unsupported:
        return []
    ranked = sorted(report.unsupported.items(), key=lambda kv: (-kv[1], kv[0]))
    lines = [
        f"\nLeft out as unsupported_type, top {min(top_n, len(ranked))} of {len(ranked):,} extensions (files, MB):"
    ]
    for ext, n in ranked[:top_n]:
        lines.append(f"  {ext:<14} {n:>9,} {report.unsupported_bytes[ext] / 1024 / 1024:>10.1f}")
    rest = ranked[top_n:]
    if rest:
        files = sum(n for _, n in rest)
        mb = sum(report.unsupported_bytes[e] for e, _ in rest) / 1024 / 1024
        lines.append(f"  {f'(other {len(rest):,})':<14} {files:>9,} {mb:>10.1f}")
    return lines


def render_age_table(report: RootsReport) -> list[str]:
    """Files and MB per modified-age bucket for each kind group, with the cumulative share of that group's
    files and bytes covered up to and including the bucket. Counts only."""
    groups = [g for g in KIND_GROUPS if g in report.age]
    if not groups:
        return []
    everything = [[sum(report.age[g][i][k] for g in groups) for k in (0, 1)] for i in range(len(AGE_BUCKETS))]
    lines = [
        "\nBy modified age (files, MB, cumulative % of the group's files and of its bytes up to that age):",
        "  docs = md txt rst org tex docx pdf rtf odt pptx odp epub; data = csv tsv json yaml toml xml xlsx ods log "
        "ini cfg srt vtt; code = the other text types",
    ]
    for name, rows in [*((g, report.age[g]) for g in groups), ("all", everything)]:
        total_files = sum(r[0] for r in rows) or 1
        total_bytes = sum(r[1] for r in rows) or 1
        lines.append(f"  {name}")
        cum_files = cum_bytes = 0
        for (label, _), (n, size) in zip(AGE_BUCKETS, rows, strict=True):
            cum_files += n
            cum_bytes += size
            lines.append(
                f"    {label:<9} {n:>9,} {size / 1024 / 1024:>10.1f}"
                f"   {100 * cum_files / total_files:>5.1f}% {100 * cum_bytes / total_bytes:>5.1f}%"
            )
    return lines


def render_roots_report(report: RootsReport) -> str:
    lines = []
    if report.home_root:
        lines += [HOME_EXPLANATION, ""]
    total_bytes = sum(report.bytes.values())
    lines.append(
        f"Dry run over {report.roots} root(s): {report.total_files:,} files would be indexed "
        f"({total_bytes / 1024 / 1024:.1f} MB) in {report.seconds:.1f} s. Nothing was parsed, embedded or sent."
    )
    lines.append("\nWould be indexed, by extension (files, MB):")
    for ext, n in sorted(report.files.items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"  {ext:<10} {n:>9,} {report.bytes[ext] / 1024 / 1024:>10.1f}")
    lines += render_age_table(report)
    lines.append("\nBy folder profile (files):")
    for profile, n in sorted(report.by_profile.items()):
        lines.append(f"  {profile:<10} {n:>9,}")
    lines.append("\nLeft out, by reason (files; folders for *_dir and denied_* folder rules):")
    for reason, n in sorted(report.excluded.items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"  {reason:<28} {n:>9,}")
    lines += render_unsupported(report)
    if report.excluded.get("unreadable_dir"):
        lines.append(
            f"\n{report.excluded['unreadable_dir']:,} folder(s) could not be read; macOS may be asking for permission "
            "(Desktop, Documents, Downloads); Full Disk Access is a system setting only you can change."
        )
    return "\n".join(lines)
