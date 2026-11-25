"""Vector store abstraction. v0 ships Chroma (embedded, on disk); LanceDB is the planned v1 swap.

Invariant: one embedding model per collection. The model name is written into the collection
metadata on creation and checked on every open. So is the embedder's document/query input_type
policy (RET-002, INPUT_TYPE_POLICY_KEY): the embedder then follows what the collection records
(embedder.follow_collection_policy), so one collection never mixes vectors made with and without it.

Several processes share one index: a long-lived `locus serve` (one per MCP client) or `locus
watch` reads it while a separate `locus index` / `locus index --prune` writes it. ChromaStore's
docstring says how a reader keeps up with another process's writes.
"""

from __future__ import annotations

import hashlib
import os
import sys
import threading
import unicodedata
import uuid
from collections import Counter
from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .noise import PARSER_VERSION, PARSER_VERSION_KEY

# Collection metadata key for the RET-002 input_type policy ("v1" / "none", see embedder.py). Absent on
# collections created before RET-002.
INPUT_TYPE_POLICY_KEY = "input_type_policy"
# Collection metadata key for the chunk size it was created with. Absent on
# collections created before it: those keep the legacy chunk size.
CHUNK_TOKENS_KEY = "chunk_tokens"
# Collection metadata key (W5): present once every chunk carries the ancestor-directory keys below, so a
# folder scope is one `where` equality instead of a rank-everything-then-filter pass.
DIR_KEYS_FLAG = "dir_keys"
# Chunk metadata `dk1..dk16`: a short hash of the file's first n directory components (NFC). A folder
# scope of depth n is `dk<n> == hash(folder)`; a deeper folder uses dk16 and is made exact by _under().
DIR_KEY_PREFIX = "dk"
DIR_KEY_DEPTH = 16
# SQLite refuses a statement with more than ~32,700 variables, and chromadb binds one per row it
# returns: a get() of more rows than that, and a query() asking for more neighbours, both fail. Every
# read below stays far under it: pages of PAGE_SIZE rows, `$in` lists of IN_BATCH paths, at most
# QUERY_CAP neighbours.
PAGE_SIZE = 5000
IN_BATCH = 500
QUERY_CAP = 30000
# The path summary (ChromaStore._path_summary) survives this store's own writes and re-reads only the files they
# touched, in one `$in` list. A write that names more files than this (counting those not yet re-read) drops it
# instead: a full read is then no dearer. Keep it at most IN_BATCH.
SUMMARY_DIRTY_LIMIT = IN_BATCH


# Chroma's largest accepted batch is 5,461 on this build; stay well under it (a 14,345-chunk text file failed).
MAX_UPSERT_BATCH = 5000


@dataclass(frozen=True)
class Hit:
    path: str
    chunk_index: int
    text: str
    score: float  # higher is better; ~cosine similarity
    # The file's modification time when this chunk was indexed (the indexer's "mtime" metadata), so a
    # caller can tell a result whose file changed on disk since (UX-004 "stale"). None when unknown.
    mtime: float | None = None
    # First page of the chunk for PDF chunks indexed with page numbers (1-based); None otherwise.
    page: int | None = None


class VectorStore(Protocol):
    def upsert(
        self,
        ids: list[str],
        embeddings: list[list[float]],
        documents: list[str],
        metadatas: list[dict[str, Any]],
    ) -> None: ...

    def delete_path(self, path: str) -> None: ...

    def query(
        self,
        embedding: list[float],
        k: int = 5,
        path_prefix: str | None = None,
        modified_after: float | None = None,
        modified_before: float | None = None,
        order: str = "relevance",
    ) -> list[Hit]: ...

    def files_modified(
        self,
        k: int = 25,
        path_prefix: str | None = None,
        modified_after: float | None = None,
        modified_before: float | None = None,
    ) -> list[Hit]: ...

    def count_files_modified(
        self,
        path_prefix: str | None = None,
        modified_after: float | None = None,
        modified_before: float | None = None,
    ) -> int: ...

    def file_hash(self, path: str) -> str | None: ...

    def list_paths(self) -> list[str]: ...

    def count(self) -> int: ...


def _under(path: str, prefix: str) -> bool:
    """True if *path* is exactly *prefix* or inside it — a real folder boundary, not a bare
    string prefix (so a prefix of '.../notes' does not also match '.../notes2/file.txt').

    Compared in Unicode NFC: macOS can hand Locus a file name decomposed (NFD) while a person or a model
    types the composed spelling of the same name, and both name the same file."""
    path = unicodedata.normalize("NFC", path)
    prefix = unicodedata.normalize("NFC", prefix).rstrip("/\\")
    return path == prefix or path.startswith(prefix + os.sep)


def _dir_hash(parts: list[str]) -> str:
    return hashlib.blake2b(os.sep.join(parts).encode("utf-8"), digest_size=6).hexdigest()


