"""Lane M regression: a tool call right after `initialize` must not race the warm-up thread (sale uplift B1/B2).
Drives the real `locus serve` over stdio with the official MCP client against the demo vault; the embedder is the
real LocalEmbedder over a model double (slow load, short encode that exits the process if two threads are in it).
The second test swaps LocalEmbedder's encode lock for contextlib.nullcontext and must see the server die: not vacuous.
sentence_transformers is faked in sys.modules by a launcher script written to tmp_path: no model, no network, no key.
About 10 s; on a failure read the server stderr in the message (a "TRAP:" line means encode ran without its lock).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client

import locus

pytest.importorskip("chromadb")  # `locus index` / `locus serve` need the vector store

FIXTURES = Path(__file__).resolve().parent / "fixtures"
KEY = json.loads((FIXTURES / "demo-vault-key.json").read_text(encoding="utf-8"))
Q1 = next(q for q in KEY["questions"] if q["id"] == "Q1")  # two documents disagree: a multi-file question

# The invariant part of a locus_search hit header ("## 3. /abs/path/file.md  (chunk 2, score 0.412"): rank,
# path, chunk_index and score (constraint 2); what follows the score is presentation.
HIT_LINE = re.compile(r"^## (\d+)\. (.+?)  \(chunk (\d+), score (-?\d+\.\d+)", re.MULTILINE)
TRAP_MARKER = "TRAP: two threads inside encode"
TIMEOUT_S = 15  # the scenario takes about 4 s (load 1.5 s + two serial encodes of 0.4 s + start-up)
LOAD_S = 1.5  # model load, held inside the server's lazy-open lock; the first call arrives well inside it
ENCODE_S = 0.4  # long enough that the warm-up query and the first search overlap if nothing serialises them

# Written to tmp_path and run as `python serve_trap.py <locus cli args>`. It fakes the one import
# LocalEmbedder.__init__ makes (`from sentence_transformers import SentenceTransformer`) with a double, so the
# REAL LocalEmbedder (its __init__, its _encode_lock, embed_query / embed_documents) runs in the REAL server.
# The double's vectors are the offline HashEmbedder's, so `locus index` and `locus serve` agree through it.
LAUNCHER = '''\
import contextlib
import importlib.machinery
import os
import sys
import threading
import time
import types

from locus.cli import main
from locus.ingest.embedder import HashEmbedder, LocalEmbedder

LOAD_S = float(os.environ.get("TRAP_LOAD_S", "0"))
ENCODE_S = float(os.environ.get("TRAP_ENCODE_S", "0"))
NO_LOCK = os.environ.get("TRAP_NO_LOCK") == "1"

_hash = HashEmbedder()
_guard = threading.Lock()
_active = 0


class _Vectors:
    def __init__(self, rows):
        self._rows = rows

    def tolist(self):
        return self._rows


class SentenceTransformer:
    """Model-shaped: slow constructor (the load), encode(texts, **kwargs).tolist(), not re-entrant."""

    def __init__(self, model_name):
        time.sleep(LOAD_S)

    def encode(self, texts, **_kwargs):
        global _active
        with _guard:
            _active += 1
            overlapping = _active > 1
        try:
            if overlapping:  # the Lane M failure class: the process dies the way a Metal assertion kills it
                print("TRAP: two threads inside encode", file=sys.stderr, flush=True)
                os._exit(134)
            time.sleep(ENCODE_S)
            return _Vectors([_hash._vector(t) for t in texts])
        finally:
            with _guard:
                _active -= 1


fake = types.ModuleType("sentence_transformers")
fake.__spec__ = importlib.machinery.ModuleSpec("sentence_transformers", None)
fake.SentenceTransformer = SentenceTransformer
sys.modules["sentence_transformers"] = fake

if NO_LOCK:  # the mutation: LocalEmbedder as it was before the fix, encode with no serialisation
    _real_init = LocalEmbedder.__init__

    def _init_without_lock(self, model):
        _real_init(self, model)
        self._encode_lock = contextlib.nullcontext()

    LocalEmbedder.__init__ = _init_without_lock

main(sys.argv[1:])
'''


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _env(served: dict, **launcher: str) -> dict[str, str]:
    """The SDK's safe minimal environment plus the pins both the indexing and the serving process share.
    The pins cover every LOCUS_* setting these assertions depend on, so a developer's .env or shell
    cannot change the outcome."""
    return get_default_environment() | served["env"] | launcher


@pytest.fixture(scope="module")
def served(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """The demo vault indexed once, through the launcher, with the double model's hash vectors."""
    root = tmp_path_factory.mktemp("first-call-race")
    launcher = root / "serve_trap.py"
    launcher.write_text(LAUNCHER, encoding="utf-8")
    vault = FIXTURES / "demo-vault"
    served = {
        "root": root,
        "launcher": launcher,
        "vault": vault,
        "env": {
            # The subprocesses must run this checkout's engine, not whatever `locus` is installed.
            "PYTHONPATH": str(Path(locus.__file__).resolve().parents[1]),
            "LOCUS_EMBEDDER": "local",  # the real LocalEmbedder; the model behind it is the double
            "LOCUS_LOCAL_MODEL": "locus-test/model-double",  # no "bge" in it: no query prefix to account for
            "LOCUS_DATA_DIR": str(root / "data"),
            "LOCUS_COLLECTION": "first-call-race",
            "LOCUS_EXTENSIONS": ".md,.txt,.csv",
            "LOCUS_CHUNK_TOKENS": "120",
            "LOCUS_OVERLAP_TOKENS": "20",
            "LOCUS_MAX_FILE_MB": "5",
            "LOCUS_WARMUP": "1",  # the warm-up thread is the other party of the race
            "HF_HUB_OFFLINE": "1",  # belt and braces: nothing here may reach for a model download
        },
    }
    proc = subprocess.run(
        [sys.executable, str(launcher), "index", str(vault)],
        env=_env(served, TRAP_LOAD_S="0", TRAP_ENCODE_S="0", TRAP_NO_LOCK="0"),
        cwd=root,  # a .env in the current directory (or its parents) is never the repo's
        capture_output=True,
        text=True,
        timeout=100,
    )
    assert proc.returncode == 0, proc.stderr
    assert "errors=0" in proc.stdout and "indexed=0 " not in proc.stdout, proc.stdout
    return served


