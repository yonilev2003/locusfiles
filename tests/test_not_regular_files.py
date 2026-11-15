"""A named pipe (FIFO), socket or device in an indexed folder is never opened.

Opening a FIFO for reading waits for a writer and never returns. Before this fix `locus index` blocked for ever on one
(a 150 s wall clock killed the run in the security self-assessment of 2026-11-15), and the file watcher and the
freshness step that `locus_search` runs could block the same way. Every test here runs the code in a child process
under a time limit, so a regression shows as a failure ("hung") and not as a test run that never ends."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytest.importorskip("chromadb")

pytestmark = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="named pipes need os.mkfifo (not on Windows)")

SRC = Path(__file__).resolve().parents[1] / "src"
LIMIT = int(os.environ.get("LOCUS_TEST_PIPE_LIMIT", "45"))  # seconds; the work itself takes about two

PRELUDE = textwrap.dedent(
    """
    import json, os, sys
    from pathlib import Path
    from locus.config import DEFAULT_EXTENSIONS, Settings
    from locus.ingest.embedder import HashEmbedder
    from locus.ingest.store import ChromaStore

    work = Path(sys.argv[1])
    root = work / "notes"
    settings = Settings(
        data_dir=work / "data", collection="pipes", embedder="hash", voyage_model="", openai_model="",
        local_model="", chunk_tokens=100, overlap_tokens=10, max_file_mb=5, extensions=DEFAULT_EXTENSIONS,
    )
    embedder = HashEmbedder()
    store = ChromaStore(settings.data_dir, settings.collection, embedder.name)
    """
)


def run(tmp_path: Path, body: str) -> dict:
    """Run PRELUDE + body in a child process and return the JSON it prints; a hang fails the test."""
    root = tmp_path / "notes"
    root.mkdir(exist_ok=True)
    (root / "plan.md").write_text("the zebra release plan, written down")
    os.mkfifo(root / "pipe.txt")  # a text-looking name: it would be picked up for indexing
    env = {**os.environ, "PYTHONPATH": str(SRC), "HOME": str(tmp_path)}
    try:
        done = subprocess.run(
            [sys.executable, "-c", PRELUDE + textwrap.dedent(body), str(tmp_path)],
            env=env, capture_output=True, text=True, timeout=LIMIT,
        )  # fmt: skip
    except subprocess.TimeoutExpired:
        pytest.fail(f"hung: the child was still running after {LIMIT} s (a named pipe was opened)")
    assert done.returncode == 0, done.stderr[-1500:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_the_walker_skips_a_pipe_and_says_why(tmp_path):
    out = run(
        tmp_path,
        """
        from locus.ingest.indexer import iter_files
        seen = []
        files = [p.name for p in iter_files(root, settings, lambda reason, p: seen.append([reason, p.name]))]
        print(json.dumps({"files": files, "skipped": seen}))
        """,
    )
    assert out["files"] == ["plan.md"] and ["not_regular", "pipe.txt"] in out["skipped"]


def test_indexing_a_folder_with_a_pipe_finishes_and_indexes_the_rest(tmp_path):
    out = run(
        tmp_path,
        """
        from locus.ingest.indexer import index_folder
        lines = []
        stats = index_folder(root, embedder, store, settings, log=lines.append)
        print(json.dumps({"seen": stats.files_seen, "indexed": stats.files_indexed, "errors": stats.errors,
                          "paths": [Path(p).name for p in store.list_paths()], "log": lines}))
        """,
    )
    assert (out["seen"], out["indexed"], out["errors"], out["paths"]) == (1, 1, [], ["plan.md"])
    assert any("1 named pipe(s), socket(s) or device file(s): they are never opened" in line for line in out["log"])
    assert not any("pipe.txt" in line for line in out["log"])  # counts only, no file names


def test_index_file_on_a_pipe_returns_at_once_and_says_so(tmp_path):
    out = run(
        tmp_path,
        """
        from locus.ingest.indexer import IndexStats, index_file
        lines, stats = [], IndexStats()
        written = index_file(root / "pipe.txt", root, embedder, store, settings, stats, log=lines.append)
        print(json.dumps({"written": written, "skipped": stats.files_skipped, "log": lines}))
        """,
    )
    assert out["written"] == 0 and out["skipped"] == 1 and "is not a regular file" in out["log"][0]


def test_hashing_a_pipe_is_refused_not_waited_on(tmp_path):
    out = run(
        tmp_path,
        """
        from locus.ingest.indexer import NotRegularFile, file_sha256
        try:
            file_sha256(root / "pipe.txt")
            result = "hashed"
        except NotRegularFile:
            result = "refused"
        print(json.dumps({"result": result}))
        """,
    )
    assert out["result"] == "refused"


def test_a_regular_file_is_hashed_as_before(tmp_path):
    import hashlib

    from locus.ingest.indexer import file_sha256

    target = tmp_path / "a.txt"
    target.write_bytes(b"some bytes" * 100_000)
    assert file_sha256(target) == hashlib.sha256(b"some bytes" * 100_000).hexdigest()


def test_the_freshness_step_of_a_search_does_not_stall_on_a_pipe(tmp_path):
    out = run(
        tmp_path,
        """
        from locus.ingest.freshness import freshen
        from locus.ingest.indexer import index_folder
        from locus.policy import Matcher, Policy
        os.unlink(root / "pipe.txt")
        index_folder(root, embedder, store, settings, log=lambda m: None)
        (root / "new.md").write_text("a new note about the release, added after indexing")
        os.mkfifo(root / "late.txt")
        matcher = Matcher(Policy(roots=(str(root),)), work)
        report = freshen([root], matcher, embedder, store, settings, budget_s=10.0)
        print(json.dumps({"indexed": report.indexed, "errors": report.errors,
                          "paths": sorted(Path(p).name for p in store.list_paths())}))
        """,
    )
    assert out["indexed"] == 1 and out["errors"] == 0 and out["paths"] == ["new.md", "plan.md"]


def test_the_watcher_ignores_a_pipe_and_can_be_stopped(tmp_path):
    """A smoke test, not a regression proof: on macOS this also passed on the code before the fix (the file-system
    event service may not report a pipe being created), so the fix in the watcher is covered by reading, not by this."""
    out = run(
        tmp_path,
        """
        import threading, time
        from locus.ingest.watcher import watch_folder
        os.unlink(root / "pipe.txt")
        stop = threading.Event()
        thread = threading.Thread(
            target=lambda: watch_folder(
                root, embedder, store, settings, debounce_seconds=0.1, poll_seconds=0.1, stop=stop
            ),
            daemon=True,
        )
        thread.start()
        time.sleep(0.5)
        os.mkfifo(root / "later.txt")
        (root / "other.md").write_text("a note written while watching")
        deadline = time.time() + 15
        while time.time() < deadline and "other.md" not in {Path(p).name for p in store.list_paths()}:
            time.sleep(0.1)
        time.sleep(0.5)
        stop.set()
        thread.join(timeout=15)
        print(json.dumps({"stopped": not thread.is_alive(),
                          "paths": sorted(Path(p).name for p in store.list_paths())}))
        """,
    )
    assert out["stopped"] is True and out["paths"] == ["other.md"]


def test_read_file_refuses_an_indexed_path_that_became_a_pipe(tmp_path):
    out = run(
        tmp_path,
        """
        import asyncio
        from locus import mcp_server as srv
        from locus.ingest.indexer import index_folder
        os.unlink(root / "pipe.txt")
        index_folder(root, embedder, store, settings, log=lambda m: None)
        target = root / "plan.md"
        os.unlink(target)
        os.mkfifo(target)  # swapped after indexing
        srv._components = lambda: (embedder, store, settings)
        text = asyncio.run(srv.locus_read_file(str(target.resolve())))
        print(json.dumps({"text": text}))
        """,
    )
    assert "no longer a regular file" in out["text"] and "zebra" not in out["text"]
