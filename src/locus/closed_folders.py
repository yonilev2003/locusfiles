"""Closed folders: a folder that stays out of the index until the user opens it on purpose.

Downloads is the one closed folder (policy.CLOSED_FOLDER_NAMES). It mixes files the user wrote with files other people
wrote, and a parent root such as `~` used to cover it without a word. Now:

- `Matcher.decide` (policy.py) refuses everything at or below a closed folder, reason `closed_folder`, unless a
  `[[folder]]` entry in `<data dir>/policy.toml` names that folder or a folder inside it. An entry for a parent
  (`~`) or a `roots` list never opens it.
- The explicit act is `locus index ~/Downloads` (or `locus watch ~/Downloads`): `gate` shows counts, says what an
  assistant can then read, and asks once (`--yes` answers for scripts). On yes it appends ONE marked block to
  `policy.toml` (atomic write, the old file kept as `policy.toml.bak`). Hand-writing the same block does the same.
- An index built before this existed may already hold rows under Downloads. `migrate` opens the folder for those, once
  per collection, with one line on stderr, so the first `--prune` after the upgrade cannot drop them.

Nothing here prints to stdout. Everything works on the local disk; there is no network call.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
import tempfile
from collections import Counter
from datetime import date
from pathlib import Path
from typing import TextIO

from .config import Settings
from .ingest.embedder import expected_embedder_name
from .ingest.indexer import iter_files
from .ingest.store import ChromaStore
from .policy import (
    FolderProfile,
    Matcher,
    Policy,
    PolicyError,
    load_policy,
    matcher_for_folder,
    parse_policy,
)
from .preflight import DECLINED, NEEDS_YES, PROCEED

OPEN_PROFILE = "untrusted"  # label only: the assistant is told the folder may hold files written by other people
MARKER_FILE = "closed-folders.json"
BLOCK_BEGIN = "# --- locus: opened folder (added by Locus; delete these lines to close the folder again) ---"
BLOCK_END = "# --- end of locus block ---"
COUNT_LIMIT = 100_000  # files counted for a message; a bigger folder is reported as "more than this many"

# The one line on stderr when the upgrade opened a folder because the index already held rows from it.
MIGRATION_LINE = (
    "locus: {folder} is a closed folder from this version on. Your index already holds files from it, so Locus opened "
    "it for you (profile untrusted) and recorded that in {policy}. Delete the marked block there to close it."
)


def _today() -> str:
    return date.today().isoformat()


def tilde(path: Path | str, home: Path) -> str:
    """`~/Downloads` for a path below the home directory, else the path as given."""
    p = Path(path)
    try:
        rel = p.relative_to(home)
    except ValueError:
        return str(p)
    return "~" if not rel.parts else "~/" + rel.as_posix()


def _toml_string(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)  # a TOML basic string escapes exactly what JSON does


def opt_in_block(folder_text: str, note: str) -> str:
    """The block that opens *folder_text* (`~/...` or absolute): the existing `[[folder]]` schema, nothing new."""
    return (
        f"{BLOCK_BEGIN}\n# {note}\n[[folder]]\npath = {_toml_string(folder_text)}\n"
        f'profile = "{OPEN_PROFILE}"\n{BLOCK_END}\n'
    )


def needs_opening(folder: Path, policy: Policy, home: Path) -> bool:
    """True when *folder* itself is at or below a closed folder and no `[[folder]]` entry covers it. A folder whose
    sub-folder is open is still closed: the files in the folder itself stay closed until it is opened too."""
    return matcher_for_folder(folder, policy, home).decide(Path(folder).resolve() / "x", is_dir=False).reason == (
        "closed_folder"
    )


def write_policy_text(policy_file: Path, text: str) -> None:
    """Replace *policy_file* with *text* atomically (a temp file in the same folder, then a rename), after checking that
    the text still parses and keeping the old file as `<name>.bak`. Owner-only."""
    parse_policy(text)  # never write a policy this Locus cannot read back
    policy_file = Path(policy_file)
    policy_file.parent.mkdir(parents=True, exist_ok=True)
    if policy_file.exists():
        backup = policy_file.with_name(policy_file.name + ".bak")
        backup.write_bytes(policy_file.read_bytes())
        os.chmod(backup, 0o600)
    fd, tmp = tempfile.mkstemp(prefix=policy_file.name + ".", suffix=".tmp", dir=policy_file.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, policy_file)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def append_opt_in(policy_file: Path, folder: Path, note: str, home: Path) -> bool:
    """Append the block that opens *folder*. False (and nothing written) when an entry already covers it. Raises
    PolicyError when the existing file does not parse: Locus does not add to a file it cannot read."""
    policy_file = Path(policy_file)
    existing = policy_file.read_text(encoding="utf-8") if policy_file.exists() else ""
    if not needs_opening(folder, parse_policy(existing), home):
        return False
    if existing and not existing.endswith("\n"):
        existing += "\n"
    write_policy_text(policy_file, existing + ("\n" if existing else "") + opt_in_block(tilde(folder, home), note))
    return True


def count_openable(folder: Path, settings: Settings, policy: Policy, home: Path) -> tuple[int, Counter[str], int]:
    """(files, files by extension, folders) that opening *folder* would index: the same walk the index makes, with the
    folder open. Counts only; stops at COUNT_LIMIT files."""
    opened = dataclasses.replace(policy, folders=(*policy.folders, FolderProfile(tilde(folder, home), OPEN_PROFILE)))
    matcher = matcher_for_folder(folder, opened, home)
    kinds: Counter[str] = Counter()
    seen_dirs: set[Path] = set()
    for path in iter_files(Path(folder), settings, None, matcher):
        kinds[path.suffix.lower() or "(none)"] += 1
        seen_dirs.add(path.parent)
        if sum(kinds.values()) >= COUNT_LIMIT:
            break
    return sum(kinds.values()), kinds, len(seen_dirs)


def _kinds_text(kinds: Counter[str], top: int = 6) -> str:
    ranked = sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0]))
    shown = ", ".join(f"{ext.lstrip('.')} {n:,}" for ext, n in ranked[:top])
    rest = sum(n for _, n in ranked[top:])
    return shown + (f", other {rest:,}" if rest else "")


def gate(
    folder: Path,
    settings: Settings,
    policy: Policy,
    policy_file: Path,
    home: Path,
    *,
    assume_yes: bool,
    command: str,
    stdin: TextIO | None = None,
    out: TextIO | None = None,
) -> str:
    """Before indexing or watching *folder*: if it is a closed folder (or inside one) that is not open, say what opening
    means and ask once. PROCEED (also when nothing was closed), DECLINED or NEEDS_YES (no terminal and no --yes).
    On yes the block is written; the caller reloads the policy. *command* is the user's own command, for the message."""
    stdin = stdin if stdin is not None else sys.stdin
    out = out if out is not None else sys.stderr
    folder = Path(folder).expanduser().resolve()
    if not needs_opening(folder, policy, home):
        return PROCEED
    where = tilde(folder, home)
    files, kinds, folders = count_openable(folder, settings, policy, home)
    more = "more than " if files >= COUNT_LIMIT else ""
    print(
        f"{where} is a closed folder. Locus does not index it unless you open it, because it mixes your own files with "
        "files other people wrote, and any of them may hold text meant to steer an AI assistant.\n"
        f"Opening it would index {more}{files:,} file(s) in {folders:,} folder(s)"
        + (f" ({_kinds_text(kinds)})" if kinds else "")
        + ".\nAfter that an AI assistant you connect can read excerpts of those files and their names. Locus marks the "
        "folder untrusted when it hands the text over, which asks the assistant to be careful; it does not stop an "
        f"assistant from obeying text it receives.\nThis is recorded in {policy_file} (a marked block; delete it to "
        "close the folder again).",
        file=out,
    )
    if assume_yes:
        print("Confirmed with --yes.", file=out)
    elif not (hasattr(stdin, "isatty") and stdin.isatty()):
        print(
            f"\nNot opened: this is not an interactive terminal, so Locus cannot ask. To open it, re-run with --yes: "
            f"`{command}`.",
            file=out,
        )
        return NEEDS_YES
    else:
        print(f"\nOpen {where}? [y/N] ", end="", file=out, flush=True)
        try:
            answer = stdin.readline()
        except KeyboardInterrupt:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("Cancelled. Nothing was opened or indexed.", file=out)
            return DECLINED
    append_opt_in(policy_file, folder, f"opened by you on {_today()}: `{command}`", home)
    print(f"Opened {where} (profile {OPEN_PROFILE}).", file=out)
    return PROCEED