async def _first_call(served: dict, tag: str, **launcher: str) -> tuple[str, str, float, str]:
    """Start `locus serve` through the launcher, `initialize`, and call `locus_search` at once (what a
    scripted client such as `claude -p` does). Returns (outcome, text, seconds, server stderr), where
    outcome is "answered", "timeout" (no reply in TIMEOUT_S) or "closed" (the connection went away)."""
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(served["launcher"]), "serve"],
        env=_env(served, **launcher),
        cwd=str(served["root"]),
    )
    errlog_path = served["root"] / f"serve-stderr-{tag}.log"
    started = time.monotonic()
    outcome, text = "closed", ""
    try:
        with errlog_path.open("a", encoding="utf-8") as errlog, anyio.fail_after(TIMEOUT_S):
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool("locus_search", {"query": Q1["question"], "top_k": 6})
                    outcome, text = "answered", result.content[0].text
    except TimeoutError:
        outcome = "timeout"
    except Exception as exc:  # noqa: BLE001 - a dead server surfaces as McpError / ClosedResourceError / a group
        text = f"{type(exc).__name__}: {exc}"
    return outcome, text, time.monotonic() - started, errlog_path.read_text(encoding="utf-8")


@pytest.mark.anyio
async def test_first_search_right_after_initialize_is_answered_with_citations(served: dict) -> None:
    outcome, text, seconds, stderr = await _first_call(
        served, "locked", TRAP_LOAD_S=str(LOAD_S), TRAP_ENCODE_S=str(ENCODE_S), TRAP_NO_LOCK="0"
    )

    assert outcome == "answered", f"{outcome} after {seconds:.1f} s: {text[:300]}\n--- server stderr ---\n{stderr}"
    assert TRAP_MARKER not in stderr, stderr
    assert "warming up" in stderr, f"the warm-up thread did not run, so nothing was raced\n{stderr}"

    hits = [(int(rank), path, int(chunk)) for rank, path, chunk, _ in HIT_LINE.findall(text)]
    assert [rank for rank, _, _ in hits] == list(range(1, 7)), f"expected 6 ranked hits\n{text}"
    vault = served["vault"].resolve()
    for _, path, _ in hits:  # every hit is citable: an absolute path inside the indexed vault (+ chunk_index)
        assert Path(path).is_absolute() and Path(path).resolve().is_relative_to(vault), path
    found = {Path(path).relative_to(vault).as_posix() for _, path, _ in hits}
    assert set(Q1["expected_paths"]) <= found, found  # the answer's two disagreeing documents are both cited


@pytest.mark.anyio
async def test_the_same_scenario_without_the_encode_lock_kills_the_server(served: dict) -> None:
    """The trap is real: with LocalEmbedder's lock replaced by contextlib.nullcontext the warm-up query
    and the first search are inside encode together, the double ends the process, and the client sees a
    closed connection (a hang would be the other face of the same failure: "timeout")."""
    outcome, text, seconds, stderr = await _first_call(
        served, "unlocked", TRAP_LOAD_S=str(LOAD_S), TRAP_ENCODE_S=str(ENCODE_S), TRAP_NO_LOCK="1"
    )

    assert outcome in {"closed", "timeout"}, f"expected the server to fail, got {outcome}: {text[:300]}\n{stderr}"
    assert TRAP_MARKER in stderr, f"the server failed, but not through the trap (outcome {outcome}, {text})\n{stderr}"
