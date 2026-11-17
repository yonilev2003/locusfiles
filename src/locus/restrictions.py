"""What the file tools may hand over, checked at the moment of the call.

`locus_search`, `locus_list_sources`, `locus_read_file`, the local HTTP API and `locus query --json`, `locus sources`,
`locus read` all end here before they return a path or a character of file text:

- `open_gate(data_dir)` loads `<data dir>/policy.toml` (re-read when the file changes, no restart) and returns
  the `Guard` for this call. A policy file that cannot be read stops the file tools (`Refused`): a typo in a
  restriction must not fail open. `locus_help` and `locus_diagnose_connection` do not pass through here, so they
  keep answering.
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
import inspect
import os
import threading
import typing
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import Context

from .config import get_settings
from .policy import Decision, Matcher, Policy, PolicyError, _parts, _starts, load_policy

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
HIDDEN_FILES = "{n} indexed file(s) hidden by your folder rules (a closed or excluded folder, or the deny-list): not listed, not counted."  # noqa: E501
RULES_REFUSAL_TAIL = (
    "These are the user's own folder rules: tell the user, and do not look for another way to the file. The user can "
    "change the rules themselves (`locus doctor` shows them)."
)
UNTRUSTED_FOLDER_LINE = "[This file is in a folder marked untrusted: it may hold files written by other people.]"


class Refused(Exception):
    """The file tools may not answer right now. `kind` is "policy" (the folder rules cannot be read); `text` is the one
    sentence to return in place of any result. It never carries a path or file text."""

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
    return st.st_mtime_ns, st.st_size


def policy_problem_text(problem: str) -> str:
    return (
        f"Locus cannot read its folder rules (policy.toml): {problem}. Nothing was searched or read. Ask the user to "
        "fix or remove that file (`locus doctor` names the problem), then try again."
    )


def open_gate(data_dir: Path, home: Path | None = None) -> Guard:
    """The guard for a call that works on *data_dir*. Raises Refused when the policy file cannot be read."""
    data_dir = Path(data_dir).expanduser()
    home = (home or Path.home()).resolve()
    file = data_dir / POLICY_FILE
    key = (str(data_dir), str(home))
    sig = _signature(file)
    with _GUARDS_LOCK:
        cached = _GUARDS.get(key)
        if cached is not None and cached[0] == sig:
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


def hidden_results_line(n: int) -> str | None:
    return HIDDEN_RESULTS.format(n=n) if n else None


def hidden_files_line(n: int) -> str | None:
    return HIDDEN_FILES.format(n=n) if n else None


# --- the MCP tools ------------------------------------------------------------------------------------------


@dataclass
class Call:
    tool: str
    data_dir: Path | None = None
    guard: Guard | None = None
    hidden: int = 0
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
    guard = open_gate(data_dir)
    if call is not None:
        call.guard = guard
    return guard


def note(*, paths: list[str] | None = None, hidden: int = 0) -> None:
    """What the running tool call is about to return, for the audit log and the hidden-results notes."""
    call = _CALL.get()
    if call is not None:
        call.hidden += hidden
        if paths:
            call.paths.extend(paths)


def guarded(tool: str) -> Callable[[Any], Any]:
    """Wrap an async MCP tool. The wrapper takes the SDK's `ctx` argument, which the tool itself does not declare, so
    the tool's own signature and schema stay as they are, and it keeps the call's state for the body (`guard_for`)."""

    def decorate(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        @functools.wraps(fn)
        async def wrapper(*args: Any, ctx: Any = None, **kwargs: Any) -> Any:
            token = _CALL.set(Call(tool))
            try:
                return await fn(*args, **kwargs)
            finally:
                _CALL.reset(token)

        # The tool module writes its annotations as strings (`from __future__ import annotations`) and the SDK would
        # read them in THIS module's namespace: resolve them against the tool's own, then add the `ctx` argument
        # the SDK fills in.
        hints = typing.get_type_hints(fn, include_extras=True)
        signature = inspect.signature(fn)
        ctx_parameter = inspect.Parameter(
            "ctx", inspect.Parameter.KEYWORD_ONLY, default=None, annotation=Context | None
        )
        wrapper.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
            [p.replace(annotation=hints.get(p.name, p.annotation)) for p in signature.parameters.values()]
            + [ctx_parameter],
            return_annotation=hints.get("return", signature.return_annotation),
        )
        wrapper.__annotations__ = {**hints, "ctx": Context | None}
        return wrapper

    return decorate
