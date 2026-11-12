"""One embedding model shared by every `locus serve` / `locus index` on this computer (ADR-089 draft, prototype).

Why: each MCP client starts its own `locus serve`, and each used to hold its own copy of the local model. Measured on
a 16 GB Mac (2026-10-26): 1.6 GB per process with multilingual-e5-small, 3.8 GB with bge-m3. Four clients do not fit
with bge-m3. With this module the model lives in ONE helper process; `locus serve` becomes a thin client that never
imports torch or sentence-transformers.

How it works
- The helper listens on a Unix-domain socket in the data folder (mode 0600, the folder is 0700). It answers three
  requests: `ping`, `embed_documents`, `embed_query`. It reads no files and has no other capability; every request is a
  list of strings in, vectors out. One request is embedded at a time (the on-device encoder is not safe to run twice
  at once, see LocalEmbedder), so concurrent clients queue.
- It is started on demand by the first client that needs it (a detached child, no LaunchAgent, nothing installed) and
  **exits by itself after `idle_seconds` without a request** (default 300), which frees the memory. The next client
  starts it again (a cold start costs the model load time: about 7 s for bge-m3).
- A lock file next to the socket keeps it to one helper per model; a second helper exits at once.
- Opt-in: `LOCUS_SHARED_EMBEDDER=1` with `LOCUS_EMBEDDER=local`. Off by default, so nothing changes until it is chosen.
  The collection name stays `local:<model>`: vectors are identical to in-process ones, so existing collections work.

What it does not do: it is not a network service (no TCP, no relay), it does not keep running, and it never sees
paths or file contents beyond the chunk text that `locus index` embeds anyway. Whether an on-demand, self-exiting helper
counts as a "background service" under the 2026-10-25 incident note is the founder's call (ADR-089 [OPEN]).
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import socket
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ENV_ENABLE = "LOCUS_SHARED_EMBEDDER"
ENV_IDLE = "LOCUS_EMBEDDER_IDLE_SECONDS"
ENV_DEVICE = "LOCUS_EMBEDDER_DEVICE"
DEFAULT_IDLE_SECONDS = 300
MAX_FRAME_BYTES = 64 * 1024 * 1024
MAX_TEXTS_PER_REQUEST = 256
MAX_CHARS_PER_REQUEST = 4_000_000
DOC_BATCH = 32  # texts per round trip from `embed_documents`


MARKER_NAME = "shared-embedder"


def shared_embedder_enabled(env: dict[str, str] | None = None, data_dir: Path | None = None) -> bool:
    """On when LOCUS_SHARED_EMBEDDER is 1/true/yes/on, or, when the variable is not set at all, when the file
    `<data_dir>/shared-embedder` exists (`touch` it to switch on, delete it to switch off: no AI-app config has to be
    edited). An explicit 0/false/no/off in the variable wins over the file."""
    value = (os.environ if env is None else env).get(ENV_ENABLE)
    if value is not None and value.strip():
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return data_dir is not None and (Path(data_dir).expanduser() / MARKER_NAME).is_file()


def idle_seconds(env: dict[str, str] | None = None) -> float:
    try:
        return max(5.0, float((os.environ if env is None else env).get(ENV_IDLE, DEFAULT_IDLE_SECONDS)))
    except ValueError:
        return float(DEFAULT_IDLE_SECONDS)


def helper_device(env: dict[str, str] | None = None) -> str | None:
    """The device the helper loads the model on: `cpu` unless LOCUS_EMBEDDER_DEVICE says otherwise (`auto` = let
    sentence-transformers choose, i.e. MPS on Apple silicon). Measured 2026-10-31 on the 16 GB Mac with bge-small:
    cpu 597 MB footprint, 9 ms per query, 1.17 s per 32-chunk batch; mps 1,474 MB, 78 ms per query, 0.86 s per batch."""
    value = ((os.environ if env is None else env).get(ENV_DEVICE) or "").strip().lower() or "cpu"
    return None if value == "auto" else value


SOCKET_PATH_MAX = 100  # bytes; macOS allows 104 for an AF_UNIX path including the terminator, Linux 108


def socket_path_for(data_dir: Path, model: str) -> Path:
    """`<data_dir>/embedder-<8 hex of the model name>.sock`. When that path would pass the AF_UNIX limit (a long
    LOCUS_DATA_DIR: bind() fails, the helper exits, and every search then waits on a helper that never comes), the
    socket, its lock and its log live in a short private folder instead, `<tmp>/locus-<uid>/`, named by a hash of the
    data folder and the model so that two data folders never share a helper. Raises SharedEmbedderError when no short
    folder is usable."""
    data_dir = Path(data_dir).expanduser()
    model = f"{model}|{os.environ.get('LOCUS_LOCAL_BACKEND') or 'auto'}"  # another backend, another helper
    plain = data_dir / f"embedder-{hashlib.sha1(model.encode()).hexdigest()[:8]}.sock"
    if len(os.fsencode(plain)) <= SOCKET_PATH_MAX:
        return plain
    key = hashlib.sha1(f"{data_dir}\0{model}".encode()).hexdigest()[:12]
    name = f"embedder-{key}.sock"
    uid = os.getuid() if hasattr(os, "getuid") else 0
    for base in dict.fromkeys((tempfile.gettempdir(), "/tmp")):
        folder = Path(base) / f"locus-{uid}"
        candidate = folder / name
        if len(os.fsencode(candidate)) > SOCKET_PATH_MAX:
            continue
        try:
            folder.mkdir(mode=0o700, exist_ok=True)
            info = folder.stat()
            if hasattr(os, "getuid") and info.st_uid != uid:
                continue  # not ours: never put a socket where another user can swap the folder
            os.chmod(folder, 0o700)
        except OSError:
            continue
        return candidate
    raise SharedEmbedderError(
        f"the folder for Locus data is too deep for the embedding helper's socket ({len(os.fsencode(plain))} bytes, "
        f"limit {SOCKET_PATH_MAX}) and no short private folder could be made. Point LOCUS_DATA_DIR at a shorter path "
        "or set LOCUS_SHARED_EMBEDDER=0."
    )


# ------------------------------------------------------------------------------------------------ framing
def _send(sock: socket.socket, obj: object) -> None:
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    sock.sendall(struct.pack(">I", len(data)) + data)


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(min(1 << 20, n - len(buf)))
        if not chunk:
            raise ConnectionError("connection closed")
        buf += chunk
    return bytes(buf)


def _recv(sock: socket.socket) -> object:
    (length,) = struct.unpack(">I", _recv_exact(sock, 4))
    if length > MAX_FRAME_BYTES:
        raise ValueError("frame too large")
    return json.loads(_recv_exact(sock, length).decode("utf-8"))


# ------------------------------------------------------------------------------------------------ server
class EmbedServer:
    """Serves one in-process embedder over a Unix socket until it has been idle for `idle_seconds`."""

    def __init__(self, embedder, socket_path: Path, idle_seconds: float = DEFAULT_IDLE_SECONDS) -> None:
        self.embedder = embedder
        self.path = Path(socket_path)
        self.idle_seconds = idle_seconds
        self._embed_lock = threading.Lock()
        self._last = time.monotonic()
        self._server: socketserver.ThreadingUnixStreamServer | None = None
        self._lock_fh = None
        self.served = 0

    def _handle(self, req: object) -> dict:
        if not isinstance(req, dict):
            return {"error": "bad request"}
        op = req.get("op")
        if op == "ping":
            return {
                "ok": True,
                "name": getattr(self.embedder, "name", ""),
                "window_tokens": getattr(self.embedder, "window_tokens", None),
                "pid": os.getpid(),
                "served": self.served,
            }
        if op in ("embed_documents", "embed_query"):
            texts = req.get("texts") if op == "embed_documents" else [req.get("text")]
            if (
                not isinstance(texts, list)
                or not texts
                or len(texts) > MAX_TEXTS_PER_REQUEST
                or not all(isinstance(t, str) for t in texts)
                or sum(len(t) for t in texts) > MAX_CHARS_PER_REQUEST
            ):
                return {"error": "bad texts"}
            self._last = time.monotonic()  # only real work keeps the model in memory; a ping (`locus stats`) does not
            with self._embed_lock:  # one encode at a time
                vecs = (
                    self.embedder.embed_documents(texts)
                    if op == "embed_documents"
                    else [self.embedder.embed_query(texts[0])]
                )
            self.served += len(texts)
            self._last = time.monotonic()
            return {"vectors": [[float(x) for x in v] for v in vecs]}
        return {"error": "unknown op"}

    def serve(self) -> bool:
        """Run until idle. Returns False at once if another helper already holds the lock."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(".lock")
        if self._lock_fh is None:  # `main` takes it before loading the model; a direct caller takes it here
            self._lock_fh = take_lock(lock_path)
            if self._lock_fh is None:
                return False
        if self.path.exists() or self.path.is_symlink():
            self.path.unlink()  # a stale socket from a helper that died; we hold the lock
        outer = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                try:
                    self.request.settimeout(60)
                    resp = outer._handle(_recv(self.request))
                except Exception as exc:  # noqa: BLE001 - a bad client must not stop the helper
                    resp = {"error": f"{type(exc).__name__}"}
                try:
                    _send(self.request, resp)
                except OSError:
                    pass

        old_umask = os.umask(0o177)  # the socket file is created 0600
        socketserver.ThreadingUnixStreamServer.request_queue_size = 128  # the default 5 refuses a burst of clients
        try:
            self._server = socketserver.ThreadingUnixStreamServer(str(self.path), Handler)
        finally:
            os.umask(old_umask)
        self._server.daemon_threads = True
        watchdog = threading.Thread(target=self._watch_idle, daemon=True)
        watchdog.start()
        try:
            self._server.serve_forever(poll_interval=0.25)
        finally:
            self._server.server_close()
            try:
                self.path.unlink()
            except OSError:
                pass
            self._lock_fh.close()
        return True

    def _watch_idle(self) -> None:
        while True:
            time.sleep(min(1.0, self.idle_seconds / 4))
            if time.monotonic() - self._last > self.idle_seconds and not self._embed_lock.locked():
                assert self._server is not None
                self._server.shutdown()
                return

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()