def _dir_parts(path: str) -> list[str]:
    return [p for p in unicodedata.normalize("NFC", path).split(os.sep) if p]


def dir_keys(path: str) -> dict[str, str]:
    """The ancestor-directory metadata keys (dk1..dk16) for a file *path*."""
    dirs = _dir_parts(path)[:-1]
    return {f"{DIR_KEY_PREFIX}{n}": _dir_hash(dirs[:n]) for n in range(1, min(len(dirs), DIR_KEY_DEPTH) + 1)}


def open_chroma_client(path: Path | str) -> Any:
    """The one place Locus opens a Chroma client (S-08). Product telemetry is switched off twice: the environment
    variable Chroma reads (set, not defaulted: a user's `ANONYMIZED_TELEMETRY=True` does not turn it on) and an explicit
    `Settings(anonymized_telemetry=False)`, which also covers a Chroma release that stops reading the variable.
    chromadb 1.5's own telemetry class does nothing, so today this guards against a later release, not a send."""
    os.environ["ANONYMIZED_TELEMETRY"] = "False"
    import chromadb
    from chromadb.config import Settings

    return chromadb.PersistentClient(path=str(path), settings=Settings(anonymized_telemetry=False))


def _close_client(client: Any) -> None:
    """Release a Chroma client so that the next PersistentClient for its path loads from disk.

    chromadb caches one "system" per path for the whole process; Client.close() drops this
    client's reference and stops the system once nothing else in the process uses it.
    """
    close = getattr(client, "close", None)
    if callable(close):
        close()


def _is_missing_collection(exc: Exception) -> bool:
    # chromadb >= 1.0 raises NotFoundError; older releases raised ValueError / InvalidCollectionException.
    return isinstance(exc, ValueError) or type(exc).__name__ in {"NotFoundError", "InvalidCollectionException"}


def _stamp(meta: dict[str, Any]) -> int:
    """The parser version stamped on a chunk's metadata; 1 for rows written before the stamp existed."""
    value = meta.get(PARSER_VERSION_KEY)
    return int(value) if isinstance(value, int) else 1


@dataclass
class _PathSummary:
    """What ChromaStore._path_summary caches: per file the modified time it had when indexed, and the roots
    its chunks were indexed under. `generation` is the index token the contents were last known to match."""

    generation: str | None
    mtimes: dict[str, float | None]
    path_roots: dict[str, tuple[str, ...]]  # path -> its chunks' distinct roots; "" holds those of chunks with no path
    root_counts: Counter[str]  # root -> how many entries of path_roots carry it
    roots: list[str]  # sorted(root_counts): the distinct roots
    outdated: set[str] = field(default_factory=set)  # files an older parser indexed (parser_version below current)

    def replace(
        self,
        paths: Collection[str],
        mtimes: dict[str, float | None],
        path_roots: dict[str, set[str]],
        outdated: Collection[str] = (),
    ) -> None:
        """Swap what is held for *paths* for what was just read for them; a path with no chunk left disappears."""
        self.outdated.difference_update(paths)
        self.outdated.update(outdated)
        for path in paths:
            self.mtimes.pop(path, None)
            for root in self.path_roots.pop(path, ()):
                self.root_counts[root] -= 1
                if self.root_counts[root] <= 0:
                    del self.root_counts[root]
        self.mtimes.update(mtimes)
        for path, roots in path_roots.items():
            self.path_roots[path] = tuple(sorted(roots))
            self.root_counts.update(roots)
        self.roots = sorted(self.root_counts)


