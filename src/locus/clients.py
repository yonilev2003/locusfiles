"""`locus setup --client NAME`: put the Locus MCP entry into one AI app's config file, or print it.

By default nothing is written: the command prints the snippet and the file it belongs in, so it can be pasted by hand.
With `--write` it merges ONE key, `locus`, into the file after showing the path and the change and asking (`--yes`
skips the question for scripts). Everything else in the file is kept as parsed; the original is first copied to
`<file>.locus-backup-YYYYMMDDTHHMMSS` and the new text goes in through a temporary file in the same folder and a rename.
A file that does not parse (invalid JSON or TOML) is never touched: the snippet is printed instead.

Writing a configuration is not a statement that Locus has been tested with that app. Each entry in REGISTRY carries a
`tested` string copied from the compatibility record, and `--list` says so in its footer.

Kinds: json-mcpServers (a top-level "mcpServers" object), toml-codex (a `[mcp_servers.locus]` table, edited as text
after the file has been checked with tomllib), command-only (a command to run; Locus never runs it) and manual
(print-only: the config location is not settled or is edited inside the app).

The `locus` entry has no env block unless `--env NAME` is given: the value is read from the environment at write time
and never printed (the change is shown with the value hidden). Keys already in an existing `locus` entry are kept.
"""

from __future__ import annotations

import argparse
import codecs
import copy
import difflib
import json
import os
import re
import shlex
import shutil
import stat
import sys
import tempfile
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Any, TextIO

ENTRY = "locus"
DOCS_READ = "2026-11-17"
MACOS, LINUX, WINDOWS = "macos", "linux", "windows"
OS_NAMES = (MACOS, LINUX, WINDOWS)
OS_LABEL = {MACOS: "macOS", LINUX: "Linux", WINDOWS: "Windows"}

EXIT_OK = 0
EXIT_DECLINED = 1
EXIT_REFUSED = 2

# What `tested` may say. Copied from the compatibility record: "not tested" until a dated run is saved.
TESTED_RECORDED = "recorded run on 0.2.0 (2026-11-14, macOS)"
TESTED_EARLIER_ENGINE = "earlier engine only (0.1.0, 2026-10-01)"
TESTED_OLDER_RECORD = "older record only (pre-release, 2026-09-16)"
TESTED_NOT = "not tested"
TESTED_FORMS = (TESTED_RECORDED, TESTED_EARLIER_ENGINE, TESTED_OLDER_RECORD, TESTED_NOT)

FOOTER = "Setup writes a configuration. It does not mean Locus has been tested with that client."
QUIT_DESKTOP = (
    "Quit Claude Desktop completely first; the app rewrites its own config while it runs and can undo an edit; "
    "start it again afterwards."
)
RESTART_GENERIC = "Restart the app afterwards so that it reads the file."
PLACEHOLDER_COMMAND = {
    MACOS: "/absolute/path/to/locus",
    LINUX: "/absolute/path/to/locus",
    WINDOWS: "C:\\absolute\\path\\to\\locus.exe",
}

JSON_KIND = "json-mcpServers"
TOML_KIND = "toml-codex"
COMMAND_KIND = "command-only"
MANUAL_KIND = "manual"
KINDS = (JSON_KIND, TOML_KIND, COMMAND_KIND, MANUAL_KIND)


def _url(host: str, *parts: str) -> str:
    return "https://" + "/".join((host, *parts))


@dataclass(frozen=True)
class Location:
    """Where a config file lives: a base folder that depends on the OS and the path below it."""

    base: str  # home | appdata | xdg_config | copilot_home
    rel: tuple[str, ...]


def _everywhere(base: str, *rel: str) -> dict[str, Location]:
    return {name: Location(base, rel) for name in OS_NAMES}


@dataclass(frozen=True)
class Client:
    name: str
    label: str
    kind: str
    locations: Mapping[str, Location]
    docs: tuple[str, ...]
    tested: str
    notes: tuple[str, ...] = ()
    after: str = RESTART_GENERIC
    before: str = ""  # something to do before the file is written (shown with the change)
    snippet: str = ""  # "agents" for the AGENTS.md text; empty: derived from kind
    no_file: str = ""  # what the PATH column says when there is no file to write
    list_note: str = ""  # one line under the --list table

    @property
    def writes_files(self) -> bool:
        return self.kind in (JSON_KIND, TOML_KIND)


AGENTS_TEXT = """\
## Searching my files with Locus

When a question depends on my own files (notes, documents, plans, code I wrote), search them with Locus before you
answer:

    locus query "<the question>" --json

Answer only from what it returns and cite the file path of every result you use (each result has a `path` and a
`chunk_index`). If nothing relevant comes back, say so instead of guessing.
"""
AGENTS_NOTE = "The --json flag of `locus query` requires Locus 0.2.0 or later."

