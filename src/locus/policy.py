"""Folder roots, the default deny-list and per-folder profiles.

Pure: nothing here reads the disk except `load_policy` (one TOML file), and nothing guesses the home
directory: the caller passes it in, so tests run over a fake home. `Matcher.decide(path)` is the one
place that says whether a path may be indexed and why not; the walker (`ingest/indexer.iter_files`),
the dry run (`rootscan`) and later the freshness scan and `locus_read_file` confinement call it.

The deny-list is code, always on: a policy file can add restrictions (`[deny] paths`, `excluded` folders)
but cannot remove a default rule. Unknown keys are errors, never ignored: a typo in a restriction must
not fail open.
"""

from __future__ import annotations

import dataclasses
import functools
import os
import re
import tomllib
import unicodedata
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path

PROFILES: tuple[str, ...] = ("trusted", "untrusted", "read-only", "excluded")
DEFAULT_PROFILE = "trusted"

# Reason names (stable: they appear in the dry-run report). The walker reports them as `denied_<name>`.
DENY_REASONS: tuple[str, ...] = (
    "secrets_dir",
    "keychain",
    "browser_profile",
    "library",
    "package_cache",
    "evidence",
    "secret_file",
    "secret_content",
    "user_deny",
    "profile_excluded",
    "closed_folder",
    "outside_roots",
)
DENIED_SKIP_REASONS: tuple[str, ...] = tuple(f"denied_{r}" for r in DENY_REASONS)

# Folders below the home directory that stay closed until the user opens them (closed_folders.py says how): they mix
# files the user wrote with files other people wrote. Only Downloads (on Windows `%USERPROFILE%\Downloads`, which is
# the same name below the home directory). A parent root never opens one; only a `[[folder]]` entry for that folder, or
# for a folder inside it, does.
CLOSED_FOLDER_NAMES: tuple[str, ...] = ("Downloads",)

# Folders below the home directory (case-insensitive: macOS volumes usually are).
_SECRETS_DIRS = (
    (".ssh",),
    (".aws",),
    (".gnupg",),
    (".kube",),
    (".docker",),
    (".azure",),
    (".config", "gcloud"),
    (".config", "gh"),
    (".config", "op"),
)
_KEYCHAIN_DIRS = (
    ("Library", "Keychains"),
    (".local", "share", "keyrings"),  # Linux (GNOME Keyring)
    (".password-store",),  # Linux (pass)
    ("AppData", "Roaming", "Microsoft", "Credentials"),  # Windows
    ("AppData", "Local", "Microsoft", "Credentials"),
    ("AppData", "Roaming", "Microsoft", "Protect"),
)
_BROWSER_DIRS = (
    (".mozilla",),
    ("snap", "firefox"),  # Linux (snap)
    (".config", "google-chrome"),  # Linux
    (".config", "chromium"),
    (".config", "microsoft-edge"),
    (".config", "BraveSoftware"),
    ("AppData", "Local", "Google", "Chrome"),  # Windows
    ("AppData", "Local", "Microsoft", "Edge"),
    ("AppData", "Local", "BraveSoftware"),
    ("AppData", "Roaming", "Mozilla"),
    ("AppData", "Roaming", "Opera Software"),
    ("Library", "Safari"),
    ("Library", "Cookies"),
    ("Library", "Application Support", "Google", "Chrome"),
    ("Library", "Application Support", "Chromium"),
    ("Library", "Application Support", "Microsoft Edge"),
    ("Library", "Application Support", "BraveSoftware"),
    ("Library", "Application Support", "Arc"),
    ("Library", "Application Support", "Firefox"),
)
# Folder names denied wherever they occur. `node_modules`, `.venv`, `.git` and the rest of config.SKIP_DIRS keep
# their own walker reason (`skipped_dir`); these are the ones SKIP_DIRS does not name.
_PACKAGE_DIRS = frozenset({"site-packages", ".tox", ".gradle", ".m2", "pods", ".npm", ".pnpm-store"})
_EVIDENCE_GLOB = "security-evidence-*"