class ChromaStore:
    """A Chroma collection on local disk that stays correct while another process writes it.

    Chroma loads a collection's vector index (HNSW) into memory when a client opens it and never
    re-reads it from disk, while its SQLite side (chunk text, metadata, counts) is read fresh on
    every call. A long-lived reader such as `locus serve` therefore kept ranking by the vectors it
    loaded at startup after a separate `locus index` run: a new file never came back from a
    search, an edited chunk came back with its new text but ranked by its old vector, and after
    `--prune` a search whose stale top-k still held a deleted chunk crashed on its missing
    metadata. Only a restart of the reader helped.

    So every write through this class records a new random token in a small per-collection
    generation file next to the index (`<data_dir>/<collection>.generation`), and every call
    first compares that file with the token this client loaded. When another process has
    written since, the client is closed and reopened, which reloads the index from disk. The
    check is one small file read per call; the reopen happens only after an external write.
    Readers never write the file, so the servers of two MCP clients on one index do not make
    each other reopen.

    Limits: only writes made through ChromaStore are detected (every Locus writer uses it).
    chromadb shares one system per path within a process, so a reopen reloads from disk only
    when this is the process's only open store for that data dir (true for serve, watch and the
    CLI). A lock serialises this store's calls, so a reopen never pulls the client from under
    another thread (`locus watch` indexes from a small thread pool).
    """

    def __init__(
        self,
        data_dir: Path,
        collection: str,
        embedder_name: str,
        *,
        create: bool = True,
        input_type_policy: str | None = None,
        chunk_tokens: int | None = None,
    ) -> None:
        """Open (and by default create) *collection*. `create=False` opens it only if it already
        exists and raises LookupError otherwise: a preview such as `locus index --dry-run` must not
        create a collection and stamp its embedder name on it (review F4).

        `input_type_policy` (RET-002) is recorded on a collection this call creates, and on an empty
        one that records none yet (nothing in it to mix with). `self.input_type_policy` is what the
        collection records; None for one built before RET-002 (the caller decides what that means).
        `input_type_policy_guard`, when set, is called with the newly recorded policy whenever a
        reopen finds it changed (another process re-created the collection); it raises to refuse.

        `chunk_tokens` works the same way for the chunk size: recorded on a collection this call
        creates, and on an empty one that records none. `self.chunk_tokens` is what the collection
        records; None for one built before chunk sizes were recorded, which keeps the legacy size."""
        data_dir = Path(data_dir).expanduser()
        data_dir.mkdir(parents=True, exist_ok=True)
        # 0700: this directory holds chunk text + vectors of the user's own files. mkdir's own
        # `mode` argument is subject to the process umask and is silently ignored entirely when
        # exist_ok=True short-circuits on an already-existing dir, so an explicit chmod is the
        # only reliable way to guarantee this regardless of umask or prior state (same reasoning
        # as notion_source.py's 0600 token file, one directory level up from "atomic" since a
        # directory has no O_CREAT-with-mode equivalent).
        os.chmod(data_dir, 0o700)
        self.embedder_name = embedder_name
        self.collection = collection
        self.data_dir = data_dir
        self._create = create
        self._new_input_type_policy = input_type_policy
        self._new_chunk_tokens = chunk_tokens
        self.input_type_policy: str | None = None
        self.chunk_tokens: int | None = None
        self.input_type_policy_guard: Callable[[str | None], None] | None = None
        self._opened = False
        self._generation_file = data_dir / f"{collection}.generation"
        self._lock = threading.RLock()
        self._summary: _PathSummary | None = None  # see _path_summary
        self._dirty: set[str] = set()  # files this store wrote since the summary last matched the index
        self._open()

    # -- cross-process freshness ------------------------------------------------------------
    def _read_generation(self) -> str | None:
        try:
            return self._generation_file.read_text(encoding="utf-8")
        except OSError:  # no write recorded yet, or unreadable this instant: compare as "none"
            return None

    def _open(self) -> None:
        # Read the token before opening: a write that lands while the index loads then shows up
        # as a change on the next call (one spare reopen) instead of being missed.
        generation = self._read_generation()
        client = open_chroma_client(self.data_dir / "chroma")
        try:
            if self._create:
                metadata: dict[str, Any] = {"hnsw:space": "cosine", "embedder": self.embedder_name, DIR_KEYS_FLAG: 1}
                if self._new_input_type_policy is not None:
                    metadata[INPUT_TYPE_POLICY_KEY] = self._new_input_type_policy
                if self._new_chunk_tokens is not None:
                    metadata[CHUNK_TOKENS_KEY] = int(self._new_chunk_tokens)
                # Metadata applies only when this call creates the collection; an existing one keeps its own.
                col = client.get_or_create_collection(name=self.collection, metadata=metadata)
            else:
                try:
                    col = client.get_collection(name=self.collection)
                except Exception as exc:
                    if not _is_missing_collection(exc):
                        raise
                    raise LookupError(f"no collection {self.collection!r} in {self.data_dir / 'chroma'}") from exc
            existing = (col.metadata or {}).get("embedder")
            if existing and existing != self.embedder_name:
                raise RuntimeError(
                    f"Collection {self.collection!r} was built with embedder {existing!r} but the current "
                    f"embedder is {self.embedder_name!r}. Use a new LOCUS_COLLECTION or delete "
                    f"{self.data_dir / 'chroma'} and re-index. Never mix models in one collection."
                    + (
                        f" To keep this collection, set LOCUS_EMBEDDER=local and LOCUS_LOCAL_MODEL={existing[6:]}."
                        if existing.startswith("local:")
                        else ""
                    )
                )
            policy, recorded_now = self._recorded_input_type_policy(col)
            chunk_tokens, chunk_recorded_now = self._recorded_chunk_tokens(col)
            if self._opened and policy != self.input_type_policy and self.input_type_policy_guard is not None:
                self.input_type_policy_guard(policy)  # raises: the embedder here cannot follow it
        except BaseException:
            _close_client(client)
            raise
        self._client, self._col, self._generation = client, col, generation
        self._has_dir_keys = bool((col.metadata or {}).get(DIR_KEYS_FLAG))
        self.input_type_policy = policy
        self.chunk_tokens = chunk_tokens
        self._opened = True
        # a write: other processes reopen, see the record, and refuse what they cannot follow
        if recorded_now or chunk_recorded_now:
            self._record_write()

    def _recorded_input_type_policy(self, col: Any) -> tuple[str | None, bool]:
        """The collection's RET-002 policy. An empty collection that records none (created by an
        older `locus serve`/`doctor` before RET-002) gets this store's policy now: no vector in it
        can mix with the new ones. Only on the first open: when a reopen finds a collection emptied
        by another process, the embedder here may be following it as legacy (no input_type) and
        would write without it under a v1 record. The flag says whether this call recorded it."""
        meta = dict(col.metadata or {})
        recorded = meta.get(INPUT_TYPE_POLICY_KEY)
        if recorded is not None:
            return str(recorded), False
        wanted = self._new_input_type_policy
        if wanted is None or not self._create or self._opened or int(col.count()) != 0:
            return None, False
        # chromadb refuses any hnsw:* key in modify() (the distance function is fixed at creation and
        # kept in the collection's configuration, so cosine stays: tests/test_input_type_policy.py).
        kept = {k: v for k, v in meta.items() if not str(k).startswith("hnsw:")}
        col.modify(metadata={**kept, INPUT_TYPE_POLICY_KEY: wanted})
        return wanted, True

    def _recorded_chunk_tokens(self, col: Any) -> tuple[int | None, bool]:
        """The collection's recorded chunk size, recorded now on an empty collection that
        has none, under the same first-open rule as the input_type policy. The flag says whether this
        call recorded it."""
        meta = dict(col.metadata or {})
        recorded = meta.get(CHUNK_TOKENS_KEY)
        if recorded is not None:
            try:
                return int(recorded), False
            except (TypeError, ValueError):
                return None, False  # unreadable: behave as an unrecorded (legacy) collection
        wanted = self._new_chunk_tokens
        if wanted is None or not self._create or self._opened or int(col.count()) != 0:
            return None, False
        kept = {k: v for k, v in meta.items() if not str(k).startswith("hnsw:")}
        col.modify(metadata={**kept, CHUNK_TOKENS_KEY: int(wanted)})
        return int(wanted), True

    def _sync(self) -> None:
        """Reopen if another process wrote the collection after this client loaded it (hold the lock)."""
        if self._read_generation() != self._generation:
            _close_client(self._client)
            self._open()

    def _drop_summary(self) -> None:
        self._summary = None
        self._dirty = set()

    def _touch(self, paths: Collection[str] | None) -> None:
        """Note that this store's own write changes the chunks of *paths* (hold the lock, after _sync): the cached
        path summary stays and re-reads just those files at its next use. It cannot stay when the write's reach
        is unknown (*paths* None), when it was already out of date before this write (another process wrote
        between this store's two writes: _sync reopened and moved the token), or when so many files changed that
        a full read is no dearer. Call it before the write, so a write that fails half-way is covered too."""
        summary = self._summary
        if summary is None:
            return
        if (
            paths is None
            or self._generation is None
            or summary.generation != self._generation
            or len(self._dirty) + len(paths) > SUMMARY_DIRTY_LIMIT
        ):
            self._drop_summary()
            return
        self._dirty.update(paths)

    def _record_write(self, paths: Collection[str] | None = None) -> None:
        """Give readers in other processes a new token to compare after a write (hold the lock). *paths* are
        the files whose chunks the write changed, when the caller knows them (an empty collection: none);
        the cached path summary then survives this store's own write (see _touch). Without them it is dropped."""
        token = uuid.uuid4().hex
        self._touch(paths)
        if self._summary is not None and self._read_generation() != self._generation:
            # another process wrote after this call's _sync: its change is not in the summary (and the token
            # written below hides it from this store's own _sync)
            self._drop_summary()
        try:
            self._generation_file.write_text(token, encoding="utf-8")
        except OSError as exc:
            self._drop_summary()  # nobody can tell this write from the last: do not trust a kept summary
            print(
                f"locus: the index changed but {self._generation_file.name} could not be written "
                f"({exc}); restart a running `locus serve` or `locus watch` to see the change",
                file=sys.stderr,
            )
            return
        self._generation = token  # this client already sees its own write
        if self._summary is not None:
            self._summary.generation = token

    # -- writes ---------------------------------------------------------------------------
    def upsert(
        self,
        ids: list[str],
        embeddings: list[list[float]],
        documents: list[str],
        metadatas: list[dict[str, Any]],
    ) -> None:
        if not ids:
            return
        with self._lock:
            self._sync()  # never write through a stale in-memory index
            # the files this call may change, for the path summary; a chunk with no path leaves its reach unknown
            paths = None if any(not m.get("path") for m in metadatas) else {str(m["path"]) for m in metadatas}
            metadatas = [{**m, **dir_keys(str(m["path"]))} if m.get("path") else m for m in metadatas]
            self._touch(paths)  # before writing: what a failing slice leaves behind is then re-read, not trusted
            written = 0
            try:
                for start in range(0, len(ids), MAX_UPSERT_BATCH):  # Chroma refuses a call above its max batch
                    end = start + MAX_UPSERT_BATCH
                    self._col.upsert(
                        ids=ids[start:end],
                        embeddings=embeddings[start:end],
                        documents=documents[start:end],
                        metadatas=metadatas[start:end],
                    )
                    written = end
            except Exception:
                if written:  # a later slice failed: take the earlier ones out, so the file is never half-indexed
                    self._col.delete(ids=ids[: min(written, len(ids))])
                    self._record_write(paths)
                raise
            self._record_write(paths)

    def delete_path(self, path: str) -> None:
        with self._lock:
            self._sync()
            self._col.delete(where={"path": {"$eq": path}})
            self._record_write({path})

    def refresh_mtime(self, path: str, mtime: float) -> None:
        """Record *mtime* on every chunk of *path*: its content is unchanged (same hash) but its
        modified time moved (a `touch`, a copy, a git checkout). Nothing is re-embedded; without
        this the UX-004 freshness check would keep calling the file changed after a re-index."""
        with self._lock:
            self._sync()
            res = self._col.get(where={"path": {"$eq": path}}, include=["metadatas"])
            ids, metas = res.get("ids") or [], res.get("metadatas") or []
            moved = [(i, m) for i, m in zip(ids, metas, strict=True) if m and _as_mtime(m.get("mtime")) != mtime]
            if not moved:
                return
            self._col.update(ids=[i for i, _ in moved], metadatas=[{**m, "mtime": mtime} for _, m in moved])
            self._record_write({path})

    # -- scoped reads (W5) ------------------------------------------------------------------
    def _pages(self, where: dict[str, Any] | None, include: list[str]) -> Iterator[dict[str, Any]]:
        """`get` in pages of PAGE_SIZE rows (callers hold the lock), so no statement nears SQLite's variable limit."""
        offset = 0
        while True:
            res = self._col.get(where=where, include=include, limit=PAGE_SIZE, offset=offset)
            if not res.get("ids"):
                return
            yield res
            if len(res["ids"]) < PAGE_SIZE:
                return
            offset += PAGE_SIZE

    def _count(self, where: dict[str, Any] | None) -> int:
        if where is None:
            return int(self._col.count())
        return sum(len(res["ids"]) for res in self._pages(where, []))

    def _scope_wheres(
        self, path_prefix: str | None, modified_after: float | None, modified_before: float | None
    ) -> list[dict[str, Any] | None]:
        """Chroma `where` filters that together select the chunks in scope (callers hold the lock). One
        filter on a collection with directory keys (a folder is one equality). A collection built before
        W5 has none: the in-scope files are found by a paged scan of the metadata and queried in batches of
        IN_BATCH paths. [] means nothing is in scope; [None] means no filter."""
        clauses: list[dict[str, Any]] = []
        if modified_after is not None:
            clauses.append({"mtime": {"$gte": modified_after}})
        if modified_before is not None:
            clauses.append({"mtime": {"$lt": modified_before}})

        def combined(extra: list[dict[str, Any]]) -> dict[str, Any] | None:
            parts = clauses + extra
            return None if not parts else parts[0] if len(parts) == 1 else {"$and": parts}

        if not path_prefix:
            return [combined([])]
        parts = _dir_parts(path_prefix)
        if not parts:  # "/" : everything
            return [combined([])]
        if self._has_dir_keys:
            n = min(len(parts), DIR_KEY_DEPTH)
            # the folder itself, or the one file the prefix names (spelled as typed, NFC or NFD)
            spellings = sorted(
                {path_prefix, unicodedata.normalize("NFC", path_prefix), unicodedata.normalize("NFD", path_prefix)}
            )
            scope = {"$or": [{f"{DIR_KEY_PREFIX}{n}": _dir_hash(parts[:n])}, {"path": {"$in": spellings}}]}
            return [combined([scope])]
        in_scope: set[str] = set()
        for res in self._pages(combined([]), ["metadatas"]):
            in_scope.update(
                str(m["path"])
                for m in res.get("metadatas") or []
                if m and m.get("path") and _under(str(m["path"]), path_prefix)
            )
        paths = sorted(in_scope)
        return [combined([{"path": {"$in": paths[i : i + IN_BATCH]}}]) for i in range(0, len(paths), IN_BATCH)]

    def ensure_dir_keys(self) -> int:
        """Upgrade a collection built before W5: write the directory keys onto every chunk, then record
        the flag so scoped reads use them. Returns how many chunks were updated (0 when already done).
        Resumable: an interrupted run leaves the flag off and the next call redoes only what is missing."""
        with self._lock:
            self._sync()
            if self._has_dir_keys:
                return 0
            updated = 0
            offset = 0
            while True:
                res = self._col.get(include=["metadatas"], limit=PAGE_SIZE, offset=offset)
                ids, metas = res.get("ids") or [], res.get("metadatas") or []
                if not ids:
                    break
                todo = [
                    (i, m)
                    for i, m in zip(ids, metas, strict=True)
                    if m and m.get("path") and f"{DIR_KEY_PREFIX}1" not in m and dir_keys(str(m["path"]))
                ]
                if todo:
                    self._col.update(
                        ids=[i for i, _ in todo], metadatas=[{**m, **dir_keys(str(m["path"]))} for _, m in todo]
                    )
                    updated += len(todo)
                offset += PAGE_SIZE
            meta = {k: v for k, v in dict(self._col.metadata or {}).items() if not str(k).startswith("hnsw:")}
            self._col.modify(metadata={**meta, DIR_KEYS_FLAG: 1})
            self._has_dir_keys = True
            self._record_write(())  # directory keys only: no path, mtime or root changes
            return updated

    # -- reads ----------------------------------------------------------------------------
    def query(
        self,
        embedding: list[float],
        k: int = 5,
        path_prefix: str | None = None,
        modified_after: float | None = None,
        modified_before: float | None = None,
        order: str = "relevance",
    ) -> list[Hit]:
        """The k best chunks for *embedding*. `modified_after` (inclusive) and `modified_before`
        (exclusive) are Unix times compared with the "mtime" metadata the indexer stored;
        a chunk without one never passes a time filter. `order` "modified_desc" sorts the filtered
        chunks newest file first (ties by score) before taking k; "relevance" keeps the ranking."""
        timed = modified_after is not None or modified_before is not None
        all_needed = order == "modified_desc"  # sorting by time needs every candidate, not just the nearest k
        k = max(1, k)

        def accept(meta: Any) -> bool:
            if not meta:  # a vector whose chunk is gone (deleted after this index was loaded): no path, no hit
                return False
            if path_prefix and not _under(str(meta.get("path", "")), path_prefix):
                return False
            return not timed or _in_window(_as_mtime(meta.get("mtime")), modified_after, modified_before)

        rows: list[tuple[Any, Any, float]] = []
        with self._lock:
            self._sync()
            if int(self._col.count()) == 0:
                return []
            # A scope (folder and/or time window) is pushed into Chroma's own `where` so only the
            # matching chunks are ranked; earlier this ranked every chunk and filtered afterwards,
            # which failed with "too many SQL variables" past ~32,700 chunks (W5). The kept `accept`
            # re-check makes the result exact even where a directory hash collides.
            for where in self._scope_wheres(path_prefix, modified_after, modified_before):
                n = min(self._count(where), QUERY_CAP) if all_needed else k
                if n == 0:
                    continue
                while True:
                    res = self._col.query(
                        query_embeddings=[embedding],
                        n_results=n,
                        where=where,
                        include=["documents", "metadatas", "distances"],
                    )
                    got = list(zip(res["documents"][0], res["metadatas"][0], res["distances"][0], strict=True))
                    kept = [r for r in got if accept(r[1])]
                    if all_needed or len(kept) >= k or len(got) < n or n >= QUERY_CAP:
                        break
                    n = min(n * 4, QUERY_CAP)  # filtered rows (a hash collision) left fewer than k: ask for more
                rows.extend(kept)
        rows.sort(key=lambda r: r[2])
        hits: list[Hit] = []
        for doc, meta, dist in rows:
            path = str(meta.get("path", ""))
            mtime = _as_mtime(meta.get("mtime"))
            hits.append(
                Hit(
                    path=path,
                    chunk_index=int(meta.get("chunk_index", 0)),
                    text=doc or "",
                    score=round(1.0 - float(dist), 4),
                    mtime=mtime,
                    page=_as_page(meta.get("page")),
                )
            )
        if order == "modified_desc":
            hits.sort(key=lambda h: (h.mtime or 0.0, h.score), reverse=True)
        return hits[:k]

    def files_modified(
        self,
        k: int = 25,
        path_prefix: str | None = None,
        modified_after: float | None = None,
        modified_before: float | None = None,
    ) -> list[Hit]:
        """No query: the files whose indexed modified time falls in the window, newest first, one
        Hit per file (its first chunk; score 0.0 because nothing was ranked). "what did I
        touch yesterday". A file without a recorded modified time never passes a time filter."""
        return self._files_in_window(path_prefix, modified_after, modified_before)[: max(1, k)]

    def count_files_modified(
        self,
        path_prefix: str | None = None,
        modified_after: float | None = None,
        modified_before: float | None = None,
    ) -> int:
        """How many files `files_modified` would list with no limit (the total behind a cut-off listing)."""
        return len(self._files_in_window(path_prefix, modified_after, modified_before))

    def _files_in_window(
        self, path_prefix: str | None, modified_after: float | None, modified_before: float | None
    ) -> list[Hit]:
        rows: list[tuple[Any, Any]] = []
        with self._lock:
            self._sync()
            if int(self._col.count()) == 0:
                return []
            for where in self._scope_wheres(path_prefix, modified_after, modified_before):
                for res in self._pages(where, ["documents", "metadatas"]):
                    rows.extend(zip(res.get("documents") or [], res.get("metadatas") or [], strict=True))
        first: dict[str, Hit] = {}
        for doc, meta in rows:
            if not meta or not meta.get("path"):
                continue
            path = str(meta["path"])
            if path_prefix and not _under(path, path_prefix):
                continue
            index = int(meta.get("chunk_index", 0))
            if path not in first or index < first[path].chunk_index:
                first[path] = Hit(
                    path=path, chunk_index=index, text=doc or "", score=0.0, mtime=_as_mtime(meta.get("mtime"))
                )
        return sorted(first.values(), key=lambda h: (h.mtime or 0.0, h.path), reverse=True)

    def file_hash(self, path: str) -> str | None:
        return self.file_state(path)[0]

    def file_state(self, path: str) -> tuple[str | None, float | None]:
        """(file_hash, mtime) recorded for *path* when it was indexed, from one chunk's metadata
        (every chunk of a file carries the same two); (None, None) when it is not indexed."""
        with self._lock:
            self._sync()
            res = self._col.get(where={"path": {"$eq": path}}, limit=1, include=["metadatas"])
        metas = res.get("metadatas") or []
        if not metas or not metas[0]:
            return None, None
        value = metas[0].get("file_hash")
        return (str(value) if value else None), _as_mtime(metas[0].get("mtime"))

    def parser_version(self, path: str) -> int:
        """The parser version stamped on *path*'s chunks when it was indexed; 1 for rows with no stamp
        (indexed before the stamp existed) and for a path that is not indexed."""
        with self._lock:
            self._sync()
            res = self._col.get(where={"path": {"$eq": path}}, limit=1, include=["metadatas"])
        metas = res.get("metadatas") or []
        return _stamp(metas[0]) if metas and metas[0] else 1

    def file_states(self) -> dict[str, tuple[str | None, float | None]]:
        """path -> (file_hash, mtime) for every indexed file, from ONE paged metadata read: what `file_state`
        returns per path, for a caller (`index --roots`) that would otherwise do one `get` per file seen.
        A point-in-time snapshot (not cached): re-read, or ask `file_state`, after writing."""
        states: dict[str, tuple[str | None, float | None]] = {}
        with self._lock:
            self._sync()
            if int(self._col.count()) == 0:
                return states
            for res in self._pages(None, ["metadatas"]):
                for meta in res.get("metadatas") or []:
                    if not meta or not meta.get("path"):
                        continue
                    path = str(meta["path"])
                    if path not in states:  # every chunk of a file carries the same two values
                        value = meta.get("file_hash")
                        stale = _stamp(meta) < PARSER_VERSION  # an older parser: no recorded time, so it is redone
                        states[path] = (
                            (str(value) if value else None),
                            None if stale else _as_mtime(meta.get("mtime")),
                        )
        return states

    def list_paths(self) -> list[str]:
        with self._lock:
            self._sync()
            if int(self._col.count()) == 0:
                return []
            metas = [m for res in self._pages(None, ["metadatas"]) for m in res.get("metadatas") or []]
        return sorted({str(m.get("path")) for m in metas if m and m.get("path")})

    def count(self) -> int:
        with self._lock:
            self._sync()
            return int(self._col.count())

    def _read_summary(
        self, where: dict[str, Any] | None
    ) -> tuple[dict[str, float | None], dict[str, set[str]], set[str]]:
        """(path -> mtime at indexing, path -> distinct roots) from a paged read of the metadata of the chunks
        matching *where* (None: all). A path's mtime is the first one its chunks carry; "" stands for no path."""
        mtimes: dict[str, float | None] = {}
        roots: dict[str, set[str]] = {}
        outdated: set[str] = set()
        for res in self._pages(where, ["metadatas"]):
            for meta in res.get("metadatas") or []:
                if not meta:
                    continue
                path = str(meta["path"]) if meta.get("path") else ""
                if path and mtimes.get(path) is None:
                    mtimes[path] = _as_mtime(meta.get("mtime"))
                if path and _stamp(meta) < PARSER_VERSION:
                    outdated.add(path)
                if meta.get("root"):
                    roots.setdefault(path, set()).add(sys.intern(str(meta["root"])))
        return mtimes, roots, outdated

    def _path_summary(self) -> tuple[dict[str, float | None], list[str]]:
        """(path -> mtime at indexing, distinct roots) from ONE paged read of every chunk's metadata (hold the
        lock, after _sync). Kept per generation token: `locus_search` asks for both on every call (freshness),
        and re-reading all metadata took ~1.5 s of a 1.5 s search on 10.5k chunks.
        A write by another process moves the token, so the next call reads everything again. This store's own
        writes (upsert, delete_path, refresh_mtime) keep the summary and name the files they touched (_touch);
        the next call re-reads just those, which is what keeps a search that follows a freshness write cheap."""
        summary = self._summary
        if summary is not None and self._generation is not None and summary.generation == self._generation:
            if self._dirty:
                touched = sorted(self._dirty)
                # reads first, changes after: a failed read leaves the summary and the touched set as they were
                mtimes, roots, outdated = self._read_summary({"path": {"$in": touched}})
                summary.replace(touched, mtimes, roots, outdated)
                self._dirty = set()
            return summary.mtimes, summary.roots
        summary = _PathSummary(self._generation, {}, {}, Counter(), [])
        if int(self._col.count()) > 0:
            summary.replace((), *self._read_summary(None))
        self._summary, self._dirty = summary, set()
        return summary.mtimes, summary.roots

    def file_mtimes(self) -> dict[str, float | None]:
        """Every indexed path with the modification time its file had when it was indexed (None
        for chunks written without one). One metadata read, like list_paths (UX-004 freshness)."""
        with self._lock:
            self._sync()
            return dict(self._path_summary()[0])

    def outdated_paths(self) -> set[str]:
        """Indexed files whose chunks were written by an older parser (no stamp counts as version 1): freshness and
        `index --roots` index them again even though the file did not change. Kept with the path summary."""
        with self._lock:
            self._sync()
            self._path_summary()
            return set(self._summary.outdated) if self._summary is not None else set()

    def has_path_under(self, prefix: str) -> bool:
        """True when some indexed file is *prefix* or inside it (a real folder boundary). One folder-scoped read that
        stops at the first match, not a read of every chunk's metadata (the one-time closed-folder check uses it)."""
        with self._lock:
            self._sync()
            if int(self._col.count()) == 0:
                return False
            for where in self._scope_wheres(prefix, None, None):
                for res in self._pages(where, ["metadatas"]):
                    if any(m and m.get("path") and _under(str(m["path"]), prefix) for m in res.get("metadatas") or []):
                        return True
        return False

    def indexed_roots(self) -> list[str]:
        """The distinct `root` values chunks were indexed under (the folders `locus index` was pointed at)."""
        with self._lock:
            self._sync()
            return list(self._path_summary()[1])

    def last_write_time(self) -> float | None:
        """When any Locus process last changed this collection (the generation file's own mtime,
        see _record_write), as a Unix time; None if no write has been recorded since that file
        existed. A re-index that finds every file unchanged writes nothing and leaves it as is."""
        try:
            return self._generation_file.stat().st_mtime
        except OSError:
            return None


