"""Command-line entry point: locus index | estimate | query | serve | watch | sync-notion | stats | doctor | setup."""

from __future__ import annotations

import argparse
import dataclasses
import math
import os
import shlex
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from .config import Settings, get_settings
from .ingest.collection import open_index
from .ingest.embedder import Embedder, input_type_status
from .ingest.indexer import IndexStats
from .ingest.store import ChromaStore

if TYPE_CHECKING:
    from .policy import Matcher


def _components(settings: Settings) -> tuple[Embedder, ChromaStore]:
    """Embedder + collection for a command. A legacy managed collection (RET-002) is announced on
    stderr once per command; it keeps working the way it was built."""
    embedder, store, warning = open_index(settings)
    if warning:
        print(f"locus: warning: {warning}", file=sys.stderr)
    return embedder, store


def _command(*words: str, flags: dict[str, bool]) -> str:
    """A copy-pasteable command: *words* (shell-quoted) plus each flag that is on, in order. The
    "how to continue" hints rebuild the user's own command this way, so a re-run keeps --prune and,
    for a run confirmed with --yes (scripts, agents), --yes (review F7)."""
    return " ".join([*(shlex.quote(w) for w in words), *(flag for flag, on in flags.items() if on)])


def cmd_index(args: argparse.Namespace) -> int:
    from .ingest.indexer import index_folder

    settings = get_settings()
    if args.roots is not None or args.policy is not None or args.folder is None:
        return _index_roots(args, settings)
    folder = Path(args.folder)
    if args.dry_run:
        return _dry_run(folder, settings)
    embedder, store = _components(settings)
    rerun = _command("locus", "index", str(folder), flags={"--prune": args.prune, "--yes": True})
    refused = _consent_gate(folder, settings, store, assume_yes=args.yes, rerun=rerun)
    if refused is not None:
        return refused
    resume = _command("locus", "index", str(folder), flags={"--prune": args.prune, "--yes": args.yes})
    stats = index_folder(
        folder,
        embedder,
        store,
        settings,
        prune=args.prune,
        resume=f"To continue, re-run `{resume}` — unchanged files are skipped.",
    )
    return _report(stats)


# Exit codes of commands that index: 0 done, 1 every attempted file failed or the user declined,
# 2 configuration error (cli.main) or no terminal to ask on and no --yes, 3 stopped early on a
# budget/billing/key/rate-limit stop — re-run to continue, 130 interrupted.
EXIT_DECLINED = 1
EXIT_NEEDS_YES = 2
EXIT_STOPPED = 3


def _consent_gate(folder: Path, settings: Settings, store: ChromaStore, *, assume_yes: bool, rerun: str) -> int | None:
    """UX-005: before a hosted embedder receives any chunk text, show what would be sent and where,
    and ask. None = go ahead; otherwise the exit code to return without indexing."""
    from .preflight import DECLINED, NEEDS_YES, build_preflight, confirm, needs_consent

    if not needs_consent(settings):
        return None
    pre = build_preflight(folder, settings, known_hash=store.file_hash)
    outcome = confirm(pre, assume_yes=assume_yes, rerun=rerun)
    if outcome == NEEDS_YES:
        return EXIT_NEEDS_YES
    if outcome == DECLINED:
        return EXIT_DECLINED
    return None


def _index_roots(args: argparse.Namespace, settings: Settings) -> int:
    """`locus index --roots A B --dry-run` (ADR-092): counts for several roots under the policy file.
    Real multi-root indexing is not enabled yet; it follows once the founder has read a report."""
    from .policy import Matcher, PolicyError, load_policy
    from .rootscan import render_roots_report, scan_roots

    if args.prune and not args.dry_run:
        print(
            "locus: --prune is not supported with --roots yet (a staged run has not seen every file).", file=sys.stderr
        )
        return EXIT_NEEDS_YES
    policy_file = Path(args.policy) if args.policy else settings.data_dir / "policy.toml"
    try:
        policy = load_policy(policy_file)
    except (PolicyError, OSError) as err:
        print(f"locus: policy file problem: {err}", file=sys.stderr)
        return EXIT_NEEDS_YES
    home = Path.home().resolve()
    given = list(args.roots or ([args.folder] if args.folder else policy.roots))
    if not given:
        print("locus: no root: pass --roots, a folder, or set roots in the policy file.", file=sys.stderr)
        return EXIT_NEEDS_YES
    roots = [Path(r).expanduser().resolve() for r in given]
    bad = [r for r in roots if not r.is_dir()]
    if bad:
        print(f"locus: not a folder: {bad[0]}", file=sys.stderr)
        return EXIT_NEEDS_YES
    matcher = Matcher(dataclasses.replace(policy, roots=tuple(str(r) for r in roots)), home)
    if args.dry_run:
        print(render_roots_report(scan_roots(roots, settings, matcher)))
        return 0
    return _index_roots_for_real(args, settings, roots, matcher)


