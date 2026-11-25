"""Walk a folder and keep the vector store in sync with it.

Per file: sha256 → skip if unchanged → parse → chunk → embed (per batch, with bounded retries) →
delete old chunks → upsert. Ids are `<sha1(path)[:16]>:<chunk_index>` so re-indexing a file is
idempotent.

RET-008: an embedding failure that would repeat for every remaining file (budget used up, key
rejected, subscription inactive, operator pause, a server asking for a long wait) stops the run
once, with one message and a resumable summary; a file that was never embedded keeps its old
chunks, and re-running picks up where the run stopped because unchanged files are skipped.

A rate limit or outage that outlasted its retries (and the halved re-sends in retry.py) may be one
file's own problem — a request the provider will never accept — or the service's. One short fixed
test request (retry.probe_service) tells them apart: if it goes through, that file is skipped and
reported as a per-file error and the run goes on; if it fails too, the run stops once. Always-
failing files therefore never block the rest of the folder, however many there are and wherever
they sit, on this run or any re-run, and --prune still runs (a failed file keeps its old chunks).
"""

from __future__ import annotations

import hashlib
import os
import stat
import sys
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC
from pathlib import Path
from typing import Any

from ..config import HARD_LINKS_ENV, SKIP_DIRS, Settings, feature_enabled
from ..policy import DENIED_SKIP_REASONS, Matcher, secret_content
from .chunk_policy import effective_chunk_tokens
from .chunker import Chunk, chunk_pages, chunk_text
from .embedder import Embedder
from .errors import BudgetExhausted, EmbedError, ProviderUnavailable, RateLimited, SubscriptionInactive, as_embed_error
from .noise import PARSER_VERSION, PARSER_VERSION_KEY, drop_base64_lines
from .parsers import parse_document, strip_parsed
from .retry import DEFAULT_RETRY, RetryPolicy, embed_with_retry, probe_service
from .store import VectorStore

Logger = Callable[[str], None]


def _stderr(msg: str) -> None:
    print(msg, file=sys.stderr)


@dataclass
class IndexStop:
    """Why a run ended early: the typed error, the file it happened on, the message shown once."""

    error: EmbedError
    path: str
    message: str

    @property
    def kind(self) -> str:
        return self.error.kind


@dataclass
class IndexStats:
    files_seen: int = 0
    files_indexed: int = 0
    files_skipped: int = 0
    files_removed: int = 0
    chunks: int = 0
    errors: list[str] = field(default_factory=list)
    retries: int = 0
    # PDFs: `pdf_no_text` had too little text per page (scanned) and were not indexed; `pdf_truncated`
    # hit the page or time limit and were indexed up to the pages read (parsers.py, LOCUS_PDF_*).
    pdf_no_text: int = 0
    pdf_truncated: int = 0
    # Files and folders the deny-list (policy.Matcher) refused; only counted when a matcher is passed.
    denied: int = 0
    # Set when the run stopped early (RET-008). `files_pending`: eligible files the run did not get
    # to process, the one it stopped on included (some may turn out unchanged); a re-run picks them up.
    stop: IndexStop | None = None
    files_pending: int = 0
    pending_paths: list[str] = field(default_factory=list)
    # Files left out of the index and why (a key of SKIPPED_LABELS) -> their paths, for the end-of-run report.
    skipped_files: dict[str, list[str]] = field(default_factory=dict)

    def note_skip(self, reason: str, path: Path | str) -> None:
        self.skipped_files.setdefault(reason, []).append(str(path))

    def summary(self) -> str:
        line = (
            f"seen={self.files_seen} indexed={self.files_indexed} skipped={self.files_skipped} "
            f"removed={self.files_removed} chunks={self.chunks} errors={len(self.errors)}"
        )
        if self.retries:
            line += f" retries={self.retries}"
        if self.pdf_no_text:
            line += f" pdf_no_text={self.pdf_no_text}"
        if self.pdf_truncated:
            line += f" pdf_truncated={self.pdf_truncated}"
        if self.denied:
            line += f" denied={self.denied}"
        if self.stop is not None:
            line += f" stopped={self.stop.kind} pending={self.files_pending}"
        return line