def _in_window(mtime: float | None, after: float | None, before: float | None) -> bool:
    """Whether a modified time lies in [after, before): after inclusive, before exclusive."""
    if mtime is None:
        return False
    return (after is None or mtime >= after) and (before is None or mtime < before)


def _as_page(value: Any) -> int | None:
    """A stored "page" metadata value as a page number, or None when absent (non-PDF, older index)."""
    return int(value) if isinstance(value, int | float) and not isinstance(value, bool) and value >= 1 else None


def _as_mtime(value: Any) -> float | None:
    """A stored "mtime" metadata value as a float, or None when absent or unusable."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def recorded_collection(data_dir: Path, collection: str) -> tuple[str | None, int] | None:
    """(the embedder name *collection* records, its chunk count), or None when the data directory
    holds no such collection. Read-only: unlike ChromaStore it creates no directory and no
    collection and changes no permission, so `locus setup` can check which embedder an existing
    index was built with before switching (constraint 4)."""
    chroma_dir = Path(data_dir).expanduser() / "chroma"
    if not chroma_dir.is_dir():
        return None
    client = open_chroma_client(chroma_dir)
    try:
        try:
            col = client.get_collection(name=collection)
        except Exception as exc:
            if not _is_missing_collection(exc):
                raise
            return None
        embedder = (col.metadata or {}).get("embedder")
        return (str(embedder) if embedder else None), int(col.count())
    finally:
        _close_client(client)