REGISTRY: tuple[Client, ...] = (
    Client(
        name="claude-code",
        label="Claude Code",
        kind=COMMAND_KIND,
        locations={},
        docs=(_url("code.claude.com", "docs", "en", "mcp"),),
        tested=TESTED_RECORDED,
        notes=(
            "There is no file to edit by default: Claude Code keeps its own list. Run the printed command yourself; "
            "Locus never runs `claude`.",
            "Inside Claude Code, `/mcp` lists the servers it has.",
        ),
        no_file="none: prints a command",
    ),
    Client(
        name="claude-desktop",
        label="Claude Desktop, local servers",
        kind=JSON_KIND,
        locations={
            MACOS: Location("home", ("Library", "Application Support", "Claude", "claude_desktop_config.json")),
            WINDOWS: Location("appdata", ("Claude", "claude_desktop_config.json")),
            LINUX: Location("xdg_config", ("Claude", "claude_desktop_config.json")),
        },
        docs=(
            _url("modelcontextprotocol.io", "docs", "develop", "connect-local-servers"),
            _url(
                "support.claude.com",
                "en",
                "articles",
                "10949351-getting-started-with-local-mcp-servers-on-claude-desktop",
            ),
        ),
        tested=TESTED_OLDER_RECORD,
        notes=("On Linux the folder is $XDG_CONFIG_HOME/Claude when that variable is set, else ~/.config/Claude.",),
        before=QUIT_DESKTOP,
        after="Start Claude Desktop again, open a new chat and look for the locus tools in the tools menu.",
    ),
    Client(
        name="cursor",
        label="Cursor",
        kind=JSON_KIND,
        locations=_everywhere("home", ".cursor", "mcp.json"),
        docs=(_url("cursor.com", "docs", "mcp"),),
        tested=TESTED_OLDER_RECORD,
        notes=(
            "This is the global file; a project can have its own .cursor/mcp.json, which this command does not touch.",
        ),
        after="Restart Cursor, or toggle the server off and on in Cursor Settings, MCP.",
    ),
    Client(
        name="codex",
        label="Codex CLI",
        kind=TOML_KIND,
        locations=_everywhere("home", ".codex", "config.toml"),
        docs=(_url("learn.chatgpt.com", "docs", "extend", "mcp"),),
        tested=TESTED_EARLIER_ENGINE,
        notes=(
            "Per the vendor page, the Codex IDE extension and the ChatGPT desktop app read the same file. Only the "
            "CLI has an earlier record; the others were not tested.",
            "The vendor page also offers `codex mcp add NAME -- command args`.",
        ),
    ),
    Client(
        name="copilot",
        label="GitHub Copilot, VS Code native and Copilot CLI",
        kind=JSON_KIND,
        locations=_everywhere("copilot_home", "mcp-config.json"),
        docs=(
            _url("code.visualstudio.com", "docs", "agents", "reference", "mcp-configuration"),
            _url("docs.github.com", "en", "copilot", "how-tos", "copilot-cli", "customize-copilot", "add-mcp-servers"),
        ),
        tested=TESTED_EARLIER_ENGINE,
        notes=(
            "VS Code and Copilot CLI share this file. The record is for Copilot CLI only (version 1.0.86, with "
            "Copilot's hosted model); VS Code with GitHub Copilot: not tested.",
            "The file is $COPILOT_HOME/mcp-config.json when COPILOT_HOME is set, else ~/.copilot/mcp-config.json. "
            "The older .vscode/mcp.json is deprecated and is not written.",
            "Only command, args and env are written; whether Copilot CLI wants more fields was not checked.",
        ),
    ),
    Client(
        name="gemini-cli",
        label="Gemini CLI",
        kind=JSON_KIND,
        locations=_everywhere("home", ".gemini", "settings.json"),
        docs=(_url("geminicli.com", "docs", "tools", "mcp-server"),),
        tested=TESTED_NOT,
        notes=(
            "Google's docs banner says Gemini CLI was replaced by Antigravity CLI for unpaid and Google One users on "
            "2026-06-18. If that is you, Gemini CLI may no longer be the tool you run. Antigravity CLI is a different "
            "tool with its own file, which this command does not write.",
            "settings.json holds other Gemini CLI settings too; only the locus entry is added or changed.",
        ),
        list_note="Google says Antigravity CLI replaced Gemini CLI for unpaid and Google One users on 2026-06-18.",
    ),
    Client(
        name="windsurf",
        label="Windsurf",
        kind=MANUAL_KIND,
        locations={},
        docs=(
            _url("docs.windsurf.com", "windsurf", "cascade", "mcp"),
            _url("code.visualstudio.com", "docs", "agents", "reference", "mcp-configuration"),
        ),
        tested=TESTED_NOT,
        notes=(
            "UNRESOLVED config path, so this client is print-only. VS Code's MCP reference lists "
            "~/.codeium/windsurf/mcp_config.json; Windsurf's own page lists the Devin-style paths "
            ".devin/mcp_config.json and ~/.config/devin/mcp_config.json. Locus does not know which one your version "
            "reads: check in Windsurf, then paste the snippet into that file.",
        ),
        no_file="UNRESOLVED: print only",
        list_note="the config path is UNRESOLVED (the vendor pages disagree), so setup only prints.",
    ),
    Client(
        name="lm-studio",
        label="LM Studio",
        kind=MANUAL_KIND,
        locations={},
        docs=(_url("lmstudio.ai", "docs", "app", "mcp"),),
        tested=TESTED_NOT,
        notes=(
            "Print-only: LM Studio edits its mcp.json from the app (Program tab, Install, Edit mcp.json). Paste the "
            "snippet there.",
            "The snippet uses the common mcpServers shape; check it against LM Studio's page when you paste it.",
        ),
        no_file="edited in the app: print only",
    ),
    Client(
        name="agents-md",
        label="AGENTS.md or CLAUDE.md text for a coding agent",
        kind=MANUAL_KIND,
        locations={},
        docs=(),
        tested=TESTED_NOT,
        notes=(AGENTS_NOTE,),
        snippet="agents",
        no_file="a text file you choose: print only",
    ),
)
BY_NAME = {c.name: c for c in REGISTRY}