# ------------------------------------------------------------------------------------------------ client
# The helpers this process started, by socket path. The warm-up (`start_helper_early`), the first tool call and any
# second client in the process all find "no helper answering" while a new one is still importing torch (7 to 9 s under
# load); without this each of them started another process that only took the lock and exited.
_SPAWNED: dict[str, subprocess.Popen] = {}
_SPAWN_LOCK = threading.Lock()


class SharedEmbedderError(RuntimeError):
    pass


class SharedEmbedderClient:
    """The Embedder interface, answered by the helper; starts it when it is not running."""

    def __init__(
        self,
        model: str,
        socket_path: Path,
        *,
        name: str | None = None,
        idle: float = DEFAULT_IDLE_SECONDS,
        spawn: bool = True,
        start_timeout: float = 180.0,
        log_path: Path | None = None,
    ) -> None:
        self.model = model
        self.path = Path(socket_path)
        self.name = name or f"local:{model}"
        self.idle = idle
        self.spawn = spawn
        self.start_timeout = start_timeout
        self.log_path = Path(log_path) if log_path else self.path.with_suffix(".log")
        self._info: dict | None = None

    # -- connection ---------------------------------------------------------------------------------
    def _request(self, req: dict, timeout: float = 120.0) -> dict:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(str(self.path))
            _send(s, req)
            resp = _recv(s)
        if not isinstance(resp, dict) or "error" in resp:
            raise SharedEmbedderError(
                f"the embedding helper refused the request: {resp.get('error') if isinstance(resp, dict) else resp}"
            )
        return resp

    def _ping(self) -> dict | None:
        try:
            return self._request({"op": "ping"}, timeout=5.0)
        except (OSError, ConnectionError, ValueError, SharedEmbedderError):
            return None

    def _log_tail(self, lines: int = 4) -> str:
        try:
            tail = self.log_path.read_text(errors="replace").strip().splitlines()[-lines:]
        except OSError:
            return ""
        return " | ".join(t.strip()[:160] for t in tail)

    def _spawn(self) -> subprocess.Popen:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        log = open(self.log_path, "ab")  # noqa: SIM115 - handed to the child
        os.chmod(self.log_path, 0o600)
        cmd = [
            sys.executable, "-m", "locus.ingest.shared_embedder", "serve",
            "--model", self.model, "--socket", str(self.path), "--idle", str(self.idle),
        ]  # fmt: skip
        proc = subprocess.Popen(  # noqa: S603 - our own module, no shell
            cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, close_fds=True
        )
        log.close()
        return proc

    def _helper_process(self) -> subprocess.Popen:
        """The helper this process already started and that is still running, else a new one."""
        with _SPAWN_LOCK:
            proc = _SPAWNED.get(str(self.path))
            if proc is None or proc.poll() is not None:
                proc = _SPAWNED[str(self.path)] = self._spawn()
            return proc

    def start_in_background(self) -> None:
        """Start the helper when none answers, and return at once. The serve warm-up calls this first so the model
        load overlaps with opening the index. Never raises: a request that needs the helper reports a failure."""
        try:
            if self.spawn and self._ping() is None:
                self._helper_process()
        except Exception:  # noqa: BLE001 - best effort; ensure_running says what went wrong when it matters
            pass

    def ensure_running(self) -> dict:
        info = self._ping()
        if info is not None:
            return info
        if not self.spawn:
            raise SharedEmbedderError(
                f"no embedding helper is listening at {self.path} and starting one is switched off"
            )
        proc = self._helper_process()
        deadline = time.monotonic() + self.start_timeout
        while time.monotonic() < deadline:
            info = self._ping()
            if info is not None:
                return info
            code = proc.poll()
            if code not in (
                None,
                0,
            ):  # it died (model not found, out of memory...); code 0 = another helper won the lock
                raise SharedEmbedderError(
                    f"the embedding helper stopped with exit code {code} before it was ready: "
                    f"{self._log_tail()} (log: {self.log_path})"
                )
            time.sleep(0.3)
        raise SharedEmbedderError(
            f"the embedding helper did not start within {self.start_timeout:.0f}s; see {self.log_path}"
        )

    def _call(self, req: dict) -> dict:
        for delay in (0.0, 0.05, 0.2):  # a refused connection under a burst is not a dead helper: retry briefly
            time.sleep(delay)
            try:
                return self._request(req)
            except (OSError, ConnectionError):
                continue
        self.ensure_running()  # idle exit between two requests: start it again, once
        return self._request(req)

    # -- Embedder interface -------------------------------------------------------------------------
    @property
    def window_tokens(self) -> int | None:
        """The model's window in tokens, asked of a running helper or taken from the table of known models; never
        starts a helper (`locus stats` and the chunk-size decision at index creation must not load a model)."""
        if self._info is None:
            self._info = self._ping()
        if self._info is not None and self._info.get("window_tokens") is not None:
            return self._info["window_tokens"]
        from .chunk_policy import KNOWN_LOCAL_WINDOWS

        return KNOWN_LOCAL_WINDOWS.get(self.model.rstrip("/").rsplit("/", 1)[-1].lower())

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), DOC_BATCH):
            out += self._call({"op": "embed_documents", "texts": texts[i : i + DOC_BATCH]})["vectors"]
        return out

    def embed_query(self, text: str) -> list[float]:
        return self._call({"op": "embed_query", "text": text})["vectors"][0]