def _index_roots_for_real(args: argparse.Namespace, settings: Settings, roots: list[Path], matcher: Matcher) -> int:
    """Staged, deduplicated multi-root index (W1). Local embedder only: a hosted one would send the
    chunks of a whole home directory, which the per-folder consent gate does not cover."""
    from .ingest.multiroot import Stage, index_roots, parse_kinds, render_report
    from .preflight import needs_consent

    if needs_consent(settings):
        print(
            "locus: --roots with a hosted embedder is not supported (it would send chunk text off this "
            "computer); set LOCUS_EMBEDDER=local.",
            file=sys.stderr,
        )
        return EXIT_NEEDS_YES
    if _needs_scope(args, roots, matcher):
        return _ask_for_scope(settings, roots, matcher)
    stage = Stage(
        kinds=parse_kinds(args.kinds) or None,
        skip_kinds=parse_kinds(args.skip_kinds),
        max_files=args.max_files,
        max_mb=args.max_mb,
        order=args.order,
        since_days=args.since,
        budget_seconds=args.budget_seconds,
    )
    embedder, store = _components(settings)
    report = index_roots(roots, embedder, store, settings, matcher, stage)
    print(render_report(report))
    for err in report.stats.errors:
        print(f"  ! {err}")
    if report.stats.stop is not None:
        return EXIT_STOPPED
    return 1 if report.stats.errors and report.stats.files_indexed == 0 else 0


def _needs_scope(args: argparse.Namespace, roots: list[Path], matcher: Matcher) -> bool:
    """A whole home with no scope at all (types, window, file count or time budget) takes hours; --all opts in."""

    def real(x) -> bool:  # `--since inf` or `nan` limits nothing
        return x is not None and math.isfinite(x)

    scoped = args.kinds or real(args.since) or args.max_files is not None or real(args.budget_seconds)
    covers_home = any(r == matcher.home or r in matcher.home.parents for r in roots)  # the home, or / and /Users
    return not scoped and not args.all and covers_home


def _ask_for_scope(settings: Settings, roots: list[Path], matcher: Matcher) -> int:
    from .rootscan import render_roots_report, scan_roots

    print(render_roots_report(scan_roots(roots, settings, matcher)))
    print(
        "\nlocus: indexing the whole home with no limit can take hours. Pass a scope: --budget-seconds N "
        "--order recent (newest first within a time budget), --since DAYS, --kinds, or --max-files; "
        "or --all to index everything.",
        file=sys.stderr,
    )
    return EXIT_NEEDS_YES


def _dry_run(folder: Path, settings: Settings) -> int:
    """`locus index --dry-run`: the preflight report for any embedder, without building the embedder
    (a local model load could download it) and without embedding or sending anything."""
    from .ingest.chunk_policy import settings_for_preview
    from .ingest.embedder import expected_embedder_name
    from .preflight import build_preflight

    known_hash = None
    store = None
    if (settings.data_dir / "chroma").exists():
        # Read-only: an existing collection is checked against the embedder and used to count
        # unchanged files; a missing one is not created (that would stamp this embedder's name on it).
        try:
            store = ChromaStore(settings.data_dir, settings.collection, expected_embedder_name(settings), create=False)
        except LookupError:
            store = None
        known_hash = store.file_hash if store is not None else None
    settings, chunk_line = settings_for_preview(settings, store)
    pre = build_preflight(folder, settings, known_hash=known_hash)
    print(pre.render())
    print(f"\n{chunk_line}")
    print("\nDry run: nothing was embedded or sent.")
    return 0