# --------------------------------------------------------------------------------------------------------------------
# Paths


def current_os() -> str:
    if sys.platform == "darwin":
        return MACOS
    if sys.platform.startswith("win"):
        return WINDOWS
    return LINUX


def _pure(os_name: str) -> type[PurePath]:
    return PureWindowsPath if os_name == WINDOWS else PurePosixPath


def display_path(client: Client, os_name: str) -> str | None:
    """The config file as the vendor writes it (`~`, `%APPDATA%`), for any OS. None when there is no file."""
    loc = client.locations.get(os_name)
    if loc is None:
        return None
    win = os_name == WINDOWS
    roots = {
        "home": "%USERPROFILE%" if win else "~",
        "appdata": "%APPDATA%",
        "xdg_config": "~/.config",
        "copilot_home": "%USERPROFILE%\\.copilot" if win else "~/.copilot",
    }
    return str(_pure(os_name)(roots[loc.base], *loc.rel))


def resolve_path(client: Client, os_name: str, env: Mapping[str, str], home: str) -> str | None:
    """The config file on a computer with this *env* and *home* (strings of that OS). None when there is no file."""
    loc = client.locations.get(os_name)
    if loc is None:
        return None
    pure = _pure(os_name)
    if loc.base == "appdata":
        root = pure(env["APPDATA"]) if env.get("APPDATA") else pure(home, "AppData", "Roaming")
    elif loc.base == "xdg_config":
        xdg = env.get("XDG_CONFIG_HOME", "")
        root = pure(xdg) if xdg and pure(xdg).is_absolute() else pure(home, ".config")
    elif loc.base == "copilot_home":
        root = pure(env["COPILOT_HOME"]) if env.get("COPILOT_HOME") else pure(home, ".copilot")
    else:
        root = pure(home)
    return str(root.joinpath(*loc.rel))


def detect_command() -> str | None:
    """The absolute path of the `locus` command on this computer (AI apps often do not see the shell's PATH)."""
    found = shutil.which("locus")
    if found:
        return os.path.abspath(found)  # not resolved: ~/.local/bin/locus survives a reinstall
    argv0 = Path(sys.argv[0]) if sys.argv and sys.argv[0] else None
    if argv0 is not None and argv0.name.startswith("locus") and argv0.is_file():
        return os.path.abspath(argv0)
    return None


# --------------------------------------------------------------------------------------------------------------------
# Entries and snippets


def _masked(entry: Any, fresh: Mapping[str, str] | None = None) -> Any:
    """*entry* with every env value hidden: a value just taken from the environment says where it came from."""
    if not isinstance(entry, dict):
        return entry
    out = dict(entry)
    env = out.get("env")
    if isinstance(env, dict):
        out["env"] = {k: (f"<value of {k} from your environment>" if fresh and k in fresh else "<hidden>") for k in env}
    return out