_SECRET_SUFFIXES = (
    ".pem", ".key", ".p12", ".pfx", ".jks",
    ".p8", ".ppk", ".gpg", ".tfvars", ".ovpn", ".tfstate", ".tfstate.backup",
)  # fmt: skip
_KEYCHAIN_SUFFIXES = (".keychain", ".keychain-db", ".kdbx", ".1pux", ".opvault")
_SECRET_PREFIXES = ("id_rsa", "id_ed25519", "id_ecdsa", "id_dsa")
_SECRET_NAMES = frozenset(
    {
        ".env", ".netrc", ".npmrc", ".pypirc", "kubeconfig",
        # browser stores that hold logins and session cookies (Chrome, Firefox)
        "cookies", "login data", "web data", "logins.json", "key3.db", "key4.db", "cookies.sqlite",
    }
)  # fmt: skip
# Whole words only, so "tokenizer.py" stays and "api-token.txt", "accessToken.json" and "cookies.txt" go.
_SECRET_WORDS = frozenset(
    {
        "token",
        "tokens",
        "secret",
        "secrets",
        "credential",
        "credentials",
        "password",
        "passwords",
        "passwd",
        "cookie",
        "cookies",
    }
)
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NON_WORD = re.compile(r"[^a-z0-9]+")


class PolicyError(ValueError):
    """The policy file is malformed. The message names the offending key."""


@dataclass(frozen=True)
class FolderProfile:
    path: str
    profile: str


@dataclass(frozen=True)
class Policy:
    roots: tuple[str, ...] = ()
    folders: tuple[FolderProfile, ...] = ()
    library_allow: tuple[str, ...] = ()
    deny_paths: tuple[str, ...] = ()
    audit_queries: bool = False  # `[audit] queries = true`: the audit log also records the query text (default off)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str | None  # one of DENY_REASONS when not allowed
    profile: str  # profile of the deepest matching folder, DEFAULT_PROFILE if none


def _str_list(table: dict, key: str, where: str) -> tuple[str, ...]:
    value = table.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise PolicyError(f"{where}{key} must be a list of strings")
    for v in value:
        _check_path_text(v, f"{where}{key}")
    return tuple(value)


def _check_path_text(text: str, where: str) -> None:
    if not (text == "~" or text.startswith(("~/", "/"))):
        raise PolicyError(f"{where}: {text!r} must start with ~/ or / (relative paths are ambiguous)")


