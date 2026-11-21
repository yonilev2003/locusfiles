"""`locus serve` over stdio is what it always was, even with the tunnel settings in the environment: the four tools, no
`search`/`fetch`, and nothing on stdout but MCP messages (one JSON object per line)."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

import locus

SRC = str(Path(locus.__file__).resolve().parent.parent)
TOKEN = "Tq7-remote-test-token-0123456789abcdef-XYZ"


def _env(tmp_path: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("LOCUS_")}
    env.update(
        {
            "PYTHONPATH": SRC,
            "PYTHONIOENCODING": "utf-8",
            "HOME": str(tmp_path / "home"),
            "LOCUS_DATA_DIR": str(tmp_path / "data"),
            "LOCUS_EMBEDDER": "hash",
            "LOCUS_ALLOW_TEST_EMBEDDER": "1",
            "LOCUS_MCP_PUBLIC_HOST": "example.test",  # a tunnel setting: the stdio transport must ignore it
            "LOCUS_MCP_TOKEN": TOKEN,
        }
    )
    return env


def test_stdio_lists_the_four_tools_and_writes_only_protocol_messages_to_stdout(tmp_path):
    proc = subprocess.Popen(
        [sys.executable, "-m", "locus.cli", "serve"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        cwd=tmp_path,  # no .env of the repo
        env=_env(tmp_path),
    )
    lines: queue.Queue[str] = queue.Queue()
    stderr_text: list[str] = []
    threading.Thread(target=lambda: [lines.put(x) for x in proc.stdout], daemon=True).start()
    threading.Thread(target=lambda: stderr_text.append(proc.stderr.read()), daemon=True).start()

    def send(message: dict) -> None:
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()

    seen: list[str] = []
    try:
        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "0"},
                },
            }
        )
        seen.append(lines.get(timeout=60))
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        seen.append(lines.get(timeout=60))
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    while not lines.empty():
        seen.append(lines.get())
    assert seen, "no output on stdout"
    messages = [json.loads(line) for line in seen]  # every stdout line is JSON: a stray print would fail here
    assert all(m.get("jsonrpc") == "2.0" for m in messages), seen
    listed = next(m for m in messages if m.get("id") == 2)
    assert {t["name"] for t in listed["result"]["tools"]} == {
        "locus_search",
        "locus_list_sources",
        "locus_read_file",
        "locus_help",
    }
    assert TOKEN not in "".join(seen) + "".join(stderr_text)
