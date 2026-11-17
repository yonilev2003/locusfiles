"""`locus setup --client NAME | --list`: print or write the MCP entry for an AI app (clients.py).

Offline and confined to a temporary home: every test points HOME, Path.home() and the OS-specific variables at a folder
under tmp_path, so no real client config is ever read or written. The platform is mocked where a test needs another OS.
"""

from __future__ import annotations

import io
import json
import os
import re
import stat
import subprocess
import tomllib
from datetime import datetime
from pathlib import Path

import pytest

from locus import cli, clients, config, licence
from locus.config import get_settings

JSON_CLIENTS = ["claude-desktop", "cursor", "copilot", "gemini-cli"]
FILE_CLIENTS = [*JSON_CLIENTS, "codex"]
NO_FILE_CLIENTS = ["claude-code", "windsurf", "lm-studio", "agents-md"]


class _Tty(io.StringIO):
    def __init__(self, answer: str = "", tty: bool = True) -> None:
        super().__init__(answer)
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """A temporary home, a temporary data folder, macOS as the platform, and no terminal on stdin."""
    folder = tmp_path / "home"
    folder.mkdir()
    for name in ("APPDATA", "XDG_CONFIG_HOME", "COPILOT_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(folder))
    monkeypatch.setenv("USERPROFILE", str(folder))
    monkeypatch.setenv("LOCUS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: folder))
    monkeypatch.setattr(config, "_SOURCE_CHECKOUT", None)
    monkeypatch.setattr(clients, "current_os", lambda: "macos")
    monkeypatch.setattr("sys.stdin", _Tty("", tty=False))
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    yield folder
    get_settings.cache_clear()


@pytest.fixture
def exe(tmp_path):
    path = tmp_path / "bin" / "locus"
    path.parent.mkdir()
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return str(path)


@pytest.fixture
def run(capsys):
    def go(*argv: str) -> tuple[int, str, str]:
        with pytest.raises(SystemExit) as exc:
            cli.main(["setup", *argv])
        captured = capsys.readouterr()
        return exc.value.code, captured.out, captured.err

    return go


def config_path(name: str, home: Path, os_name: str = "macos") -> Path:
    return Path(clients.resolve_path(clients.BY_NAME[name], os_name, os.environ, str(home)))


def write_json(path: Path, data: object, indent: object = 2) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(data, indent=indent) + "\n").encode()
    path.write_bytes(raw)
    return raw


def backups(path: Path) -> list[Path]:
    return sorted(path.parent.glob(path.name + ".locus-backup-*"))


def tree(folder: Path) -> list[str]:
    return sorted(str(p.relative_to(folder)) for p in folder.rglob("*"))


OTHER_SERVERS = {
    "other": {"command": "/usr/bin/other", "args": ["--x"], "env": {"TOKEN": "keep-this-token"}},
    "second": {"url": "https://example.test/mcp"},
}


# --- the registry -------------------------------------------------------------------------------------------


def test_tested_strings_are_the_recorded_forms_and_follow_the_matrix():
    assert {c.name: c.tested for c in clients.REGISTRY} == {
        "claude-code": "recorded run on 0.2.0 (2026-11-14, macOS)",
        "claude-desktop": "older record only (pre-release, 2026-09-16)",
        "cursor": "older record only (pre-release, 2026-09-16)",
        "codex": "earlier engine only (0.1.0, 2026-10-01)",
        "copilot": "earlier engine only (0.1.0, 2026-10-01)",
        "gemini-cli": "not tested",
        "windsurf": "not tested",
        "lm-studio": "not tested",
        "agents-md": "not tested",
    }
    assert all(c.tested in clients.TESTED_FORMS for c in clients.REGISTRY)


def test_no_registry_text_claims_that_an_app_works():
    for client in clients.REGISTRY:
        text = " ".join([client.label, client.tested, client.no_file, client.list_note, *client.notes, client.after])
        assert not re.search(r"works (with|on|in)|compatible|supported", text, re.I), client.name
    assert not re.search(r"works (with|on|in)|compatible", clients.render_docs_markdown(), re.I)


def test_every_client_names_a_kind_and_dated_https_docs():
    assert clients.DOCS_READ == "2026-11-17"
    for client in clients.REGISTRY:
        assert client.kind in clients.KINDS
        assert all(url.startswith("https://") for url in client.docs)
        assert (client.kind in ("json-mcpServers", "toml-codex")) == bool(client.locations)


def test_the_first_set_of_clients_is_there():
    assert list(clients.BY_NAME) == [
        "claude-code",
        "claude-desktop",
        "cursor",
        "codex",
        "copilot",
        "gemini-cli",
        "windsurf",
        "lm-studio",
        "agents-md",
    ]


# --- paths per client and OS --------------------------------------------------------------------------------

MAC = ("/Users/me", {})
LIN = ("/home/me", {})
WIN = ("C:\\Users\\me", {"APPDATA": "C:\\Users\\me\\AppData\\Roaming"})
XDG = ("/home/me", {"XDG_CONFIG_HOME": "/xdg"})
LINUX_DESKTOP = "/home/me/.config/Claude/claude_desktop_config.json"
WINDOWS_DESKTOP = "C:\\Users\\me\\AppData\\Roaming\\Claude\\claude_desktop_config.json"


@pytest.mark.parametrize(
    ("name", "os_name", "where", "expected"),
    [
        ("claude-desktop", "macos", MAC, "/Users/me/Library/Application Support/Claude/claude_desktop_config.json"),
        ("claude-desktop", "linux", LIN, "/home/me/.config/Claude/claude_desktop_config.json"),
        ("claude-desktop", "linux", XDG, "/xdg/Claude/claude_desktop_config.json"),
        ("claude-desktop", "linux", ("/home/me", {"XDG_CONFIG_HOME": "relative"}), LINUX_DESKTOP),
        ("claude-desktop", "windows", WIN, WINDOWS_DESKTOP),
        ("claude-desktop", "windows", ("C:\\Users\\me", {}), WINDOWS_DESKTOP),
        ("cursor", "macos", MAC, "/Users/me/.cursor/mcp.json"),
        ("cursor", "linux", LIN, "/home/me/.cursor/mcp.json"),
        ("cursor", "windows", WIN, "C:\\Users\\me\\.cursor\\mcp.json"),
        ("codex", "macos", MAC, "/Users/me/.codex/config.toml"),
        ("codex", "linux", LIN, "/home/me/.codex/config.toml"),
        ("codex", "windows", WIN, "C:\\Users\\me\\.codex\\config.toml"),
        ("copilot", "macos", MAC, "/Users/me/.copilot/mcp-config.json"),
        ("copilot", "linux", ("/home/me", {"COPILOT_HOME": "/opt/copilot"}), "/opt/copilot/mcp-config.json"),
        ("copilot", "windows", WIN, "C:\\Users\\me\\.copilot\\mcp-config.json"),
        ("gemini-cli", "macos", MAC, "/Users/me/.gemini/settings.json"),
        ("gemini-cli", "linux", LIN, "/home/me/.gemini/settings.json"),
        ("gemini-cli", "windows", WIN, "C:\\Users\\me\\.gemini\\settings.json"),
    ],
)  # fmt: skip
def test_resolved_config_path_per_client_and_os(name, os_name, where, expected):
    home, env = where
    assert clients.resolve_path(clients.BY_NAME[name], os_name, env, home) == expected


@pytest.mark.parametrize(
    ("name", "os_name", "expected"),
    [
        ("claude-desktop", "macos", "~/Library/Application Support/Claude/claude_desktop_config.json"),
        ("claude-desktop", "linux", "~/.config/Claude/claude_desktop_config.json"),
        ("claude-desktop", "windows", "%APPDATA%\\Claude\\claude_desktop_config.json"),
        ("cursor", "windows", "%USERPROFILE%\\.cursor\\mcp.json"),
        ("codex", "linux", "~/.codex/config.toml"),
        ("copilot", "macos", "~/.copilot/mcp-config.json"),
        ("copilot", "windows", "%USERPROFILE%\\.copilot\\mcp-config.json"),
        ("gemini-cli", "macos", "~/.gemini/settings.json"),
    ],
)
def test_displayed_config_path_is_the_vendors_own_form(name, os_name, expected):
    assert clients.display_path(clients.BY_NAME[name], os_name) == expected


@pytest.mark.parametrize("name", NO_FILE_CLIENTS)
@pytest.mark.parametrize("os_name", clients.OS_NAMES)
def test_clients_without_a_file_have_no_path(name, os_name):
    assert clients.display_path(clients.BY_NAME[name], os_name) is None
    assert clients.resolve_path(clients.BY_NAME[name], os_name, {}, "/h") is None


# --- --list and --print write nothing -----------------------------------------------------------------------


def test_list_prints_the_table_with_the_plain_footer_and_writes_nothing(run, home):
    code, out, _ = run("--list")
    assert code == 0
    for client in clients.REGISTRY:
        assert client.name in out and client.tested in out
    assert "~/Library/Application Support/Claude/claude_desktop_config.json" in out
    assert out.rstrip().endswith(
        "Setup writes a configuration. It does not mean Locus has been tested with that client."
    )
    assert "Antigravity" in out and "UNRESOLVED" in out
    assert tree(home) == []


def test_list_for_another_os_shows_that_os_paths(run):
    code, out, _ = run("--list", "--os", "windows")
    assert code == 0 and "%APPDATA%\\Claude\\claude_desktop_config.json" in out and "CONFIG FILE (Windows)" in out


@pytest.mark.parametrize("flags", [[], ["--print"]])
@pytest.mark.parametrize("name", FILE_CLIENTS)
def test_print_is_the_default_and_writes_nothing(run, home, exe, name, flags):
    code, out, _ = run("--client", name, "--command", exe, *flags)
    path = config_path(name, home)
    assert code == 0
    assert str(path) in out and exe in out and "serve" in out and "Nothing was written" in out
    assert tree(home) == []


@pytest.mark.parametrize("name", JSON_CLIENTS)
def test_the_printed_json_snippet_is_the_entry(run, exe, name):
    _, out, _ = run("--client", name, "--command", exe)
    snippet = out[out.index("{") : out.rindex("}") + 1]
    assert json.loads(snippet) == {"mcpServers": {"locus": {"command": exe, "args": ["serve"]}}}


def test_the_printed_toml_snippet_is_the_table(run, exe):
    _, out, _ = run("--client", "codex", "--command", exe)
    block = out[out.index("[mcp_servers.locus]") :].split("\n\n")[0]
    assert tomllib.loads(block) == {"mcp_servers": {"locus": {"command": exe, "args": ["serve"]}}}


def test_claude_code_prints_the_command_and_never_runs_anything(run, exe, monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("setup must not run a program")

    for name in ("run", "Popen", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, boom)
    monkeypatch.setattr(os, "system", boom)
    code, out, _ = run("--client", "claude-code", "--command", exe)
    assert code == 0
    assert f"claude mcp add --scope user locus -- {exe} serve" in out
    assert "recorded run on 0.2.0 (2026-11-14, macOS)" in out
    code, out, err = run("--client", "claude-code", "--write", "--yes", "--command", exe)
    assert code == 2 and "no config file" in err
    code, out, _ = run("--client", "claude-code", "--remove", "--command", exe)
    assert code == 0 and "claude mcp remove locus --scope user" in out


def test_claude_code_command_quotes_a_path_with_spaces(run, tmp_path):
    spaced = tmp_path / "my bin" / "locus"
    spaced.parent.mkdir()
    spaced.write_text("")
    _, out, _ = run("--client", "claude-code", "--command", str(spaced))
    assert f"-- '{spaced}' serve" in out


def test_agents_md_prints_the_documented_command_and_the_version_line(run):
    code, out, _ = run("--client", "agents-md")
    assert code == 0
    assert 'locus query "<the question>" --json' in out and "cite the file path" in out
    assert "requires Locus 0.2.0 or later" in out
    code, _, err = run("--client", "agents-md", "--write")
    assert code == 2 and "print-only" in err


def test_gemini_and_windsurf_notes_appear(run, home):
    _, out, _ = run("--client", "gemini-cli")
    assert "Antigravity CLI" in out and "2026-06-18" in out and "not tested" in out
    code, out, _ = run("--client", "windsurf")
    assert code == 0
    assert "UNRESOLVED" in out and "~/.codeium/windsurf/mcp_config.json" in out and ".devin/mcp_config.json" in out
    assert "mcpServers" in out
    code, _, err = run("--client", "windsurf", "--write", "--yes")
    assert code == 2 and "print-only" in err
    assert tree(home) == []


def test_lm_studio_is_print_only_and_says_where_the_file_is_edited(run, home):
    code, out, _ = run("--client", "lm-studio")
    assert code == 0 and "Program tab" in out and "mcpServers" in out
    code, _, err = run("--client", "lm-studio", "--write", "--yes")
    assert code == 2 and tree(home) == []


def test_claude_desktop_print_says_to_quit_the_app_first(run):
    _, out, _ = run("--client", "claude-desktop")
    assert clients.QUIT_DESKTOP in out
    assert (
        out.count("Quit Claude Desktop completely first; the app rewrites its own config while it runs and can undo")
        == 1
    )


def test_os_windows_prints_a_windows_path_and_json_escapes_the_backslashes(run, home):
    command = "C:\\Users\\me\\AppData\\Local\\Programs\\locus\\locus.exe"
    code, out, _ = run("--client", "claude-desktop", "--os", "windows", "--command", command)
    assert code == 0
    assert "%APPDATA%\\Claude\\claude_desktop_config.json" in out
    snippet = out[out.index("{") : out.rindex("}") + 1]
    assert json.loads(snippet)["mcpServers"]["locus"]["command"] == command
    assert '"C:\\\\Users\\\\me' in snippet
    assert tree(home) == []


def test_os_other_than_this_one_can_only_print(run, home):
    code, _, err = run("--client", "cursor", "--os", "windows", "--write", "--yes", "--command", "C:\\x\\locus.exe")
    assert code == 2 and "can only print" in err and tree(home) == []


def test_os_windows_without_a_command_prints_a_placeholder(run):
    code, out, _ = run("--client", "cursor", "--os", "windows")
    assert code == 0 and "C:\\\\absolute\\\\path\\\\to\\\\locus.exe" in out and "placeholder" in out


def test_copilot_print_names_the_shared_file_and_not_the_deprecated_one(run, home, exe):
    _, out, _ = run("--client", "copilot", "--command", exe)
    assert str(home / ".copilot" / "mcp-config.json") in out
    assert "VS Code with GitHub Copilot: not tested" in out
    assert "is deprecated and is not written" in out


# --- writing ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", JSON_CLIENTS)
def test_write_creates_a_missing_file_after_yes(run, home, exe, name):
    path = config_path(name, home)
    code, out, _ = run("--client", name, "--write", "--yes", "--command", exe)
    assert code == 0 and "Wrote" in out
    assert json.loads(path.read_text()) == {"mcpServers": {"locus": {"command": exe, "args": ["serve"]}}}
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert backups(path) == []
    assert [p.name for p in path.parent.iterdir()] == [path.name]  # no stray temporary file


@pytest.mark.parametrize("name", JSON_CLIENTS)
def test_write_merges_only_locus_and_backs_up_the_original(run, home, exe, name):
    path = config_path(name, home)
    original = write_json(path, {"theme": "dark", "mcpServers": OTHER_SERVERS, "tail": [1, 2]}, indent=4)
    code, out, _ = run("--client", name, "--write", "--yes", "--command", exe)
    assert code == 0
    data = json.loads(path.read_text())
    assert data["theme"] == "dark" and data["tail"] == [1, 2]
    assert {k: v for k, v in data["mcpServers"].items() if k != "locus"} == OTHER_SERVERS
    assert data["mcpServers"]["locus"] == {"command": exe, "args": ["serve"]}
    assert path.read_text().startswith('{\n    "theme"')  # the file's own four-space indentation
    [backup] = backups(path)
    assert backup.read_bytes() == original
    assert re.fullmatch(re.escape(path.name) + r"\.locus-backup-\d{8}T\d{6}", backup.name)
    assert str(backup) in out and "2 other server entries in it stay as they are" in out


def test_the_change_is_shown_and_secret_values_in_other_entries_are_not(run, home, exe):
    path = config_path("cursor", home)
    write_json(path, {"mcpServers": OTHER_SERVERS})
    _, out, err = run("--client", "cursor", "--write", "--yes", "--command", exe)
    assert str(path) in out and '+"locus": {' in out
    assert "keep-this-token" not in out + err


def test_second_run_changes_nothing(run, home, exe):
    path = config_path("cursor", home)
    write_json(path, {"mcpServers": OTHER_SERVERS})
    run("--client", "cursor", "--write", "--yes", "--command", exe)
    after_first = path.read_bytes()
    files = tree(home)
    code, out, _ = run("--client", "cursor", "--write", "--yes", "--command", exe)
    assert code == 0 and "nothing changed" in out
    assert path.read_bytes() == after_first and tree(home) == files and len(backups(path)) == 1


def test_a_different_command_replaces_only_the_locus_entry(run, home, exe, tmp_path):
    path = config_path("cursor", home)
    write_json(
        path, {"mcpServers": {**OTHER_SERVERS, "locus": {"command": "/old/locus", "args": ["serve"], "timeout": 5}}}
    )
    code, out, _ = run("--client", "cursor", "--write", "--yes", "--command", exe)
    data = json.loads(path.read_text())
    assert code == 0 and "Replace" in out
    assert data["mcpServers"]["locus"] == {"command": exe, "args": ["serve"], "timeout": 5}  # the user's own key stays
    assert {k: v for k, v in data["mcpServers"].items() if k != "locus"} == OTHER_SERVERS
    assert '-  "command": "/old/locus"' in out


def test_backup_names_do_not_collide_within_one_second(run, home, exe, tmp_path, monkeypatch):
    monkeypatch.setattr(clients, "_now", lambda: datetime(2026, 11, 17, 10, 15, 0))
    path = config_path("cursor", home)
    write_json(path, {"mcpServers": {}})
    other = tmp_path / "bin" / "locus2"
    other.write_text("")
    run("--client", "cursor", "--write", "--yes", "--command", exe)
    run("--client", "cursor", "--write", "--yes", "--command", str(other))
    assert [b.name for b in backups(path)] == [
        "mcp.json.locus-backup-20261117T101500",
        "mcp.json.locus-backup-20261117T101500-1",
    ]


@pytest.mark.parametrize("name", FILE_CLIENTS)
def test_a_file_that_changes_while_the_question_is_open_is_not_overwritten(run, home, exe, monkeypatch, name):
    path = config_path(name, home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n" if name != "codex" else "# before\n")
    edited = b'{"mcpServers": {"added-meanwhile": {"command": "x"}}}\n' if name != "codex" else b"# the app edited it\n"

    def answer(prompt: str) -> str:
        path.write_bytes(edited)  # the app rewrites its own file while the user is reading the question
        return "y"

    monkeypatch.setattr(clients, "_interactive", lambda: True)
    monkeypatch.setattr(clients, "_ask", answer)
    code, _, err = run("--client", name, "--write", "--command", exe)
    assert code == 2 and "changed after Locus read it" in err and "run the command again" in err
    assert path.read_bytes() == edited and backups(path) == []
    assert [p.name for p in path.parent.iterdir()] == [path.name]  # no temporary file left behind


def test_a_file_that_appears_while_the_question_is_open_is_not_overwritten(run, home, exe, monkeypatch):
    path = config_path("cursor", home)

    def answer(prompt: str) -> str:
        write_json(path, {"mcpServers": {"other": {"command": "/usr/bin/other"}}})
        return "y"

    monkeypatch.setattr(clients, "_interactive", lambda: True)
    monkeypatch.setattr(clients, "_ask", answer)
    code, _, err = run("--client", "cursor", "--write", "--command", exe)
    assert code == 2 and "changed after Locus read it" in err
    assert json.loads(path.read_text()) == {"mcpServers": {"other": {"command": "/usr/bin/other"}}}


@pytest.mark.parametrize("name", JSON_CLIENTS)
@pytest.mark.parametrize(
    "bad",
    ["{ not json", '{"mcpServers": [1]}', "[1, 2]", "// a comment\n{}\n", '{"mcpServers": {"a": 1},}'],
    ids=["broken", "list", "array", "comment", "comma"],
)
def test_unparseable_json_is_refused_and_the_snippet_is_printed(run, home, exe, name, bad):
    path = config_path(name, home)
    path.parent.mkdir(parents=True)
    path.write_text(bad)
    code, out, err = run("--client", name, "--write", "--yes", "--command", exe)
    assert code == 2 and "Nothing was written" in err
    assert path.read_text() == bad and backups(path) == []
    assert '"mcpServers"' in out and exe in out  # the manual snippet


def test_invalid_toml_is_refused(run, home, exe):
    path = config_path("codex", home)
    path.parent.mkdir(parents=True)
    path.write_text('model = "gpt"\n[mcp_servers.x\ncommand = \n')
    code, out, err = run("--client", "codex", "--write", "--yes", "--command", exe)
    assert code == 2 and "not valid TOML" in err
    assert "model" in path.read_text() and backups(path) == [] and "[mcp_servers.locus]" in out


def test_a_folder_where_the_file_should_be_is_refused(run, home, exe):
    path = config_path("cursor", home)
    path.mkdir(parents=True)
    code, _, err = run("--client", "cursor", "--write", "--yes", "--command", exe)
    assert code == 2 and "is a folder" in err


def test_an_empty_json_file_is_treated_as_an_empty_object(run, home, exe):
    path = config_path("cursor", home)
    path.parent.mkdir(parents=True)
    path.write_text("")
    code, _, _ = run("--client", "cursor", "--write", "--yes", "--command", exe)
    assert code == 0 and json.loads(path.read_text())["mcpServers"]["locus"]["command"] == exe


def test_a_byte_order_mark_and_windows_line_endings_survive(run, home, exe):
    path = config_path("cursor", home)
    path.parent.mkdir(parents=True)
    path.write_bytes(
        b"\xef\xbb\xbf" + json.dumps({"mcpServers": {"a": {"command": "x"}}}, indent=2).replace("\n", "\r\n").encode()
    )
    code, _, _ = run("--client", "cursor", "--write", "--yes", "--command", exe)
    raw = path.read_bytes()
    assert (
        code == 0 and raw.startswith(b"\xef\xbb\xbf{\r\n") and b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")
    )
    assert json.loads(raw.decode("utf-8-sig"))["mcpServers"]["a"] == {"command": "x"}


def test_a_symlinked_config_is_written_through(run, home, exe, tmp_path):
    real = tmp_path / "dotfiles" / "mcp.json"
    write_json(real, {"mcpServers": OTHER_SERVERS})
    path = config_path("cursor", home)
    path.parent.mkdir(parents=True)
    path.symlink_to(real)
    code, _, _ = run("--client", "cursor", "--write", "--yes", "--command", exe)
    assert code == 0 and path.is_symlink()
    assert json.loads(real.read_text())["mcpServers"]["locus"]["command"] == exe
    assert len(backups(real)) == 1


# --- TOML ---------------------------------------------------------------------------------------------------

CODEX_BEFORE = """\
# my codex settings
model = "gpt-5"   # keep this comment

[projects."/work"]
trust_level = "trusted"

[mcp_servers.other]
command = "/usr/bin/other"

[mcp_servers.other.env]
TOKEN = "keep-this-token"
"""


def test_toml_append_keeps_the_rest_of_the_file_as_it_was(run, home, exe):
    path = config_path("codex", home)
    path.parent.mkdir(parents=True)
    path.write_text(CODEX_BEFORE)
    code, out, _ = run("--client", "codex", "--write", "--yes", "--command", exe)
    text = path.read_text()
    assert code == 0
    assert text.startswith(CODEX_BEFORE)  # everything before is untouched, comments and spacing included
    assert text[len(CODEX_BEFORE) :] == f'\n[mcp_servers.locus]\ncommand = "{exe}"\nargs = ["serve"]\n'
    assert tomllib.loads(text)["mcp_servers"]["locus"] == {"command": exe, "args": ["serve"]}
    assert tomllib.loads(text)["mcp_servers"]["other"] == tomllib.loads(CODEX_BEFORE)["mcp_servers"]["other"]
    [backup] = backups(path)
    assert backup.read_text() == CODEX_BEFORE
    assert "keep-this-token" not in out


def test_toml_creates_a_missing_file(run, home, exe):
    path = config_path("codex", home)
    code, _, _ = run("--client", "codex", "--write", "--yes", "--command", exe)
    assert code == 0 and path.read_text() == f'[mcp_servers.locus]\ncommand = "{exe}"\nargs = ["serve"]\n'


def test_toml_replace_keeps_position_neighbours_and_comments(run, home, exe):
    path = config_path("codex", home)
    path.parent.mkdir(parents=True)
    before = (
        '# top\n\n[mcp_servers.a]\ncommand = "a"\n\n'
        "# the locus table\n[mcp_servers.locus]\n"
        'command = "/old/locus"\nargs = ["serve"]\nstartup_timeout_sec = 30\n\n'
        '[mcp_servers.locus.env]\nLOCUS_EMBEDDER = "local"\n\n'
        '[mcp_servers.z]\ncommand = "z"\n# end\n'
    )
    path.write_text(before)
    code, out, _ = run("--client", "codex", "--write", "--yes", "--command", exe)
    text = path.read_text()
    assert code == 0
    data = tomllib.loads(text)["mcp_servers"]
    assert data["locus"] == {
        "command": exe,
        "args": ["serve"],
        "startup_timeout_sec": 30,
        "env": {"LOCUS_EMBEDDER": "local"},
    }
    assert data["a"] == {"command": "a"} and data["z"] == {"command": "z"}
    assert text.startswith('# top\n\n[mcp_servers.a]\ncommand = "a"\n\n# the locus table\n[mcp_servers.locus]')
    assert text.endswith('[mcp_servers.z]\ncommand = "z"\n# end\n')
    assert text.count("[mcp_servers.locus]") == 1
    assert backups(path)[0].read_text() == before
    assert "<hidden>" in out and '"local"' not in out


def test_toml_second_run_changes_nothing(run, home, exe):
    path = config_path("codex", home)
    run("--client", "codex", "--write", "--yes", "--command", exe)
    first = path.read_bytes()
    code, out, _ = run("--client", "codex", "--write", "--yes", "--command", exe)
    assert code == 0 and "nothing changed" in out and path.read_bytes() == first and backups(path) == []


def test_toml_locus_defined_another_way_is_refused(run, home, exe):
    path = config_path("codex", home)
    path.parent.mkdir(parents=True)
    path.write_text('[mcp_servers]\nlocus = { command = "x" }\n')
    code, out, err = run("--client", "codex", "--write", "--yes", "--command", exe)
    assert code == 2 and "does not edit" in err and path.read_text() == '[mcp_servers]\nlocus = { command = "x" }\n'
    assert "[mcp_servers.locus]" in out


def test_toml_escapes_a_windows_style_path(tmp_path):
    entry = {"command": "C:\\Program Files\\Locus\\locus.exe", "args": ["serve"]}
    assert tomllib.loads(clients._toml_block(entry))["mcp_servers"]["locus"] == entry


# --- remove -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", JSON_CLIENTS)
def test_remove_without_write_prints_and_changes_nothing(run, home, exe, name):
    path = config_path(name, home)
    original = write_json(path, {"mcpServers": {**OTHER_SERVERS, "locus": {"command": exe, "args": ["serve"]}}})
    code, out, _ = run("--client", name, "--remove", "--command", exe)
    assert code == 0 and "--remove --write" in out
    assert path.read_bytes() == original and backups(path) == []


@pytest.mark.parametrize("name", JSON_CLIENTS)
def test_remove_write_removes_only_the_locus_entry(run, home, exe, name):
    path = config_path(name, home)
    original = write_json(
        path,
        {"keep": True, "mcpServers": {"first": {"command": "1"}, "locus": {"command": exe}, "last": {"command": "3"}}},
    )
    code, out, _ = run("--client", name, "--remove", "--write", "--yes")
    assert code == 0 and "Removed the entry" in out
    assert json.loads(path.read_text()) == {
        "keep": True,
        "mcpServers": {"first": {"command": "1"}, "last": {"command": "3"}},
    }
    assert backups(path)[0].read_bytes() == original


def test_remove_when_there_is_nothing_to_remove(run, home):
    path = config_path("cursor", home)
    code, out, _ = run("--client", "cursor", "--remove", "--write", "--yes")
    assert code == 0 and "nothing to remove" in out and tree(home) == []
    original = write_json(path, {"mcpServers": OTHER_SERVERS})
    code, out, _ = run("--client", "cursor", "--remove", "--write", "--yes")
    assert code == 0 and "nothing to remove" in out and path.read_bytes() == original and backups(path) == []


def test_remove_toml_removes_the_table_and_its_sub_tables(run, home, exe):
    path = config_path("codex", home)
    path.parent.mkdir(parents=True)
    path.write_text(CODEX_BEFORE)
    run("--client", "codex", "--write", "--yes", "--command", exe, "--env", "PATH")
    code, _, _ = run("--client", "codex", "--remove", "--write", "--yes")
    assert code == 0
    assert path.read_text() == CODEX_BEFORE
    assert len(backups(path)) == 2


# --- asking before writing ----------------------------------------------------------------------------------


def test_without_a_terminal_and_without_yes_nothing_is_written(run, home, exe):
    code, _, err = run("--client", "cursor", "--write", "--command", exe)
    assert code == 2 and "no terminal" in err and "--yes" in err and tree(home) == []


def test_the_question_shows_the_path_the_change_and_the_desktop_warning(run, home, exe, monkeypatch, capsys):
    seen = {}

    def answer(prompt: str) -> str:
        seen["prompt"], seen["shown"] = prompt, capsys.readouterr().out
        return "y"

    monkeypatch.setattr(clients, "_interactive", lambda: True)
    monkeypatch.setattr(clients, "_ask", answer)
    path = config_path("claude-desktop", home)
    code, _, _ = run("--client", "claude-desktop", "--write", "--command", exe)
    assert code == 0 and path.is_file()
    assert str(path) in seen["shown"] and '+"locus": {' in seen["shown"] and clients.QUIT_DESKTOP in seen["shown"]
    assert seen["prompt"].startswith("Write this change?")


@pytest.mark.parametrize("reply", ["", "n", "no", "maybe"])
def test_anything_but_yes_declines_and_writes_nothing(run, home, exe, monkeypatch, reply):
    monkeypatch.setattr(clients, "_interactive", lambda: True)
    monkeypatch.setattr(clients, "_ask", lambda prompt: reply)
    code, out, _ = run("--client", "cursor", "--write", "--command", exe)
    assert code == 1 and "Nothing was written" in out and tree(home) == []


# --- the environment ----------------------------------------------------------------------------------------


def test_env_value_is_read_at_write_time_and_never_printed(run, home, exe, monkeypatch):
    monkeypatch.setenv("LOCUS_TEST_VALUE", "s3cret-value-123")
    code, out, err = run("--client", "cursor", "--command", exe, "--env", "LOCUS_TEST_VALUE")
    assert (
        code == 0 and "s3cret-value-123" not in out + err and "<value of LOCUS_TEST_VALUE from your environment>" in out
    )
    assert tree(home) == []
    path = config_path("cursor", home)
    code, out, err = run("--client", "cursor", "--write", "--yes", "--command", exe, "--env", "LOCUS_TEST_VALUE")
    assert code == 0 and "s3cret-value-123" not in out + err
    assert json.loads(path.read_text())["mcpServers"]["locus"]["env"] == {"LOCUS_TEST_VALUE": "s3cret-value-123"}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    code, out, _ = run("--client", "cursor", "--write", "--yes", "--command", exe, "--env", "LOCUS_TEST_VALUE")
    assert code == 0 and "nothing changed" in out


def test_an_existing_env_block_in_the_locus_entry_is_kept_and_hidden(run, home, exe):
    path = config_path("cursor", home)
    write_json(
        path, {"mcpServers": {"locus": {"command": "/old", "args": ["serve"], "env": {"KEPT": "private-old-value"}}}}
    )
    code, out, err = run("--client", "cursor", "--write", "--yes", "--command", exe)
    assert code == 0 and "private-old-value" not in out + err and "<hidden>" in out
    assert json.loads(path.read_text())["mcpServers"]["locus"] == {
        "command": exe, "args": ["serve"], "env": {"KEPT": "private-old-value"}
    }  # fmt: skip


@pytest.mark.parametrize("name", JSON_CLIENTS)
def test_a_remote_locus_entry_is_replaced_not_mixed_and_its_headers_are_hidden(run, home, exe, name):
    path = config_path(name, home)
    remote = {"url": "http://127.0.0.1:8765/mcp", "headers": {"Authorization": "Bearer private-token"}}
    write_json(path, {"mcpServers": {**OTHER_SERVERS, "locus": remote}})
    code, out, err = run("--client", name, "--write", "--yes", "--command", exe)
    assert code == 0 and "private-token" not in out + err and "<hidden>" in out
    servers = json.loads(path.read_text())["mcpServers"]
    assert servers["locus"] == {"command": exe, "args": ["serve"]}
    assert {k: v for k, v in servers.items() if k != "locus"} == OTHER_SERVERS


def test_a_remote_locus_table_in_toml_is_replaced_not_mixed(run, home, exe):
    path = config_path("codex", home)
    path.parent.mkdir(parents=True)
    path.write_text(
        'model = "gpt-5"\n\n[mcp_servers.locus]\nurl = "http://127.0.0.1:8765/mcp"\n\n'
        '[mcp_servers.locus.http_headers]\nAuthorization = "Bearer private-token"\n\n'
        '[mcp_servers.other]\ncommand = "other"\n'
    )
    code, out, err = run("--client", "codex", "--write", "--yes", "--command", exe)
    assert code == 0 and "private-token" not in out + err
    data = tomllib.loads(path.read_text())
    assert data["mcp_servers"] == {"locus": {"command": exe, "args": ["serve"]}, "other": {"command": "other"}}
    assert data["model"] == "gpt-5"


def test_toml_env_goes_in_a_sub_table(run, home, exe, monkeypatch):
    monkeypatch.setenv("LOCUS_TEST_VALUE", 'quo"te\\slash')
    path = config_path("codex", home)
    code, out, _ = run("--client", "codex", "--write", "--yes", "--command", exe, "--env", "LOCUS_TEST_VALUE")
    assert code == 0 and "slash" not in out
    assert tomllib.loads(path.read_text())["mcp_servers"]["locus"]["env"] == {"LOCUS_TEST_VALUE": 'quo"te\\slash'}


def test_env_that_is_not_set_stops_before_anything_is_written(run, home, exe, monkeypatch):
    monkeypatch.delenv("LOCUS_TEST_MISSING", raising=False)
    code, _, err = run("--client", "cursor", "--write", "--yes", "--command", exe, "--env", "LOCUS_TEST_MISSING")
    assert code == 2 and "LOCUS_TEST_MISSING" in err and tree(home) == []


def test_env_given_as_name_equals_value_is_refused_without_echoing_the_value(run, exe):
    code, out, err = run("--client", "cursor", "--command", exe, "--env", "KEY=topsecretvalue")
    assert code == 2 and "topsecretvalue" not in out + err and "name of a variable only" in err


# --- argument handling --------------------------------------------------------------------------------------


def test_unknown_client_lists_the_known_names(run):
    code, _, err = run("--client", "emacs")
    assert code == 2 and "unknown client" in err and "claude-desktop" in err


def test_client_names_are_case_insensitive(run, exe):
    assert run("--client", "Cursor", "--command", exe)[0] == 0


def test_command_must_be_absolute_and_exist_to_be_written(run, home, tmp_path):
    code, _, err = run("--client", "cursor", "--command", "locus")
    assert code == 2 and "absolute" in err
    code, _, err = run("--client", "cursor", "--write", "--yes", "--command", str(tmp_path / "nowhere" / "locus"))
    assert code == 2 and "is not a file" in err and tree(home) == []


def test_without_a_found_command_write_asks_for_one(run, home, monkeypatch):
    monkeypatch.setattr(clients, "detect_command", lambda: None)
    code, out, err = run("--client", "cursor")
    assert code == 0 and "/absolute/path/to/locus" in out and "placeholder" in out
    code, _, err = run("--client", "cursor", "--write", "--yes")
    assert code == 2 and "--command PATH" in err and tree(home) == []


def test_the_command_is_found_on_the_path(run, exe, monkeypatch):
    monkeypatch.setenv("PATH", str(Path(exe).parent))
    _, out, _ = run("--client", "cursor")
    assert f'"command": "{exe}"' in out


def test_modes_are_exclusive(run, capsys):
    for argv in (["--client", "cursor", "--local"], ["--list", "--client", "cursor"], ["--managed", "--list"]):
        with pytest.raises(SystemExit) as exc:
            cli.main(["setup", *argv])
        assert exc.value.code == 2
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        cli.main(["setup", "--client", "cursor", "--print", "--write"])
    assert exc.value.code == 2


def test_client_options_without_client_are_refused_not_ignored(run, home):
    code, _, err = run("--local", "--write")
    assert code == 2 and "go with --client" in err and not (home.parent / ".env").exists()
    code, _, err = run("--client", "cursor", "--collection", "x_collection")
    assert code == 2 and "go with --managed or --local only" in err
    code, _, err = run("--client", "cursor", "--yes")
    assert code == 2 and "--yes goes with --write" in err
    code, _, err = run("--list", "--write")
    assert code == 2 and "--list takes only --os" in err


# --- what stayed the same -----------------------------------------------------------------------------------


def test_the_licence_question_still_stops_setup_before_the_client_part(run, home, exe, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", _Tty("y\n"))
    code, out, err = run("--client", "cursor", "--write", "--yes", "--command", exe)
    assert code == cli.EXIT_NEEDS_YES and licence.COMMERCIAL_STOP in err and "Cursor" not in out
    assert tree(home) == []


def test_the_licence_question_is_asked_once_and_a_no_lets_setup_continue(run, exe, monkeypatch, tmp_path):
    monkeypatch.setattr("sys.stdin", _Tty("n\n"))
    code, out, err = run("--client", "cursor", "--command", exe)
    assert code == 0 and licence.QUESTION in err and "Cursor" in out
    assert (tmp_path / "data" / licence.ACK_FILE).is_file()


def test_managed_and_local_are_still_the_other_two_modes():
    parser = cli.build_parser()
    assert parser.parse_args(["setup", "--local"]).client is None
    assert parser.parse_args(["setup", "--managed"]).managed is True
    assert parser.parse_args(["setup", "--client", "cursor"]).write is False
    with pytest.raises(SystemExit):
        parser.parse_args(["setup"])


def test_docs_clients_md_is_the_registry_table():
    text = (Path(__file__).resolve().parents[1] / "docs" / "clients.md").read_text(encoding="utf-8")
    assert text == clients.render_docs_markdown()
    assert "does not mean Locus has been tested with that client" in text
