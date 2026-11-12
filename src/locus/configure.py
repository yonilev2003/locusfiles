"""`locus setup --managed | --local`: choose the embedder in one step (grill fix 6, handover book §5).

Before this command a Locus plan meant setting LOCUS_EMBEDDER=managed and LOCUS_API_KEY by hand,
and an install that missed it fell through to the `local` default. `locus setup --managed`:

1. checks the index first: if the configured collection was built with another embedder it stops
   and says a re-index into a new collection is needed (constraint 4: one embedding model per
   collection, never switched silently). `--collection NAME` names that new collection;
2. takes the key from LOCUS_API_KEY when it is already set, else asks for it with getpass (never
   echoed). The key is never printed or logged, here or in any error;
3. checks it with the read-only account-status call that `locus_account_status` makes
   (support.fetch_account_status). That call spends nothing: no embedding request is made;
4. writes LOCUS_EMBEDDER (and the key, and LOCUS_COLLECTION when given) into the `.env` file the
   engine already reads (config.get_settings), creating it owner-only (0600) or updating it in
   place: every other line is kept as it was;
5. prints the next steps: index a folder, connect an AI app.

`locus setup --local` does steps 1, 4 and 5 for on-device embeddings; it needs no key and makes no
network call.

Exit codes: 0 saved; 1 the key was not accepted or could not be checked (nothing saved);
2 nothing saved because of the configuration (an index built with another embedder, no key and no
terminal to ask on, a value that is not a key or collection name, an invalid LOCUS_API_BASE).
"""

from __future__ import annotations

import http.client
import importlib.util
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
import urllib.error
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, TextIO

from dotenv import dotenv_values

from . import config, support
from .config import Settings, get_settings

EXIT_SAVED = 0
EXIT_NOT_ACCEPTED = 1
EXIT_CONFIG = 2

# What `locus setup` may write. Values are checked against these before anything is written.
# A Locus key is "locus_live_" + url-safe base64 (locus-web/lib/api-keys.ts); this accepts any
# token of url-safe characters so a test or staging key works too, and refuses spaces, quotes, "#"
# and "$", which would change how a .env line is read.
_KEY_RE = re.compile(r"^[A-Za-z0-9._~+/=-]{8,512}$")
# Chroma's own collection-name rule: 3-63 characters, letters, digits, ".", "_", "-", starting
# and ending with a letter or digit.
_COLLECTION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,61}[A-Za-z0-9]$")
# One assignment line of KEY in a .env file, with an optional "export " prefix.
_ASSIGN_RE = r"^(?P<lead>\s*(?:export\s+)?){key}\s*="

WATCHED = ("LOCUS_EMBEDDER", "LOCUS_API_KEY", "LOCUS_COLLECTION")


@dataclass(frozen=True)
class SetupResult:
    code: int
    env_file: Path | None = None


def env_file_to_write(cwd: Path | None = None) -> Path:
    """The `.env` whose values the engine uses when run from *cwd* (default: the current folder):
    the nearest `.env` in *cwd* or a parent folder (what get_settings loads first, as
    find_dotenv(usecwd=True) finds it), else a source checkout's own `.env` (get_settings' fallback
    for an editable install), else a new `.env` in *cwd*."""
    start = (Path(cwd) if cwd is not None else Path.cwd()).absolute()
    for folder in (start, *start.parents):
        candidate = folder / ".env"
        if candidate.is_file():
            return candidate
    if config._SOURCE_CHECKOUT is not None:
        return config._SOURCE_CHECKOUT / ".env"
    return start / ".env"