def _only_keys(table: dict, allowed: set[str], where: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise PolicyError(f"unknown key {unknown[0]!r} in {where}")


def parse_policy(text: str) -> Policy:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as err:
        raise PolicyError(f"not valid TOML: {err}") from err
    _only_keys(data, {"version", "roots", "folder", "library", "deny", "audit"}, "the policy file")
    if data.get("version", 1) != 1:
        raise PolicyError(f"version {data['version']!r} is not supported (this Locus reads version 1)")
    roots = _str_list(data, "roots", "")
    folders: list[FolderProfile] = []
    for entry in data.get("folder", []):
        if not isinstance(entry, dict):
            raise PolicyError("[[folder]] entries must be tables")
        _only_keys(entry, {"path", "profile"}, "[[folder]]")
        path, profile = entry.get("path"), entry.get("profile")
        if not isinstance(path, str) or not isinstance(profile, str):
            raise PolicyError("[[folder]] needs a path and a profile, both strings")
        _check_path_text(path, "[[folder]] path")
        if profile not in PROFILES:
            raise PolicyError(f"unknown profile {profile!r} for a folder (use one of {', '.join(PROFILES)})")
        folders.append(FolderProfile(path, profile))
    library, deny = data.get("library", {}), data.get("deny", {})
    _only_keys(library, {"allow"}, "[library]")
    _only_keys(deny, {"paths"}, "[deny]")
    audit = data.get("audit", {})
    if not isinstance(audit, dict):
        raise PolicyError("[audit] must be a table")
    _only_keys(audit, {"queries"}, "[audit]")
    if not isinstance(audit.get("queries", False), bool):
        raise PolicyError("[audit] queries must be true or false")
    return Policy(
        roots=roots,
        folders=tuple(folders),
        library_allow=_str_list(library, "allow", "[library] "),
        deny_paths=_str_list(deny, "paths", "[deny] "),
        audit_queries=audit.get("queries", False),
    )


def load_policy(path: Path) -> Policy:
    """The policy at *path*; a missing file means the defaults (no roots, no profiles)."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return Policy()
    return parse_policy(text)


@functools.lru_cache(maxsize=1 << 17)  # the same folder names come back in every path of a big index
def _fold(text: str) -> str:
    # NFC first: APFS keeps the spelling of whoever created a name, so "Café" may arrive composed or decomposed (F5).
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", text).casefold())


def _parts(path: Path | str) -> tuple[str, ...]:
    return tuple(_fold(p) for p in Path(path).parts)


def _raw(text: str, home: Path) -> Path:
    return home if text == "~" else home / text[2:] if text.startswith("~/") else Path(text)


def _expand(text: str, home: Path) -> tuple[str, ...]:
    return _parts(_raw(text, home))


def _variants(path: Path) -> tuple[tuple[str, ...], ...]:
    """*path* as written and as the file system resolves it (a link leads somewhere else), as folded components."""
    return tuple(dict.fromkeys((_parts(path), _parts(os.path.realpath(path)))))


def _starts(parts: tuple[str, ...], prefix: tuple[str, ...]) -> bool:
    return parts[: len(prefix)] == prefix


def _contains(parts: tuple[str, ...], seq: tuple[str, ...]) -> bool:
    """True when *seq* occurs as consecutive path components anywhere in *parts*."""
    n = len(seq)
    return any(parts[i : i + n] == seq for i in range(len(parts) - n + 1))


def _by_first(seqs: tuple[tuple[str, ...], ...]) -> dict[str, tuple[tuple[str, ...], ...]]:
    index: dict[str, list[tuple[str, ...]]] = {}
    for seq in seqs:
        index.setdefault(seq[0], []).append(seq)
    return {first: tuple(group) for first, group in index.items()}


def _contains_any(parts: tuple[str, ...], index: dict[str, tuple[tuple[str, ...], ...]]) -> bool:
    """`any(_contains(parts, s) for s in seqs)` for the sequences behind *index* (`_by_first`), looking only where a
    sequence can start. The same answer, a few times faster on the long paths of a big index (query-time re-check)."""
    for i, part in enumerate(parts):
        for seq in index.get(part, ()):
            if parts[i : i + len(seq)] == seq:
                return True
    return False


def _foreign_library(parts: tuple[str, ...]) -> bool:
    """`/Users/<name>/Library` or `/Volumes/<disk>/Users/<name>/Library`: another user's or a backup's Library."""
    return any(p == "library" and i >= 2 and parts[i - 2] == "users" for i, p in enumerate(parts))


def secret_name(name: str) -> bool:
    """True for a file name that looks like a secret (kept conservative: a false skip is cheaper than a leak)."""
    low = _fold(name)
    if low in _SECRET_NAMES or low.startswith(".env.") or low.startswith(_SECRET_PREFIXES):
        return True
    if low.endswith(_SECRET_SUFFIXES):
        return True
    words = _NON_WORD.split(_CAMEL.sub(" ", name).casefold())
    return bool(_SECRET_WORDS.intersection(words)) or "apikey" in "".join(words)


# Content that is a credential whatever the file is called. Conservative on purpose: each pattern is a fixed,
# well-known shape (a PEM private key, an AWS access key id, a GitHub token, an `sk-` API key, a JWT, a Netscape
# cookie jar), so ordinary prose, code and numbers do not match. A false skip is cheaper than a leak.
_NETSCAPE_LINE = re.compile(r"^[^\s#][^\t\n]*\t(?:TRUE|FALSE)\t[^\t\n]*\t(?:TRUE|FALSE)\t\d+\t[^\t\n]+\t", re.M)
_SECRET_CONTENT: tuple[re.Pattern[str], ...] = (
    re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"),
    re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])"),
    re.compile(r"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{36,}"),
    re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{32,}"),
    re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    re.compile(r"^# (?:Netscape )?HTTP Cookie File", re.M),
)


def secret_content(text: str) -> bool:
    """True when *text* holds a credential of a well-known shape (see `_SECRET_CONTENT`); never returns the match."""
    if any(p.search(text) for p in _SECRET_CONTENT):
        return True
    return len(_NETSCAPE_LINE.findall(text)) >= 3


class Matcher:
    """A policy compiled against one home directory. Cheap to call once per file."""

    def __init__(self, policy: Policy, home: Path):
        self.policy = policy
        self.home = Path(home)
        h = _parts(self.home)
        self._home_parts = h
        expanded = [_expand(r, self.home) for r in policy.roots]
        # A root given through a symlink is the same place as its target; the walker resolves roots: both (F13).
        resolved = [_parts(os.path.realpath(os.path.join(*e))) for e in expanded if e]
        self._roots = tuple(dict.fromkeys([*expanded, *resolved]))
        # Matched as a tail anywhere in the path, not only under this home: a backup disk, a Time Machine copy or
        # another user's home holds the same credential and browser folders (F6).
        self._secrets = _by_first(tuple(tuple(_fold(x) for x in t) for t in _SECRETS_DIRS))
        self._keychain = _by_first(tuple(tuple(_fold(x) for x in t) for t in _KEYCHAIN_DIRS))
        self._browsers = _by_first(tuple(tuple(_fold(x) for x in t) for t in _BROWSER_DIRS))
        self._library = h + ("library",)
        self._library_allow = tuple(_expand(a, self.home) for a in policy.library_allow)
        self._user_deny = tuple(_expand(d, self.home) for d in policy.deny_paths)
        folders = [(_expand(f.path, self.home), f.profile) for f in policy.folders]
        self._folders = sorted(folders, key=lambda fp: len(fp[0]), reverse=True)  # deepest first
        # Closed folders (CLOSED_FOLDER_NAMES below this home) and the `[[folder]]` entries that open them (any entry
        # that is not `excluded`), each as written and as the file system resolves it: `~/Downloads` may be a link.
        self._closed = tuple(dict.fromkeys(v for n in CLOSED_FOLDER_NAMES for v in _variants(self.home / n)))
        self._openers = tuple(
            dict.fromkeys(
                v for f in policy.folders if f.profile != "excluded" for v in _variants(_raw(f.path, self.home))
            )
        )

    def closed_dirs(self) -> tuple[Path, ...]:
        """The closed folders of this home, as written (not case-folded), for messages and counts."""
        return tuple(self.home / n for n in CLOSED_FOLDER_NAMES)

    def _closed_by(self, parts: tuple[str, ...], is_dir: bool) -> bool:
        """True when *parts* is at or below a closed folder that no `[[folder]]` entry opens. An entry opens its own
        folder and everything below it; a folder on the way to an entry may be walked through (its own files stay
        closed)."""
        for closed in self._closed:
            if _starts(parts, closed):
                return not any(
                    _starts(e, closed) and (_starts(parts, e) or (is_dir and _starts(e, parts))) for e in self._openers
                )
        return False

    def explicit_profile(self, path: Path | str) -> str | None:
        """The profile of the deepest `[[folder]]` entry that covers *path*, or None when no entry does."""
        parts = _parts(path)
        for prefix, profile in self._folders:
            if _starts(parts, prefix):
                return profile
        return None

    def profile_of(self, path: Path | str) -> str:
        return self.explicit_profile(path) or DEFAULT_PROFILE

    def _library_denied(self, parts: tuple[str, ...], is_dir: bool) -> bool:
        if not _starts(parts, self._library):
            return _foreign_library(parts)  # no allow-list applies to a Library that is not this home's
        for exc in self._library_allow:
            if _starts(parts, exc) or (is_dir and _starts(exc, parts)):
                return False
        return True

    def decide(self, path: Path | str, is_dir: bool = False) -> Decision:
        """May *path* (absolute) be indexed? A dir decision prunes the whole subtree."""
        path = Path(path)
        parts = _parts(path)
        profile = self.profile_of(path)

        def no(reason: str) -> Decision:
            return Decision(False, reason, profile)

        if self._roots and not any(_starts(parts, r) for r in self._roots):
            return no("outside_roots")
        if _contains_any(parts, self._secrets):
            return no("secrets_dir")
        if _contains_any(parts, self._keychain):
            return no("keychain")
        if _contains_any(parts, self._browsers):
            return no("browser_profile")
        if self._library_denied(parts, is_dir):
            return no("library")
        below_home = parts[len(self._home_parts) :] if _starts(parts, self._home_parts) else parts
        if any(fnmatchcase(c, _EVIDENCE_GLOB) for c in below_home):
            return no("evidence")
        if _PACKAGE_DIRS.intersection(below_home):
            return no("package_cache")
        if not is_dir:
            if _fold(path.name).endswith(_KEYCHAIN_SUFFIXES):
                return no("keychain")
            if secret_name(path.name):
                return no("secret_file")
        if any(_starts(parts, d) for d in self._user_deny):
            return no("user_deny")
        if profile == "excluded":
            return no("profile_excluded")
        if self._closed_by(parts, is_dir):
            return no("closed_folder")
        return Decision(True, None, profile)


def matcher_for_folder(folder: Path | str, policy: Policy, home: Path) -> Matcher:
    """The matcher for indexing one *folder*: the policy's rules with that folder as the only root, so the deny-list
    and the policy's `excluded` folders and `[deny] paths` apply to it (the rule `--roots` applies to its roots)."""
    root = Path(folder).expanduser().resolve()
    return Matcher(dataclasses.replace(policy, roots=(str(root),)), home)


def decide(path: Path | str, policy: Policy, home: Path, is_dir: bool = False) -> Decision:
    """One-shot form of `Matcher(policy, home).decide(path)`."""
    return Matcher(policy, home).decide(path, is_dir)