class NotRegularFile(OSError):
    """The path is a named pipe, a socket or a device, not a regular file."""


def not_indexable(st: os.stat_result) -> str | None:
    """Why a file with this stat result must not be opened for indexing (a SKIP_REASONS name), or None.

    Opening a named pipe for reading waits for a writer and never returns, so a FIFO in an indexed folder would hold
    `locus index`, the file watcher and the freshness step of every search for ever. Only regular files are opened."""
    if not stat.S_ISREG(st.st_mode):
        return "not_regular"
    if st.st_nlink > 1 and not feature_enabled(HARD_LINKS_ENV):
        # A hard link is the same file under a second name. Someone who can write into an indexed folder can link a
        # file from elsewhere on the same disk into it under a harmless name, and its text would become searchable.
        # No name-based rule can see that, so a file with more than one link is not indexed unless the user allows it.
        return "hard_link"
    return None


_WHY_NOT_INDEXED = {
    "not_regular": "is not a regular file (a named pipe, socket or device is never opened)",
    "hard_link": f"has more than one hard link, so its other name may be outside the folder (set {HARD_LINKS_ENV}=1)",
}


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    # O_NONBLOCK: opening a named pipe returns at once instead of waiting for a writer; fstat then refuses it, so the
    # file that is hashed is the one that was checked (no gap between a stat and an open).
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise NotRegularFile(f"{path} is not a regular file")
        fh = os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise
    with fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def path_id(path: Path) -> str:
    return hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:16]


def stop_message(
    err: EmbedError,
    *,
    root: Path | str,
    indexed: int,
    pending: int,
    resume: str | None = None,
    probed: bool = False,
) -> str:
    """The one user-facing message for a run that stopped early: what happened, what was kept,
    and how to continue. Worded from the typed error only; `resume` replaces the default
    "re-run `locus index <root>`" sentence (e.g. for `locus watch`, which retries by itself).
    `probed`: the stop followed a failed test request (retry.probe_service)."""
    head = str(err)  # typed messages already say which service refused and why
    parts = [f"Stopped early: {head.rstrip('.')}."]
    if probed:
        parts.append(
            "After this file's retries, a one-line test request failed too, so the problem is the service "
            "or the account, not this file."
        )
    if isinstance(err, RateLimited | ProviderUnavailable) and err.retry_after:
        parts.append(f"It asked to wait {err.retry_after:.0f} s before trying again.")
    upgrade_url = getattr(err, "upgrade_url", None)
    if isinstance(err, BudgetExhausted) and err.resets_at is not None:
        parts.append(f"The budget resets {err.resets_at.astimezone(UTC):%Y-%m-%d %H:%M} UTC.")
    if isinstance(err, BudgetExhausted | SubscriptionInactive) and upgrade_url and upgrade_url not in head:
        parts.append(f"Plan and billing: {upgrade_url}")
    parts.append(
        f"{indexed} file(s) indexed in this run; {pending} not processed yet. Nothing already indexed was removed."
    )
    parts.append(resume or f"To continue, re-run `locus index {root}` — unchanged files are skipped.")
    return " ".join(parts)


# Why a file read for indexing ended up with nothing to index (IndexStats.note_skip) -> the words the report uses.
SKIPPED_LABELS: dict[str, str] = {
    "html_no_text": "HTML with no text of its own (only images or scripts)",
    "no_text": "no readable text (empty, a scan, or content Locus cannot read)",
    "unreadable_encoding": "text in an encoding Locus could not read safely",
    "secret_content": "refused: the text looks like a credential",
    "secret_name": "refused: the file name looks like a credential",
    "only_base64": "nothing but base64 data",
}
SKIPPED_NAMES_SHOWN = 5
_HTML_SUFFIXES = frozenset({".html", ".htm", ".xhtml"})


def skipped_report(stats: IndexStats) -> list[str]:
    """One line per reason with the first few names, for the end of a run; empty when nothing was left out."""
    lines = []
    for reason, label in SKIPPED_LABELS.items():
        paths = stats.skipped_files.get(reason)
        if not paths:
            continue
        shown = ", ".join(paths[:SKIPPED_NAMES_SHOWN])
        more = f", and {len(paths) - SKIPPED_NAMES_SHOWN} more" if len(paths) > SKIPPED_NAMES_SHOWN else ""
        lines.append(f"not indexed, {label}: {len(paths)} file(s): {shown}{more}")
    return lines


