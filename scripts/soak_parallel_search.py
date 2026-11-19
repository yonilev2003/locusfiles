"""Soak test: N parallel locus_search calls while files in the folder change underneath them.

Runs on a COPY of the demo vault in a fresh temporary folder, indexed with the offline hash embedder. It never opens
the user's own index: the data directory is created here and the script refuses to run against any other.

    python scripts/soak_parallel_search.py [--parallel 16] [--rounds 5] [--edits 3]

Reports calls, errors, latency (p50, p95, max) and how many answers carried the "changed since indexing" note.
Exit code 1 when any call raised or returned an error text.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("LOCUS_ALLOW_TEST_EMBEDDER", "1")

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

from locus import mcp_server as srv  # noqa: E402
from locus.ingest.embedder import HashEmbedder  # noqa: E402
from locus.ingest.indexer import index_folder  # noqa: E402
from locus.ingest.store import ChromaStore  # noqa: E402
from tests.demo_index import QUERIES, VAULT, make_settings  # noqa: E402


def _percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * pct))]


async def _soak(parallel: int, rounds: int, edits: int, vault: Path) -> int:
    latencies: list[float] = []
    errors: list[str] = []
    stale = 0
    files = sorted(vault.rglob("*.md"))

    async def one(i: int) -> None:
        nonlocal stale
        started = time.perf_counter()
        try:
            out = await srv.locus_search(query=QUERIES[i % len(QUERIES)], top_k=5)
        except Exception as exc:  # noqa: BLE001 - the soak reports every failure
            errors.append(f"{type(exc).__name__}: {exc}")
            return
        latencies.append(time.perf_counter() - started)
        if out.startswith("Error") or "## 1." not in out:
            errors.append(out[:120])
        if "changed since" in out.lower():
            stale += 1

    for r in range(rounds):
        for f in files[r * edits : (r + 1) * edits]:  # touch a few files: they become pending changed files
            f.write_text(f.read_text() + f"\nedit in round {r}\n")
        await asyncio.gather(*(one(i) for i in range(parallel)))

    print(f"calls={parallel * rounds} ok={len(latencies)} errors={len(errors)} stale_note={stale}")
    if latencies:
        print(
            f"latency_s p50={statistics.median(latencies):.3f} p95={_percentile(latencies, 0.95):.3f} "
            f"max={max(latencies):.3f}"
        )
    for e in errors[:5]:
        print(f"error: {e}")
    return 1 if errors else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--parallel", type=int, default=16)
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--edits", type=int, default=3, help="files changed before each round")
    args = ap.parse_args()
    base = Path(tempfile.mkdtemp(prefix="locus-soak-")).resolve()
    try:
        vault = base / "vault"
        shutil.copytree(VAULT, vault)
        settings = make_settings(base / "data")
        embedder = HashEmbedder()
        store = ChromaStore(settings.data_dir, settings.collection, embedder.name)
        index_folder(vault, embedder, store, settings, log=lambda _m: None)
        srv._components = lambda: (embedder, store, settings)  # type: ignore[assignment]
        return asyncio.run(_soak(args.parallel, args.rounds, args.edits, vault))
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
