"""Consent before any chunk text leaves this computer to be embedded (UX-005, master plan Ch.7).

Before `locus index` (and the first pass of `locus watch`, and `locus sync-notion`) sends anything
to a hosted embedder, it shows what it is about to do: how many files and bytes, of which types,
what it leaves out and why, how many files are unchanged and will not be sent, an estimate of the
tokens it will send (PROD-012), and where the text goes, in the same words as the public privacy
page. Then it asks. `--yes` answers for the user; without a terminal to ask on, `--yes` is
required. The local embedders need no consent: nothing is sent to be embedded.

Everything here is read-only and offline: it walks with the indexer's own iter_files(), hashes only
files that are already indexed (to find unchanged ones), parses a sample, and opens no connection.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO
from urllib.parse import urlsplit

from .config import Settings
from .ingest.estimator import (
    Estimate,
    FileEntry,
    Scan,
    estimate,
    exclusion_lines,
    human_bytes,
    human_tokens,
    pdf_lines,
    scan,
)
from .ingest.indexer import file_sha256

# Canonical per-mode data-flow statements. They must stay word-for-word equal to the exports of the
# same names in locus-web/lib/data-flow-claims.ts (the /privacy page's source); tests/test_preflight.py
# compares them, so a change there fails here until both say the same thing.
LOCAL = (
    "Indexing and embeddings run on your computer. When your AI assistant searches, the matching "
    "excerpts go to that assistant, as they would if you pasted them in."
)
OWN_KEY = "Chunk text goes straight from your computer to the embedding provider you chose, under your own account."
MANAGED = (
    "On managed plans, chunk text and your search queries pass through Locus's relay to Voyage AI to be embedded. "
    "We don't store them; we keep only usage counts."
)
ALWAYS = "Your raw files are never uploaded to Locus."

HOSTED_EMBEDDERS = frozenset({"voyage", "openai", "managed"})


def needs_consent(settings: Settings) -> bool:
    """True when indexing would send chunk text off this computer to be embedded."""
    return settings.embedder in HOSTED_EMBEDDERS


def data_flow_sentence(settings: Settings) -> str:
    if settings.embedder == "managed":
        return MANAGED
    if settings.embedder in ("voyage", "openai"):
        return OWN_KEY
    return LOCAL


def destination(settings: Settings) -> str:
    if settings.embedder == "managed":
        relay_url = settings.hosted_url(settings.managed_relay_url)  # an invalid LOCUS_API_BASE stops here
        host = urlsplit(relay_url).netloc or relay_url
        return f"Voyage AI through Locus's relay ({host})"
    if settings.embedder == "voyage":
        return f"Voyage AI, with your own key (model {settings.voyage_model})"
    if settings.embedder == "openai":
        return f"OpenAI, with your own key (model {settings.openai_model})"
    return "nowhere: embeddings are computed on this computer"


@dataclass
class Preflight:
    settings: Settings
    scan: Scan
    to_send: list[FileEntry]
    unchanged: int
    estimate: Estimate

    @property
    def sends_anything(self) -> bool:
        return needs_consent(self.settings) and bool(self.to_send)

    def render(self) -> str:
        s = self.settings
        hosted = needs_consent(s)
        lines = [f"Before indexing {self.scan.root}  (LOCUS_EMBEDDER={s.embedder})"]
        lines.append(f"Eligible files: {len(self.scan.files):,} ({human_bytes(self.scan.total_bytes)})")
        if self.unchanged:
            lines.append(f"  {self.unchanged:,} unchanged since the last index: skipped, nothing sent for them")
        send_bytes = sum(e.size for e in self.to_send)
        verb = "To embed and send" if hosted else "To embed"
        lines.append(f"{verb}: {len(self.to_send):,} file(s), {human_bytes(send_bytes)}")
        if self.to_send:
            by_ext: dict[str, list[int]] = {}
            for e in self.to_send:
                row = by_ext.setdefault(e.ext or "(none)", [0, 0])
                row[0] += 1
                row[1] += e.size
            top = sorted(by_ext.items(), key=lambda kv: -kv[1][1])
            listed = ", ".join(f"{ext} {n:,} ({human_bytes(b)})" for ext, (n, b) in top[:8])
            lines.append(f"  by type: {listed}{', …' if len(top) > 8 else ''}")
        lines.extend(exclusion_lines(self.scan))
        est = self.estimate
        lines.extend(pdf_lines(est))
        if self.to_send:
            lines.append(
                f"Estimated {'text to send' if hosted else 'text to embed'}: about {human_tokens(est.tokens)} tokens "
                f"(range {human_tokens(est.tokens_low)}–{human_tokens(est.tokens_high)}; ASSUMPTION ratios "
                f"v{est.ratios_version}; `locus estimate` shows the detail)"
            )
        lines.append(f"Where the text goes: {destination(s)}.")
        lines.append(data_flow_sentence(s))
        if hosted:
            lines.append("Your searches later send each query the same way.")
        lines.append(ALWAYS)
        if hosted:
            lines.append(
                "To embed on this computer instead, use LOCUS_EMBEDDER=local with a new LOCUS_COLLECTION "
                "(no embedding fees)."
            )
        return "\n".join(lines)


# The consent screen should not keep the user waiting as long as `locus estimate` may.
PREFLIGHT_SAMPLE_SECONDS = 15.0


def build_preflight(
    root: Path | str,
    settings: Settings,
    known_hash: Callable[[str], str | None] | None = None,
    sample_seconds: float = PREFLIGHT_SAMPLE_SECONDS,
) -> Preflight:
    """Scan *root*, split new/changed files from unchanged ones (via the store's stored hashes,
    when given) and estimate the tokens the changed ones would send."""
    result = scan(root, settings)
    to_send: list[FileEntry] = []
    unchanged = 0
    for entry in result.files:
        stored = known_hash(str(entry.path)) if known_hash is not None else None
        if stored is not None:
            try:
                if stored == file_sha256(entry.path):
                    unchanged += 1
                    continue
            except OSError:
                pass  # unreadable now: let the real run report it
        to_send.append(entry)
    est = estimate(to_send, settings, sample_seconds=sample_seconds)
    return Preflight(settings=settings, scan=result, to_send=to_send, unchanged=unchanged, estimate=est)


# confirm() outcomes
PROCEED = "proceed"
DECLINED = "declined"
NEEDS_YES = "needs_yes"


def confirm(
    pre: Preflight,
    *,
    assume_yes: bool,
    stdin: TextIO | None = None,
    out: TextIO | None = None,
    rerun: str | None = None,
) -> str:
    """Show the preflight and ask. Returns PROCEED, DECLINED or NEEDS_YES (no terminal, no --yes).
    Nothing is printed or asked when nothing would be sent. `rerun` is the command with --yes that
    the NEEDS_YES message suggests (default: `locus index <root> --yes`)."""
    stdin = stdin if stdin is not None else sys.stdin
    out = out if out is not None else sys.stderr
    if not pre.sends_anything:
        return PROCEED
    print(pre.render(), file=out)
    if assume_yes:
        print("Confirmed with --yes.", file=out)
        return PROCEED
    if not (hasattr(stdin, "isatty") and stdin.isatty()):
        example = rerun or f"locus index {pre.scan.root} --yes"
        print(
            f"\nNot sent: this is not an interactive terminal, so Locus cannot ask. To confirm, re-run "
            f"with --yes: `{example}`.",
            file=out,
        )
        return NEEDS_YES
    print(f"\nSend this text to {destination(pre.settings)}? [y/N] ", end="", file=out, flush=True)
    try:
        answer = stdin.readline()
    except KeyboardInterrupt:
        answer = ""
    if answer.strip().lower() in ("y", "yes"):
        return PROCEED
    print("Cancelled. Nothing was sent.", file=out)
    return DECLINED
