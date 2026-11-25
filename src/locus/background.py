"""`locus service install | uninstall | status`: keep `locus watch` running in the background (opt-in).

macOS gets a launchd agent (`~/Library/LaunchAgents/locus.watch.plist`), Linux a systemd user unit
(`~/.config/systemd/user/locus-watch.service`). Both run `locus service run <folders>`, a small supervisor that
starts one `locus watch <folder>` per folder at low priority and stops them while Locus is paused (`locus pause`,
LOCUS_DISABLED) or the computer runs on battery, then starts them again.

Install writes only the unit file (plus a numbered backup of a different one that was there) and then asks
launchctl or systemctl to load it. Nothing is installed without a confirmation or --yes. `--dry-run` prints the
unit text and changes nothing. The unit never passes --yes to `locus watch`, so a hosted embedder or a closed
folder that has not been confirmed yet makes the watcher stop and say so in the log instead of sending anything.

The supervisor writes nothing to stdout; its messages go to stderr, which the unit sends to `<data dir>/service.log`.
Every path, the platform and the command runner are arguments, so the tests never touch the real home folder.
"""

from __future__ import annotations

import os
import plistlib
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, TextIO

from . import killswitch

LABEL = "locus.watch"
UNIT_NAME = "locus-watch.service"
LOG_NAME = "service.log"
NICE = 10
POLL_SECONDS = 30.0
RESTART_SECONDS = 300.0  # wait this long before restarting a watcher that exited

EXIT_OK = 0
EXIT_DECLINED = 1
EXIT_PROBLEM = 2  # bad arguments, unsupported system, or no terminal and no --yes


class Result(Protocol):
    returncode: int
    stdout: str


Runner = Callable[[Sequence[str]], Result]


def run_command(argv: Sequence[str]) -> Result:
    """The real runner: launchctl / systemctl. Never used by the tests."""
    return subprocess.run(list(argv), capture_output=True, text=True, check=False)  # noqa: S603


def detect_platform(name: str | None = None) -> str | None:
    """ "macos", "linux" or None (anything else, Windows included)."""
    name = sys.platform if name is None else name
    if name == "darwin":
        return "macos"
    if name.startswith("linux"):
        return "linux"
    return None


def unit_path(platform: str, home: Path) -> Path:
    if platform == "macos":
        return home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    return home / ".config" / "systemd" / "user" / UNIT_NAME


def log_path(data_dir: Path) -> Path:
    return data_dir / LOG_NAME


def service_argv(locus_cmd: str, folders: Sequence[Path]) -> list[str]:
    return [locus_cmd, "service", "run", *(str(f) for f in folders)]


# --- the unit files, as text -------------------------------------------------------------------------------------


def render_plist(argv: Sequence[str], log_file: Path, data_dir: Path) -> str:
    """launchd agent: starts at login, restarts if it dies, runs as a low-priority background job."""
    body = {
        "Label": LABEL,
        "ProgramArguments": list(argv),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ProcessType": "Background",
        "Nice": NICE,
        "LowPriorityIO": True,
        "ThrottleInterval": 30,
        "StandardOutPath": str(log_file),
        "StandardErrorPath": str(log_file),
        "EnvironmentVariables": {"LOCUS_DATA_DIR": str(data_dir)},
    }
    return plistlib.dumps(body, sort_keys=False).decode("utf-8")


def _systemd_quote(arg: str) -> str:
    """One word of an ExecStart line: double-quoted, with backslash, quote and % (specifier) escaped."""
    escaped = arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{escaped}"'