# Why iter_files left something out; the `on_skip` hook receives one of these with the path.
SKIP_REASONS: tuple[str, ...] = (
    "skipped_dir",  # a SKIP_DIRS folder (.git, node_modules, ...): not walked at all
    "hidden_dir",  # any other folder whose name starts with "."
    "hidden_file",  # a file whose name starts with "." (.env, .DS_Store, ...)
    "symlink",
    "unsupported_type",  # extension not in LOCUS_EXTENSIONS
    "too_large",  # over LOCUS_MAX_FILE_MB
    "unreadable",  # stat() failed
    "unreadable_dir",  # a folder the OS would not list (macOS permission, ACL): its contents are not covered
    "not_regular",  # a named pipe, socket or device: opening one for reading can wait for ever, so it is never opened
    "hard_link",  # more than one hard link: the other name may sit outside the folder (LOCUS_ALLOW_HARD_LINKS)
    *DENIED_SKIP_REASONS,  # policy.py: denied_<rule>, only when a Matcher is passed to iter_files
)

SkipHook = Callable[[str, Path], None]


def iter_files(
    root: Path, settings: Settings, on_skip: SkipHook | None = None, policy: Matcher | None = None
) -> Iterator[Path]:
    """Yield indexable files under *root*, skipping noise directories and oversized files.

    The single definition of eligibility: `locus index`, the watcher's full passes, the
    pre-index estimate and the consent preflight all walk through here. `on_skip(reason, path)`
    (reason from SKIP_REASONS) lets the last two count exclusions without re-implementing them.
    A *policy* (policy.Matcher) is asked first about every folder and file and prunes what
    it denies, reporting `denied_<rule>`; without one behaviour is unchanged.
    """
    max_bytes = int(settings.max_file_mb * 1024 * 1024)
    root = Path(root).resolve()  # one spelling per folder: a symlinked root must not index the same file twice

    def skip(reason: str, path: Path) -> None:
        if on_skip is not None:
            on_skip(reason, path)

    def unreadable(err: OSError) -> None:
        # os.walk drops a folder it cannot list without a word; count it so a report can say "N folders not covered".
        skip("unreadable_dir", Path(err.filename or root))

    if policy is not None:
        verdict = policy.decide(root, is_dir=True)
        if not verdict.allowed:
            skip(f"denied_{verdict.reason}", root)
            return

    for dirpath, dirnames, filenames in os.walk(root, onerror=unreadable):
        kept: list[str] = []
        for d in sorted(dirnames):
            if policy is not None and not (verdict := policy.decide(Path(dirpath) / d, is_dir=True)).allowed:
                skip(f"denied_{verdict.reason}", Path(dirpath) / d)
            elif d in SKIP_DIRS:
                skip("skipped_dir", Path(dirpath) / d)
            elif d.startswith("."):
                skip("hidden_dir", Path(dirpath) / d)
            else:
                kept.append(d)
        dirnames[:] = kept
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if policy is not None and not (verdict := policy.decide(path)).allowed:
                skip(f"denied_{verdict.reason}", path)
                continue
            if name.startswith("."):
                skip("hidden_file", path)
                continue
            if path.is_symlink():
                # Never follow symlinks: a link inside the selected folder can point
                # anywhere on disk, which would index content outside what the user
                # chose (breaks the local-first / scoped-folder guarantee).
                skip("symlink", path)
                continue
            if path.suffix.lower() not in settings.extensions:
                skip("unsupported_type", path)
                continue
            try:
                st = path.stat()
            except OSError:
                skip("unreadable", path)
                continue
            if (why := not_indexable(st)) is not None:
                skip(why, path)
                continue
            if st.st_size > max_bytes:
                skip("too_large", path)
                continue
            yield path


