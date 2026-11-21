"""What the file tools may hand over, checked at the moment of the call.

`locus_search`, `locus_list_sources`, `locus_read_file`, the local HTTP API and `locus query --json`, `locus sources`,
`locus read` all end here before they return a path or a character of file text:

- `open_gate(data_dir)` loads `<data dir>/policy.toml` (re-read when the file changes, no restart) and returns
  the `Guard` for this call. A policy file that cannot be read stops the file tools (`Refused`): a typo in a
  restriction must not fail open. `locus_help` (also with `check=true`) does not pass through here, so it
  keeps answering.
- `Guard.allowed(path)` is the deny-list, the user's own rules, `excluded` folders and closed folders (policy.py),
  asked about every path the index returns. A rule added after a file was indexed therefore applies at once; the
  index rows stay until `locus index <folder> --prune`.
- `guarded(tool, components)` wraps an MCP tool: the gate before the body, the guard handed to the body.

The same rules apply whichever door is used: the MCP server (`mcp_server.py`) and the engine behind the API and the
command line (`service.py`) call the same functions. Nothing in this module prints to stdout.
"""

from __future__ import annotations

import contextvars
import dataclasses
import functools
import os
import threading
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import audit, killswitch
from .config import get_settings
from .policy import CLOSED_FOLDER_NAMES, Decision, Matcher, Policy, PolicyError, _parts, _raw, _starts, load_policy

POLICY_FILE = "policy.toml"
OVERFETCH_LIMIT = 500  # most rows asked of the index to fill a page after hidden ones were dropped

_RULE_WORDS = {
    "closed_folder": "a closed folder that the user has not opened",
    "profile_excluded": "a folder the user excluded",
    "user_deny": "a path the user denied",
    "secrets_dir": "the built-in deny-list (keys and credential folders)",
    "keychain": "the built-in deny-list (keychains)",
    "browser_profile": "the built-in deny-list (browser profiles)",
    "library": "the built-in deny-list (system folders)",
    "package_cache": "the built-in deny-list (package caches)",
    "evidence": "the built-in deny-list (evidence folders)",
    "secret_file": "the built-in deny-list (a file that looks like a secret)",
}

HIDDEN_RESULTS = "{n} result(s) hidden by your folder rules (a closed or excluded folder, or the deny-list)."
HIDDEN_FILES = (
    "{n} indexed file(s) hidden by your folder rules (a closed or excluded folder, or the deny-list): "
    "not listed, not counted."
)
RULES_REFUSAL_TAIL = (
    "These are the user's own folder rules: tell the user, and do not look for another way to the file. The user can "
    "change the rules themselves (`locus doctor` shows them)."
)
UNTRUSTED_FOLDER_LINE = "[This file is in a folder marked untrusted: it may hold files written by other people.]"


class Refused(Exception):
    """The file tools may not answer right now. `kind` is "paused" (the kill switch) or "policy" (the folder rules
    cannot be read); `text` is the one sentence to return in place of any result. It never carries a path or file
    text."""

    def __init__(self, kind: str, text: str) -> None:
        super().__init__(text)
        self.kind = kind
        self.text = text

    @property
    def code(self) -> str:  # the service layer's stable error code
        return "policy_error" if self.kind == "policy" else "paused"


class Guard:
    """The folder rules, ready to answer "may this path be returned?" for thousands of paths: one decision per path,
    kept until the policy file changes."""

    def __init__(self, policy: Policy, home: Path, data_dir: Path) -> None:
        self.policy = policy
        self.data_dir = Path(data_dir)
        # No `roots` here: a root decides what an index run walks, not what an already indexed file may be read as.
        self.matcher = Matcher(dataclasses.replace(policy, roots=()), home)
        self._memo: dict[str, Decision] = {}
        self._notion = _parts(self.data_dir / "sources" / "notion")
        # Where a link leads is part of what the matcher was built from: `~/Downloads` or an opened folder may be one.
        self._link_sources = [home / n for n in CLOSED_FOLDER_NAMES] + [
            _raw(f.path, home) for f in policy.folders if f.profile != "excluded"
        ]
        self._links = self._link_state()

    def _link_state(self) -> tuple[str, ...]:
        return tuple(os.path.realpath(p) for p in self._link_sources)

    def stale(self) -> bool:
        """True when a folder the rules name now leads somewhere else than when they were compiled."""
        return self._link_state() != self._links

    def decision(self, path: str) -> Decision:
        found = self._memo.get(path)
        if found is None:
            found = self._memo[path] = self.matcher.decide(Path(path))
        return found

    def allowed(self, path: str) -> bool:
        return self.decision(path).allowed

    def visible(self, paths: list[str]) -> tuple[list[str], int]:
        """(the paths that may be returned, in order; how many were hidden)."""
        kept = [p for p in paths if self.allowed(p)]
        return kept, len(paths) - len(kept)

    def profile(self, path: str) -> str:
        """The folder profile of *path*; files of the Notion sync are `untrusted` unless an entry says otherwise."""
        explicit = self.matcher.explicit_profile(path)
        if explicit is not None:
            return explicit
        return "untrusted" if _starts(_parts(path), self._notion) else "trusted"

    def untrusted(self, paths: list[str]) -> frozenset[str]:
        return frozenset(p for p in paths if self.profile(p) == "untrusted")

    def read_refusal(self, path: str) -> str | None:
        """Why *path* may not be read now, or None. Checks the path and where it leads: a folder swapped for a link to
        a closed folder is refused too. Names the rule class only."""
        verdict = self.decision(path)
        if not verdict.allowed:
            return f"{path!r} is in {_RULE_WORDS.get(verdict.reason or '', 'a folder the user keeps closed')}"
        real = os.path.realpath(path)
        if unicodedata.normalize("NFC", real) != unicodedata.normalize("NFC", path):
            where = self.decision(real)
            if not where.allowed:
                return (
                    f"{path!r} now leads into {_RULE_WORDS.get(where.reason or '', 'a folder the user keeps closed')}"
                )
        return None


