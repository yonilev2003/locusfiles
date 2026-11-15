# ruff: noqa: E501 - long literals in test data
"""One on-device embedding model shared through a Unix socket (prototype, opt-in).

Offline: the helper serves the HashEmbedder (`--model hash`); no model is loaded. Sockets live in a short /tmp folder
because AF_UNIX paths are limited to 104 bytes on macOS and the test scratch folders are longer.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from locus.ingest import shared_embedder as se
from locus.ingest.embedder import HashEmbedder


@pytest.fixture()
def sockdir():
    d = Path(tempfile.mkdtemp(prefix="lxe", dir="/tmp"))
    os.chmod(d, 0o700)
    yield d
    # stop any helper a test started, then remove the folder
    for sock in d.glob("*.sock"):
        client = se.SharedEmbedderClient("hash", sock, spawn=False)
        info = client._ping()
        if info:
            try:
                os.kill(info["pid"], 15)
            except OSError:
                pass
    time.sleep(0.5)
    shutil.rmtree(d, ignore_errors=True)


def _wait(cond, timeout=15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.1)
    return False


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    # a zombie child of this test process still answers signal 0
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(out) and not out.startswith("Z")


@pytest.fixture()
def thread_server(sockdir):
    path = sockdir / "e.sock"
    server = se.EmbedServer(HashEmbedder(), path, idle_seconds=60)
    t = threading.Thread(target=server.serve, daemon=True)
    t.start()
    assert _wait(path.exists)
    yield server, path
    server.stop()
    t.join(5)


def test_vectors_are_identical_to_in_process_ones(thread_server):
    _, path = thread_server
    client = se.SharedEmbedderClient("hash", path, name="hash-256", spawn=False)
    local = HashEmbedder()
    texts = ["שלום עולם", "minimum wage", "x" * 5000] + [f"chunk {i}" for i in range(70)]  # more than one batch
    assert client.embed_documents(texts) == local.embed_documents(texts)
    assert client.embed_query("שאלה על שכר") == local.embed_query("שאלה על שכר")
    assert client.name == "hash-256"


def test_a_ping_does_not_keep_the_helper_alive(sockdir):
    path = sockdir / "e.sock"
    server = se.EmbedServer(HashEmbedder(), path, idle_seconds=2)
    t = threading.Thread(target=server.serve, daemon=True)
    t.start()
    assert _wait(path.exists)
    client = se.SharedEmbedderClient("hash", path, spawn=False)
    deadline = time.monotonic() + 15
    while t.is_alive() and time.monotonic() < deadline:  # `locus stats` every half second must not postpone the exit
        client._ping()
        time.sleep(0.5)
    t.join(1)
    assert not t.is_alive(), "pings kept the idle helper in memory"


def test_the_socket_and_lock_are_private(thread_server):
    _, path = thread_server
    assert (path.stat().st_mode & 0o777) == 0o600
    assert (path.with_suffix(".lock").stat().st_mode & 0o777) == 0o600


def test_a_bad_request_does_not_stop_the_helper(thread_server):
    _, path = thread_server
    client = se.SharedEmbedderClient("hash", path, spawn=False)
    for bad in ({"op": "nope"}, {"op": "embed_documents", "texts": "not a list"}, {"op": "embed_documents", "texts": [1, 2]},
                {"op": "embed_documents", "texts": ["a"] * (se.MAX_TEXTS_PER_REQUEST + 1)}, [], {"op": "embed_query"}):  # fmt: skip
        with pytest.raises(se.SharedEmbedderError):
            client._request(bad)
    with socket.socket(socket.AF_UNIX) as s:  # garbage bytes and a half frame
        s.connect(str(path))
        s.sendall(b"\xff\xff\xff\xff junk")
    with socket.socket(socket.AF_UNIX) as s:
        s.connect(str(path))
        s.sendall(b"\x00\x00")
    assert client.embed_query("still alive") == HashEmbedder().embed_query("still alive")


def test_concurrent_clients_get_their_own_answers(thread_server):
    _, path = thread_server
    local = HashEmbedder()
    errors: list[str] = []

    def work(k: int) -> None:
        c = se.SharedEmbedderClient("hash", path, spawn=False)
        for i in range(25):
            q = f"client {k} query {i}"
            try:
                if c.embed_query(q) != local.embed_query(q):
                    errors.append(q)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{q}: {type(exc).__name__}")

    threads = [threading.Thread(target=work, args=(k,)) for k in range(16)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []


def test_a_second_helper_on_the_same_socket_exits_at_once(thread_server):
    _, path = thread_server
    assert se.EmbedServer(HashEmbedder(), path, 60).serve() is False
    assert se.SharedEmbedderClient("hash", path, spawn=False)._ping() is not None  # the first one is untouched


def test_stopping_removes_the_socket(sockdir):
    path = sockdir / "e.sock"
    server = se.EmbedServer(HashEmbedder(), path, idle_seconds=60)
    t = threading.Thread(target=server.serve, daemon=True)
    t.start()
    assert _wait(path.exists)
    server.stop()
    t.join(5)
    assert not path.exists()


def test_no_helper_and_no_permission_to_start_one_is_a_clear_error(sockdir):
    c = se.SharedEmbedderClient("hash", sockdir / "none.sock", spawn=False)
    with pytest.raises(se.SharedEmbedderError, match="starting one is switched off"):
        c.embed_query("x")


def test_the_client_starts_the_helper_shares_it_and_it_exits_when_idle(sockdir):
    path = sockdir / "e.sock"
    a = se.SharedEmbedderClient("hash", path, name="hash-256", idle=3, start_timeout=30)
    assert a.embed_query("first") == HashEmbedder().embed_query("first")  # starts the helper
    info = a._ping()
    pid = info["pid"]
    assert pid != os.getpid() and _alive(pid)
    b = se.SharedEmbedderClient("hash", path, name="hash-256", idle=3, start_timeout=30)
    assert b._ping()["pid"] == pid  # a second client reuses it: one helper, one model
    assert a.window_tokens is None  # the hash embedder has no window
    assert (
        se.SharedEmbedderClient("intfloat/multilingual-e5-small", sockdir / "none.sock", spawn=False).window_tokens
        == 512
    )
    assert se.SharedEmbedderClient("BAAI/bge-m3", sockdir / "none.sock", spawn=False).window_tokens == 8192
    assert not (sockdir / "none.sock").exists()  # asking for a window starts nothing
    # silence: the helper unloads itself
    assert _wait(lambda: not _alive(pid), timeout=20), "the helper did not exit when idle"
    assert not path.exists()
    # the next request starts a fresh one
    assert a.embed_query("again") == HashEmbedder().embed_query("again")
    assert a._ping()["pid"] != pid


def test_two_clients_starting_at_once_end_up_with_one_helper(sockdir):
    path = sockdir / "e.sock"
    pids: list[int] = []

    def go() -> None:
        c = se.SharedEmbedderClient("hash", path, idle=30, start_timeout=30)
        c.embed_query("race")
        pids.append(c._ping()["pid"])

    ts = [threading.Thread(target=go) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(set(pids)) == 1


def test_start_in_background_returns_at_once_and_the_next_request_does_not_start_a_second_helper(sockdir, monkeypatch):
    """The serve warm-up starts the helper first so the model load overlaps with opening the index. The request that
    follows (from another client object in the same process, as in `locus serve`) must wait for THAT helper, not start
    a second process. `hash-slow` is a model that takes 3 s to load."""
    path = sockdir / "e.sock"
    spawned: list[str] = []
    real_spawn = se.SharedEmbedderClient._spawn

    def counting_spawn(self):
        spawned.append(str(self.path))
        return real_spawn(self)

    monkeypatch.setattr(se.SharedEmbedderClient, "_spawn", counting_spawn)
    se._SPAWNED.clear()
    first = se.SharedEmbedderClient("hash-slow", path, name="hash-256", idle=30, start_timeout=60)
    started = time.monotonic()
    first.start_in_background()
    assert time.monotonic() - started < 2.0  # did not wait for the 3 s load
    assert spawned == [str(path)]

    second = se.SharedEmbedderClient("hash-slow", path, name="hash-256", idle=30, start_timeout=60)
    assert second.embed_query("x") == HashEmbedder().embed_query("x")  # waited for the helper that was starting
    assert spawned == [str(path)]  # still one


def test_start_in_background_never_raises_and_respects_spawn_off(sockdir, monkeypatch):
    spawned: list[int] = []
    monkeypatch.setattr(se.SharedEmbedderClient, "_spawn", lambda self: spawned.append(1))
    se._SPAWNED.clear()
    se.SharedEmbedderClient("hash", sockdir / "off.sock", spawn=False).start_in_background()
    assert spawned == []  # starting one is switched off

    def boom(self):
        raise OSError("no permission")

    monkeypatch.setattr(se.SharedEmbedderClient, "_spawn", boom)
    se.SharedEmbedderClient("hash", sockdir / "boom.sock").start_in_background()  # swallowed


def test_start_helper_early_only_for_a_local_embedder_with_the_helper_switched_on(tmp_path, monkeypatch):
    from types import SimpleNamespace

    started: list[str] = []
    monkeypatch.setattr(se.SharedEmbedderClient, "start_in_background", lambda self: started.append(self.model))

    def settings(kind: str) -> SimpleNamespace:
        return SimpleNamespace(embedder=kind, data_dir=tmp_path, local_model="BAAI/bge-small-en-v1.5")

    monkeypatch.setenv(se.ENV_ENABLE, "1")
    se.start_helper_early(settings("local"))
    assert started == ["BAAI/bge-small-en-v1.5"]
    started.clear()
    for kind in ("voyage", "openai", "managed", "hash"):
        se.start_helper_early(settings(kind))  # no model on this computer to load, or not through the helper
    monkeypatch.setenv(se.ENV_ENABLE, "0")
    se.start_helper_early(settings("local"))  # an explicit off wins
    assert started == []


def test_off_by_default_and_the_socket_path_is_short(tmp_path):
    assert se.shared_embedder_enabled({}) is False
    for on in ("1", "true", "YES", "on"):
        assert se.shared_embedder_enabled({"LOCUS_SHARED_EMBEDDER": on})
    assert se.idle_seconds({}) == 300 and se.idle_seconds({"LOCUS_EMBEDDER_IDLE_SECONDS": "oops"}) == 300
    assert se.idle_seconds({"LOCUS_EMBEDDER_IDLE_SECONDS": "1"}) == 5  # never a busy restart loop
    p = se.socket_path_for(Path("/Users/someone/.locus"), "BAAI/bge-m3")
    assert len(str(p)) < 80 and p.suffix == ".sock"


def test_get_embedder_uses_the_helper_only_when_asked_and_never_imports_torch(tmp_path):
    code = (
        "import os, sys\n"
        "from locus.config import DEFAULT_EXTENSIONS, Settings\n"
        "from locus.ingest.embedder import get_embedder\n"
        f"s = Settings(data_dir=__import__('pathlib').Path({str(tmp_path)!r}), collection='coll', embedder='local', voyage_model='',"
        " openai_model='', local_model='intfloat/multilingual-e5-small', chunk_tokens=700, overlap_tokens=100, max_file_mb=5,"
        " extensions=DEFAULT_EXTENSIONS)\n"
        "e = get_embedder(s)\n"
        "assert type(e).__name__ == 'SharedEmbedderClient', type(e)\n"
        "assert e.name == 'local:intfloat/multilingual-e5-small'\n"
        "assert 'torch' not in sys.modules and 'sentence_transformers' not in sys.modules and 'fastembed' not in sys.modules and 'onnxruntime' not in sys.modules\n"
        "print('ok')\n"
    )
    env = dict(os.environ, LOCUS_SHARED_EMBEDDER="1")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    assert r.stdout.strip() == "ok", r.stderr[-800:]
    assert not list(tmp_path.glob("*.sock"))  # building the client starts nothing


def test_without_the_variable_the_local_embedder_is_unchanged(tmp_path, monkeypatch):
    from locus.config import DEFAULT_EXTENSIONS, Settings
    from locus.ingest import embedder as emb

    monkeypatch.delenv("LOCUS_SHARED_EMBEDDER", raising=False)
    made = []

    class Fake:
        def __init__(self, model):
            made.append(model)

    monkeypatch.setattr(emb, "LocalEmbedder", Fake)
    s = Settings(data_dir=tmp_path, collection="coll", embedder="local", voyage_model="", openai_model="", local_model="m/x",
                 chunk_tokens=700, overlap_tokens=100, max_file_mb=5, extensions=DEFAULT_EXTENSIONS)  # fmt: skip
    assert isinstance(emb.get_embedder(s), Fake) and made == ["m/x"]


def test_a_marker_file_switches_it_on_and_an_explicit_off_wins(tmp_path):
    assert se.shared_embedder_enabled({}, tmp_path) is False
    (tmp_path / "shared-embedder").write_text("")
    assert se.shared_embedder_enabled({}, tmp_path) is True
    assert se.shared_embedder_enabled({"LOCUS_SHARED_EMBEDDER": ""}, tmp_path) is True  # an empty variable is "not set"
    assert se.shared_embedder_enabled({"LOCUS_SHARED_EMBEDDER": "0"}, tmp_path) is False
    assert se.shared_embedder_enabled({"LOCUS_SHARED_EMBEDDER": "1"}, tmp_path / "other") is True
    assert se.shared_embedder_enabled({}, None) is False


def test_a_helper_that_cannot_load_its_model_fails_fast_with_the_log_tail(sockdir):
    c = se.SharedEmbedderClient("hash-fail", sockdir / "e.sock", idle=30, start_timeout=60)
    t = time.monotonic()
    with pytest.raises(se.SharedEmbedderError, match="stopped with exit code") as e:
        c.embed_query("x")
    assert time.monotonic() - t < 30 and "cannot load the model" in str(e.value)


def test_stats_reports_the_helper_without_starting_it(sockdir, tmp_path, monkeypatch, capsys):
    from locus import cli

    data = sockdir  # the fixture removes helpers it finds here
    (data / "shared-embedder").write_text("")
    monkeypatch.setattr("locus.config.load_dotenv", lambda *a, **k: False)
    monkeypatch.setattr("locus.config.find_dotenv", lambda *a, **k: "")
    monkeypatch.delenv("LOCUS_SHARED_EMBEDDER", raising=False)
    monkeypatch.setenv("LOCUS_DATA_DIR", str(data))
    monkeypatch.setenv("LOCUS_EMBEDDER", "local")
    monkeypatch.setenv("LOCUS_LOCAL_MODEL", "hash")
    cli.get_settings.cache_clear()
    try:
        with pytest.raises(SystemExit) as exc:
            cli.main(["stats"])
        assert exc.value.code == 0
    finally:
        cli.get_settings.cache_clear()
    out = capsys.readouterr().out
    assert "embedder   : local:hash" in out and "not running (starts on the first search)" in out
    assert not list(data.glob("*.sock"))  # `stats` did not start a helper


def test_only_the_helper_that_wins_the_lock_loads_the_model(sockdir):
    """Several clients starting together each spawn a helper. The losers must exit BEFORE loading the model (found on
    the maintainer's Mac: four helpers loaded 1.5 GB each at once)."""
    path = sockdir / "e.sock"
    cmd = [
        sys.executable,
        "-m",
        "locus.ingest.shared_embedder",
        "serve",
        "--model",
        "hash-slow",
        "--socket",
        str(path),
        "--idle",
        "20",
    ]
    procs = [
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(4)
    ]
    try:
        time.sleep(1.5)  # the winner is still loading (3 s); the three losers must already be gone
        assert sum(1 for p in procs if p.poll() is None) == 1
        assert all(p.poll() == 0 for p in procs if p.poll() is not None)
        assert _wait(lambda: se.SharedEmbedderClient("hash-slow", path, spawn=False)._ping() is not None, timeout=20)
        assert se.SharedEmbedderClient("hash-slow", path, spawn=False)._ping()["pid"] in {p.pid for p in procs}
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
        for p in procs:
            p.wait(10)


# --- device of the helper's model (stability G3, 2026-10-31): cpu by default, 877 MB less than MPS for bge-small
def test_helper_device_defaults_to_cpu_and_can_be_overridden():
    from locus.ingest.shared_embedder import helper_device

    assert helper_device({}) == "cpu"
    assert helper_device({"LOCUS_EMBEDDER_DEVICE": "  "}) == "cpu"
    assert helper_device({"LOCUS_EMBEDDER_DEVICE": "MPS"}) == "mps"
    assert helper_device({"LOCUS_EMBEDDER_DEVICE": "auto"}) is None


def test_local_embedder_passes_the_device_only_when_given(monkeypatch):
    import sys
    import types

    from locus.ingest.embedder import LocalEmbedder

    seen: list[dict] = []

    class Fake:
        max_seq_length = 512

        def __init__(self, model, **kwargs):
            seen.append(kwargs)

    monkeypatch.setitem(sys.modules, "sentence_transformers", types.SimpleNamespace(SentenceTransformer=Fake))
    LocalEmbedder("some/model", device="cpu")
    LocalEmbedder("some/model")
    assert seen == [{"local_files_only": True, "device": "cpu"}, {"local_files_only": True}]


def test_build_embedder_loads_the_model_on_the_helper_device(monkeypatch):
    import locus.ingest.embedder as emb
    from locus.ingest import shared_embedder as se

    got = {}

    class Fake:
        def __init__(self, model, device=None):
            got["device"] = device

    monkeypatch.setattr(emb, "LocalEmbedder", Fake)
    monkeypatch.delenv("LOCUS_EMBEDDER_DEVICE", raising=False)
    se._build_embedder("some/model")
    assert got["device"] == "cpu"
    monkeypatch.setenv("LOCUS_EMBEDDER_DEVICE", "auto")
    se._build_embedder("some/model")
    assert got["device"] is None


def test_a_long_data_dir_gets_a_short_private_socket_folder_and_a_working_helper(tmp_path):
    long_dir = tmp_path
    for part in ("a_very_long_folder_name_for_the_locus_data", "and_another_long_level_below_it") * 2:
        long_dir = long_dir / part
    assert len(os.fsencode(long_dir / "embedder-00000000.sock")) > 104  # would fail to bind as it was
    p = se.socket_path_for(long_dir, "hash")
    assert len(os.fsencode(p)) <= se.SOCKET_PATH_MAX and p.suffix == ".sock"
    assert p.parent.name == f"locus-{os.getuid()}" and (p.parent.stat().st_mode & 0o777) == 0o700
    assert p == se.socket_path_for(long_dir, "hash")  # stable between processes
    assert p != se.socket_path_for(long_dir / "other", "hash")  # two data folders never share a helper
    assert se.socket_path_for(Path("/Users/someone/.locus"), "hash").parent == Path(
        "/Users/someone/.locus"
    )  # short: unchanged
    client = se.SharedEmbedderClient("hash", p, idle=5, start_timeout=60)
    assert len(client.embed_query("hello")) == len(HashEmbedder().embed_query("hello"))  # exits by itself when idle


def test_no_short_folder_means_a_clear_error_without_file_text(tmp_path, monkeypatch):
    monkeypatch.setattr(se, "SOCKET_PATH_MAX", 10)  # nothing, not even /tmp/locus-<uid>/, fits
    with pytest.raises(se.SharedEmbedderError) as err:
        se.socket_path_for(tmp_path / "data", "hash")
    assert "LOCUS_DATA_DIR" in str(err.value) and "too deep" in str(err.value)