def _report(stats: IndexStats) -> int:
    """Print the run summary and per-file errors. A stop's own message was already logged once
    (stderr) by the indexer; it is not repeated here."""
    print(stats.summary())
    for err in stats.errors:
        print(f"  ! {err}")
    if stats.stop is not None:
        return EXIT_STOPPED
    return 1 if stats.errors and stats.files_indexed == 0 else 0


def _existing_store(settings: Settings) -> ChromaStore | None:
    """The configured collection opened read-only, or None when there is none (or it cannot be opened): a preview
    must not create a collection or fail because of one."""
    from .ingest.embedder import expected_embedder_name

    if not (settings.data_dir / "chroma").exists():
        return None
    try:
        return ChromaStore(settings.data_dir, settings.collection, expected_embedder_name(settings), create=False)
    except Exception:  # noqa: BLE001 - missing, another embedder's, unreadable: the preview falls back to defaults
        return None


def cmd_estimate(args: argparse.Namespace) -> int:
    """PROD-012: files, bytes by type and estimated tokens for a folder. Builds no embedder and
    opens no connection (the local embedder alone would download a model)."""
    import json

    from .ingest.chunk_policy import settings_for_preview
    from .ingest.estimator import estimate, render_estimate, scan

    settings = get_settings()
    settings, chunk_line = settings_for_preview(settings, _existing_store(settings))
    result = scan(Path(args.folder), settings)
    est = estimate(result.files, settings, sample_seconds=args.sample_seconds)
    if args.json:
        payload = {
            "root": str(result.root),
            "excluded": dict(result.excluded),
            "chunk_tokens": settings.chunk_tokens,
            **est.to_dict(),
        }
        print(json.dumps(payload, indent=2))
    else:
        print(render_estimate(result, est, settings))
        print(f"\n{chunk_line}")
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    settings = get_settings()
    embedder, store = _components(settings)
    hits = store.query(embedder.embed_query(args.query), k=args.k, path_prefix=args.prefix)
    if not hits:
        print("no results (is anything indexed? try `locus stats`)")
        return 1
    for i, hit in enumerate(hits, 1):
        snippet = hit.text.replace("\n", " ")
        snippet = snippet if len(snippet) <= args.width else snippet[: args.width] + "…"
        print(f"{i}. [{hit.score:.3f}] {hit.path} #{hit.chunk_index}\n   {snippet}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .mcp_server import main as serve

    if args.transport == "relay":
        # The relay worker calls Locus's hosted token endpoint: an invalid LOCUS_API_BASE is a
        # configuration error here (exit 2), while stdio / streamable-http never need it.
        settings = get_settings()
        settings.hosted_url(settings.mcp_relay_token_url)
    serve(args.transport, host=args.host, port=args.port, path=args.path)
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    from .ingest.indexer import index_folder
    from .ingest.watcher import PendingQueue, watch_folder

    settings = get_settings()
    embedder, store = _components(settings)
    root = Path(args.folder)
    rerun = _command("locus", "watch", str(root), flags={"--no-initial": args.no_initial, "--yes": True})
    refused = _consent_gate(root, settings, store, assume_yes=args.yes, rerun=rerun)
    if refused is not None:
        return refused
    pending = PendingQueue()
    if not args.no_initial:
        stats = index_folder(
            root,
            embedder,
            store,
            settings,
            prune=True,
            resume="The watcher keeps running, queues them and retries when the pause ends.",
        )
        if stats.stop is not None:
            # RET-008: the files the initial pass did not reach wait in the watcher's queue and are
            # retried when the pause ends, instead of waiting for their next edit.
            pending.hold(stats.pending_paths, stats.stop.error)
    watch_folder(root, embedder, store, settings, pending=pending)
    return 0


def cmd_sync_notion(args: argparse.Namespace) -> int:
    from .ingest.indexer import index_folder
    from .ingest.notion_source import sync_notion

    settings = get_settings()
    sync_stats = sync_notion(settings)
    print(f"notion sync: {sync_stats.summary()}")

    notion_folder = settings.data_dir / "sources" / "notion"
    embedder, store = _components(settings)
    refused = _consent_gate(notion_folder, settings, store, assume_yes=args.yes, rerun="locus sync-notion --yes")
    if refused is not None:
        return refused
    stats = index_folder(
        notion_folder,
        embedder,
        store,
        settings,
        prune=True,
        resume=f"To continue, re-run `{_command('locus', 'sync-notion', flags={'--yes': args.yes})}` — "
        "unchanged pages are skipped.",
    )
    return _report(stats)


def cmd_stats(_: argparse.Namespace) -> int:
    settings = get_settings()
    embedder, store = _components(settings)
    paths = store.list_paths()
    print(f"data_dir   : {settings.data_dir}")
    print(f"collection : {settings.collection}")
    print(f"embedder   : {embedder.name}")
    if hasattr(embedder, "_ping"):  # the shared helper (ADR-089 draft): say whether it is running, never start it here
        info = embedder._ping()
        state = f"running (pid {info['pid']})" if info else "not running (starts on the first search)"
        print(f"helper     : shared, {state}")
    print(f"input_type : {input_type_status(embedder)}")
    print(f"files      : {len(paths)}")
    print(f"chunks     : {store.count()}")
    return 0


# locus doctor's install hints: uv first, then pipx (README.md installs from PyPI that way); the
# source-checkout form comes last, for contributors.
_REINSTALL_HINT = (
    "reinstall with the tool you installed Locus with: "
    'uv tool install --reinstall --python 3.12 "locusfiles[local]" (keep the extras you installed, '
    'e.g. "locusfiles[relay]"), or pipx reinstall locusfiles. '
    "From a source checkout: pip install -e '.[dev]'"
)
_LOCAL_EXTRA_HINT = (
    "install the local extra: "
    'uv tool install --reinstall --python 3.12 "locusfiles[local]", or '
    'pipx install --force "locusfiles[local]". '
    "From a source checkout: pip install -e '.[dev,local]'"
)


def cmd_doctor(_: argparse.Namespace) -> int:
    """Diagnose the most common setup failures before they turn into a support message."""
    ok = True

    def check(label: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and passed
        mark = "OK  " if passed else "FAIL"
        print(f"[{mark}] {label}" + (f" — {detail}" if detail else ""))

    check("Python interpreter", sys.version_info >= (3, 11), sys.executable)
    for mod in ("chromadb", "mcp", "pydantic", "dotenv", "watchdog", "pypdfium2", "docx"):
        try:
            __import__(mod)
            check(f"dependency: {mod}", True)
        except ImportError as exc:
            check(f"dependency: {mod}", False, f"not importable ({exc}) — {_REINSTALL_HINT}")

    try:
        settings = get_settings()
    except Exception as exc:  # noqa: BLE001 - config parsing itself is part of what we diagnose
        check("settings load from .env / environment", False, f"{type(exc).__name__}: {exc}")
        return 1
    check("settings load from .env / environment", True, f"embedder={settings.embedder}")
    if settings.api_base_error:
        # Local commands keep working, but managed embedding and the MCP relay would refuse to start.
        check("LOCUS_API_BASE", False, settings.api_base_error)

    if settings.embedder == "voyage":
        check("VOYAGE_API_KEY set", bool(_os_env("VOYAGE_API_KEY")))
    elif settings.embedder == "openai":
        check("OPENAI_API_KEY set", bool(_os_env("OPENAI_API_KEY")))
    elif settings.embedder == "local":
        from .ingest.embedder import local_backend

        try:
            backend, _ = local_backend(settings.local_model)
        except ModuleNotFoundError:  # LOCUS_LOCAL_BACKEND=onnx without fastembed
            backend = "onnx-missing"
        except RuntimeError as exc:
            check("dependency: on-device embedding stack (LOCUS_EMBEDDER=local)", False, str(exc))
            backend = None
        if backend == "onnx":
            check("dependency: fastembed (on-device ONNX; LOCUS_EMBEDDER=local)", True)
        elif backend == "onnx-missing":
            check("dependency: fastembed (LOCUS_LOCAL_BACKEND=onnx)", False, _LOCAL_EXTRA_HINT)
        elif backend == "torch":
            try:
                import sentence_transformers  # noqa: F401

                check("dependency: sentence-transformers (LOCUS_EMBEDDER=local)", True)
            except ImportError:
                check(
                    "dependency: fastembed or sentence-transformers (LOCUS_EMBEDDER=local)",
                    False,
                    _LOCAL_EXTRA_HINT,
                )
    elif settings.embedder == "managed":
        check(
            "LOCUS_API_KEY set (LOCUS_EMBEDDER=managed)",
            bool(settings.managed_api_key),
            f"relay: {settings.managed_relay_url}",
        )
    elif settings.embedder != "hash":
        check("LOCUS_EMBEDDER value", False, f"{settings.embedder!r} is not voyage/openai/local/managed/hash")

    try:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(settings.data_dir, 0o700)  # see store.py's ChromaStore.__init__ for why
        probe = settings.data_dir / ".doctor_probe"
        probe.write_text("ok")
        probe.unlink()
        check("data directory is writable", True, str(settings.data_dir))
    except OSError as exc:
        check("data directory is writable", False, f"{settings.data_dir}: {exc}")

    if ok:
        legacy: str | None = None
        try:
            embedder, store, legacy = open_index(settings)
            _ = embedder.embed_query("doctor check")
            store.count()
            check("embedder + store round-trip", True)
        except Exception as exc:  # noqa: BLE001 - report, don't crash the diagnostic
            check("embedder + store round-trip", False, f"{type(exc).__name__}: {exc}")
        if legacy:
            # RET-002: not a failure (search works as it always did), but worth acting on.
            print(f"[WARN] collection input_type policy — {legacy}")

    if ok:
        print("\nAll checks passed — ready for `locus index <folder>`.")
    else:
        print("\nFix the FAIL lines above, then re-run `locus doctor`.")
    return 0 if ok else 1


def cmd_setup(args: argparse.Namespace) -> int:
    """`locus setup --managed | --local`: the embedder in one step (configure.py says how)."""
    from .configure import run_setup

    mode = "managed" if args.managed else "local"
    return run_setup(mode, collection=args.collection, new_key=args.new_key, skip_check=args.skip_check).code


def _os_env(name: str) -> str:
    import os

    return os.getenv(name, "")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="locus", description="Local-first personal AI brain over MCP.")
    sub = parser.add_subparsers(dest="command", required=True)

    yes_help = (
        "confirm sending chunk text to the hosted embedder (voyage/openai/managed) without asking; "
        "required when not run from a terminal"
    )
    p = sub.add_parser(
        "index",
        help="index (or re-index) a folder",
        description="Index a folder. With a hosted embedder it first shows what would be sent and where, "
        "and asks. Exit codes: 0 done, 1 all files failed or declined, 2 configuration error or no "
        "--yes without a terminal, 3 stopped early on a budget/billing/key/rate limit (re-run to continue).",
    )
    p.add_argument("folder", nargs="?")
    p.add_argument(
        "--roots",
        nargs="+",
        metavar="ROOT",
        help="several roots under the policy file's deny-list and profiles (ADR-092); staged, deduplicated, resumable",
    )
    p.add_argument("--kinds", metavar="EXT,EXT", help="with --roots: only these file types, e.g. md,txt,docx")
    p.add_argument("--skip-kinds", metavar="EXT,EXT", help="with --roots: leave these file types for a later run")
    p.add_argument("--max-files", type=int, metavar="N", help="with --roots: embed at most N new files this run")
    p.add_argument("--max-mb", type=float, metavar="MB", help="with --roots: stop embedding after about MB megabytes")
    p.add_argument(
        "--order",
        choices=("walk", "recent"),
        default="walk",
        help="with --roots: walk = folder order (default); recent = newest modified first across all roots",
    )
    p.add_argument(
        "--since", type=float, metavar="DAYS", help="with --roots: only files modified in the last DAYS days"
    )
    p.add_argument(
        "--budget-seconds",
        type=float,
        metavar="N",
        help="with --roots: stop starting new files after N seconds (finishes the current one); "
        "use with --order recent for a time-boxed hot-set pass",
    )
    p.add_argument("--all", action="store_true", help="with --roots on the whole home: index everything, no scope")
    p.add_argument("--policy", metavar="FILE", help="policy file (default: <data dir>/policy.toml)")
    p.add_argument("--prune", action="store_true", help="remove files that no longer exist")
    p.add_argument("-y", "--yes", action="store_true", help=yes_help)
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="only show what would be indexed and sent (files, types, exclusions, token estimate); send nothing",
    )
    p.set_defaults(func=cmd_index)

    p = sub.add_parser(
        "estimate",
        help="estimate files, size and embedding tokens for a folder before indexing (sends nothing)",
    )
    p.add_argument("folder")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument(
        "--sample-seconds",
        type=float,
        default=60.0,
        help="time limit for parsing a sample of files to measure them (default 60)",
    )
    p.set_defaults(func=cmd_estimate)

    p = sub.add_parser("query", help="semantic search from the terminal")
    p.add_argument("query")
    p.add_argument("-k", type=int, default=5)
    p.add_argument("--prefix", default=None, help="restrict to an absolute path prefix")
    p.add_argument("--width", type=int, default=200, help="snippet width")
    p.set_defaults(func=cmd_query)

    p = sub.add_parser(
        "serve",
        help="run the MCP server on stdio (or streamable-http for a tunnel, or relay for Claude.ai web/ChatGPT)",
    )
    p.add_argument(
        "--transport",
        choices=["stdio", "streamable-http", "relay"],
        default="stdio",
        help="stdio (default, for local MCP clients like Claude Desktop), streamable-http "
        "(for a user-run tunnel to Claude.ai web / ChatGPT — requires LOCUS_MCP_TOKEN in .env), "
        "or relay (Locus-hosted, outbound-only, for Claude.ai web / ChatGPT — requires "
        "LOCUS_API_KEY and the `relay` extra; one computer per account serves at a time and the newest "
        "one takes over. Relay exit codes: 1 = channel lost or token not refreshed (safe to restart), "
        "2 = key rejected/revoked, subscription inactive or channel refused at start, 3 = superseded by "
        "another computer — do "
        "not auto-restart on 3)",
    )
    p.add_argument("--host", default="127.0.0.1", help="bind host for streamable-http (default: localhost only)")
    p.add_argument("--port", type=int, default=8000, help="bind port for streamable-http")
    p.add_argument("--path", default="/mcp", help="URL path for streamable-http")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("watch", help="index a folder, then keep it in sync")
    p.add_argument("folder")
    p.add_argument("--no-initial", action="store_true", help="skip the initial full index")
    p.add_argument("-y", "--yes", action="store_true", help=yes_help)
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser(
        "sync-notion",
        help="OAuth-authenticate with Notion (first run only), pull page content, and index it",
    )
    p.add_argument("-y", "--yes", action="store_true", help=yes_help)
    p.set_defaults(func=cmd_sync_notion)

    p = sub.add_parser("stats", help="show index statistics")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("doctor", help="diagnose common setup problems")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser(
        "setup",
        help="choose the embedder in one step: --managed (a Locus plan) or --local (on this computer)",
        description="Write LOCUS_EMBEDDER (and, for --managed, LOCUS_API_KEY) into the .env file Locus reads "
        "from this folder, owner-only, keeping every other line. --managed takes LOCUS_API_KEY if it is "
        "already set, else asks for it without showing it, and checks it with Locus's account-status "
        "endpoint (no embedding call, nothing spent). An index built with another embedder is never "
        "switched silently: setup stops and asks for a new collection. Exit codes: 0 saved, 1 key not "
        "accepted or not checked (nothing saved), 2 configuration problem (nothing saved).",
    )
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--managed", action="store_true", help="managed embeddings with your Locus plan's API key")
    mode.add_argument("--local", action="store_true", help="on-device embeddings, no account and no key")
    p.add_argument(
        "--collection",
        default=None,
        help="index into this collection and save it as LOCUS_COLLECTION (needed when the current index was "
        "built with another embedder)",
    )
    p.add_argument(
        "--new-key",
        action="store_true",
        help="with --managed: ask for a key even if LOCUS_API_KEY is already set (to replace a revoked one)",
    )
    p.add_argument(
        "--skip-check",
        action="store_true",
        help="with --managed: save the key without checking it with Locus (when you are offline)",
    )
    p.set_defaults(func=cmd_setup)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        sys.exit(args.func(args))
    except RuntimeError as exc:  # configuration errors (missing key, embedder mismatch)
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)
    except (NotADirectoryError, FileNotFoundError) as exc:
        print(f"error: no such folder: {exc}", file=sys.stderr)
        sys.exit(2)
    except OSError as exc:  # permission errors, disk issues, etc. — no raw traceback
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:  # Ctrl-C: stop cleanly; a file is only replaced once fully embedded
        print("\ninterrupted — anything already indexed is kept; re-run to continue.", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