def _recorded_state(store: VectorStore, path: str) -> tuple[str | None, float | None]:
    """(file_hash, mtime) the store recorded for *path*; a store without file_state gives no mtime."""
    file_state = getattr(store, "file_state", None)
    if file_state is not None:
        return file_state(path)
    return store.file_hash(path), None


def _parser_outdated(store: VectorStore, path: str) -> bool:
    """True when *path* was indexed by an older parser (rows with no stamp count as version 1), so an
    unchanged file is parsed once more. A store that cannot say is never outdated."""
    version = getattr(store, "parser_version", None)
    if version is None:
        return False
    return version(path) < PARSER_VERSION


# Chroma 1.5 gives back about one stored float in eight one unit in the last place off (measured
# 2026-10-02: 1790977746.7976983 is read back as ...6985), so an exact compare would rewrite the
# modified time of those unchanged files on every run. A real touch moves it by far more.
_MTIME_ROUND_TRIP = 1e-6


def _keep_mtime_current(store: VectorStore, path: Path, recorded: float | None) -> None:
    """An unchanged file whose modified time moved (touch, copy, git checkout): record the new time
    so the UX-004 freshness check (locus_search, locus_diagnose_connection) stops calling it
    changed. One metadata update, no re-embedding; only when the times differ."""
    refresh = getattr(store, "refresh_mtime", None)
    if recorded is None or refresh is None:
        return
    try:
        current = float(path.stat().st_mtime)
    except OSError:
        return
    if abs(current - recorded) > _MTIME_ROUND_TRIP:
        refresh(str(path), current)


def index_file(
    path: Path,
    root: Path,
    embedder: Embedder,
    store: VectorStore,
    settings: Settings,
    stats: IndexStats | None = None,
    log: Logger = _stderr,
    retry: RetryPolicy = DEFAULT_RETRY,
) -> int:
    """Index one file. Returns the number of chunks written (0 if skipped).

    Embedding failures surface as typed errors (errors.py) after `retry`'s bounded backoff; the
    file's previous chunks stay in the store until its new vectors are all in hand.
    """
    stats = stats or IndexStats()
    path = Path(path)
    try:
        why = not_indexable(path.stat())
    except OSError:
        why = None  # unreadable or gone: the open below reports it, as before
    if why is not None:
        stats.files_skipped += 1
        log(f"not indexed: {path} {_WHY_NOT_INDEXED[why]}")
        return 0
    digest = file_sha256(path)
    recorded_hash, recorded_mtime = _recorded_state(store, str(path))
    if recorded_hash == digest and not _parser_outdated(store, str(path)):
        _keep_mtime_current(store, path, recorded_mtime)
        stats.files_skipped += 1
        return 0

    parsed = parse_document(path, strip=False)  # the credential check below must see the file as it is
    text = parsed.text
    if parsed.no_text:
        stats.pdf_no_text += 1
        had_entries = store.file_hash(str(path)) is not None
        log(
            f"no extractable text in {path} (scanned or image-only PDF): not indexed"
            + ("; earlier index entries for it were removed" if had_entries else "")
        )
    if parsed.truncated:
        stats.pdf_truncated += 1
        log(
            f"{path}: PDF cut at the page or time limit; only the first {parsed.pages_read} of "
            f"{parsed.n_pages} pages are indexed"
        )
    if not text or not text.strip():
        store.delete_path(str(path))
        stats.files_skipped += 1
        if not parsed.no_text:  # a scanned PDF has its own count and message
            stats.note_skip("html_no_text" if path.suffix.lower() in _HTML_SUFFIXES else "no_text", path)
        return 0
    if secret_content(text):  # a credential whatever the name: never indexed, earlier entries removed
        store.delete_path(str(path))
        stats.files_skipped += 1
        stats.denied += 1
        stats.note_skip("secret_content", path)
        log(f"not indexed: {path} holds what looks like a credential (secret-content)")
        return 0
    parsed = strip_parsed(parsed)  # base64 payloads go only now: a JWT's long payload must not hide it from the check
    text = parsed.text
    if not text or not text.strip():
        store.delete_path(str(path))
        stats.files_skipped += 1
        stats.note_skip("only_base64", path)
        return 0

    chunk_tokens = effective_chunk_tokens(settings, store)
    if parsed.pages:
        chunks = chunk_pages(parsed.pages, chunk_tokens, settings.overlap_tokens)
    else:
        chunks = chunk_text(text, chunk_tokens, settings.overlap_tokens)
    # Base64 lines left after the parse step (a payload wrapped at 64 or 76 characters): only those lines go, the prose
    # around them stays; a chunk that was nothing but payload is dropped.
    kept = [(drop_base64_lines(c.text), c.page) for c in chunks]
    chunks = [Chunk(t, i, page) for i, (t, page) in enumerate(k for k in kept if k[0].strip())]
    if not chunks:
        store.delete_path(str(path))
        stats.files_skipped += 1
        stats.note_skip("only_base64", path)
        return 0

    def _count_retry() -> None:
        stats.retries += 1

    vectors = embed_with_retry(embedder, [c.text for c in chunks], retry, log, _count_retry)
    base = path_id(path)
    stat = path.stat()
    metadatas: list[dict[str, Any]] = [
        {
            "path": str(path),
            "name": path.name,
            "ext": path.suffix.lower(),
            "chunk_index": c.index,
            "n_chunks": len(chunks),
            "file_hash": digest,
            "mtime": float(stat.st_mtime),
            "root": str(root),
            PARSER_VERSION_KEY: PARSER_VERSION,
            **({"page": c.page} if c.page is not None else {}),  # Chroma stores no None
        }
        for c in chunks
    ]
    store.delete_path(str(path))
    store.upsert(
        ids=[f"{base}:{c.index}" for c in chunks],
        embeddings=vectors,
        documents=[c.text for c in chunks],
        metadatas=metadatas,
    )
    stats.files_indexed += 1
    stats.chunks += len(chunks)
    log(f"indexed {path} ({len(chunks)} chunks)")
    return len(chunks)