def hint_lines(root: Path, settings: Settings, policy: Policy, home: Path) -> list[str]:
    """After an index or dry run over *root*: one line for each closed folder inside it that was left out, with how many
    files it holds and the command that opens it. Counts only, no file names."""
    lines: list[str] = []
    root = Path(root).resolve()
    for closed in matcher_for_folder(root, policy, home).closed_dirs():
        try:
            closed.relative_to(root)
        except ValueError:
            continue
        if not closed.is_dir() or not needs_opening(closed, policy, home):
            continue
        files = count_openable(closed, settings, policy, home)[0]
        if files:
            where = tilde(closed, home)
            more = "more than " if files >= COUNT_LIMIT else ""
            lines.append(
                f"{more}{files:,} file(s) in {where} are closed: run `locus index {where}` to open (it asks first)."
            )
    return lines


def state_lines(policy: Policy, home: Path) -> list[str]:
    """For `locus doctor`: each closed folder and whether it is open. Names of folders only."""
    lines = []
    for closed in Matcher(policy, home).closed_dirs():
        where = tilde(closed, home)
        if needs_opening(closed, policy, home):
            lines.append(f"{where} is closed (open it with `locus index {where}`)")
        else:
            lines.append(f"{where} is open (profile {Matcher(policy, home).profile_of(closed)})")
    return lines