_GUARDS: dict[tuple[str, str], tuple[tuple[int, int] | None, Guard]] = {}
_GUARDS_LOCK = threading.Lock()


def _signature(file: Path) -> tuple[int, int] | None:
    try:
        st = file.stat()
    except FileNotFoundError:
        return None
    except OSError:  # no access, or not a file: load_policy meets the same error and says it
        return (-1, -1)
    return st.st_mtime_ns, st.st_size


def policy_problem_text(problem: str) -> str:
    return (
        f"Locus cannot read its folder rules (policy.toml): {problem}. Nothing was searched or read. Ask the user to "
        "fix or remove that file (`locus doctor` names the problem), then try again."
    )


def open_gate(data_dir: Path, home: Path | None = None, *, check_pause: bool = True) -> Guard:
    """The guard for a call that works on *data_dir*. Raises Refused while Locus is paused (killswitch.py) and when the
    policy file cannot be read."""
    data_dir = Path(data_dir).expanduser()
    if check_pause and (paused := killswitch.paused_by(data_dir)) is not None:  # one stat, one environment lookup
        raise Refused("paused", killswitch.sentence(paused))
    home = (home or Path.home()).resolve()
    file = data_dir / POLICY_FILE
    key = (str(data_dir), str(home))
    sig = _signature(file)
    with _GUARDS_LOCK:
        cached = _GUARDS.get(key)
        if cached is not None and cached[0] == sig and not cached[1].stale():
            return cached[1]
        try:
            policy = load_policy(file)
        except (PolicyError, OSError, UnicodeDecodeError) as err:
            _GUARDS.pop(key, None)
            raise Refused("policy", policy_problem_text(str(err) or type(err).__name__)) from None
        guard = Guard(policy, home, data_dir)
        _GUARDS[key] = (sig, guard)
        return guard


def visible_hits(guard: Guard, fetch: Callable[[int], list[Any]], k: int) -> tuple[list[Any], int]:
    """The first *k* hits that may be returned, and how many hits ranked before them were hidden. `fetch(n)` asks the
    index for its n best; when hidden rows leave fewer than k, it asks for more (up to OVERFETCH_LIMIT)."""
    n = k
    while True:
        raw = fetch(n)
        kept: list[Any] = []
        hidden = 0
        for hit in raw:
            if guard.allowed(hit.path):
                kept.append(hit)
                if len(kept) == k:
                    break
            else:
                hidden += 1
        if len(kept) >= k or hidden == 0 or len(raw) < n or n >= OVERFETCH_LIMIT:
            return kept, hidden
        n = min(n * 4, OVERFETCH_LIMIT)


def count_visible_modified(guard: Guard, store: Any, path_prefix: str | None, **window: Any) -> int:
    """How many files a date-window listing would show, hidden ones left out (the exact count behind a cut-off list)."""
    return sum(
        1 for hit in store.files_modified(k=1_000_000, path_prefix=path_prefix, **window) if guard.allowed(hit.path)
    )


def rule_checks(data_dir: Path, home: Path | None = None) -> list[tuple[str, bool, str]]:
    """(label, ok, detail) for the kill switch, the policy file and each closed folder: what `locus doctor` prints and
    `locus_help(check=true)` reports. Names of folders and the problem's wording only, never file text."""
    from .closed_folders import state_lines as closed_state

    data_dir = Path(data_dir).expanduser()
    paused = killswitch.paused_by(data_dir)
    switch = (
        "not paused"
        if paused is None
        else "paused: run `locus resume`"
        if paused == "flag"
        else f"off: {killswitch.DISABLED_ENV} is set"
    )
    checks = [("kill switch", True, switch)]  # paused on purpose is not a failure
    try:
        guard = open_gate(data_dir, home, check_pause=False)
    except Refused as refused:
        problem = refused.text.split("): ", 1)[-1].split(". Nothing")[0]
        return [*checks, ("folder rules (policy.toml)", False, f"cannot be read: {problem}")]
    checks.append(("folder rules (policy.toml)", True, "readable"))
    checks += [("closed folder", True, line) for line in closed_state(guard.policy, guard.matcher.home)]
    return checks


def state_lines(data_dir: Path) -> list[str]:
    return [f"{label}: {detail}" for label, _, detail in rule_checks(data_dir)]