def _entry(command: str, env_names: Sequence[str] = (), values: Mapping[str, str] | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {"command": command, "args": ["serve"]}
    if env_names:
        entry["env"] = {
            n: (values[n] if values is not None else f"<value of {n} from your environment>") for n in env_names
        }
    return entry


def _json_text(value: Any, indent: Any = 2) -> str:
    return json.dumps(value, indent=indent, ensure_ascii=False)


def _toml_key(key: str) -> str:
    return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else _toml_str(key)


def _toml_str(value: str) -> str:
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")  # JSON escapes are valid TOML ones


class _Unrenderable(ValueError):
    pass


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return _toml_str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return (
            "{ " + ", ".join(f"{_toml_key(k)} = {_toml_value(v)}" for k, v in value.items()) + " }" if value else "{}"
        )
    raise _Unrenderable(type(value).__name__)


def _toml_lines(path: list[str], table: Mapping[str, Any]) -> list[str]:
    lines = ["[" + ".".join(_toml_key(p) for p in path) + "]"]
    subtables = []
    for key, value in table.items():
        if isinstance(value, dict):
            subtables.append((key, value))
        else:
            lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
    for key, value in subtables:
        lines += ["", *_toml_lines([*path, key], value)]
    return lines


def _toml_block(entry: Mapping[str, Any]) -> str:
    return "\n".join(_toml_lines(["mcp_servers", ENTRY], entry)) + "\n"


def _json_snippet(entry: Mapping[str, Any]) -> str:
    text = _json_text({"mcpServers": {ENTRY: entry}})
    return re.sub(r'\[\s+"serve"\s+\]', '["serve"]', text)  # the form the help articles show


def _quote_command(command: str, os_name: str) -> str:
    if os_name == WINDOWS:
        return f'"{command}"' if re.search(r"\s", command) else command
    return shlex.quote(command)


def _claude_code_command(command: str, env_names: Sequence[str], os_name: str) -> str:
    parts = ["claude", "mcp", "add", "--scope", "user", ENTRY]
    for name in env_names:
        parts += ["-e", f"{name}=<your value>"]
    return " ".join(parts) + f" -- {_quote_command(command, os_name)} serve"


def snippet_for(client: Client, command: str, env_names: Sequence[str], os_name: str) -> str:
    entry = _entry(command, env_names)
    if client.snippet == "agents":
        return AGENTS_TEXT.rstrip("\n")
    if client.kind == COMMAND_KIND:
        return _claude_code_command(command, env_names, os_name)
    if client.kind == TOML_KIND:
        return _toml_block(entry).rstrip("\n")
    return _json_snippet(entry)


# --------------------------------------------------------------------------------------------------------------------
# Planning a change to a file


@dataclass
class Plan:
    action: str  # add | replace | remove | noop | refuse
    message: str = ""  # why it is a noop or a refusal
    old_view: str = ""
    new_view: str = ""
    new_bytes: bytes = b""
    others: int = 0  # other entries in the file, which stay as they are
    exists: bool = False
    source: bytes | None = None  # the file as it was read for this plan (None: it did not exist)


def _refuse(path: Path, why: str, exists: bool = True) -> Plan:
    return Plan("refuse", f"{path} {why}", exists=exists)


def _read_text(path: Path) -> tuple[bool, str, bool, bytes | None] | str:
    """(exists, text, had_bom, raw bytes), or a reason the file cannot be used."""
    if path.is_dir():
        return "is a folder, not a file"
    if not path.exists():
        return False, "", False, None
    raw = path.read_bytes()
    try:
        return True, raw.decode("utf-8-sig"), raw.startswith(codecs.BOM_UTF8), raw
    except UnicodeDecodeError:
        return "is not UTF-8 text"


def _indent_of(text: str) -> Any:
    match = re.search(r"^([ \t]+)\S", text, re.MULTILINE)
    if not match:
        return 2
    return "\t" if match.group(1).startswith("\t") else match.group(1)


def _merge_entry(current: Any, command: str, fresh: Mapping[str, str]) -> dict[str, Any]:
    """The `locus` entry after setup: command and args set, `--env` names added, anything else kept as it was."""
    base = dict(current) if isinstance(current, dict) else {}
    desired = {**base, "command": command, "args": ["serve"]}
    if fresh:
        old_env = base.get("env")
        desired["env"] = {**(old_env if isinstance(old_env, dict) else {}), **fresh}
    return desired


def _json_view(entry: Any) -> str:
    return f'"{ENTRY}": ' + _json_text(entry)


def _plan_json(path: Path, command: str, fresh: Mapping[str, str], remove: bool) -> Plan:
    read = _read_text(path)
    if isinstance(read, str):
        return _refuse(path, read)
    exists, text, bom, raw_before = read
    try:
        data = json.loads(text) if text.strip() else {}
    except ValueError as exc:
        where = f" (line {exc.lineno}, column {exc.colno})" if isinstance(exc, json.JSONDecodeError) else ""
        return _refuse(path, f"is not valid JSON{where}")
    if not isinstance(data, dict):
        return _refuse(path, "does not hold a JSON object at the top level")
    servers = data.get("mcpServers", {})
    if not isinstance(servers, dict):
        return _refuse(path, 'has an "mcpServers" key that is not an object')
    others = sum(1 for k in servers if k != ENTRY)
    current = servers.get(ENTRY)
    if remove:
        if ENTRY not in servers:
            return Plan("noop", f'{path} has no "{ENTRY}" entry: nothing to remove.', exists=exists, others=others)
        del servers[ENTRY]
        old_view, new_view, action = _json_view(_masked(current)), "", "remove"
    else:
        desired = _merge_entry(current, command, fresh)
        if current == desired:
            return Plan(
                "noop", f'{path} already has this "{ENTRY}" entry: nothing changed.', exists=exists, others=others
            )
        servers[ENTRY] = desired
        data["mcpServers"] = servers
        old_view = _json_view(_masked(current)) if current is not None else ""
        new_view, action = _json_view(_masked(desired, fresh)), ("replace" if current is not None else "add")
    out = _json_text(data, _indent_of(text))
    if text.endswith("\n") or not exists:
        out += "\n"
    if "\r\n" in text:
        out = out.replace("\n", "\r\n")
    raw = (codecs.BOM_UTF8 if bom else b"") + out.encode("utf-8")
    return Plan(
        action, old_view=old_view, new_view=new_view, new_bytes=raw, others=others, exists=exists, source=raw_before
    )


_HEADER = re.compile(r"^[ \t]*(\[\[?)(.*?)(\]\]?)[ \t]*(#.*)?$")
_KEY_PART = re.compile(r"""\s*(?:"((?:[^"\\]|\\.)*)"|'([^']*)'|([A-Za-z0-9_-]+))\s*(\.|$)""")


def _key_path(body: str) -> list[str] | None:
    parts: list[str] = []
    pos = 0
    while True:
        match = _KEY_PART.match(body, pos)
        if not match:
            return None
        quoted, literal, bare, sep = match.groups()
        try:
            parts.append(
                json.loads(f'"{quoted}"') if quoted is not None else (literal if literal is not None else bare)
            )
        except ValueError:
            return None
        pos = match.end()
        if not sep:
            return parts


def _locus_spans(lines: list[str]) -> list[tuple[int, int]]:
    """Line ranges of `[mcp_servers.locus]` and its sub-tables: from the header to the next table header."""
    headers: list[tuple[int, bool]] = []  # (line, is a locus table)
    for number, line in enumerate(lines):
        match = _HEADER.match(line.rstrip("\r\n"))
        if not match:
            continue
        is_locus = False
        if match.group(1) == "[" and match.group(3) == "]":
            path = _key_path(match.group(2))
            is_locus = path is not None and path[:2] == ["mcp_servers", ENTRY]
        headers.append((number, is_locus))
    spans = []
    for index, (start, is_locus) in enumerate(headers):
        if is_locus:
            end = headers[index + 1][0] if index + 1 < len(headers) else len(lines)
            spans.append((start, end))
    return spans


def _norm(data: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(data)
    if out.get("mcp_servers") == {}:
        del out["mcp_servers"]
    return out


def _plan_toml(path: Path, command: str, fresh: Mapping[str, str], remove: bool) -> Plan:
    read = _read_text(path)
    if isinstance(read, str):
        return _refuse(path, read)
    exists, text, bom, raw_before = read
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return _refuse(path, f"is not valid TOML ({exc})")
    servers = data.get("mcp_servers", {})
    if not isinstance(servers, dict):
        return _refuse(path, 'has an "mcp_servers" key that is not a table')
    others = sum(1 for k in servers if k != ENTRY)
    current = servers.get(ENTRY)
    if remove:
        if ENTRY not in servers:
            return Plan(
                "noop", f"{path} has no [mcp_servers.{ENTRY}] table: nothing to remove.", exists=exists, others=others
            )
        desired = None
    else:
        desired = _merge_entry(current, command, fresh)
        if current == desired:
            return Plan(
                "noop",
                f"{path} already has this [mcp_servers.{ENTRY}] table: nothing changed.",
                exists=exists,
                others=others,
            )
    lines = text.splitlines(keepends=True)
    spans = _locus_spans(lines)
    if current is not None and not spans:
        return _refuse(
            path, f"defines {ENTRY} in a form this command does not edit (not a [mcp_servers.{ENTRY}] table)"
        )
    nl = "\r\n" if "\r\n" in text else "\n"
    try:
        block = _toml_block(desired).replace("\n", nl) if desired is not None else ""
        old_view = _toml_block(_masked(current)) if current is not None else ""
        new_view = _toml_block(_masked(desired, fresh)) if desired is not None else ""
    except _Unrenderable as exc:
        return _refuse(path, f"has a {ENTRY} entry with a value of type {exc} that this command cannot rewrite")
    if spans:
        out: list[str] = []
        done = 0
        inserted = False
        for start, end in spans:
            out += lines[done:start]
            if desired is not None and not inserted:
                out.append(block + (nl if end < len(lines) else ""))
                inserted = True
            done = end
        out += lines[done:]
        new_text = "".join(out)
        if spans[-1][1] == len(lines):
            new_text = new_text.rstrip() + nl if new_text.strip() else ""
    else:
        new_text = text if not text or text.endswith("\n") else text + nl
        new_text += (nl if new_text.strip() else "") + block
    expected = copy.deepcopy(data)
    if desired is None:
        expected.get("mcp_servers", {}).pop(ENTRY, None)
    else:
        expected.setdefault("mcp_servers", {})[ENTRY] = desired
    try:
        parsed = tomllib.loads(new_text)
    except tomllib.TOMLDecodeError:
        parsed = None
    if parsed is None or _norm(parsed) != _norm(expected):
        return _refuse(path, "has a layout this command could not edit safely (its own check of the result failed)")
    action = "remove" if remove else ("replace" if current is not None else "add")
    raw = (codecs.BOM_UTF8 if bom else b"") + new_text.encode("utf-8")
    return Plan(
        action, old_view=old_view, new_view=new_view, new_bytes=raw, others=others, exists=exists, source=raw_before
    )


def plan_change(client: Client, path: Path, command: str, fresh: Mapping[str, str], remove: bool) -> Plan:
    if client.kind == TOML_KIND:
        return _plan_toml(path, command, fresh, remove)
    return _plan_json(path, command, fresh, remove)


# --------------------------------------------------------------------------------------------------------------------
# Writing


def _now() -> datetime:
    return datetime.now()


def backup_path(target: Path, now: datetime) -> Path:
    base = target.with_name(f"{target.name}.locus-backup-{now:%Y%m%dT%H%M%S}")
    candidate, number = base, 1
    while candidate.exists():
        candidate = base.with_name(f"{base.name}-{number}")
        number += 1
    return candidate


def write_plan(path: Path, plan: Plan, secret: bool) -> Path | None:
    """Copy the file to its backup name, then replace it atomically. Returns the backup path (None for a new file)."""
    target = path.resolve() if path.is_symlink() else path  # a symlinked config is written through to its target
    target.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    mode = 0o600 if secret else 0o644
    if (target.read_bytes() if target.is_file() else None) != plan.source:
        raise OSError("the file changed after Locus read it (the app may be running); run the command again")
    if target.exists():
        mode = stat.S_IMODE(target.stat().st_mode)
        backup = backup_path(target, _now())
        shutil.copy2(target, backup)
        if backup.read_bytes() != target.read_bytes():
            backup.unlink(missing_ok=True)
            raise OSError(f"the copy of {target} did not match the original; nothing was changed")
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.locus-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(plan.new_bytes)
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return backup


# --------------------------------------------------------------------------------------------------------------------
# The command


def _interactive() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _ask(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:
        return ""


def add_client_arguments(parser: argparse.ArgumentParser, mode: Any) -> None:
    """`--client` and `--list` join the --managed/--local group; the rest are options of --client."""
    mode.add_argument(
        "--client",
        metavar="NAME",
        default=None,
        help="connect an AI app: print (default) or write its MCP entry; see --list for the names",
    )
    mode.add_argument(
        "--list",
        dest="list_clients",
        action="store_true",
        help="list the clients --client knows, with their config file and how far Locus has tested each",
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--print",
        dest="print_only",
        action="store_true",
        help="with --client: print the snippet and the file it goes in, write nothing (the default)",
    )
    action.add_argument(
        "--write",
        action="store_true",
        help="with --client: write the entry after showing the file and the change and asking (backup first)",
    )
    parser.add_argument("-y", "--yes", action="store_true", help="with --write: do not ask (for scripts)")
    parser.add_argument(
        "--remove", action="store_true", help="with --client: remove only the locus entry (with --write)"
    )
    parser.add_argument(
        "--os",
        dest="target_os",
        choices=["auto", *OS_NAMES],
        default="auto",
        help="show the paths of another OS (printing only); default: this computer's",
    )
    parser.add_argument(
        "--command", metavar="PATH", default=None, help="the absolute path of locus to write (default: found)"
    )
    parser.add_argument(
        "--env",
        metavar="KEY",
        action="append",
        default=None,
        help="add an env entry named KEY to the locus entry; its value is read from your environment when "
        "writing and never printed (repeatable)",
    )


def client_flags_used(args: argparse.Namespace) -> bool:
    return bool(
        args.print_only or args.write or args.yes or args.remove or args.command or args.env or args.target_os != "auto"
    )


def _say(text: str = "", out: TextIO | None = None) -> None:
    print(text, file=out if out is not None else sys.stdout)


def _list(os_name: str, out: TextIO) -> None:
    rows = [(c.name, display_path(c, os_name) or c.no_file, c.tested) for c in REGISTRY]
    widths = [max(len(r[i]) for r in rows) for i in range(3)]
    head = ("NAME", f"CONFIG FILE ({OS_LABEL[os_name]})", "TESTED BY LOCUS")
    widths = [max(w, len(h)) for w, h in zip(widths, head, strict=True)]
    _say("Clients that `locus setup --client NAME` knows:", out)
    _say(out=out)
    for row in (head, *rows):
        _say("  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)).rstrip(), out)
    _say(out=out)
    for client in REGISTRY:
        if client.list_note:
            _say(f"{client.name}: {client.list_note}", out)
    _say(out=out)
    _say(FOOTER, out)


def _diff_lines(old: str, new: str) -> list[str]:
    diff = difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=200)
    return [line for line in diff if not line.startswith(("---", "+++", "@@"))]


def _describe_file(client: Client, path: Path, plan: Plan, os_name: str, out: TextIO) -> None:
    state = "exists" if plan.exists else "does not exist yet; it will be created"
    if not plan.exists and not path.parent.exists():
        state += f" (the folder {path.parent} does not exist either, which can mean the app is not installed)"
    _say(f"File: {path} ({state})", out)
    if plan.others:
        if plan.others == 1:
            _say("1 other server entry in it stays as it is.", out)
        else:
            _say(f"{plan.others} other server entries in it stay as they are.", out)


def _env_error(names: Sequence[str]) -> str | None:
    for name in names:
        if "=" in name:
            return "--env takes the name of a variable only, never NAME=value; nothing was written."
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            return f"--env {name!r} is not a variable name; nothing was written."
    return None


def _print_client(
    client: Client,
    command: str,
    placeholder: bool,
    env_names: Sequence[str],
    os_name: str,
    here: str,
    remove: bool,
    out: TextIO,
) -> None:
    _say(f"{client.label} (--client {client.name})", out)
    _say(f"Tested by Locus: {client.tested}", out)
    for url in client.docs:
        _say(f"Docs (read {DOCS_READ}): {url}", out)
    path_text = display_path(client, os_name)
    if client.writes_files and path_text is not None:
        real = resolve_path(client, os_name, os.environ, str(Path.home())) if os_name == here else None
        shown = real or path_text
        extra = ""
        if real is not None:
            extra = " (exists)" if Path(real).exists() else " (does not exist yet)"
        elif os_name != here:
            extra = f" (printed for {OS_LABEL[os_name]}; this computer runs {OS_LABEL[here]})"
        _say(f"Config file: {shown}{extra}", out)
    elif client.no_file:
        _say(f"Config file: {client.no_file}", out)
    if remove:
        _say(out=out)
        if client.kind == COMMAND_KIND:
            _say("To remove it, run:", out)
            _say("  claude mcp remove locus --scope user", out)
        elif client.writes_files:
            _say(
                f'To remove it, delete the "{ENTRY}" entry from that file by hand, or add --write to have Locus do it.',
                out,
            )
        else:
            _say("Print-only client: remove the entry by hand where you pasted it.", out)
    else:
        _say(out=out)
        if client.kind == COMMAND_KIND:
            _say("Run this in a terminal:", out)
        elif client.snippet == "agents":
            _say("Add this to an AGENTS.md or CLAUDE.md that your coding agent reads:", out)
        elif client.kind == TOML_KIND:
            _say("Add this table to that file (keep everything already in it):", out)
        else:
            _say('Add this under "mcpServers" in that file; keep any servers already there:', out)
        _say(out=out)
        _say(snippet_for(client, command, env_names, os_name), out)
    if placeholder:
        _say(out=out)
        _say(
            "The path of locus above is a placeholder: replace it with the full path (`command -v locus`, on Windows "
            "`where locus`), or pass --command PATH.",
            out,
        )
    if client.before:
        _say(out=out)
        _say(client.before, out)
    if client.notes:
        _say(out=out)
        for note in client.notes:
            _say(f"Note: {note}", out)
    if client.writes_files and not env_names and not remove:
        _say(out=out)
        _say(
            "Apps do not read your .env file. To pass a setting such as LOCUS_EMBEDDER, add --env NAME (its value is "
            "copied from your environment when writing and never printed).",
            out,
        )
    _say(out=out)
    if client.writes_files and os_name == here:
        flag = "--remove --write" if remove else "--write"
        _say("Nothing was written. To have Locus do it, with a backup of the file first:", out)
        _say(f"  locus setup --client {client.name} {flag}", out)
    elif client.writes_files:
        _say("Nothing was written. Writing is only possible for the OS this computer runs.", out)
    else:
        _say("Nothing was written.", out)


def run_client_setup(args: argparse.Namespace, out: TextIO | None = None, err: TextIO | None = None) -> int:
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    if not args.client and not args.list_clients:
        _say("error: --print, --write, --yes, --remove, --os, --command and --env go with --client NAME.", err)
        return EXIT_REFUSED
    if args.collection or args.new_key or args.skip_check:
        _say("error: --collection, --new-key and --skip-check go with --managed or --local only.", err)
        return EXIT_REFUSED
    here = current_os()
    os_name = here if args.target_os == "auto" else args.target_os
    if args.list_clients:
        if args.write or args.print_only or args.yes or args.remove or args.command or args.env:
            _say("error: --list takes only --os.", err)
            return EXIT_REFUSED
        _list(os_name, out)
        return EXIT_OK
    client = BY_NAME.get(args.client.strip().lower())
    if client is None:
        _say(f"error: unknown client {args.client!r}. Known: {', '.join(BY_NAME)} (see --list).", err)
        return EXIT_REFUSED
    env_names = list(dict.fromkeys(args.env or []))
    problem = _env_error(env_names)
    if problem:
        _say(f"error: {problem}", err)
        return EXIT_REFUSED
    if args.yes and not args.write:
        _say("error: --yes goes with --write.", err)
        return EXIT_REFUSED
    if args.remove and env_names:
        _say("error: --env does not go with --remove.", err)
        return EXIT_REFUSED

    command, placeholder = args.command, False
    if command:
        command = os.path.expanduser(command) if os_name == here else command
        if not _pure(os_name)(command).is_absolute():
            _say(f"error: --command must be an absolute path ({OS_LABEL[os_name]} style), not {command!r}.", err)
            return EXIT_REFUSED
    else:
        command = detect_command() if os_name == here else None
        if command is None:
            command, placeholder = PLACEHOLDER_COMMAND[os_name], True

    if not args.write:
        _print_client(client, command, placeholder, env_names, os_name, here, args.remove, out)
        if os_name == here and args.command and not Path(command).exists() and client.kind != MANUAL_KIND:
            _say(f"Note: {command} does not exist on this computer.", out)
        return EXIT_OK

    # --write from here on
    if not client.writes_files:
        why = "has no config file for Locus to edit" if client.kind == COMMAND_KIND else "is print-only"
        _say(f"error: {client.label} {why}, so --write does nothing here. Printing instead:", err)
        _say(out=err)
        _print_client(client, command, placeholder, env_names, os_name, here, args.remove, out)
        return EXIT_REFUSED
    if os_name != here:
        _say(f"error: --os {os_name} can only print: this computer runs {OS_LABEL[here]}. Nothing was written.", err)
        return EXIT_REFUSED
    if not args.remove and (placeholder or not Path(command).is_file()):
        problem = "the locus command was not found" if placeholder else f"{command} is not a file"
        _say(f"error: {problem}; pass --command PATH (the full path of locus). Nothing was written.", err)
        return EXIT_REFUSED
    fresh: dict[str, str] = {}
    missing = []
    for name in env_names:
        value = os.environ.get(name, "")
        if value:
            fresh[name] = value
        else:
            missing.append(name)
    if missing:
        _say(f"error: not set (or empty) in your environment: {', '.join(missing)}. Nothing was written.", err)
        return EXIT_REFUSED

    path = Path(resolve_path(client, os_name, os.environ, str(Path.home())) or "")
    try:
        plan = plan_change(client, path, command, fresh, args.remove)
    except OSError as exc:
        _say(f"error: could not read {path}: {exc.strerror or exc}. Nothing was written.", err)
        return EXIT_REFUSED
    if plan.action == "refuse":
        _say(f"error: {plan.message}. Nothing was written; the file is untouched.", err)
        _say("Add the entry by hand instead:", err)
        _say(out=out)
        _say(snippet_for(client, command, env_names, os_name), out)
        return EXIT_REFUSED
    if plan.action == "noop":
        _say(plan.message, out)
        return EXIT_OK

    _say(f"{client.label} (--client {client.name}); tested by Locus: {client.tested}", out)
    _describe_file(client, path, plan, os_name, out)
    verb = {"add": "Add", "replace": "Replace", "remove": "Remove"}[plan.action]
    _say(f'{verb} the "{ENTRY}" entry (- removed, + added; secret values are never shown):', out)
    for line in _diff_lines(plan.old_view, plan.new_view):
        _say(f"  {line}", out)
    if plan.exists:
        _say(f"A copy of the current file is saved first, next to it, as {path.name}.locus-backup-<date>T<time>.", out)
    if client.before:
        _say(out=out)
        _say(client.before, out)
    if not args.yes:
        if not _interactive():
            _say(
                "error: --write asks before it changes a file and there is no terminal to ask on; "
                "run it in a terminal, or add --yes. Nothing was written.",
                err,
            )
            return EXIT_REFUSED
        if _ask("Write this change? [y/N] ").strip().lower() not in ("y", "yes"):
            _say("Nothing was written.", out)
            return EXIT_DECLINED
    try:
        backup = write_plan(path, plan, secret=bool(fresh))
    except OSError as exc:
        _say(f"error: could not write {path}: {exc.strerror or exc}. The original is as it was.", err)
        return EXIT_REFUSED
    _say(f"{'Removed the entry from' if plan.action == 'remove' else 'Wrote'} {path}.", out)
    if backup is not None:
        _say(f"Backup of the file as it was: {backup}", out)
    _say(client.after, out)
    return EXIT_OK


# --------------------------------------------------------------------------------------------------------------------
# docs/clients.md (the same table; tests keep the file equal to this text)


def render_docs_markdown() -> str:
    lines = [
        "# Clients that `locus setup --client` knows",
        "",
        "`locus setup --client NAME` prints the MCP entry for one AI app and the file it belongs in. With `--write` it "
        "adds the `locus` entry to that file after asking, keeping a dated backup of the file first. "
        "`locus setup --list` prints this table.",
        "",
        f'**{FOOTER}** The column "Tested by Locus" says what record exists; read it as that and nothing more.',
        "",
        "| Name | Config file | Tested by Locus |",
        "|---|---|---|",
    ]
    for client in REGISTRY:
        if client.locations:
            where = "<br>".join(
                f"{OS_LABEL[n]}: `{display_path(client, n)}`" for n in OS_NAMES if n in client.locations
            )
        else:
            where = client.no_file
        lines.append(f"| `{client.name}` ({client.label}) | {where} | {client.tested} |")
    lines += ["", "## Notes", ""]
    for client in REGISTRY:
        docs = "; ".join(client.docs)
        lines.append(
            f"- **{client.name}**: "
            + " ".join(client.notes)
            + (f" Vendor pages (read {DOCS_READ}): {docs}." if docs else "")
        )
    lines += [
        "",
        "## What `--write` does",
        "",
        "- Shows the file and the change, with secret values hidden, and asks (`--yes` skips the question).",
        "- Copies the file to `<file>.locus-backup-YYYYMMDDTHHMMSS`, then replaces it through a temporary file.",
        "- Adds or replaces only the `locus` entry. A file that does not parse is left alone; the snippet is printed.",
        "- Running it twice changes nothing the second time. `--remove --write` removes only the `locus` entry.",
        "- `--env NAME` adds an env entry whose value is read from your environment when writing; it is never printed.",
        "",
    ]
    return "\n".join(lines)


__all__ = [
    "BY_NAME",
    "REGISTRY",
    "Client",
    "add_client_arguments",
    "client_flags_used",
    "run_client_setup",
]