def index_folder(
    root: Path,
    embedder: Embedder,
    store: VectorStore,
    settings: Settings,
    prune: bool = False,
    log: Logger = _stderr,
    retry: RetryPolicy = DEFAULT_RETRY,
    resume: str | None = None,
    matcher: Matcher | None = None,
    on_file: Callable[[IndexStats], None] | None = None,
) -> IndexStats:
    """Index every eligible file under *root* (*on_file(stats)* is called after each: progress). `resume` is the
    stop message's how-to-continue sentence (default: re-run `locus index <root>`). `matcher` is the deny-list
    (policy.Matcher): files and folders it refuses are not indexed, and `prune` removes the ones it refuses now that
    an earlier run indexed."""
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(root)
    stats = IndexStats()
    upgrade = getattr(store, "ensure_dir_keys", None)  # W5: a collection from before folder-scope keys gets them once
    if upgrade is not None:
        upgrade()
    seen: set[str] = set()
    aborted = False
    too_large: list[Path] = []
    not_regular = 0
    hard_linked = 0
    unreadable: list[Path] = []  # folders/files the OS would not list or stat: whether they still exist is unknown
    denied_by_rule: Counter[str] = Counter()  # deny-list rule -> files and folders refused (counts only, no names)

    def on_skip(reason: str, p: Path) -> None:
        nonlocal not_regular, hard_linked
        if reason == "too_large":
            too_large.append(p)
        elif reason in ("unreadable_dir", "unreadable"):
            unreadable.append(p)
        elif reason == "not_regular":
            not_regular += 1
        elif reason == "hard_link":
            hard_linked += 1
        elif reason in DENIED_SKIP_REASONS:
            if reason == "denied_secret_file":
                stats.note_skip("secret_name", p)
            stats.denied += 1
            denied_by_rule[reason.removeprefix("denied_")] += 1

    files = iter_files(root, settings, on_skip=on_skip, policy=matcher)
    for path in files:
        stats.files_seen += 1
        seen.add(str(path))
        try:
            index_file(path, root, embedder, store, settings, stats, log, retry)
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the run
            typed = as_embed_error(exc)
            entry = f"{path}: {type(exc).__name__}: {exc}"
            if typed is not None and typed.stops_run:
                probed = retry.could_be_request_specific(typed)
                stop_error = probe_service(embedder, typed, retry) if probed else typed
                if stop_error is None:
                    # The service embedded a one-line test request, so this file's own request is
                    # what fails (over a per-request or per-minute token limit, content the
                    # provider refuses): skip it and go on, so such files never block the rest of
                    # the folder on any re-run. It keeps its old chunks, and --prune keeps them
                    # too (it is in `seen`).
                    stats.errors.append(entry)
                    log(f"ERROR {path}: {exc} — skipped this file (a test request went through); the run goes on")
                    continue
                # Not a per-file problem: every remaining file would fail the same way, so
                # continuing would repeat the same error once per file (seen in practice: a
                # 239-file vault produced 230 near-identical "no payment method" errors, twice
                # each). Stop once, send nothing more, and say how to resume. Counting what is
                # left only walks the tree; it embeds nothing.
                stats.pending_paths = [str(path), *(str(p) for p in files)]
                stats.files_pending = len(stats.pending_paths)
                message = stop_message(
                    stop_error,
                    root=root,
                    indexed=stats.files_indexed,
                    pending=stats.files_pending,
                    resume=resume,
                    probed=probed,
                )
                stats.stop = IndexStop(error=stop_error, path=str(path), message=message)
                log(f"ERROR {path}: {message}")
                aborted = True
                break
            stats.errors.append(entry)
            log(f"ERROR {path}: {exc}")
        if on_file is not None:
            on_file(stats)
    if prune and aborted:
        # `seen` only covers files visited before the abort, not the whole folder, so prune's
        # "not in seen" check would wrongly delete every not-yet-visited file's chunks.
        log("skipped --prune: the run stopped early, so it hasn't seen the whole folder yet")
    elif prune:
        prefix = str(root) + os.sep
        # A folder the walk could not read says nothing about what is in it: keep its index (F9).
        guarded = [str(u) for u in unreadable]
        kept = 0
        for indexed in store.list_paths():
            # Remove anything under root that iter_files would not currently index — a file
            # that vanished, but also one that's still on disk but no longer eligible (an
            # extension dropped from settings, now over the size cap, now a symlink, or now
            # refused by the deny-list when a matcher is given).
            # Checking Path(indexed).exists() here was the bug: it only caught the first
            # case, so e.g. removing .txt from LOCUS_EXTENSIONS left old .txt chunks in the
            # index forever even after --prune (found via a real repro, PR #1 2026-09-12).
            if indexed.startswith(prefix) and indexed not in seen:
                if any(indexed == g or indexed.startswith(g + os.sep) for g in guarded):
                    kept += 1
                    continue
                store.delete_path(indexed)
                stats.files_removed += 1
                log(f"removed {indexed}")
        if guarded:
            log(
                f"--prune kept the index of {kept} file(s) under {len(guarded)} folder(s) or file(s) it could not "
                "read (permission, a volume that went away); fix access and run --prune again"
            )
    if denied_by_rule:
        rules = ", ".join(f"{rule} {n}" for rule, n in sorted(denied_by_rule.items()))
        log(
            f"refused by the deny-list (keys, passwords, browser profiles, ~/Library, package caches): "
            f"{stats.denied} file(s) or folder(s), by rule: {rules}. They are not indexed; "
            "`--prune` removes any index entries an earlier run made for them."
        )
    if not_regular:
        log(f"not indexed: {not_regular} named pipe(s), socket(s) or device file(s): they are never opened")
    if hard_linked:
        log(
            f"not indexed: {hard_linked} file(s) with more than one hard link (the other name may sit outside this "
            f"folder); set {HARD_LINKS_ENV}=1 to index them"
        )
    for big in too_large:
        # A real run used to skip these without a word; only `--dry-run` counted them, without a name.
        log(
            f"not indexed: {big} is over the {settings.max_file_mb:g} MB file limit; "
            "set LOCUS_MAX_FILE_MB higher and run `locus index` again to include it"
        )
    for line in skipped_report(stats):
        log(line)
    log(stats.summary())
    return stats


def remove_path(path: Path, store: VectorStore, log: Logger = _stderr) -> None:
    store.delete_path(str(path))
    log(f"removed {path}")