def render_systemd(argv: Sequence[str], log_file: Path, data_dir: Path) -> str:
    """systemd user unit: idle CPU and IO scheduling, restarts on failure, starts with the user session."""
    exec_start = " ".join(_systemd_quote(a) for a in argv)
    env = _systemd_quote(f"LOCUS_DATA_DIR={data_dir}")
    return (
        "[Unit]\n"
        "Description=Locus: keep the index in sync with your folders (locus watch)\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={exec_start}\n"
        f"Environment={env}\n"
        "Restart=on-failure\n"
        "RestartSec=30\n"
        f"Nice={NICE}\n"
        "CPUSchedulingPolicy=idle\n"
        "IOSchedulingClass=idle\n"
        f"StandardOutput=append:{log_file}\n"
        f"StandardError=append:{log_file}\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def render_unit(platform: str, argv: Sequence[str], log_file: Path, data_dir: Path) -> str:
    return (render_plist if platform == "macos" else render_systemd)(argv, log_file, data_dir)


def consent_text(platform: str, unit_file: Path, folders: Sequence[Path], log_file: Path) -> str:
    manager = "launchd" if platform == "macos" else "systemd (user)"
    folder_lines = "\n".join(f"    {f}" for f in folders)
    return (
        f"Locus will start a background job with {manager} that keeps the index in sync with:\n"
        f"{folder_lines}\n"
        "\n"
        f"  - It writes one file: {unit_file}\n"
        f"  - It starts at login, at low CPU and disk priority, and writes its log to {log_file}.\n"
        "  - It stops reading folders while you are on battery power and while Locus is paused (`locus pause`).\n"
        "  - It never passes --yes: a hosted embedder or a closed folder that you have not confirmed yet stops it, "
        "and the log says so.\n"
        "  - Nothing leaves your computer because of this job beyond what `locus watch` already does.\n"
        "  - Remove it at any time with `locus service uninstall`."
    )


# --- install / uninstall / status --------------------------------------------------------------------------------


def _uid() -> int:
    return os.getuid()


def _load_commands(platform: str, unit_file: Path) -> list[list[str]]:
    if platform == "macos":
        domain = f"gui/{_uid()}"
        return [["launchctl", "bootout", f"{domain}/{LABEL}"], ["launchctl", "bootstrap", domain, str(unit_file)]]
    return [["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", "--now", UNIT_NAME]]


def _unload_commands(platform: str) -> list[list[str]]:
    if platform == "macos":
        return [["launchctl", "bootout", f"gui/{_uid()}/{LABEL}"]]
    return [["systemctl", "--user", "disable", "--now", UNIT_NAME]]


def _interactive(stdin: TextIO) -> bool:
    try:
        return stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _write_unit(unit_file: Path, text: str) -> Path | None:
    """Write *text* to *unit_file* (atomic replace). A different file already there is first copied to
    `<name>.locus-backup-<UTC time>`; the backup's path is returned, else None."""
    unit_file.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if unit_file.exists():
        if unit_file.read_text(encoding="utf-8", errors="replace") == text:
            return None
        backup = unit_file.with_name(f"{unit_file.name}.locus-backup-{datetime.now(UTC):%Y%m%dT%H%M%S}")
        backup.write_bytes(unit_file.read_bytes())
    fd, tmp = tempfile.mkstemp(prefix=".locus-unit-", dir=unit_file.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, unit_file)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return backup


@dataclass(frozen=True)
class Plan:
    platform: str
    unit_file: Path
    log_file: Path
    folders: list[Path]
    text: str


def make_plan(platform: str, home: Path, data_dir: Path, folders: Sequence[Path], locus_cmd: str) -> Plan:
    resolved = [Path(f).expanduser().resolve() for f in folders]
    log_file = log_path(data_dir)
    text = render_unit(platform, service_argv(locus_cmd, resolved), log_file, data_dir)
    return Plan(platform, unit_path(platform, home), log_file, resolved, text)


def install(
    plan: Plan,
    *,
    yes: bool,
    dry_run: bool,
    runner: Runner,
    stdin: TextIO,
    out: TextIO,
    err: TextIO,
) -> int:
    if dry_run:
        print(f"# {plan.unit_file}  (dry run: nothing was written, nothing was started)", file=out)
        print(plan.text, end="" if plan.text.endswith("\n") else "\n", file=out)
        return EXIT_OK
    print(consent_text(plan.platform, plan.unit_file, plan.folders, plan.log_file), file=err)
    if not yes:
        if not _interactive(stdin):
            print(
                "\nNothing was installed: this is not an interactive terminal. Re-run with --yes to confirm.", file=err
            )
            return EXIT_PROBLEM
        print("\nInstall this background job? [y/N] ", end="", file=err, flush=True)
        if stdin.readline().strip().lower() not in ("y", "yes"):
            print("Cancelled. Nothing was installed.", file=err)
            return EXIT_DECLINED
    plan.log_file.parent.mkdir(parents=True, exist_ok=True)
    backup = _write_unit(plan.unit_file, plan.text)
    if backup is not None:
        print(f"locus: kept the unit file that was there as {backup}", file=err)
    for argv in _load_commands(plan.platform, plan.unit_file):
        result = runner(argv)
        # the first macOS command (bootout of an older copy) is allowed to fail: there may be none
        if result.returncode != 0 and not (plan.platform == "macos" and argv[1] == "bootout"):
            print(
                f"locus: `{shlex.join(argv)}` failed ({result.returncode}). The unit file is at {plan.unit_file}; "
                "`locus service uninstall` removes it.",
                file=err,
            )
            return EXIT_PROBLEM
    print(f"Installed {plan.unit_file}. Remove it with `locus service uninstall`.", file=out)
    return EXIT_OK


def uninstall(platform: str, home: Path, *, runner: Runner, out: TextIO, err: TextIO) -> int:
    unit_file = unit_path(platform, home)
    if not unit_file.exists():
        print("The Locus background job is not installed.", file=out)
        return EXIT_OK
    for argv in _unload_commands(platform):
        result = runner(argv)
        if result.returncode != 0:
            print(
                f"locus: `{shlex.join(argv)}` returned {result.returncode} (continuing: removing the file).", file=err
            )
    unit_file.unlink()
    if platform == "linux":
        runner(["systemctl", "--user", "daemon-reload"])
    print(f"Removed {unit_file}.", file=out)
    return EXIT_OK


def status(platform: str, home: Path, data_dir: Path, *, runner: Runner, out: TextIO) -> int:
    unit_file = unit_path(platform, home)
    if not unit_file.exists():
        print("not installed", file=out)
        return EXIT_OK
    argv = (
        ["launchctl", "print", f"gui/{_uid()}/{LABEL}"]
        if platform == "macos"
        else ["systemctl", "--user", "is-active", UNIT_NAME]
    )
    result = runner(argv)
    running = result.returncode == 0 and (platform == "macos" or result.stdout.strip() == "active")
    print(f"unit file : {unit_file}", file=out)
    print(f"manager   : {'loaded and running' if running else 'installed, not running'}", file=out)
    if reason := killswitch.paused_by(data_dir):
        print(
            f"paused    : yes ({'LOCUS_DISABLED' if reason == 'env' else 'locus pause'}); watchers are stopped",
            file=out,
        )
    print(f"log       : {log_path(data_dir)}", file=out)
    return EXIT_OK


# --- the supervisor the unit runs ---------------------------------------------------------------------------------


def on_battery(
    platform: str,
    *,
    runner: Runner = run_command,
    power_root: Path = Path("/sys/class/power_supply"),
) -> bool:
    """True when the computer is running on battery. Any failure to find out counts as "not on battery"."""
    try:
        if platform == "macos":
            return "Battery Power" in runner(["pmset", "-g", "batt"]).stdout
        mains_online = False
        discharging = False
        for supply in power_root.iterdir():
            kind = (supply / "type").read_text().strip() if (supply / "type").exists() else ""
            if kind == "Mains" and (supply / "online").exists():
                mains_online = mains_online or (supply / "online").read_text().strip() == "1"
            elif kind == "Battery" and (supply / "status").exists():
                discharging = discharging or (supply / "status").read_text().strip() == "Discharging"
        return discharging and not mains_online
    except (OSError, ValueError):
        return False


class Child(Protocol):
    def poll(self) -> int | None: ...
    def terminate(self) -> None: ...
    def wait(self, timeout: float | None = None) -> int: ...


def _log(msg: str) -> None:
    print(f"locus service: {msg}", file=sys.stderr, flush=True)


def spawn_watch(folder: Path) -> Child:
    """`locus watch <folder>` in a child process, same low priority as this supervisor. No --yes."""
    return subprocess.Popen([sys.executable, "-m", "locus.cli", "watch", str(folder)])  # noqa: S603


def supervise(
    folders: Sequence[Path],
    *,
    data_dir: Path,
    spawn: Callable[[Path], Child],
    held: Callable[[], str | None],
    sleep: Callable[[float], None] = time.sleep,
    poll_seconds: float = POLL_SECONDS,
    restart_seconds: float = RESTART_SECONDS,
    stop: threading.Event | None = None,
    max_cycles: int | None = None,
) -> int:
    """Keep one watcher per folder running unless `held()` returns a reason (paused, on battery).

    While held, every watcher is terminated; when the reason goes away they are started again (the watcher's
    initial pass indexes what changed meanwhile). A watcher that exits on its own is restarted after
    `restart_seconds`."""
    stop = stop if stop is not None else threading.Event()
    children: dict[Path, Child] = {}
    retry_after: dict[Path, float] = {}
    clock = 0.0
    last_reason: str | None = None
    cycles = 0
    try:
        while not stop.is_set() and (max_cycles is None or cycles < max_cycles):
            cycles += 1
            reason = held()
            if reason != last_reason:
                _log(f"watchers stopped: {reason}" if reason else "watchers running")
                last_reason = reason
            if reason:
                for folder in list(children):
                    _stop_child(children.pop(folder))
            else:
                for folder in folders:
                    child = children.get(folder)
                    if child is not None and child.poll() is not None:
                        _log(f"watcher for {folder} exited ({child.poll()}); restarting in {restart_seconds:.0f}s")
                        children.pop(folder)
                        retry_after[folder] = clock + restart_seconds
                        child = None
                    if child is None and retry_after.get(folder, 0.0) <= clock:
                        children[folder] = spawn(folder)
            sleep(poll_seconds)
            clock += poll_seconds
    finally:
        for child in children.values():
            _stop_child(child)
    return EXIT_OK


def _stop_child(child: Child) -> None:
    if child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=10)
    except Exception:  # noqa: BLE001 - a child that will not stop is left to the OS when we exit
        pass


def run_service(folders: Sequence[Path], data_dir: Path, platform: str) -> int:
    """Entry point of `locus service run`: nice(10), SIGTERM stops everything cleanly."""
    import signal

    try:
        os.nice(NICE)
    except OSError:
        pass
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    def held() -> str | None:
        if killswitch.paused_by(data_dir):
            return "Locus is paused"
        if on_battery(platform):
            return "on battery power"
        return None

    return supervise([Path(f) for f in folders], data_dir=data_dir, spawn=spawn_watch, held=held, stop=stop)
