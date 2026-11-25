#!/usr/bin/env python3
"""Install the built wheel the way a customer would and prove the engine works offline (IP-008 / UX-001).

    python scripts/package_smoke.py --wheel dist/ --installer venv --extras relay
    python scripts/package_smoke.py --wheel dist/ --installer uv --extras relay --python 3.12
    python scripts/package_smoke.py --locus "python -m locus.cli"      # no install: an existing command

Steps, all in a fresh temporary directory outside the checkout (so neither src/ nor a .env in the working
directory's parents can leak in; an installed engine reads no other .env. `--installer none` runs the given
command as is: from a source checkout that engine still loads the checkout's .env):
1. install  -- `venv`: python -m venv + pip; `uv`: `uv tool install` into private UV_TOOL_DIR/UV_TOOL_BIN_DIR
               (the documented install path, docs/install.md); `none`: use --locus as given;
2. check the environment -- `locus` imports from the install, not the checkout; the distribution's
   metadata name is the wheel's; torch is absent unless the `local` extra was requested; env size;
3. `locus --help`, `locus doctor`;
4. offline index + query on a two-file fixture with LOCUS_EMBEDDER=hash (no network, no API key);
5. `locus serve` over stdio: initialize -> tools/list -> tools/call locus_search, asserting the core
   tools exist, the result cites the fixture path with a chunk index, and every stdout line is
   JSON-RPC (AGENTS.md constraint 5: `locus serve` writes nothing else to stdout).

Stdlib only; runs on Linux, macOS and Windows.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from email.parser import Parser
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CORE_TOOLS = {"locus_search", "locus_list_sources", "locus_read_file"}
FIXTURE = {
    "notes/pricing-decision.md": (
        "# Pricing decision\n\nWe made the pricing decision after comparing three plans. "
        "The pricing decision favours a monthly plan with an annual discount.\n"
    ),
    "notes/travel.txt": "Packing list for the conference trip: charger, passport, umbrella.\n",
}
QUERY = "pricing decision"
EXPECT_FILE = "pricing-decision.md"
TIMEOUT = 180  # seconds per command; the first chromadb import on a cold runner is slow


class SmokeError(RuntimeError):
    pass


def log(msg: str) -> None:
    print(f"[smoke] {msg}", flush=True)


def run(cmd: list[str], *, env: dict[str, str] | None = None, cwd: Path | None = None) -> str:
    log("$ " + " ".join(shlex.quote(c) for c in cmd))
    proc = subprocess.run(cmd, env=env, cwd=cwd, capture_output=True, text=True, timeout=TIMEOUT)
    if proc.stdout.strip():
        print(proc.stdout.rstrip())
    if proc.returncode != 0:
        raise SmokeError(f"exit {proc.returncode}: {' '.join(cmd)}\n{proc.stderr[-4000:]}")
    return proc.stdout


def wheel_info(wheel: Path) -> tuple[str, str]:
    with zipfile.ZipFile(wheel) as whl:
        meta_name = next(n for n in whl.namelist() if n.endswith(".dist-info/METADATA"))
        meta = Parser().parsestr(whl.read(meta_name).decode("utf-8"))
    return meta["Name"], meta["Version"]


def find_wheel(path: Path) -> Path:
    if path.is_file():
        return path
    wheels = sorted(path.glob("*.whl"))
    if len(wheels) != 1:
        raise SmokeError(f"expected exactly one wheel in {path}, found {[w.name for w in wheels]}")
    return wheels[0]


def _bin(env_dir: Path, exe: str) -> Path:
    return env_dir / ("Scripts" if os.name == "nt" else "bin") / (exe + (".exe" if os.name == "nt" else ""))


def install_venv(work: Path, requirement: str) -> tuple[list[str], Path]:
    venv = work / "venv"
    run([sys.executable, "-m", "venv", str(venv)])
    py = _bin(venv, "python")
    run(
        [str(py), "-m", "pip", "install", "--disable-pip-version-check", "--quiet", requirement],
        env=base_env(installed=True),
    )
    return [str(_bin(venv, "locus"))], py


def install_uv(work: Path, requirement: str, python: str | None) -> tuple[list[str], Path]:
    uv = shutil.which("uv")
    if not uv:
        raise SmokeError("uv is not on PATH (pip install uv, or see https://docs.astral.sh/uv/)")
    tool_dir, bin_dir = work / "uv-tools", work / "uv-bin"
    env = {**base_env(installed=True), "UV_TOOL_DIR": str(tool_dir), "UV_TOOL_BIN_DIR": str(bin_dir)}
    cmd = [uv, "tool", "install", *(["--python", python] if python else []), requirement]
    run(cmd, env=env)
    locus = bin_dir / ("locus.exe" if os.name == "nt" else "locus")
    pythons = [p for p in tool_dir.glob("*/*/python*") if p.parent.name in ("bin", "Scripts") and p.is_file()]
    if not locus.exists() or not pythons:
        raise SmokeError(f"uv tool install left no locus shim ({locus}) or tool python under {tool_dir}")
    return [str(locus)], sorted(pythons, key=lambda p: len(p.name))[0]


def base_env(*, installed: bool) -> dict[str, str]:
    """The caller's environment minus LOCUS_* settings and, for an installed wheel, PYTHONPATH:
    a developer shell with PYTHONPATH=src would otherwise test the checkout instead of the wheel."""
    drop = ("PYTHONPATH", "PYTHONHOME") if installed else ()
    return {k: v for k, v in os.environ.items() if not k.startswith("LOCUS_") and k not in drop}


def check_environment(py: Path, dist_name: str, version: str, extras: set[str]) -> None:
    probe = (
        "import importlib.metadata as md, importlib.util as u, json, locus\n"
        f"print(json.dumps({{'file': locus.__file__, 'version': locus.__version__, "
        f"'dist_version': md.version({dist_name!r}), 'torch': u.find_spec('torch') is not None}}))\n"
    )
    out = json.loads(run([str(py), "-c", probe], env=base_env(installed=True), cwd=py.parent).splitlines()[-1])
    if Path(out["file"]).resolve().is_relative_to(REPO):
        raise SmokeError(f"locus imported from the checkout ({out['file']}), not from the installed wheel")
    if out["dist_version"] != version or out["version"] != version:
        raise SmokeError(f"version mismatch: wheel {version}, dist {out['dist_version']}, __version__ {out['version']}")
    if "local" not in extras and out["torch"]:
        raise SmokeError(f"torch is installed without the `local` extra (extras: {sorted(extras) or 'none'})")
    env_root = py.parent.parent
    size = sum(f.stat().st_size for f in env_root.rglob("*") if f.is_file())
    log(f"environment OK: {env_root} ({size / 1e6:.0f} MB), torch absent={not out['torch']}")


class StdioClient:
    """Minimal MCP stdio client: newline-delimited JSON-RPC, stdlib only."""

    def __init__(self, cmd: list[str], env: dict[str, str], cwd: Path) -> None:
        log("$ " + " ".join(shlex.quote(c) for c in cmd))
        self.proc = subprocess.Popen(
            cmd,
            env=env,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.stderr: list[str] = []
        threading.Thread(target=self._pump, args=(self.proc.stdout, self.lines), daemon=True).start()
        threading.Thread(target=self._drain, daemon=True).start()
        self._id = 0

    @staticmethod
    def _pump(stream, sink: queue.Queue[str | None]) -> None:  # type: ignore[no-untyped-def]
        for line in stream:
            sink.put(line)
        sink.put(None)

    def _drain(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self.stderr.append(line)

    def send(self, method: str, params: dict | None = None, *, notify: bool = False) -> dict | None:
        msg: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            self._id += 1
            msg["id"] = self._id
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        if notify:
            return None
        deadline = time.monotonic() + TIMEOUT
        while True:
            try:
                line = self.lines.get(timeout=max(0.1, deadline - time.monotonic()))
            except queue.Empty as exc:
                raise SmokeError(f"no response to {method} within {TIMEOUT}s\n{''.join(self.stderr)[-4000:]}") from exc
            if line is None:
                raise SmokeError(f"server exited during {method}\n{''.join(self.stderr)[-4000:]}")
            try:
                reply = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SmokeError(f"non-JSON line on `locus serve` stdout (constraint 5): {line!r}") from exc
            if not isinstance(reply, dict) or reply.get("jsonrpc") != "2.0":
                raise SmokeError(f"non-JSON-RPC line on `locus serve` stdout (constraint 5): {line!r}")
            if reply.get("id") != self._id:
                continue  # a server notification (e.g. a log message); fine, keep waiting
            if "error" in reply:
                raise SmokeError(f"{method} returned an error: {reply['error']}")
            return reply["result"]

    def close(self) -> None:
        if self.proc.stdin:
            self.proc.stdin.close()
        try:
            self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=20)


def mcp_smoke(locus: list[str], env: dict[str, str], cwd: Path, version: str | None) -> None:
    client = StdioClient([*locus, "serve"], env, cwd)
    try:
        init = client.send(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "locus-package-smoke", "version": "0"},
            },
        )
        assert init is not None
        server = init.get("serverInfo", {}).get("name")
        if server != "locus_mcp":
            raise SmokeError(f"serverInfo.name is {server!r}, expected 'locus_mcp'")
        client.send("notifications/initialized", notify=True)
        listed = client.send("tools/list", {})
        assert listed is not None
        names = sorted(t["name"] for t in listed.get("tools", []))
        log(f"tools/list -> {names}")
        if not CORE_TOOLS <= set(names):
            raise SmokeError(f"tools/list lacks {sorted(CORE_TOOLS - set(names))}")
        called = client.send("tools/call", {"name": "locus_search", "arguments": {"query": QUERY, "top_k": 3}})
        assert called is not None
        text = "\n".join(c.get("text", "") for c in called.get("content", []) if c.get("type") == "text")
        if called.get("isError") or EXPECT_FILE not in text:
            raise SmokeError(f"locus_search did not cite {EXPECT_FILE} (constraint 2):\n{text[:2000]}")
        log("tools/call locus_search cites the fixture path")
        if "locus_help" in names:  # 0.1.0 predates locus_help; the conformance suite pins the tool list
            helped = client.send("tools/call", {"name": "locus_help", "arguments": {"query": "how do I install Locus"}})
            assert helped is not None
            help_text = "\n".join(c.get("text", "") for c in helped.get("content", []) if c.get("type") == "text")
            if helped.get("isError") or "\nSource: " not in help_text:
                # The articles are package data (locus/support_kb/*.md): a wheel without them imports
                # fine and answers "could not be read" here.
                raise SmokeError(f"locus_help did not answer from its bundled articles:\n{help_text[:1000]}")
            log("tools/call locus_help answers from the bundled help articles")
        if version is not None:  # the engine must report the wheel's version whatever the dist is called
            # Older wheels list locus_diagnose_connection; this version folded it into locus_help(check=true).
            if "locus_diagnose_connection" in names:
                diag_tool, diag_args = "locus_diagnose_connection", {}
            else:
                diag_tool, diag_args = "locus_help", {"check": True}
            diag = client.send("tools/call", {"name": diag_tool, "arguments": diag_args})
            assert diag is not None
            diag_text = "\n".join(c.get("text", "") for c in diag.get("content", []) if c.get("type") == "text")
            if f"locus version: {version}" not in diag_text:
                raise SmokeError(f"{diag_tool} does not report version {version}:\n{diag_text[:1000]}")
            log(f"tools/call {diag_tool} reports version {version}")
    finally:
        client.close()


def smoke(locus: list[str], work: Path, *, installed: bool, version: str | None = None) -> None:
    env = base_env(installed=installed)
    env.update(
        {
            "LOCUS_EMBEDDER": "hash",
            "LOCUS_ALLOW_TEST_EMBEDDER": "1",  # the engine refuses the offline test embedder unless this is set (A9)
            "LOCUS_DATA_DIR": str(work / "data"),
            "PYTHONIOENCODING": "utf-8",
        }
    )
    help_text = run([*locus, "--help"], env=env, cwd=work)
    for word in ("index", "query", "serve", "doctor"):
        if word not in help_text:
            raise SmokeError(f"`locus --help` does not mention {word!r}")
    doctor = run([*locus, "doctor"], env=env, cwd=work)
    if "All checks passed" not in doctor:
        raise SmokeError("`locus doctor` did not report all checks passed")
    for rel, body in FIXTURE.items():
        target = work / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    run([*locus, "index", str(work / "notes"), "--prune"], env=env, cwd=work)
    hits = run([*locus, "query", QUERY, "-k", "2"], env=env, cwd=work)
    first = next((line for line in hits.splitlines() if line.startswith("1. ")), "")
    if EXPECT_FILE not in first or " #" not in first:
        raise SmokeError(f"top query hit is not {EXPECT_FILE} with a chunk index: {first!r}")
    stats = run([*locus, "stats"], env=env, cwd=work)
    counts = {k.strip(): v.strip() for k, _, v in (line.partition(":") for line in stats.splitlines())}
    if counts.get("files") != "2":
        raise SmokeError("`locus stats` does not report 2 files")
    mcp_smoke(locus, env, work, version)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Install the wheel into a clean environment and smoke-test it.")
    ap.add_argument("--wheel", type=Path, help="a .whl file, or a directory holding exactly one")
    ap.add_argument("--installer", choices=("venv", "uv", "none"), default="venv")
    ap.add_argument("--extras", default="relay", help="comma-separated extras to install (default: relay)")
    ap.add_argument("--python", help="uv only: Python version for `uv tool install --python`")
    ap.add_argument("--locus", help="installer none: the command to test, e.g. 'python -m locus.cli'")
    ap.add_argument("--keep", action="store_true", help="keep the temporary directory")
    args = ap.parse_args(argv)

    extras = {e.strip() for e in args.extras.split(",") if e.strip()}
    work = Path(tempfile.mkdtemp(prefix="locus-smoke-"))
    try:
        if args.installer == "none":
            if not args.locus:
                ap.error("--installer none needs --locus")
            smoke(shlex.split(args.locus, posix=os.name != "nt"), work, installed=False)
        else:
            if not args.wheel:
                ap.error("--wheel is required unless --installer none")
            wheel = find_wheel(args.wheel)
            dist_name, version = wheel_info(wheel)
            log(f"wheel {wheel.name}: distribution {dist_name} {version}, extras {sorted(extras) or 'none'}")
            requirement = f"{dist_name}[{','.join(sorted(extras))}] @ {wheel.resolve().as_uri()}"
            if not extras:
                requirement = f"{dist_name} @ {wheel.resolve().as_uri()}"
            if args.installer == "venv":
                locus, py = install_venv(work, requirement)
            else:
                locus, py = install_uv(work, requirement, args.python)
            check_environment(py, dist_name, version, extras)
            smoke(locus, work, installed=True, version=version)
    except (SmokeError, subprocess.TimeoutExpired) as exc:
        print(f"[smoke] FAIL: {exc}", file=sys.stderr)
        return 1
    finally:
        if args.keep:
            log(f"kept {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)
    log("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