def update_env_file(path: Path, values: dict[str, str]) -> None:
    """Set each KEY=value of *values* in the .env file at *path*, creating it owner-only (0600).

    An existing file is updated in place: every assignment of a key in *values* (also an
    "export KEY=" one) gets the new value, keys it lacks are appended, and every other line,
    comments and blank lines included, stays as it was. The new text goes to a 0600 temporary file
    in the same folder that then replaces the old one, so the file is never half-written and never
    readable by others, even for a moment; an existing file loses any looser mode it had. A
    symlinked .env is written through to its target."""
    path = Path(path)
    if path.is_symlink():
        path = path.resolve()
    lines: list[str] = []
    if path.exists():
        with open(path, encoding="utf-8", newline="") as handle:  # newline="": keep CRLF lines as they are
            lines = handle.read().splitlines(keepends=True)
    newline = "\r\n" if lines and lines[0].endswith("\r\n") else "\n"
    remaining = dict(values)
    out: list[str] = []
    for line in lines:
        for key, value in values.items():
            match = re.match(_ASSIGN_RE.format(key=re.escape(key)), line)
            if match:
                ending = "\r\n" if line.endswith("\r\n") else ("\n" if line.endswith("\n") else "")
                line = f"{match.group('lead')}{key}={value}{ending}"
                remaining.pop(key, None)
                break
        out.append(line)
    if remaining:
        if out and not out[-1].endswith(("\n", "\r\n")):
            out[-1] += newline
        out.append(f"# written by `locus setup`{newline}")
        out.extend(f"{key}={value}{newline}" for key, value in remaining.items())
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".env.locus-", dir=path.parent)  # O_EXCL, mode 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write("".join(out))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _out(stream: TextIO | None) -> TextIO:
    return stream if stream is not None else sys.stdout


def _err(stream: TextIO | None) -> TextIO:
    return stream if stream is not None else sys.stderr


def _ask_key(prompt: str) -> str:
    """Read the key without echoing it (a seam for tests)."""
    import getpass

    return getpass.getpass(prompt)


def _interactive() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _target_embedder_name(settings: Settings, mode: str) -> str:
    from .ingest.embedder import expected_embedder_name

    return expected_embedder_name(replace(settings, embedder=mode))


def _locus_command() -> str:
    """The absolute path of the `locus` command (AI apps started from the Dock or Finder often do
    not see the shell's PATH), or plain `locus` when it cannot be found."""
    found = shutil.which("locus")
    if found:
        return os.path.abspath(found)  # not resolved: ~/.local/bin/locus survives a reinstall
    argv0 = Path(sys.argv[0]) if sys.argv and sys.argv[0] else None
    if argv0 is not None and argv0.name.startswith("locus") and argv0.is_file():
        return os.path.abspath(argv0)
    return "locus"


def _check_collection(settings: Settings, mode: str, collection: str, err: TextIO) -> bool:
    """False (and a message) when *collection* exists and was built with another embedder."""
    from .ingest.store import recorded_collection

    target = _target_embedder_name(settings, mode)
    info = recorded_collection(settings.data_dir, collection)
    if info is None:
        return True
    recorded, chunks = info
    if recorded is None or recorded == target:
        return True
    suggestion = f"{collection}-{mode}"
    if settings.embedder != mode:
        back = f"LOCUS_EMBEDDER={settings.embedder}"
    else:
        back = f"the embedder it was built with ({recorded})"
    print(
        f"locus setup: nothing was changed. Your index (collection {collection!r} in {settings.data_dir}, "
        f"{chunks} chunks) was built with {recorded!r}. {mode.capitalize()} embeddings ({target!r}) make "
        "different vectors, and one collection never mixes two embedding models, so switching needs a "
        "re-index into a new collection.\n"
        f"  To switch: locus setup --{mode} --collection {suggestion}\n"
        "  then:      locus index <folder>\n"
        f"Your current index stays as it is. To use it again later, set {back} and "
        f"LOCUS_COLLECTION={collection}.",
        file=err,
    )
    return False


def _shell_overrides(shell: dict[str, str | None], values: dict[str, str], err: TextIO) -> None:
    """Warn about variables set in the shell environment: they win over any .env file."""
    for key, wanted in values.items():
        current = shell.get(key)
        if current is None or current == wanted:
            continue
        shown = "a different value" if key == "LOCUS_API_KEY" else f"{key}={current}"
        print(
            f"locus setup: warning: {shown} is set in this shell's environment, and the environment wins "
            f"over the file. Remove it (`unset {key}`, and from your shell profile) or Locus keeps using it.",
            file=err,
        )


def _key_from_user(err: TextIO) -> str | None:
    if not _interactive():
        print(
            "locus setup: nothing was saved. No LOCUS_API_KEY is set and there is no terminal to ask on. "
            "Run `locus setup --managed` in a terminal, or set LOCUS_API_KEY in the environment for this one "
            "command.",
            file=err,
        )
        return None
    try:
        key = _ask_key("Locus API key (from your dashboard; input stays hidden): ")
    except EOFError:
        key = ""
    key = key.strip()
    if not key:
        print("locus setup: nothing was saved: no key was entered.", file=err)
        return None
    return key