def start_helper_early(settings) -> None:
    """For `locus serve`'s warm-up: when `settings` use the shared helper, start it now (no waiting) so its model load
    runs while the index is opened. Does nothing for any other embedder. Never raises."""
    try:
        if settings.embedder != "local" or not shared_embedder_enabled(data_dir=settings.data_dir):
            return
        SharedEmbedderClient(
            settings.local_model, socket_path_for(settings.data_dir, settings.local_model), idle=idle_seconds()
        ).start_in_background()
    except Exception:  # noqa: BLE001 - get_embedder reports a bad socket path or setting when the index opens
        pass


# ------------------------------------------------------------------------------------------------ entry point
def take_lock(lock_path: Path):
    """The helper's single-instance lock (`flock`, held for the process's life), or None when another helper has it."""
    fh = open(lock_path, "w")  # noqa: SIM115 - held for the helper's lifetime
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    os.chmod(lock_path, 0o600)
    return fh


def _build_embedder(model: str):
    if model == "hash":  # tests
        from .embedder import HashEmbedder

        return HashEmbedder()
    if model == "hash-slow":  # tests: a model that takes seconds to load
        time.sleep(3)
        from .embedder import HashEmbedder

        return HashEmbedder()
    if model == "hash-fail":  # tests: a model that cannot load
        raise RuntimeError("cannot load the model (test)")
    from .embedder import LocalEmbedder

    return LocalEmbedder(model, device=helper_device())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m locus.ingest.shared_embedder")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sv = sub.add_parser("serve", help="run the helper until it has been idle for --idle seconds")
    sv.add_argument("--model", required=True)
    sv.add_argument("--socket", required=True)
    sv.add_argument("--idle", type=float, default=DEFAULT_IDLE_SECONDS)
    a = ap.parse_args(argv)
    path = Path(a.socket)
    # Take the lock BEFORE loading the model: several clients that start together each spawn a helper, and only the
    # one that wins the lock may spend the memory (loading first made every one of them load 1.5 to 3.8 GB).
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = take_lock(path.with_suffix(".lock"))
    if lock is None:
        return 0
    server = EmbedServer(_build_embedder(a.model), path, a.idle)
    server._lock_fh = lock
    server.serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