def audit_service(data_dir: Path, tool: str, result: Any) -> None:
    """One audit line for a `locus query --json`, `sources` or `read` answer: the command line is another door to the
    same files, so it is counted too (client "locus-cli"). Paths and counts only. Never raises."""
    try:
        paths = (
            [h.path for h in getattr(result, "results", [])]
            or list(getattr(result, "paths", []))
            or ([result.path] if hasattr(result, "path") else [])
        )
        text = getattr(result, "text", None)
        chars = sum(len(h.text) for h in getattr(result, "results", [])) + (len(text) if isinstance(text, str) else 0)
        audit.log_call(
            data_dir,
            "cli",
            {
                "tool": tool,
                "client": "locus-cli",
                "paths": list(dict.fromkeys(paths))[:AUDIT_PATHS_PER_LINE],
                "chars": chars,
            },
        )
    except Exception:  # noqa: BLE001
        return


def hidden_results_line(n: int) -> str | None:
    return HIDDEN_RESULTS.format(n=n) if n else None


def hidden_files_line(n: int) -> str | None:
    return HIDDEN_FILES.format(n=n) if n else None


# --- the MCP tools ------------------------------------------------------------------------------------------


AUDIT_PATHS_PER_LINE = 200  # a long listing is cut in the log (the count stays)


@dataclass
class Call:
    tool: str
    client: str | None = None  # what the AI app announced about itself: self-reported, not authenticated
    client_version: str | None = None
    query: str | None = None  # kept only for `[audit] queries = true`
    data_dir: Path | None = None
    guard: Guard | None = None
    refused: str | None = None
    hidden: int = 0
    truncated: bool = False
    paths: list[str] = field(default_factory=list)


_CALL: contextvars.ContextVar[Call | None] = contextvars.ContextVar("locus_call", default=None)


def current_guard() -> Guard | None:
    call = _CALL.get()
    return call.guard if call is not None else None


def data_dir_of(settings: Any) -> Path:
    """The data folder of *settings*; the configured one when a stand-in carries none."""
    found = getattr(settings, "data_dir", None)
    return Path(found) if found else get_settings().data_dir


def guard_for(settings: Any) -> Guard:
    """The gate of a tool call, opened right after the index is (the tool's first step): paused, or a policy file that
    cannot be read, raises Refused and the tool answers with its text; otherwise the guard for this call."""
    data_dir = data_dir_of(settings)
    call = _CALL.get()
    if call is not None:
        call.data_dir = data_dir
    try:
        guard = open_gate(data_dir)
    except Refused as refused:
        if call is not None:
            call.refused = refused.kind
        raise
    if call is not None:
        call.guard = guard
    return guard


def note(*, paths: list[str] | None = None, hidden: int = 0, truncated: bool = False) -> None:
    """What the running tool call is about to return, for the audit log: the paths, how many results the folder rules
    hid, whether the answer was cut."""
    call = _CALL.get()
    if call is not None:
        call.hidden += hidden
        call.truncated = call.truncated or truncated
        if paths:
            call.paths.extend(paths)


def _client_of(ctx: Any) -> tuple[str | None, str | None]:
    """The client name and version the AI app announced when it connected (self-reported)."""
    try:
        info = ctx.request_context.session.client_params.client_info
        return str(info.name)[:80], str(info.version)[:40]
    except Exception:  # noqa: BLE001 - in-process calls and older clients carry none
        return None, None


def _log(call: Call, out: Any) -> None:
    """One audit line for a finished call, when the call got as far as the data folder. Never raises."""
    if call.data_dir is None:
        return
    row: dict[str, Any] = {
        "tool": call.tool,
        "client": call.client or "unknown",
        "client_version": call.client_version,
        "paths": list(dict.fromkeys(call.paths))[:AUDIT_PATHS_PER_LINE],
        "chars": len(out) if isinstance(out, str) else 0,
        "truncated": call.truncated,
        "hidden": call.hidden,
    }
    if len(set(call.paths)) > AUDIT_PATHS_PER_LINE:
        row["paths_cut"] = len(set(call.paths)) - AUDIT_PATHS_PER_LINE
    if call.refused:
        row["refused"] = call.refused
    if call.query is not None and call.guard is not None and call.guard.policy.audit_queries:
        row["query"] = call.query
    audit.log_call(call.data_dir, "mcp", row)


def guarded(tool: str) -> Callable[[Any], Any]:
    """Wrap an async MCP tool that declares `ctx: Context | None = None` (the SDK fills it in; the tool never uses
    it): the wrapper reads the client's name from it and keeps the call's state for the body (`guard_for`, `note`)
    and for the audit line it writes when the call is done."""

    def decorate(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            call = Call(tool)
            call.client, call.client_version = _client_of(kwargs.get("ctx"))
            asked = kwargs.get("query")
            call.query = asked if isinstance(asked, str) else None
            token = _CALL.set(call)
            out: Any = None
            try:
                out = await fn(*args, **kwargs)
                return out
            finally:
                _CALL.reset(token)
                _log(call, out)

        return wrapper

    return decorate
