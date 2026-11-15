"""S-08: telemetry is off by construction, and indexing and searching make no network call.

Three layers:
  1. the switches: Chroma gets `Settings(anonymized_telemetry=False)` and a forced environment variable, and
     Hugging Face's telemetry variable is forced to 1, whatever the user's environment says (ONNX Runtime's variable
     is forced by `import locus` and checked in tests/test_ort_telemetry_off.py);
  2. a child process (not under pytest: Chroma turns telemetry off by itself when pytest is loaded, which would hide a
     regression) indexes a folder and searches it with every Python-level network entry point recording and refusing
     the call: `getaddrinfo`, `connect`, `connect_ex`, `create_connection`;
  3. a control in the same child shows that the recorder would have caught a connection.

Limit, stated plainly: the recorder sees calls made through Python's `socket` module (urllib, requests, httpx, the
huggingface_hub client). It cannot see a socket opened inside a native library, and ONNX Runtime 1.31 and later has one
(Microsoft's usage-telemetry uploader in the macOS and Linux builds); that one is switched off by its own variable and
checked by the files it would create (tests/test_ort_telemetry_off.py), not by this recorder. chromadb 1.5.9's
telemetry class is an empty stub, and fastembed 0.9.1 contains no telemetry code (checked by reading both); layer 1 is
what keeps that true if a later release changes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import types
from pathlib import Path

import pytest

pytest.importorskip("chromadb")

SRC = Path(__file__).resolve().parents[1] / "src"


def test_chroma_client_has_telemetry_off_even_when_the_environment_asks_for_it(tmp_path, monkeypatch):
    from locus.ingest.store import open_chroma_client

    monkeypatch.setenv("ANONYMIZED_TELEMETRY", "True")
    client = open_chroma_client(tmp_path / "chroma")
    try:
        assert client.get_settings().anonymized_telemetry is False
        assert os.environ["ANONYMIZED_TELEMETRY"] == "False"
    finally:
        client.close()


def test_every_store_open_goes_through_the_one_helper():
    """No other module may call chromadb.PersistentClient directly: that would skip the telemetry switch."""
    offenders = [
        str(p.relative_to(SRC))
        for p in SRC.rglob("*.py")
        if "chromadb.PersistentClient(" in p.read_text(encoding="utf-8") and p.name != "store.py"
    ]
    store_source = (SRC / "locus" / "ingest" / "store.py").read_text(encoding="utf-8")
    assert offenders == []
    assert store_source.count("chromadb.PersistentClient(") == 1  # inside open_chroma_client only


def test_hugging_face_telemetry_is_forced_off_before_any_model_load(monkeypatch):
    from locus.ingest import embedder as emb

    monkeypatch.setenv("HF_HUB_DISABLE_TELEMETRY", "0")
    emb.prepare_hf_download()
    assert os.environ["HF_HUB_DISABLE_TELEMETRY"] == "1"


def test_building_the_local_embedder_sets_it_on_the_torch_route_too(monkeypatch):
    from locus.ingest.embedder import LocalEmbedder

    class FakeModel:
        max_seq_length = 512

    fake = types.SimpleNamespace(SentenceTransformer=lambda *a, **k: FakeModel())
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake)
    monkeypatch.setenv("LOCUS_LOCAL_BACKEND", "torch")
    monkeypatch.setenv("HF_HUB_DISABLE_TELEMETRY", "0")
    LocalEmbedder("example/model")
    assert os.environ["HF_HUB_DISABLE_TELEMETRY"] == "1"


CHILD = textwrap.dedent(
    """
    import json, os, socket, sys, time

    attempts = []

    def refuse(kind):
        def blocked(*args, **kwargs):
            attempts.append(kind)
            raise OSError("network blocked by the test")
        return blocked

    socket.getaddrinfo = refuse("getaddrinfo")
    socket.socket.connect = refuse("connect")
    socket.socket.connect_ex = refuse("connect_ex")
    socket.create_connection = refuse("create_connection")

    from locus import cli

    def run(argv):
        try:
            cli.main(argv)
        except SystemExit as exit_:
            return exit_.code or 0
        return 0

    folder = sys.argv[1]
    codes = {
        "index": run(["index", folder, "--prune"]),
        "query": run(["query", "zebra", "-k", "2"]),
        "stats": run(["stats"]),
        "estimate": run(["estimate", folder, "--sample-seconds", "1"]),
    }
    time.sleep(1.0)  # a background telemetry thread would have made its call by now
    during = list(attempts)

    try:  # the control: the recorder must see a connection when one is attempted
        socket.create_connection(("example.com", 80), timeout=1)
    except OSError:
        pass
    print(json.dumps({
        "codes": codes,
        "attempts": during,
        "control_seen": len(attempts) > len(during),
        "pytest_loaded": "pytest" in sys.modules,
        "chroma_env": os.environ.get("ANONYMIZED_TELEMETRY"),
    }))
    """
)


def test_indexing_and_searching_make_no_network_call(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "zebra.md").write_text("The zebra release plan: ship on Monday.")
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith(("LOCUS_", "HF_", "ANONYMIZED_"))},
        "PYTHONPATH": str(SRC),
        "HOME": str(tmp_path / "home"),
        "USERPROFILE": str(tmp_path / "home"),
        "LOCUS_DATA_DIR": str(tmp_path / "data"),
        "LOCUS_EMBEDDER": "hash",
        "LOCUS_ALLOW_TEST_EMBEDDER": "1",  # the hash embedder is refused at runtime without it
        "ANONYMIZED_TELEMETRY": "True",  # a user environment asking for telemetry must not win
        "HF_HUB_DISABLE_TELEMETRY": "0",
    }
    (tmp_path / "home").mkdir()
    done = subprocess.run(
        [sys.executable, "-c", CHILD, str(notes)], env=env, cwd=tmp_path, capture_output=True, text=True, timeout=180
    )
    assert done.returncode == 0, done.stderr[-2000:]
    result = json.loads(done.stdout.strip().splitlines()[-1])
    assert result["codes"] == {"index": 0, "query": 0, "stats": 0, "estimate": 0}, done.stderr[-2000:]
    assert result["pytest_loaded"] is False  # Chroma would switch telemetry off by itself under pytest
    assert result["control_seen"] is True  # the recorder works
    assert result["attempts"] == []
    assert result["chroma_env"] == "False"
    assert "zebra.md" in done.stdout  # the query printed the indexed file
