"""The audit log: what Locus handed to an assistant or a program, as paths and counts.

One JSON line per call in `<data dir>/audit/<source>-YYYY-MM-DD.jsonl` (one file a UTC day, owner-only,
appended, never rewritten). `api-*` is the local HTTP API (api.py); `mcp-*` is the three file tools of `locus
serve` and `cli-*` is `locus query --json`, `locus sources` and `locus read`. A line holds the time, the tool or
route, the client name the caller announced (self-reported, not authenticated), the file paths returned, how
many characters, whether the answer was cut, how many results folder rules hid. It never holds a chunk of file
text. It does not hold the query either, unless `[audit] queries = true` is set in policy.toml.

Files older than RETENTION_DAYS are deleted when the log is opened and once a day after that. The log lives in
the data folder, so it is as readable as the index is, and the paths in it can themselves be sensitive. A line
that cannot be written never stops a search (log_call says so on stderr and goes on); the HTTP API chooses the
opposite for itself.

The counters a volume limit would need (calls and characters returned in a window) are `summarize`'s output; nothing
here limits anything.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

RETENTION_DAYS = 90
_NAME = re.compile(r"^([a-z]+)-(\d{4}-\d{2}-\d{2})\.jsonl$")

API_FIELDS = ("ts", "method", "route", "status", "results", "bytes", "paths", "error")
MCP_FIELDS = ("ts", "tool", "client", "client_version", "paths", "chars", "truncated", "hidden", "refused", "query")

# Placeholders for a volume limit (research note section 3.2, R8): more than this in WINDOW_MINUTES would be a lot.
# There is no measurement of legitimate use behind these numbers; nothing in Locus enforces or warns on them yet.
WARN_CALLS = 300
WARN_CHARS = 25_000_000
WINDOW_MINUTES = 10


class AuditLog:
    """Append-only: one JSON object per line in `<prefix>-YYYY-MM-DD.jsonl` (UTC day), opened with O_APPEND and never
    rewritten. Only the *fields* are ever written."""

    def __init__(self, directory: Path, prefix: str = "api", fields: tuple[str, ...] = API_FIELDS) -> None:
        self.directory = Path(directory)
        self.prefix = prefix
        self.fields = fields
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.directory, 0o700)
        except OSError:
            pass  # not ours to change (a shared folder): the files are still created owner-only
        self._lock = threading.Lock()
        self._purged_on = ""
        self.write_probe()

    def write_probe(self) -> None:
        """Fail at start-up, not at the first request, when the log cannot be written."""
        with self._lock:
            os.close(os.open(self._file(datetime.now(UTC)), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600))
            self._purge(datetime.now(UTC))

    def _file(self, when: datetime) -> str:
        return str(self.directory / f"{self.prefix}-{when:%Y-%m-%d}.jsonl")

    def _purge(self, now: datetime) -> None:
        """Delete the daily files of any source that are older than RETENTION_DAYS (once per UTC day)."""
        today = f"{now:%Y-%m-%d}"
        if self._purged_on == today:
            return
        self._purged_on = today
        oldest = (now - timedelta(days=RETENTION_DAYS)).date().isoformat()
        try:
            names = os.listdir(self.directory)
        except OSError:
            return
        for name in names:
            found = _NAME.match(name)
            if found and found.group(2) < oldest:
                try:
                    os.unlink(self.directory / name)
                except OSError:
                    pass

    def write(self, record: dict[str, Any]) -> None:
        when = datetime.now(UTC)
        entry = {"ts": when.isoformat(timespec="seconds")} | {k: record[k] for k in self.fields[1:] if k in record}
        line = (json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        with self._lock:
            fd = os.open(self._file(when), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, line)
            finally:
                os.close(fd)
            self._purge(when)


_LOGS: dict[tuple[str, str], AuditLog] = {}
_LOGS_LOCK = threading.Lock()


def log_call(data_dir: Path, source: str, record: dict[str, Any]) -> bool:
    """Write one line for *source* ("mcp" or "cli") under *data_dir*/audit. Never raises: a log that cannot be written
    is said once per line on stderr and the call it describes still answers. True when the line was written."""
    try:
        key = (str(Path(data_dir).expanduser()), source)
        with _LOGS_LOCK:
            log = _LOGS.get(key)
            if log is None:
                log = _LOGS[key] = AuditLog(Path(key[0]) / "audit", source, MCP_FIELDS)
        log.write(record)
        return True
    except Exception as exc:  # noqa: BLE001
        print(
            f"locus: the audit log could not be written ({type(exc).__name__}); the answer was still given",
            file=sys.stderr,
        )
        with _LOGS_LOCK:
            _LOGS.pop((str(Path(data_dir).expanduser()), source), None)
        return False


def read_lines(directory: Path, since: datetime | None = None) -> list[dict[str, Any]]:
    """Every line of every daily file in *directory*, oldest first; with *since*, only lines at or after it."""
    found: list[dict[str, Any]] = []
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return found
    for name in names:
        if not _NAME.match(name):
            continue
        try:
            text = (Path(directory) / name).read_text(encoding="utf-8")
        except OSError:
            continue
        for raw in text.splitlines():
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            if since is not None:
                try:
                    if datetime.fromisoformat(row["ts"]) < since:
                        continue
                except (KeyError, ValueError):
                    continue
            found.append(row)
    return found


def summarize(rows: list[dict[str, Any]], top: int = 10) -> dict[str, Any]:
    """Counts only: calls per tool and per client, characters returned, the paths returned most often."""
    calls: Counter[str] = Counter()
    clients: Counter[str] = Counter()
    paths: Counter[str] = Counter()
    chars = 0
    refused = 0
    for row in rows:
        calls[str(row.get("tool") or row.get("route") or "?")] += 1
        clients[str(row.get("client") or "?")] += 1
        chars += int(row.get("chars") or row.get("bytes") or 0)
        refused += 1 if row.get("refused") else 0
        paths.update(row.get("paths") or [])
    return {
        "calls": sum(calls.values()),
        "refused": refused,
        "chars": chars,
        "by_tool": dict(calls),
        "by_client": dict(clients),
        "top_paths": paths.most_common(top),
    }


_SINCE = re.compile(r"^(\d+)([mhd])$")


def parse_since(text: str, now: datetime | None = None) -> datetime:
    """`30m`, `12h` or `7d` as the moment that long before *now*. Raises ValueError for anything else."""
    found = _SINCE.match(text.strip().lower())
    if not found:
        raise ValueError(f"{text!r} is not a time span: use a number and m, h or d (for example 30m, 12h, 7d)")
    unit = {"m": "minutes", "h": "hours", "d": "days"}[found.group(2)]
    return (now or datetime.now(UTC)) - timedelta(**{unit: int(found.group(1))})


def render_summary(summary: dict[str, Any], span: str) -> str:
    lines = [
        f"Audit log, last {span}: counts and paths only (no file text, no query text).",
        f"calls: {summary['calls']:,} (refused {summary['refused']:,}); characters returned: {summary['chars']:,}",
    ]
    if summary["by_tool"]:
        lines.append("by tool: " + ", ".join(f"{k} {v:,}" for k, v in sorted(summary["by_tool"].items())))
        lines.append("by client: " + ", ".join(f"{k} {v:,}" for k, v in sorted(summary["by_client"].items())))
    if summary["top_paths"]:
        lines.append("paths returned most often:")
        lines += [f"  {n:>5,}  {path}" for path, n in summary["top_paths"]]
    lines.append(
        f"Placeholders for a volume warning, not enforced anywhere: more than {WARN_CALLS} calls or "
        f"{WARN_CHARS:,} characters in {WINDOW_MINUTES} minutes."
    )
    return "\n".join(lines)


def cmd_audit(data_dir: Path, span: str) -> int:
    try:
        since = parse_since(span)
    except ValueError as err:
        print(f"locus: {err}", file=sys.stderr)
        return 2
    rows = read_lines(Path(data_dir).expanduser() / "audit", since)
    print(render_summary(summarize(rows), span))
    return 0
