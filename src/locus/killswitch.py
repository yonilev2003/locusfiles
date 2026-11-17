"""The kill switch: `locus pause` stops the three file tools until `locus resume`.

One flag file, `<data dir>/PAUSED`, and one environment variable, `LOCUS_DISABLED=1`. While either is set,
`locus_search`, `locus_list_sources` and `locus_read_file` (and the local HTTP API, `locus query --json`, `locus
sources` and `locus read`) answer with one fixed sentence: no path, no file text. `locus_help` and
`locus_diagnose_connection` keep answering and say that Locus is paused. The check is one `stat` and one
environment lookup per call, so pausing and resuming take effect without restarting anything.

The scope is the whole data folder, so every AI app that runs `locus serve` from it is stopped at once. Indexing
and watching keep writing to the local index; they return nothing to an assistant. Nothing here prints to stdout
except the two commands' own answer."""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from pathlib import Path

PAUSE_FILE = "PAUSED"
DISABLED_ENV = "LOCUS_DISABLED"
_TRUE = frozenset({"1", "true", "yes", "on"})

# The fixed sentences a paused tool answers with. They say nothing about the user's files.
PAUSED_SENTENCE = "Locus is paused by the user. Ask them to run `locus resume`."
DISABLED_SENTENCE = (
    f"Locus is switched off by the user ({DISABLED_ENV} is set). Ask them to unset it and restart the AI app."
)


def paused_by(data_dir: Path, env: dict[str, str] | None = None) -> str | None:
    """ "env" when LOCUS_DISABLED is set, "flag" when the PAUSED file exists, else None."""
    value = (os.environ if env is None else env).get(DISABLED_ENV, "")
    if value.strip().lower() in _TRUE:
        return "env"
    return "flag" if (Path(data_dir).expanduser() / PAUSE_FILE).exists() else None


def sentence(reason: str) -> str:
    return DISABLED_SENTENCE if reason == "env" else PAUSED_SENTENCE


def state_line(data_dir: Path) -> str | None:
    """One line for the tools that keep answering while paused (help, diagnose, doctor, stats); None when not paused."""
    reason = paused_by(data_dir)
    if reason is None:
        return None
    return (
        "Locus is paused: the file tools return nothing until the user runs `locus resume`."
        if reason == "flag"
        else f"Locus is switched off: {DISABLED_ENV} is set, so the file tools return nothing."
    )


def pause(data_dir: Path) -> bool:
    """Create the flag (owner-only). False when it was there already."""
    data_dir = Path(data_dir).expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)
    flag = data_dir / PAUSE_FILE
    try:
        fd = os.open(flag, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(f"paused at {datetime.now(UTC).isoformat(timespec='seconds')}\n")
    return True


def resume(data_dir: Path) -> bool:
    """Remove the flag. False when there was none."""
    try:
        (Path(data_dir).expanduser() / PAUSE_FILE).unlink()
    except FileNotFoundError:
        return False
    return True


def cmd_pause(data_dir: Path) -> int:
    created = pause(data_dir)
    print("Locus is paused." if created else "Locus was already paused.")
    print(
        "The file tools (search, list sources, read file, the local API, `locus query --json`) return nothing "
        "until you run `locus resume`."
    )
    if os.environ.get(DISABLED_ENV, "").strip().lower() in _TRUE:
        print(f"({DISABLED_ENV} is set in this environment too.)", file=sys.stderr)
    return 0


def cmd_resume(data_dir: Path) -> int:
    removed = resume(data_dir)
    print("Locus is running again." if removed else "Locus was not paused.")
    if os.environ.get(DISABLED_ENV, "").strip().lower() in _TRUE:
        print(f"{DISABLED_ENV} is still set in this environment, so the file tools stay off until you unset it.")
        return 1
    return 0
