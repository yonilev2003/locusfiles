#!/usr/bin/env python3
"""A new user's first ten minutes, run as a script.

Installs Locus into a throw-away location (never your own `locus`, never your own index), then does what the
README tells a user to do and checks what a user would see:

  install -> doctor -> index the demo folder -> ask one question -> ask again as JSON -> speak MCP over
  stdio like a client does -> pause and resume -> print a client config.

Standard library only, so it runs the same on macOS, Linux and Windows. Exit code 0 only if every step passed.

  python scripts/user_journey.py                                    # from this checkout's `locusfiles[local]`
  python scripts/user_journey.py --package "locusfiles[local]==0.2.0rc1" --prerelease   # from PyPI
  python scripts/user_journey.py --report journey.json              # also write a machine-readable report

The real embedding model is used (first run downloads it); `--fresh-model` also ignores any cached copy.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
QUESTION = "why did the annual discount change from two months free, and what is the new Solo annual price"
EXPECT_FILE = "pricing-review"
TOOLS = {"locus_search", "locus_list_sources", "locus_read_file"}
UNINDEXED = "/etc/hosts" if os.name != "nt" else "C:\\Windows\\System32\\drivers\\etc\\hosts"
STEPS: list[dict] = []


def record(name: str, ok: bool, detail: str, started: float) -> bool:
    STEPS.append({"step": name, "ok": ok, "seconds": round(time.time() - started, 1), "detail": detail[:400]})
    print(f"{'PASS' if ok else 'FAIL'}  {name:<34} {time.time() - started:6.1f}s  {detail[:110]}", flush=True)
    return ok


def run(cmd: list[str], env: dict, timeout: int, stdin_text: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL if stdin_text is None else None,
        input=stdin_text,
    )


def exe(directory: Path, name: str) -> Path:
    return directory / (name + (".exe" if os.name == "nt" else ""))


def install(args, work: Path, env: dict) -> Path | None:
    t = time.time()
    if args.installer == "uv" and shutil.which("uv"):
        bindir = work / "bin"
        env["UV_TOOL_DIR"], env["UV_TOOL_BIN_DIR"] = str(work / "uv-tools"), str(bindir)
        cmd = ["uv", "tool", "install", "--python", args.python, args.package]
        if args.prerelease:
            cmd.insert(3, "--prerelease=allow")
        if not any(c in args.package for c in "=@") and Path(args.package.split("[")[0]).exists():
            pass  # a local path installs as given
        r = run(cmd, env, 900)
        located = exe(bindir, "locus")
    else:
        venv = work / "venv"
        r = run([sys.executable, "-m", "venv", str(venv)], env, 300)
        scripts = venv / ("Scripts" if os.name == "nt" else "bin")
        pip = [str(exe(scripts, "python")), "-m", "pip", "install", "--quiet", args.package]
        if args.prerelease:
            pip.insert(-1, "--pre")
        r = run(pip, env, 900) if r.returncode == 0 else r
        located = exe(scripts, "locus")
    ok = r.returncode == 0 and located.exists()
    record("install " + args.package, ok, "ok" if ok else (r.stderr or r.stdout)[-300:], t)
    return located if ok else None


def mcp_stdio(locus: Path, env: dict) -> None:
    """Talk to `locus serve` the way an MCP client does: newline-delimited JSON-RPC over stdio."""
    t = time.time()
    proc = subprocess.Popen(
        [str(locus), "serve"],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    lines: queue.Queue[str] = queue.Queue()
    threading.Thread(target=lambda: [lines.put(x) for x in proc.stdout], daemon=True).start()
    seen: list[str] = []

    def call(msg: dict, want_id: int | None, wait: float = 60.0) -> dict | None:
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()
        if want_id is None:
            return None
        end = time.time() + wait
        while time.time() < end:
            try:
                raw = lines.get(timeout=0.5)
            except queue.Empty:
                continue
            seen.append(raw)
            try:
                obj = json.loads(raw)
            except ValueError:
                return {"_not_json": raw}
            if obj.get("id") == want_id:
                return obj
        return None

    try:
        init = call(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "user-journey", "version": "1"},
                },
            },
            1,
        )
        call({"jsonrpc": "2.0", "method": "notifications/initialized"}, None)
        tools = call({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, 2)
        names = {x["name"] for x in (tools or {}).get("result", {}).get("tools", [])}
        found = call(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "locus_search", "arguments": {"query": QUESTION}},
            },
            3,
            wait=120,
        )
        text = json.dumps(found or {})
        init_ok = bool(init and "result" in init)
        record("mcp: initialize", init_ok, "ok" if init_ok else str(init)[:150], t)
        record("mcp: the three tools are listed", TOOLS <= names, f"got {sorted(names)}", t)
        record(
            "mcp: search answers with a file path",
            EXPECT_FILE in text and "error" not in (found or {}),
            "found " + EXPECT_FILE if EXPECT_FILE in text else text[:200],
            t,
        )
        listed = call(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "locus_list_sources", "arguments": {}},
            },
            4,
            wait=60,
        )
        record("mcp: list_sources names the indexed files", EXPECT_FILE in json.dumps(listed or {}), "paths listed", t)
        refused = call(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "locus_read_file", "arguments": {"path": UNINDEXED}},
            },
            5,
            wait=60,
        )
        rtext = json.dumps(refused or {})
        leaked = "localhost" in rtext or "127.0.0.1" in rtext
        record(
            "mcp: read_file refuses a path the index does not list",
            bool(refused) and not leaked,
            "refused" if not leaked else "FILE TEXT RETURNED",
            t,
        )
        bad = [x for x in seen if not x.lstrip().startswith("{")]
        record("mcp: stdout carries only protocol", not bad, f"{len(seen)} lines" if not bad else bad[0][:100], t)
    finally:
        try:
            proc.stdin.close()
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


def http_transport(locus: Path, env: dict) -> None:
    """`locus serve --transport streamable-http`: a token is required, and a foreign Host header is refused."""
    import socket
    import urllib.error
    import urllib.request

    t = time.time()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    token = "journey-" + "x" * 40
    proc = subprocess.Popen(
        [str(locus), "serve", "--transport", "streamable-http", "--port", str(port)],
        env=dict(env, LOCUS_MCP_TOKEN=token),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "user-journey", "version": "1"},
            },
        }
    ).encode()

    def post(headers: dict) -> int:
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", **headers}
        try:
            return urllib.request.urlopen(
                urllib.request.Request(f"http://127.0.0.1:{port}/mcp", body, h), timeout=20
            ).status
        except urllib.error.HTTPError as exc:
            return exc.code
        except Exception:  # noqa: BLE001
            return 0

    try:
        up = False
        for _ in range(90):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                up = True
                break
            except OSError:
                time.sleep(1)
        record("http: the server starts on a local port", up, f"port {port}", t)
        if up:
            no_token = post({})
            good = post({"Authorization": f"Bearer {token}"})
            wrong_host = post({"Authorization": f"Bearer {token}", "Host": "evil.example"})
            record("http: no token is refused", no_token in (401, 403), f"status {no_token}", t)
            record("http: the right token is accepted", good == 200, f"status {good}", t)
            record("http: a foreign Host header is refused", 400 <= wrong_host < 500, f"status {wrong_host}", t)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            proc.kill()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--package", default=f"{ROOT}[local]", help="pip/uv requirement to install")
    ap.add_argument("--prerelease", action="store_true", help="allow a pre-release (rc) from PyPI")
    ap.add_argument("--installer", choices=("uv", "pip"), default="uv")
    ap.add_argument("--python", default="3.12", help="Python for uv tool install")
    ap.add_argument("--vault", default=str(ROOT / "tests" / "fixtures" / "demo-vault"))
    ap.add_argument("--fresh-model", action="store_true", help="ignore cached model files (downloads again)")
    ap.add_argument("--report", help="write a JSON report here")
    ap.add_argument("--keep", action="store_true", help="keep the temporary directory")
    args = ap.parse_args()

    work = Path(tempfile.mkdtemp(prefix="locus-journey-"))
    env = dict(os.environ, LOCUS_DATA_DIR=str(work / "data"), LOCUS_EMBEDDER="local", PYTHONIOENCODING="utf-8")
    env.pop("LOCUS_API_KEY", None)
    if args.fresh_model:
        env.update(HF_HOME=str(work / "hf"), FASTEMBED_CACHE_PATH=str(work / "fastembed"))
    print(f"work dir {work}\n", flush=True)
    try:
        locus = install(args, work, env)
        if locus:
            t = time.time()
            r = run([str(locus), "doctor"], env, 600)
            record(
                "doctor",
                r.returncode == 0,
                (r.stdout + r.stderr).strip().splitlines()[-1:]
                and (r.stdout + r.stderr).strip().splitlines()[-1]
                or "",
                t,
            )
            t = time.time()
            r = run([str(locus), "index", args.vault], env, 1500)
            out = r.stdout + r.stderr
            record(
                "index the demo folder",
                r.returncode == 0 and "errors=0" in out,
                out.strip().splitlines()[-1] if out.strip() else "",
                t,
            )
            t = time.time()
            r = run([str(locus), "query", QUESTION, "-k", "4"], env, 300)
            first = next((ln for ln in r.stdout.splitlines() if ".md" in ln), "")
            record(
                "ask: the right file is in the answer", r.returncode == 0 and EXPECT_FILE in r.stdout, first.strip(), t
            )
            t = time.time()
            r = run([str(locus), "query", QUESTION, "-k", "3", "--json"], env, 300)
            try:
                hits = json.loads(r.stdout)["results"]
                ok = bool(hits) and all("path" in h and "chunk_index" in h for h in hits)
                record("ask as JSON: every hit has path and chunk", ok, f"{len(hits)} hits", t)
            except Exception as exc:  # noqa: BLE001
                record("ask as JSON: every hit has path and chunk", False, f"{exc}: {r.stdout[:120]}", t)
            mcp_stdio(locus, env)
            http_transport(locus, env)
            t = time.time()
            run([str(locus), "pause"], env, 60)
            paused = run([str(locus), "query", "pricing", "--json"], env, 120).returncode
            run([str(locus), "resume"], env, 60)
            resumed = run([str(locus), "query", "pricing", "--json"], env, 120).returncode
            record(
                "pause stops the file tools, resume restores",
                paused != 0 and resumed == 0,
                f"paused exit {paused}, resumed exit {resumed}",
                t,
            )
            t = time.time()
            r = run([str(locus), "setup", "--client", "cursor", "--print"], env, 60)
            record(
                "setup --client cursor --print",
                r.returncode == 0 and "mcpServers" in r.stdout,
                "prints a config, writes nothing",
                t,
            )
    finally:
        if args.report:
            Path(args.report).write_text(
                json.dumps(
                    {
                        "package": args.package,
                        "python": sys.version.split()[0],
                        "platform": sys.platform,
                        "steps": STEPS,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)
    failed = [s for s in STEPS if not s["ok"]]
    print(
        f"\n{len(STEPS) - len(failed)} of {len(STEPS)} steps passed"
        + (f"; FAILED: {', '.join(s['step'] for s in failed)}" if failed else "")
    )
    return 1 if failed or not STEPS else 0


if __name__ == "__main__":
    sys.exit(main())
