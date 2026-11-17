"""Command-line entry point.

locus index | estimate | query | serve | watch | sync-notion | stats | doctor | setup | license
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import os
import shlex
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from . import closed_folders, killswitch, restrictions
from .config import Settings, get_settings, require_hosted, require_notion
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
    closed_folders.mark_if_new(settings, store)
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
    rerun = _command("locus", "index", str(folder), flags={"--prune": args.prune, "--yes": True})
    matcher = _deny_list_for(folder, settings, ask=None if args.dry_run else (args.yes, rerun))
    if isinstance(matcher, int):
        return matcher
    if args.dry_run:
        return _dry_run(folder, settings, matcher)
    embedder, store = _components(settings)
    refused = _consent_gate(folder, settings, store, assume_yes=args.yes, rerun=rerun, matcher=matcher)
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
        matcher=matcher,
    )
    code = _report(stats)
    _print_closed_hint(folder, settings, matcher)
    return code


def _deny_list_for(folder: Path, settings: Settings, ask: tuple[bool, str] | None = None) -> Matcher | int:
    """The deny-list (policy.py) for indexing *folder*, with the user's `<data dir>/policy.toml` rules added: the
    same rules `--roots` applies, so a plain `index`, `watch` and `--prune` refuse the same keys, passwords and
    browser profiles. Or the exit code to return when the policy file is unreadable: a typo in a restriction must not
    fail open. `ask` = (--yes given, the user's command): *folder* is one the user named, so when it is a closed
    folder (Downloads) they are asked once whether to open it (closed_folders.py)."""
    from .policy import PolicyError, load_policy, matcher_for_folder
    from .preflight import NEEDS_YES, PROCEED

    home = Path.home().resolve()
    policy_file = settings.data_dir / "policy.toml"
    try:
        policy = load_policy(policy_file)
        if ask is not None:
            outcome = closed_folders.gate(
                folder, settings, policy, policy_file, home, assume_yes=ask[0], command=ask[1]
            )
            if outcome != PROCEED:
                return EXIT_NEEDS_YES if outcome == NEEDS_YES else EXIT_DECLINED
            policy = load_policy(policy_file)  # the gate may have opened the folder
    except (PolicyError, OSError) as err:
        print(f"locus: policy file problem: {err}", file=sys.stderr)
        return EXIT_NEEDS_YES
    return matcher_for_folder(folder, policy, home)


def _print_closed_hint(root: Path, settings: Settings, matcher: Matcher) -> None:
    """After an index or a dry run: say how many files a closed folder inside *root* left out, and how to open it."""
    try:
        lines = closed_folders.hint_lines(root, settings, matcher.policy, matcher.home)
    except Exception:  # noqa: BLE001 - a hint must never turn a finished run into a failure
        return
    for line in lines:
        print(line)


# Exit codes of commands that index: 0 done, 1 every attempted file failed or the user declined,
# 2 configuration error (cli.main) or no terminal to ask on and no --yes, 3 stopped early on a
# budget/billing/key/rate-limit stop — re-run to continue, 130 interrupted.
EXIT_DECLINED = 1
EXIT_NEEDS_YES = 2
EXIT_STOPPED = 3


def _consent_gate(
    folder: Path,
    settings: Settings,
    store: ChromaStore,
    *,
    assume_yes: bool,
    rerun: str,
    matcher: Matcher | None = None,
) -> int | None:
    """UX-005: before a hosted embedder receives any chunk text, show what would be sent and where,
    and ask. None = go ahead; otherwise the exit code to return without indexing. *matcher* (the deny-list)
    keeps refused files out of what the screen says would be sent."""
    from .preflight import DECLINED, NEEDS_YES, build_preflight, confirm, needs_consent

    if not needs_consent(settings):
        return None
    pre = build_preflight(folder, settings, known_hash=store.file_hash, matcher=matcher)
    outcome = confirm(pre, assume_yes=assume_yes, rerun=rerun)
    if outcome == NEEDS_YES:
        return EXIT_NEEDS_YES
    if outcome == DECLINED:
        return EXIT_DECLINED
    return None


def _index_roots(args: argparse.Namespace, settings: Settings) -> int:
    """`locus index --roots A B --dry-run`: counts for several roots under the policy file.
    Real multi-root indexing is not enabled yet; it follows once the dry-run report has been reviewed."""
    from .policy import Matcher, PolicyError, load_policy
    from .preflight import NEEDS_YES, PROCEED
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
    if not args.dry_run and (args.roots or args.folder):  # roots the user typed: a closed one among them is asked about
        rerun = "locus index --roots " + " ".join(shlex.quote(str(r)) for r in roots) + " --yes"
        try:
            for root in roots:
                outcome = closed_folders.gate(
                    root, settings, policy, policy_file, home, assume_yes=args.yes, command=rerun
                )
                if outcome != PROCEED:
                    return EXIT_NEEDS_YES if outcome == NEEDS_YES else EXIT_DECLINED
            policy = load_policy(policy_file)
        except (PolicyError, OSError) as err:
            print(f"locus: policy file problem: {err}", file=sys.stderr)
            return EXIT_NEEDS_YES
    matcher = Matcher(dataclasses.replace(policy, roots=tuple(str(r) for r in roots)), home)
    if args.dry_run:
        print(render_roots_report(scan_roots(roots, settings, matcher)))
        for root in roots:
            _print_closed_hint(root, settings, matcher)
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
    for root in roots:
        _print_closed_hint(root, settings, matcher)
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


def _dry_run(folder: Path, settings: Settings, matcher: Matcher | None = None) -> int:
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
    pre = build_preflight(folder, settings, known_hash=known_hash, matcher=matcher)
    print(pre.render())
    print(f"\n{chunk_line}")
    if matcher is not None:
        _print_closed_hint(folder, settings, matcher)
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
    matcher = _deny_list_for(Path(args.folder), settings)
    if isinstance(matcher, int):
        return matcher
    settings, chunk_line = settings_for_preview(settings, _existing_store(settings))
    result = scan(Path(args.folder), settings, matcher)
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
    if args.json:
        return _run_service(lambda engine: engine.search(args.query, k=args.k, path_prefix=args.prefix), as_json=True)
    settings = get_settings()
    embedder, store = _components(settings)
    try:  # the folder rules apply here too (restrictions.py), as they do to `query --json` and the MCP tools
        guard = restrictions.open_gate(settings.data_dir)
    except restrictions.Refused as refused:
        print(f"error: {refused.text}", file=sys.stderr)
        return EXIT_REFUSED
    vector = embedder.embed_query(args.query)
    hits, _ = restrictions.visible_hits(
        guard, lambda n: store.query(vector, k=n, path_prefix=args.prefix), args.k
    )
    if not hits:
        print("no results (is anything indexed? try `locus stats`)")
        return 1
    for i, hit in enumerate(hits, 1):
        snippet = hit.text.replace("\n", " ")
        snippet = snippet if len(snippet) <= args.width else snippet[: args.width] + "…"
        print(f"{i}. [{hit.score:.3f}] {hit.path} #{hit.chunk_index}\n   {snippet}")
    return 0


# Exit codes of the commands a program calls: `query --json`, `sources`, `read`, and `locus api` at start-up. Each
# service error code (service.ERROR_CODES) has one; tests/test_cli_json.py checks that none is missing.
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_NOT_CONFIGURED = 3
EXIT_REFUSED = 4
EXIT_EMBEDDER = 5
SERVICE_EXIT_CODES = {
    "invalid_request": EXIT_USAGE,
    "not_configured": EXIT_NOT_CONFIGURED,
    "no_index": EXIT_NOT_CONFIGURED,
    "not_indexed": EXIT_REFUSED,
    "confinement_refused": EXIT_REFUSED,
    "file_unreadable": EXIT_ERROR,
    "embedder_error": EXIT_EMBEDDER,
    "embedder_unavailable": EXIT_EMBEDDER,
    "index_error": EXIT_ERROR,
    "paused": EXIT_REFUSED,
    "policy_error": EXIT_NOT_CONFIGURED,
    "internal_error": EXIT_ERROR,
}
EXIT_CODES_EPILOG = """\
exit codes (for `query --json`, `sources`, `read` and `api`):
  0  ok
  1  unexpected error: a bug, an index that cannot be read, a file that can no longer be read
  2  usage error: a bad argument or parameter
  3  not configured (embedder, key, collection), nothing is indexed yet, or the folder rules cannot be read
  4  refused: the path is not in the index, or a folder rule closes it (the deny-list, an excluded or closed folder), or
     the confinement check failed (the file is now a link or outside the indexed files), or Locus is paused
  5  embedder error: the query could not be embedded, or the on-device model could not be loaded
JSON goes to stdout only; logs and warnings go to stderr. `query` without --json keeps its older
behaviour (exit 1 when nothing matched)."""


def _emit_json(payload: dict) -> None:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:  # Hebrew and other non-ASCII text must not depend on the terminal's code page
        reconfigure(encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _run_service(call, *, as_json: bool, render=None) -> int:
    """Run an engine operation (service.Engine) and print its answer: JSON on stdout with --json, else `render(result)`.
    A failure is a JSON error object on stdout with --json, always one line on stderr, and the exit code above."""
    from .service import Engine, ServiceError

    try:
        result = call(Engine())
    except ServiceError as exc:
        error = exc
    except Exception as exc:  # noqa: BLE001 - a program is waiting for an answer: say what class of failure it was
        error = ServiceError("internal_error", f"Unexpected error ({type(exc).__name__}).")
    else:
        if as_json:
            _emit_json(result.to_dict())
        elif render is not None:
            render(result)
        return EXIT_OK
    if as_json:
        _emit_json(error.to_dict())
    print(f"error: {error.message}", file=sys.stderr)
    return SERVICE_EXIT_CODES[error.code]


def cmd_sources(args: argparse.Namespace) -> int:
    def render(result) -> None:
        print("\n".join(result.paths))
        more = result.total - len(result.paths)
        print(f"total: {result.total} file(s) in scope ({result.indexed_total} in the whole index)", file=sys.stderr)
        if more > 0:
            print(f"... {more} more (raise --limit, up to 1000, or narrow --path-prefix)", file=sys.stderr)
        if result.note:
            print(result.note, file=sys.stderr)

    return _run_service(
        lambda engine: engine.list_sources(args.path_prefix, args.limit), as_json=args.json, render=render
    )


def cmd_read(args: argparse.Namespace) -> int:
    def render(result) -> None:
        print(result.text)
        if result.note:
            print(result.note, file=sys.stderr)
        if result.more:
            print(
                f"[... {result.total_chars - result.end_char} more chars; --start-char {result.next_start_char}]",
                file=sys.stderr,
            )

    return _run_service(
        lambda engine: engine.read_file(args.path, args.start_char, args.max_chars), as_json=args.json, render=render
    )


def cmd_api(args: argparse.Namespace) -> int:
    from . import api

    if args.print_openapi:  # needs no token and starts nothing
        shown = f"[{args.host}]" if ":" in args.host else args.host
        _emit_json(api.build_openapi(f"http://{shown}:{args.port}"))
        return EXIT_OK
    return api.serve(host=args.host, port=args.port)


def cmd_serve(args: argparse.Namespace) -> int:
    from .mcp_server import main as serve

    if args.transport == "relay":
        # The relay worker calls Locus's hosted token endpoint: an invalid LOCUS_API_BASE is a
        # configuration error here (exit 2), while stdio / streamable-http never need it.
        settings = get_settings()
        require_hosted("`locus serve --transport relay`")  # off unless LOCUS_ENABLE_HOSTED=1 (after .env is read)
        settings.hosted_url(settings.mcp_relay_token_url)
    serve(args.transport, host=args.host, port=args.port, path=args.path)
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    from .ingest.indexer import index_folder
    from .ingest.watcher import PendingQueue, watch_folder

    settings = get_settings()
    root = Path(args.folder)
    rerun = _command("locus", "watch", str(root), flags={"--no-initial": args.no_initial, "--yes": True})
    matcher = _deny_list_for(root, settings, ask=(args.yes, rerun))
    if isinstance(matcher, int):
        return matcher
    embedder, store = _components(settings)
    refused = _consent_gate(root, settings, store, assume_yes=args.yes, rerun=rerun, matcher=matcher)
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
            matcher=matcher,
        )
        if stats.stop is not None:
            # RET-008: the files the initial pass did not reach wait in the watcher's queue and are
            # retried when the pause ends, instead of waiting for their next edit.
            pending.hold(stats.pending_paths, stats.stop.error)
    watch_folder(root, embedder, store, settings, pending=pending, matcher=matcher)
    return 0


def cmd_sync_notion(args: argparse.Namespace) -> int:
    from .ingest.indexer import index_folder
    from .ingest.notion_source import sync_notion

    settings = get_settings()
    require_notion("`locus sync-notion`")  # off unless LOCUS_ENABLE_NOTION=1: it copies page text into ~/.locus
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


def cmd_pause(_: argparse.Namespace) -> int:
    return killswitch.cmd_pause(get_settings().data_dir)


def cmd_resume(_: argparse.Namespace) -> int:
    return killswitch.cmd_resume(get_settings().data_dir)


def cmd_stats(_: argparse.Namespace) -> int:
    settings = get_settings()
    embedder, store = _components(settings)
    paths = store.list_paths()
    print(f"data_dir   : {settings.data_dir}")
    print(f"collection : {settings.collection}")
    print(f"embedder   : {embedder.name}")
    if hasattr(embedder, "_ping"):  # the shared helper: say whether it is running, never start it here
        info = embedder._ping()
        state = f"running (pid {info['pid']})" if info else "not running (starts on the first search)"
        print(f"helper     : shared, {state}")
    print(f"input_type : {input_type_status(embedder)}")
    print(f"files      : {len(paths)}")
    print(f"chunks     : {store.count()}")
    if paused := killswitch.state_line(settings.data_dir):
        print(f"state      : {paused}")
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
    if not _licence_question_answered():
        return EXIT_NEEDS_YES
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

    for label, passed, detail in restrictions.rule_checks(settings.data_dir):
        check(label, passed, detail)

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


def _licence_question_answered() -> bool:
    """The one soft notice (licence.py): in a terminal, the first time, ask whether Locus will be used commercially.
    False when the answer is yes: the stop text is printed and the command stops. Never a lock and never a network
    call; without a terminal nothing is asked."""
    from .licence import ask_once

    try:
        data_dir = get_settings().data_dir
    except Exception:  # noqa: BLE001 - a bad setting is the command's own business; the question can wait
        return True
    return ask_once(data_dir)


def cmd_license(_: argparse.Namespace) -> int:
    """`locus license`: the licence in two sentences and the page."""
    from .licence import licence_text

    print(licence_text())
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    """`locus setup --managed | --local`: the embedder in one step (configure.py says how)."""
    from .configure import run_setup

    if not _licence_question_answered():
        return EXIT_NEEDS_YES

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
        help="several roots under the policy file's deny-list and profiles; staged, deduplicated, resumable",
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
    p.add_argument(
        "--prune",
        action="store_true",
        help="remove index entries of files that no longer exist or that the deny-list or policy file now refuses",
    )
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

    p = sub.add_parser(
        "query",
        help="semantic search from the terminal",
        epilog=EXIT_CODES_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("query")
    p.add_argument("-k", type=int, default=5)
    p.add_argument("--prefix", default=None, help="restrict to an absolute path prefix")
    p.add_argument("--width", type=int, default=200, help="snippet width")
    p.add_argument(
        "--json",
        action="store_true",
        help='print the result as JSON ("schema": "locus.query.v1") on stdout; path and chunk_index on every hit',
    )
    p.set_defaults(func=cmd_query)

    p = sub.add_parser(
        "sources",
        help="list the indexed files (paths only) and how many there are",
        epilog=EXIT_CODES_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--path-prefix", default=None, help="only files inside this absolute folder")
    p.add_argument("--limit", type=int, default=100, help="most paths to print, 1 to 1000 (the total counts them all)")
    p.add_argument("--json", action="store_true", help='print JSON ("schema": "locus.sources.v1") on stdout')
    p.set_defaults(func=cmd_sources)

    p = sub.add_parser(
        "read",
        help="read an excerpt of one indexed file (refuses any path the index does not list)",
        epilog=EXIT_CODES_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("path", help="the file's absolute path, exactly as `locus query` or `locus sources` print it")
    p.add_argument("--start-char", type=int, default=0, help="character offset to start from")
    p.add_argument("--max-chars", type=int, default=6000, help="most characters to return, 200 to 40000")
    p.add_argument("--json", action="store_true", help='print JSON ("schema": "locus.read.v1") on stdout')
    p.set_defaults(func=cmd_read)

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

    p = sub.add_parser(
        "api",
        help="run a local HTTP API (bearer token required) for tools that cannot speak MCP",
        description="Serve search, list-sources and read-file over local HTTP, on the same index, freshness step "
        "and confinement as the MCP tools. Refuses to start without LOCUS_API_TOKEN (at least 32 characters). "
        "Binds 127.0.0.1 by default. GET /openapi.json (or --print-openapi) describes every route. Settings: "
        "LOCUS_API_TOKEN, LOCUS_API_ALLOWED_HOSTS, LOCUS_API_ALLOWED_ORIGINS, LOCUS_API_RATE_PER_MIN, "
        "LOCUS_API_MAX_BODY_BYTES, LOCUS_API_MAX_EXCERPT_CHARS. The one line printed to stdout is the start-up line.",
        epilog=EXIT_CODES_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--host", default="127.0.0.1", help="address to listen on (default: 127.0.0.1, this computer only)")
    p.add_argument("--port", type=int, default=8765, help="port to listen on; 0 picks a free one (default: 8765)")
    p.add_argument(
        "--print-openapi",
        action="store_true",
        help="print the OpenAPI 3.1 document for --host and --port to stdout and exit; no token needed",
    )
    p.set_defaults(func=cmd_api)

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

    p = sub.add_parser(
        "pause",
        help="stop the file tools at once (search, list sources, read file, the local API, query --json)",
        description="Creates <data dir>/PAUSED. Every AI app that runs `locus serve` from this data folder gets one "
        "fixed sentence instead of any file, until `locus resume`. LOCUS_DISABLED=1 does the same from the "
        "environment. Indexing and watching are not stopped. locus_help and locus_diagnose_connection keep answering.",
    )
    p.set_defaults(func=cmd_pause)

    p = sub.add_parser("resume", help="undo `locus pause`")
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser(
        "doctor",
        help="diagnose common setup problems",
        description="Check Python, packages, settings and the embedder. In a terminal, the first run also asks "
        "whether you will use Locus commercially (see `locus license`); exit code 2 if you answer yes.",
    )
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser(
        "license",
        aliases=["licence"],
        help="what the licence allows, in two sentences, and where to ask about commercial use",
    )
    p.set_defaults(func=cmd_license)

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


def _boot(args: argparse.Namespace) -> None:
    """Before a command that reads or writes the index: the one-time closed-folder migration (closed_folders.py). The
    previews (`--dry-run`, `estimate`) change nothing, so they do not run it. Never raises."""
    if args.command not in ("index", "watch", "query", "sources", "read", "serve", "api", "stats", "doctor"):
        return
    if getattr(args, "dry_run", False):
        return
    try:
        closed_folders.migrate(get_settings(), Path.home().resolve())
    except Exception:  # noqa: BLE001 - a start-up check must never stop the command
        pass


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        _boot(args)
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