# --- the one-time migration ---------------------------------------------------------------------------------------


def _read_marker(data_dir: Path) -> dict:
    try:
        data = json.loads((data_dir / MARKER_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_marker(data_dir: Path, marker: dict) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=MARKER_FILE + ".", suffix=".tmp", dir=data_dir)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(marker, handle, indent=2, sort_keys=True)
    os.chmod(tmp, 0o600)
    os.replace(tmp, data_dir / MARKER_FILE)


def mark_if_new(settings: Settings, store: object) -> None:
    """A collection that is empty when it is opened was made under these rules, so there is nothing to carry over:
    record that, and the one-time migration never looks at it. Never raises."""
    try:
        data_dir = Path(settings.data_dir).expanduser()
        marker = _read_marker(data_dir)
        if settings.collection not in marker.get("collections", {}) and int(store.count()) == 0:  # type: ignore[attr-defined]
            marker.setdefault("collections", {})[settings.collection] = {"date": _today(), "opened": []}
            marker["version"] = 1
            _write_marker(data_dir, marker)
    except Exception:  # noqa: BLE001 - a bookkeeping file must never stop an index or a search
        return


def migrate(settings: Settings, home: Path, err: TextIO | None = None) -> str:
    """Open each closed folder the index already holds files from, once per collection. Returns what happened:
    "done" (nothing to open, or opened), "skipped" (done earlier, or there is no data folder yet), "unchecked" (the
    policy file or the index could not be read: nothing was changed, the next start tries again). Never raises."""
    err = err if err is not None else sys.stderr
    data_dir = Path(settings.data_dir).expanduser()
    marker = _read_marker(data_dir)
    done = marker.get("collections", {})
    if settings.collection in done or not data_dir.exists():
        return "skipped"
    try:
        policy_file = data_dir / "policy.toml"
        policy = load_policy(policy_file)
        opened: list[str] = []
        if (data_dir / "chroma").exists():
            store = _open_readonly(settings)
            if store is None:  # another embedder's collection or none at all: nothing of ours to protect
                pass
            else:
                for closed in Matcher(policy, home).closed_dirs():
                    if not needs_opening(closed, policy, home):
                        continue
                    spellings = dict.fromkeys((str(closed), os.path.realpath(closed)))
                    if any(store.has_path_under(s) for s in spellings):
                        note = f"opened by migration {_today()}, rows already indexed"
                        if append_opt_in(policy_file, closed, note, home):
                            opened.append(tilde(closed, home))
                            policy = load_policy(policy_file)
        marker.setdefault("collections", {})[settings.collection] = {"date": _today(), "opened": opened}
        marker["version"] = 1
        _write_marker(data_dir, marker)
        for where in opened:
            print(MIGRATION_LINE.format(folder=where, policy=policy_file), file=err)
        return "done"
    except (
        RuntimeError
    ):  # the collection was built with another embedder: nothing here can open it, and no marker is set
        return "unchecked"
    except (PolicyError, OSError) as exc:
        print(
            f"locus: the closed-folder check could not run ({type(exc).__name__}); nothing was changed and it will try "
            "again next time. `locus index --prune` could drop index entries for Downloads until it has run.",
            file=err,
        )
        return "unchecked"
    except Exception as exc:  # noqa: BLE001 - a start-up check must never stop the command
        print(f"locus: the closed-folder check failed ({type(exc).__name__}); nothing was changed.", file=err)
        return "unchecked"


def _open_readonly(settings: Settings) -> ChromaStore | None:
    try:
        return ChromaStore(settings.data_dir, settings.collection, expected_embedder_name(settings), create=False)
    except LookupError:
        return None