def _describe_plan(data: dict[str, Any]) -> tuple[str, bool]:
    """('<tier> (<status>)', entitled) from an account-status answer; server text is shown
    printable-only and short, so it cannot put escape sequences on the terminal."""

    def shown(value: object, default: str) -> str:
        text = "".join(ch for ch in str(value or "") if ch.isprintable()).strip()[:32]
        return text or default

    plan = f"{shown(data.get('tier'), 'unknown plan')} ({shown(data.get('status'), 'unknown')})"
    return plan, support.account_is_entitled(data)


def _validate_key(settings: Settings, key: str, out: TextIO, err: TextIO) -> int | None:
    """None when the key may be saved, else the exit code. Prints what it found, never the key."""
    url = settings.hosted_url(settings.support_account_status_url)  # an invalid LOCUS_API_BASE stops here (exit 2)
    try:
        data = support.fetch_account_status(url, key, timeout=15)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            print(
                "locus setup: nothing was saved. Locus did not accept this key (it is mistyped or was "
                "revoked). Copy a current key from your dashboard and run `locus setup --managed --new-key`.",
                file=err,
            )
            return EXIT_NOT_ACCEPTED
        if exc.code == 404:
            print(
                "locus setup: warning: the key is valid, but Locus found no plan for this account, so "
                "managed embedding will refuse requests until one is active. Saving it anyway.",
                file=err,
            )
            return None
        reason = f"HTTP {exc.code}"
    except urllib.error.URLError as exc:
        reason = str(exc.reason)
    except (ValueError, OSError, http.client.HTTPException) as exc:  # unreadable answer, timeout, dropped line
        reason = type(exc).__name__
    else:
        plan, entitled = _describe_plan(data)
        print(f"Key accepted. Plan: {plan}.", file=out)
        if not entitled:
            billing = settings.billing_url or "your dashboard"
            print(
                f"locus setup: warning: this plan is not active right now, so managed embedding will refuse "
                f"requests until it is. Check billing at {billing}. Saving the key anyway.",
                file=err,
            )
        return None
    print(
        f"locus setup: nothing was saved. Could not check the key with Locus right now ({reason}). Try again "
        "in a moment, or add --skip-check to save it without checking.",
        file=err,
    )
    return EXIT_NOT_ACCEPTED


def _next_steps(settings: Settings, mode: str, env_file: Path, collection: str | None, out: TextIO) -> None:
    command = _locus_command()
    env: dict[str, str] = {"LOCUS_EMBEDDER": mode}
    if mode == "managed":
        env["LOCUS_API_KEY"] = "<your key>"  # a placeholder: the key itself is never printed
    # The app reads no .env, so it also needs every setting that decides which index it opens, when
    # one is set here (shell or .env): without them it opens the default collection in ~/.locus,
    # which is empty or was built with another embedder (e.g. after an earlier --collection switch).
    if collection is not None or os.environ.get("LOCUS_COLLECTION"):
        env["LOCUS_COLLECTION"] = collection or settings.collection
    if os.environ.get("LOCUS_DATA_DIR"):
        env["LOCUS_DATA_DIR"] = str(settings.data_dir)
    if mode == "local" and os.environ.get("LOCUS_LOCAL_MODEL"):
        env["LOCUS_LOCAL_MODEL"] = settings.local_model
    client_json = json.dumps({"mcpServers": {"locus": {"command": command, "args": ["serve"], "env": env}}})
    env_flags = " ".join(f"-e {shlex.quote(f'{k}={v}')}" for k, v in env.items())
    folder = env_file.parent
    print(f"\nSaved to {env_file} (readable only by you).", file=out)
    if config._SOURCE_CHECKOUT is not None and env_file == config._SOURCE_CHECKOUT / ".env":
        print("This source checkout reads it from any folder that has no .env of its own.", file=out)
    else:
        print(f"Locus reads it when you run `locus` in {folder} or a folder inside it.", file=out)
    print("\nNext:", file=out)
    print("  1. Index a folder:  locus index ~/Documents/notes", file=out)
    if mode == "managed":
        print(
            "     It shows what would be sent and asks first. On managed plans, chunk text and your search "
            "queries pass through Locus's relay to Voyage AI to be embedded.",
            file=out,
        )
    print('  2. Try a search:   locus query "a question your files answer"', file=out)
    print(
        "  3. Connect an AI app. Apps start `locus serve` from their own folder, where this file is not read, "
        "so give the app the same settings.",
        file=out,
    )
    print("     Claude Desktop or Cursor (MCP config file):", file=out)
    print(f"       {client_json}", file=out)
    print("     Claude Code:", file=out)
    print(f"       claude mcp add locus {env_flags} -- {shlex.quote(command)} serve", file=out)
    if mode == "managed":
        print("     Replace <your key> with the key you just set up.", file=out)
    if settings.api_base:
        print(f"     Guide for each app: {settings.api_base}/docs/connect", file=out)
    print("  4. Check everything: locus doctor", file=out)


