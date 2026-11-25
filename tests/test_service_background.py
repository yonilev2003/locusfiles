"""`locus service install | uninstall | status` (background.py): the unit files as text, the consent step, the fake
launchctl / systemctl runner, the battery and pause checks and the supervisor loop. Nothing here touches the real home
folder, ~/Library, ~/.config, launchctl or systemctl: every path is under tmp_path and the runner is a fake."""

from __future__ import annotations

import io
import plistlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from locus import background, cli, killswitch


class FakeRunner:
    def __init__(self, fail_on: str | None = None, stdout: str = ""):
        self.calls: list[list[str]] = []
        self.fail_on = fail_on
        self.stdout = stdout

    def __call__(self, argv):
        self.calls.append(list(argv))
        code = 1 if self.fail_on and self.fail_on in argv else 0
        return SimpleNamespace(returncode=code, stdout=self.stdout)


class FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    notes = home / "notes"
    notes.mkdir()
    data = tmp_path / "data"
    return SimpleNamespace(home=home, notes=notes, data=data, out=io.StringIO(), err=io.StringIO())


def plan_for(env, platform):
    return background.make_plan(platform, env.home, env.data, [env.notes], "/opt/bin/locus")


def do_install(env, platform, runner, *, yes=False, dry_run=False, stdin=None):
    return background.install(
        plan_for(env, platform), yes=yes, dry_run=dry_run, runner=runner,
        stdin=stdin or io.StringIO(), out=env.out, err=env.err,
    )  # fmt: skip


# --- unit text ---------------------------------------------------------------------------------------------------


def test_plist_parses_and_is_low_priority(env):
    text = plan_for(env, "macos").text
    data = plistlib.loads(text.encode("utf-8"))
    assert data["Label"] == "locus.watch"
    assert data["ProgramArguments"] == ["/opt/bin/locus", "service", "run", str(env.notes.resolve())]
    assert data["ProcessType"] == "Background"
    assert data["LowPriorityIO"] is True
    assert data["Nice"] == background.NICE
    assert data["RunAtLoad"] is True
    assert data["StandardErrorPath"] == str(env.data / "service.log")
    assert data["EnvironmentVariables"] == {"LOCUS_DATA_DIR": str(env.data)}
    assert "--yes" not in data["ProgramArguments"]


def test_systemd_unit_structure(env):
    plan = plan_for(env, "linux")
    sections: dict[str, list[str]] = {}
    current = None
    for line in plan.text.splitlines():
        if line.startswith("["):
            current = line.strip("[]")
            sections[current] = []
        elif line.strip():
            assert current is not None and "=" in line
            sections[current].append(line)
    assert set(sections) == {"Unit", "Service", "Install"}
    service = dict(line.split("=", 1) for line in sections["Service"])
    assert service["ExecStart"] == f'"/opt/bin/locus" "service" "run" "{env.notes.resolve()}"'
    assert service["Nice"] == str(background.NICE)
    assert service["IOSchedulingClass"] == "idle"
    assert service["CPUSchedulingPolicy"] == "idle"
    assert service["StandardError"] == f"append:{env.data / 'service.log'}"
    assert "WantedBy=default.target" in sections["Install"]
    assert "--yes" not in plan.text


def test_systemd_quotes_awkward_paths(env):
    text = background.render_systemd(["/x/locus", "service", "run", '/a b/"c"/50%'], env.data / "l", env.data)
    assert 'ExecStart="/x/locus" "service" "run" "/a b/\\"c\\"/50%%"' in text


def test_plist_keeps_awkward_paths_intact(env):
    folder = env.home / "my notes & <more>"
    folder.mkdir()
    plan = background.make_plan("macos", env.home, env.data, [folder], "/opt/bin/locus")
    assert plistlib.loads(plan.text.encode())["ProgramArguments"][-1] == str(folder.resolve())


# --- install ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("platform", ["macos", "linux"])
def test_dry_run_prints_text_and_changes_nothing(env, platform):
    runner = FakeRunner()
    assert do_install(env, platform, runner, dry_run=True) == 0
    assert runner.calls == []
    assert not unit_file(env, platform).exists()
    assert not env.data.exists()
    assert plan_for(env, platform).text in env.out.getvalue()


def unit_file(env, platform):
    return background.unit_path(platform, env.home)


@pytest.mark.parametrize("platform", ["macos", "linux"])
def test_no_terminal_and_no_yes_installs_nothing(env, platform):
    runner = FakeRunner()
    assert do_install(env, platform, runner) == background.EXIT_PROBLEM
    assert runner.calls == []
    assert not unit_file(env, platform).exists()
    assert "--yes" in env.err.getvalue()
    assert str(unit_file(env, platform)) in env.err.getvalue()  # the consent text came first


