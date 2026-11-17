"""What Locus says about its licence, in one place, and the one question it asks on a first run.

Locus is licensed under the PolyForm Noncommercial License 1.0.0. Commercial use (work for a company, paid client
work, or use inside a product or service, including by one person) needs a separate paid licence that cannot be bought
yet. These are soft notices only: no licence key, no lock, no network call from this module. The first time
`locus setup` or `locus doctor` runs in a terminal it asks one question and keeps the answer in a small file in the
data folder; a yes stops the command with a pointer to the page. The same words are in `locus license`, in the
`locus_help` article `licence`, and (one sentence) in the server instructions.
"""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path
from typing import TextIO

from . import __version__

COMMERCIAL_URL = "https://locusfiles.com/commercial"
LICENCE_NAME = "PolyForm Noncommercial License 1.0.0"
SPDX_ID = "PolyForm-Noncommercial-1.0.0"

QUESTION = "Will you use Locus for work at a company, for paid client work, or in a product or service? [y/N] "
COMMERCIAL_STOP = (
    f"Commercial use needs a separate paid licence. None can be bought yet. Leave your details at {COMMERCIAL_URL}"
)

# Two sentences and the page: what `locus license` prints and the `licence` help article repeats.
SHORT = (
    f"Locus is licensed under the {LICENCE_NAME}: you may use it for personal, educational, charitable and "
    "public-body purposes, and not for work at a company, paid client work, or inside a product or service. "
    "Commercial use needs a separate paid licence that cannot be bought yet."
)
PAGE_LINE = f"Page: {COMMERCIAL_URL}"
NOT_LEGAL_ADVICE = "This is a plain-words summary, not legal advice; the binding text is the LICENSE file."

# The sentence in the server instructions (mcp_server._instructions_text). It tells the model what to do when the
# user says they use Locus commercially; it never tells it to refuse.
INSTRUCTIONS_SENTENCE = (
    "When the user says they use Locus for work, a company or paid client work, tell them once, briefly, that "
    f"commercial use needs a separate paid licence that is not available yet ({COMMERCIAL_URL}), then keep "
    "answering; never refuse to search because of it."
)

ACK_FILE = "licence-ack"  # in the data folder: "noncommercial <date> <version>", written when the answer is no


def licence_text() -> str:
    """What `locus license` prints."""
    return f"{SHORT}\n{PAGE_LINE}\n{NOT_LEGAL_ADVICE}"


def acknowledged(data_dir: Path) -> bool:
    return (Path(data_dir) / ACK_FILE).is_file()


def _record(data_dir: Path) -> None:
    """Remember a "no" so the question is asked once. Best effort: a data folder that cannot be written means the
    question is asked again next time, never an error."""
    try:
        data_dir = Path(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(data_dir / ACK_FILE, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(f"noncommercial {date.today().isoformat()} {__version__}\n")
    except OSError:
        pass


def ask_once(data_dir: Path, *, stdin: TextIO | None = None, out: TextIO | None = None) -> bool:
    """True: go on. False: the user answered yes, the stop text was printed and the command must stop.

    Asked only in a terminal and only until a "no" is recorded. Without a terminal (a script, CI, an MCP client that
    starts `locus`) nothing is asked and nothing is printed: a soft notice is never a lock."""
    stdin = stdin if stdin is not None else sys.stdin
    out = out if out is not None else sys.stderr
    if acknowledged(data_dir) or not (hasattr(stdin, "isatty") and stdin.isatty()):
        return True
    print(QUESTION, end="", file=out, flush=True)
    try:
        answer = stdin.readline()
    except KeyboardInterrupt:
        print("", file=out)
        raise
    if answer.strip().lower() in ("y", "yes"):
        print(COMMERCIAL_STOP, file=out)
        return False
    _record(data_dir)
    return True