def run_setup(
    mode: str,
    *,
    collection: str | None = None,
    new_key: bool = False,
    skip_check: bool = False,
    cwd: Path | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> SetupResult:
    """Configure *mode* ("managed" or "local") and return the exit code and the file written."""
    if mode not in ("managed", "local"):
        raise ValueError(f"unknown setup mode {mode!r}")
    stdout, stderr = _out(out), _err(err)
    # What the shell itself sets, read before get_settings() loads any .env into the environment.
    shell = {key: os.environ.get(key) for key in WATCHED}
    env_file = env_file_to_write(cwd)
    settings = get_settings()

    if collection is not None and not _COLLECTION_RE.match(collection):
        print(
            f"locus setup: nothing was saved: {collection!r} is not a collection name (3-63 letters, digits, "
            "'.', '_' or '-', starting and ending with a letter or digit).",
            file=stderr,
        )
        return SetupResult(EXIT_CONFIG)
    target_collection = collection or settings.collection
    if not _check_collection(settings, mode, target_collection, stderr):
        return SetupResult(EXIT_CONFIG)

    values: dict[str, str] = {"LOCUS_EMBEDDER": mode}
    if mode == "managed":
        if not skip_check:
            settings.hosted_url(settings.support_account_status_url)  # fail on a bad LOCUS_API_BASE before asking
        key = None if new_key else (os.environ.get("LOCUS_API_KEY") or _file_value(env_file, "LOCUS_API_KEY"))
        if key:
            key = key.strip()
            print("Using the LOCUS_API_KEY already set on this computer (add --new-key to enter another).", file=stdout)
        else:
            key = _key_from_user(stderr)
            if key is None:
                return SetupResult(EXIT_CONFIG)
        if not _KEY_RE.match(key):
            print(
                "locus setup: nothing was saved: that does not look like a Locus API key (it starts with "
                "locus_live_ and has no spaces or quotes). Copy it again from your dashboard and run "
                "`locus setup --managed --new-key`.",
                file=stderr,
            )
            return SetupResult(EXIT_CONFIG)
        if skip_check:
            print("Key not checked (--skip-check): `locus doctor` will tell you if it works.", file=stdout)
        else:
            refused = _validate_key(settings, key, stdout, stderr)
            if refused is not None:
                return SetupResult(refused)
        values["LOCUS_API_KEY"] = key
    else:
        if (
            importlib.util.find_spec("fastembed") is None and importlib.util.find_spec("sentence_transformers") is None
        ):  # a lookup only: importing would load the model stack
            print(
                "locus setup: note: on-device embeddings need the `local` extra "
                "(about 460 MB installed, with the model). "
                'Install it with `uv tool install --reinstall --python 3.12 "locusfiles[local]"` (or pipx: '
                '`pipx install --force "locusfiles[local]"`) before you index.',
                file=stderr,
            )
    if collection is not None:
        values["LOCUS_COLLECTION"] = collection

    update_env_file(env_file, values)
    _shell_overrides(shell, values, stderr)
    label = "managed embeddings (a Locus plan)" if mode == "managed" else "on-device embeddings"
    print(f"\nLocus is set up for {label}.", file=stdout)
    _next_steps(settings, mode, env_file, collection, stdout)
    return SetupResult(EXIT_SAVED, env_file)


def _file_value(path: Path, key: str) -> str | None:
    if not path.is_file():
        return None
    value = dotenv_values(path).get(key)
    return value or None