@pytest.mark.parametrize("answer, installed", [("y\n", True), ("no\n", False), ("\n", False)])
def test_interactive_confirmation(env, answer, installed):
    runner = FakeRunner()
    code = do_install(env, "linux", runner, stdin=FakeTTY(answer))
    assert unit_file(env, "linux").exists() is installed
    assert code == (0 if installed else background.EXIT_DECLINED)
    assert bool(runner.calls) is installed


def test_install_macos_writes_only_the_plist_and_loads_it(env):
    runner = FakeRunner()
    assert do_install(env, "macos", runner, yes=True) == 0
    target = unit_file(env, "macos")
    assert target == env.home / "Library" / "LaunchAgents" / "locus.watch.plist"
    assert plistlib.loads(target.read_bytes())["Label"] == "locus.watch"
    assert [c[:2] for c in runner.calls] == [["launchctl", "bootout"], ["launchctl", "bootstrap"]]
    assert runner.calls[1][3] == str(target)
    written = {p for p in env.home.rglob("*") if p.is_file()}
    assert written == {target}  # nothing else under home
    assert (env.data / "service.log").parent.is_dir()


def test_install_linux_enables_the_user_unit(env):
    runner = FakeRunner()
    assert do_install(env, "linux", runner, yes=True) == 0
    target = unit_file(env, "linux")
    assert target == env.home / ".config" / "systemd" / "user" / "locus-watch.service"
    assert target.read_text() == plan_for(env, "linux").text
    assert runner.calls == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", "locus-watch.service"],
    ]


def test_install_backs_up_a_different_existing_unit(env):
    target = unit_file(env, "linux")
    target.parent.mkdir(parents=True)
    target.write_text("[Service]\nExecStart=/bin/true\n")
    assert do_install(env, "linux", FakeRunner(), yes=True) == 0
    backups = list(target.parent.glob("locus-watch.service.locus-backup-*"))
    assert len(backups) == 1 and backups[0].read_text() == "[Service]\nExecStart=/bin/true\n"
    # the same text again: no new backup
    assert do_install(env, "linux", FakeRunner(), yes=True) == 0
    assert len(list(target.parent.glob("locus-watch.service.locus-backup-*"))) == 1


def test_a_failing_manager_command_is_reported(env):
    code = do_install(env, "linux", FakeRunner(fail_on="enable"), yes=True)
    assert code == background.EXIT_PROBLEM
    assert "uninstall" in env.err.getvalue()


def test_macos_bootout_failure_is_tolerated(env):
    assert do_install(env, "macos", FakeRunner(fail_on="bootout"), yes=True) == 0


# --- uninstall / status -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("platform", ["macos", "linux"])
def test_uninstall_removes_the_file(env, platform):
    do_install(env, platform, FakeRunner(), yes=True)
    runner = FakeRunner()
    assert background.uninstall(platform, env.home, runner=runner, out=env.out, err=env.err) == 0
    assert not unit_file(env, platform).exists()
    assert runner.calls[0][1] in ("bootout", "--user")


def test_uninstall_when_not_installed_does_nothing(env):
    runner = FakeRunner()
    assert background.uninstall("linux", env.home, runner=runner, out=env.out, err=env.err) == 0
    assert runner.calls == [] and "not installed" in env.out.getvalue()


def test_status_not_installed_installed_and_paused(env):
    def status(platform, runner):
        out = io.StringIO()
        background.status(platform, env.home, env.data, runner=runner, out=out)
        return out.getvalue()

    assert status("linux", FakeRunner()) == "not installed\n"
    do_install(env, "linux", FakeRunner(), yes=True)
    assert "running" in status("linux", FakeRunner(stdout="active\n"))
    assert "not running" in status("linux", FakeRunner(stdout="inactive\n"))
    killswitch.pause(env.data)
    assert "paused    : yes" in status("linux", FakeRunner(stdout="active\n"))


# --- battery, pause, supervisor ---------------------------------------------------------------------------------


def test_on_battery_macos_reads_pmset(env):
    assert background.on_battery("macos", runner=FakeRunner(stdout="Now drawing from 'Battery Power'\n"))
    assert not background.on_battery("macos", runner=FakeRunner(stdout="Now drawing from 'AC Power'\n"))


def make_supply(root: Path, name: str, **files: str) -> None:
    (root / name).mkdir(parents=True)
    for key, value in files.items():
        (root / name / key).write_text(value + "\n")


def test_on_battery_linux_reads_sysfs(tmp_path):
    root = tmp_path / "ps"
    make_supply(root, "BAT0", type="Battery", status="Discharging")
    make_supply(root, "AC", type="Mains", online="0")
    assert background.on_battery("linux", power_root=root)
    (root / "AC" / "online").write_text("1\n")
    assert not background.on_battery("linux", power_root=root)


def test_on_battery_unknown_means_not_on_battery(tmp_path):
    assert not background.on_battery("linux", power_root=tmp_path / "missing")
    empty = tmp_path / "empty"
    empty.mkdir()
    assert not background.on_battery("linux", power_root=empty)  # a desktop: no battery


class FakeChild:
    def __init__(self):
        self.code: int | None = None
        self.terminated = False

    def poll(self):
        return self.code

    def terminate(self):
        self.terminated = True
        self.code = -15

    def wait(self, timeout=None):
        return self.code


def run_supervisor(tmp_path, reasons, **kw):
    spawned: list[tuple[Path, FakeChild]] = []

    def spawn(folder):
        child = FakeChild()
        spawned.append((folder, child))
        return child

    seq = iter(reasons)
    code = background.supervise(
        [Path("/a")], data_dir=tmp_path, spawn=spawn, held=lambda: next(seq), sleep=lambda _: None,
        max_cycles=len(reasons), **kw,
    )  # fmt: skip
    return code, spawned


def test_supervisor_stops_while_held_and_restarts_after(tmp_path):
    _, spawned = run_supervisor(tmp_path, [None, "Locus is paused", "Locus is paused", None])
    assert len(spawned) == 2
    assert spawned[0][1].terminated  # stopped when paused
    assert spawned[1][1].terminated  # and stopped at shutdown


def test_supervisor_never_starts_while_held(tmp_path):
    _, spawned = run_supervisor(tmp_path, ["on battery power"] * 3)
    assert spawned == []


def test_supervisor_restarts_a_dead_watcher_only_after_the_wait(tmp_path):
    spawned = []

    def spawn(folder):
        child = FakeChild()
        child.code = 2  # exits at once, as a watcher that needs a confirmation does
        spawned.append(child)
        return child

    background.supervise(
        [Path("/a")], data_dir=tmp_path, spawn=spawn, held=lambda: None, sleep=lambda _: None,
        poll_seconds=30, restart_seconds=90, max_cycles=9,
    )  # fmt: skip
    assert len(spawned) == 3  # cycles 1, 5, 9: not a tight restart loop


def test_pause_flag_is_what_the_service_checks(env):
    assert killswitch.paused_by(env.data, env={}) is None
    killswitch.pause(env.data)
    assert killswitch.paused_by(env.data, env={}) == "flag"


def test_supervisor_prints_nothing_to_stdout(tmp_path, capsys):
    run_supervisor(tmp_path, [None, "Locus is paused"])
    assert capsys.readouterr().out == ""


# --- the command -------------------------------------------------------------------------------------------------


def test_cli_install_dry_run_prints_plist_on_stdout(tmp_path, monkeypatch, capsys):
    notes = tmp_path / "notes"
    notes.mkdir()
    monkeypatch.setenv("LOCUS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LOCUS_EMBEDDER", "local")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(background, "detect_platform", lambda name=None: "macos")
    monkeypatch.setattr(background, "run_command", lambda argv: pytest.fail("no real launchctl in tests"))
    with pytest.raises(SystemExit) as stop:
        cli.main(["service", "install", str(notes), "--dry-run"])
    assert stop.value.code == 0
    assert plistlib.loads(capsys.readouterr().out.split("\n", 1)[1].encode())["Label"] == "locus.watch"
    assert not (tmp_path / "home").exists()
    assert not (tmp_path / "data").exists()


def test_cli_refuses_a_hosted_embedder_and_a_missing_folder(tmp_path, monkeypatch, capsys):
    notes = tmp_path / "notes"
    notes.mkdir()
    monkeypatch.setenv("LOCUS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LOCUS_EMBEDDER", "voyage")
    monkeypatch.setenv("VOYAGE_API_KEY", "x")
    monkeypatch.setattr(background, "detect_platform", lambda name=None: "linux")
    with pytest.raises(SystemExit) as stop:
        cli.main(["service", "install", str(notes), "--dry-run"])
    assert stop.value.code == 2 and "local embedder" in capsys.readouterr().err
    monkeypatch.setenv("LOCUS_EMBEDDER", "local")
    with pytest.raises(SystemExit) as stop:
        cli.main(["service", "install", str(tmp_path / "nope"), "--dry-run"])
    assert stop.value.code == 2
    with pytest.raises(SystemExit) as stop:
        cli.main(["service", "install"])
    assert stop.value.code == 2


def test_unsupported_platform(monkeypatch, capsys):
    assert background.detect_platform("win32") is None
    assert background.detect_platform("linux") == "linux"
    assert background.detect_platform("darwin") == "macos"
    monkeypatch.setattr(background, "detect_platform", lambda name=None: None)
    with pytest.raises(SystemExit) as stop:
        cli.main(["service", "status"])
    assert stop.value.code == 2
