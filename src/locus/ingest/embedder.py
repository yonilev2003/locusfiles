"""Embedding backends behind one small interface.

- HashEmbedder: deterministic, offline, NO semantics — tests and smoke runs only.
- VoyageEmbedder: Anthropic's recommended provider; best retrieval quality (ADR-001).
- OpenAIEmbedder: alternative API backend.
- LocalEmbedder: on-device (fastembed/ONNX by default, sentence-transformers for models fastembed lacks);
  no chunk text leaves the machine for embedding.
- ManagedEmbedder: v1-Light hosted relay (locus-web/app/api/embed-relay) — a paying user's LOCUS_API_KEY
  buys them Voyage-quality embeddings without holding their own Voyage key. Chunk text (when indexing)
  and search queries go through Locus's relay, hosted on Vercel, to Voyage AI under Locus's Voyage
  account — not the user's. Locus does not store that text; the relay keeps only usage counts for
  quotas and billing. Compared with VoyageEmbedder (the user's own Voyage key), Locus's relay is an
  extra party in the path, and Voyage sees Locus, not the user, as its customer.

Never mix backends inside one collection (the store enforces this).

RET-002, document/query `input_type`: an embedder that distinguishes the two sides declares
`input_type_policy` (INPUT_TYPE_V1: chunks go out as "document", searches as "query"). The store
records it in the metadata of each new collection, next to the embedder name. `follow_collection_policy`
makes an embedder use the policy its collection was built with, so one collection never holds both
kinds of vector. A managed collection built before RET-002 records nothing and was built with no
input_type at all (INPUT_TYPE_NONE): it keeps working that way and the user is told to re-index.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import json
import math
import os
import re
import sys
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Iterator
from typing import Protocol, TypeVar, runtime_checkable

from .. import __version__
from ..config import DEFAULT_LOCAL_MODEL, Settings
from .errors import ProviderUnavailable, from_provider_exception, from_relay_http

T = TypeVar("T")


def _typed_provider_call(provider: str, call: Callable[[], T]) -> T:
    """Run one provider SDK call, re-raising its failures as the typed errors in errors.py."""
    try:
        return call()
    except Exception as exc:
        typed = from_provider_exception(exc, provider)
        if typed is None:
            raise
        raise typed from exc


# Longest search query the embed relay's query carve-out accepts, in Unicode code points. Must equal
# QUERY_MAX_CHARS in locus-web/lib/quota.ts: a purpose "query" request over it is refused with HTTP 400.
QUERY_MAX_CHARS = 2000

# RET-002 input_type policies, the values recorded under store.INPUT_TYPE_POLICY_KEY.
INPUT_TYPE_V1 = "v1"  # chunks embedded with input_type "document", searches with "query"
INPUT_TYPE_NONE = "none"  # no input_type on either side: managed collections built before RET-002
INPUT_TYPE_POLICIES = frozenset({INPUT_TYPE_V1, INPUT_TYPE_NONE})


@runtime_checkable
class Embedder(Protocol):
    """`embed_documents` embeds chunk text for the index; `embed_query` embeds one search.

    Optional (RET-002, read with getattr so plain test embedders need neither): `input_type_policy`,
    the policy this embedder uses now, None when it has no document/query distinction; and
    `unrecorded_input_type_policy`, the policy a collection it built before policies were recorded
    was built with. An embedder that can follow more than one policy also has
    `use_input_type_policy(policy)`.
    """

    name: str

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


def input_type_policy_of(embedder: Embedder) -> str | None:
    """The input_type policy *embedder* uses now; None for one without a document/query distinction."""
    return getattr(embedder, "input_type_policy", None)


def legacy_input_type_warning(collection: str) -> str:
    """What to tell the user about a managed collection built without input_type (RET-002)."""
    return (
        f"collection {collection!r} was indexed through the Locus managed relay before Locus sent Voyage's "
        "document/query input_type (RET-002). Search keeps working as before, and new or changed files are "
        "embedded the same old way so the collection never mixes the two, but a re-index is recommended "
        "(Voyage recommends input_type for retrieval): set LOCUS_COLLECTION to a new name in .env, run "
        "`locus index <folder>` again, then restart your MCP client so `locus serve` opens the new collection. "
        "That re-embeds every file and counts against your managed token budget."
    )


def input_type_status(embedder: Embedder) -> str:
    """One line for `locus stats` / `locus doctor` / diagnostics: the policy in effect for this collection."""
    policy = input_type_policy_of(embedder)
    if policy is None:
        return "n/a (this embedder has no document/query input_type)"
    if policy == INPUT_TYPE_V1:
        return "v1 (document/query)"
    return "none (legacy managed collection, indexed without document/query input_type; re-index recommended)"


def follow_collection_policy(embedder: Embedder, recorded: str | None, collection: str) -> str | None:
    """Make *embedder* embed the way *collection* was built, so the collection never mixes vectors
    made with and without input_type (RET-002).

    `recorded` is the collection's recorded policy; None means it was created before policies were
    recorded, i.e. with the embedder's `unrecorded_input_type_policy`. Returns a warning for the user
    when the collection is a legacy one built without input_type (search keeps working), else None.
    Raises RuntimeError for a policy this version cannot follow (e.g. one a newer Locus recorded).
    """
    current = input_type_policy_of(embedder)
    if current is None:
        return None  # local / openai / hash: nothing to follow
    built = _built_with(embedder, recorded)
    if built == current:
        return None
    use = getattr(embedder, "use_input_type_policy", None)
    if built not in INPUT_TYPE_POLICIES or use is None:
        raise RuntimeError(
            f"Collection {collection!r} records input_type policy {built!r}, which this Locus ({__version__}) "
            f"cannot follow for embedder {embedder.name!r}. Upgrade Locus, or use a new LOCUS_COLLECTION and "
            "re-index. Never mix policies in one collection."
        )
    use(built)
    return legacy_input_type_warning(collection) if built == INPUT_TYPE_NONE else None


def _built_with(embedder: Embedder, recorded: str | None) -> str | None:
    """The policy a collection recording *recorded* was built with (None: before policies were recorded)."""
    if recorded is not None:
        return recorded
    return getattr(embedder, "unrecorded_input_type_policy", input_type_policy_of(embedder))


def check_collection_policy(embedder: Embedder, recorded: str | None, collection: str) -> None:
    """Refuse a collection whose recorded policy changed while this process had it open (the store
    calls this on such a reopen): another process emptied it and a newer engine recorded v1 on it,
    while *embedder* still follows the old policy. Writing or searching on would mix the two kinds
    of vector, so only a restart, which follows the collection again, may continue."""
    current = input_type_policy_of(embedder)
    if current is None or _built_with(embedder, recorded) == current:
        return
    raise RuntimeError(
        f"Collection {collection!r} now records input_type policy {recorded!r}, but this process embeds "
        f"with {current!r}: another Locus process re-created it while this one had it open. Restart "
        "`locus serve` / `locus watch` so it follows the collection. Never mix policies in one collection."
    )


def _batched(items: list[str], size: int) -> Iterator[list[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


class HashEmbedder:
    """Bag-of-words hashing into a fixed-size normalised vector. Offline; for tests only."""

    def __init__(self, dim: int = 256) -> None:
        self.dim = dim
        self.name = f"hash-{dim}"

    def _vector(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for token in re.findall(r"\w+", text.lower()):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).hexdigest()
            vec[int(digest, 16) % self.dim] += 1.0
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


class VoyageEmbedder:
    """Voyage AI text embeddings. Uses input_type document/query as Voyage recommends."""

    # It always has, so a collection it built before policies were recorded is v1 too.
    input_type_policy = INPUT_TYPE_V1
    unrecorded_input_type_policy = INPUT_TYPE_V1

    def __init__(self, model: str, api_key: str | None = None, batch_size: int = 128) -> None:
        import voyageai

        key = api_key or os.getenv("VOYAGE_API_KEY")
        if not key:
            raise RuntimeError("VOYAGE_API_KEY is not set. Put it in .env, or use LOCUS_EMBEDDER=local.")
        self._client = voyageai.Client(api_key=key)
        self.model = model
        self.batch_size = batch_size
        self.name = f"voyage:{model}"

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for batch in _batched(texts, self.batch_size):
            result = _typed_provider_call(
                "Voyage AI", lambda b=batch: self._client.embed(b, model=self.model, input_type="document")
            )
            out.extend(result.embeddings)
        return out

    def embed_query(self, text: str) -> list[float]:
        result = _typed_provider_call(
            "Voyage AI", lambda: self._client.embed([text], model=self.model, input_type="query")
        )
        return result.embeddings[0]


class OpenAIEmbedder:
    def __init__(self, model: str, api_key: str | None = None, batch_size: int = 256) -> None:
        from openai import OpenAI

        key = api_key or os.getenv("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("OPENAI_API_KEY is not set. Put it in .env, or use LOCUS_EMBEDDER=local.")
        self._client = OpenAI(api_key=key)
        self.model = model
        self.batch_size = batch_size
        self.name = f"openai:{model}"

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for batch in _batched(texts, self.batch_size):
            response = _typed_provider_call(
                "OpenAI", lambda b=batch: self._client.embeddings.create(model=self.model, input=b)
            )
            out.extend(item.embedding for item in response.data)
        return out

    def embed_query(self, text: str) -> list[float]:
        response = _typed_provider_call(
            "OpenAI", lambda: self._client.embeddings.create(model=self.model, input=[text])
        )
        return response.data[0].embedding


# Hugging Face's own test for its *_OFFLINE variables (huggingface_hub.constants._is_true).
_HF_TRUE = frozenset({"1", "ON", "YES", "TRUE"})


class LocalModelUnavailable(RuntimeError):
    """The on-device model could not be loaded: on a first run, almost always a model download from
    huggingface.co that failed (offline, a proxy or a firewall, HF_HUB_OFFLINE set before any copy
    exists). A RuntimeError, so `locus index`, `watch` and `query` print it after `error:` and exit 2
    (cli.main) instead of a traceback; the MCP server answers with it (mcp_server._components)."""


def _hf_offline() -> bool:
    return any(
        (os.environ.get(var) or "").strip().upper() in _HF_TRUE for var in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")
    )


def _through_a_proxy(exc: BaseException) -> bool:
    """True when the failure, or anything it was raised from, is a proxy error (httpx.ProxyError,
    requests' ProxyError). huggingface_hub re-raises those even when the model is already cached."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if "proxy" in type(current).__name__.lower():
            return True
        current = current.__cause__ or current.__context__
    return False


def local_model_error(model: str, exc: BaseException) -> str:
    """What `LocalEmbedder` says when its model cannot be loaded: what failed, why the first run
    needs the network, what to do (the HF_HUB_OFFLINE or proxy hint when it applies), and the
    managed alternative. Never a traceback."""
    reason = (str(exc).strip().splitlines() or [""])[0]
    if len(reason) > 200:
        reason = reason[:197] + "..."
    lines = [
        f"Locus could not load its on-device embedding model {model} "
        f"({type(exc).__name__}{': ' + reason if reason else ''}).",
        "The first run with LOCUS_EMBEDDER=local downloads this model from huggingface.co and keeps a "
        "copy on this computer; later runs use that copy.",
    ]
    if _hf_offline():
        lines.append(
            "HF_HUB_OFFLINE (or TRANSFORMERS_OFFLINE) is set, so the model may not be downloaded, and no "
            "copy of it was found here. Unset it for one run so the model downloads, then set it again if "
            "you need it."
        )
    else:
        if _through_a_proxy(exc):
            lines.append(
                "The proxy this computer uses did not let the download through to huggingface.co. Ask "
                "whoever runs it to allow huggingface.co, or set HTTPS_PROXY to a proxy that does."
            )
        else:
            lines.append(
                "Check that this computer is online and can reach huggingface.co (a firewall or a proxy can "
                "block it; set HTTPS_PROXY if you must go through a proxy)."
            )
        lines.append(
            "If the model is already downloaded on this computer, set HF_HUB_OFFLINE=1 to load that copy "
            "without going online."
        )
    if model != DEFAULT_LOCAL_MODEL:
        lines.append(
            "LOCUS_LOCAL_MODEL is set: check that it names a sentence-transformers model on huggingface.co "
            "or a model folder on this computer."
        )
    lines.append("Then try again.")
    lines.append("Or use a Locus plan instead, with no model download: run `locus setup --managed`.")
    return "\n".join(lines)


# The English BGE v1.5 instruction belongs to the English `bge-{small,base,large}-en` models only. bge-m3 and
# the other multilingual models were trained without it, and prepending an English instruction to a Hebrew
# query shifts it (ADR-087 item 2). Matched on the last path segment so a local model folder works too.
_ENGLISH_BGE_MODEL = re.compile(r"^bge-[a-z]+-en(-v\d+(\.\d+)?)?$")


def uses_english_bge_query_instruction(model: str) -> bool:
    name = model.strip().rstrip("/").rsplit("/", 1)[-1].lower()
    return _ENGLISH_BGE_MODEL.match(name) is not None


# The e5 family was trained with "query: " on searches and "passage: " on documents; without them retrieval is worse.
_E5_MODEL = re.compile(r"^(multilingual-)?e5-(small|base|large)(-v\d+)?$")


def local_prefixes(model: str) -> tuple[str, str]:
    """(query prefix, document prefix) the model was trained with. Matched on the last path segment, so a model
    folder works too. bge-m3 and the other multilingual models take none."""
    name = model.strip().rstrip("/").rsplit("/", 1)[-1].lower()
    if _E5_MODEL.match(name):
        return "query: ", "passage: "
    if _ENGLISH_BGE_MODEL.match(name):
        return LocalEmbedder._BGE_QUERY_PREFIX, ""
    return "", ""


LOCAL_BACKEND_ENV = "LOCUS_LOCAL_BACKEND"  # auto (default) | onnx | torch


def prepare_hf_download() -> None:
    """Make a first-run model download fail or finish instead of hanging.

    huggingface_hub's Xet transfer path stalled for 10+ minutes without output on a clean machine
    (2026-11-12, `locus doctor`, "connection struggling" retried forever, no timeout); the plain HTTP path
    fetched the same 133 MB in 40 s. The models are small, so plain HTTP is the default; a user who sets
    HF_HUB_DISABLE_XET themselves keeps their value. Must run before huggingface_hub is first imported."""
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")


def _announce_download(model: str) -> None:
    """Say on stderr that the first-run download starts (it is silent otherwise). Not when HF_HUB_OFFLINE is set:
    nothing can be downloaded then, and the error that follows says so."""
    if _hf_offline():
        return
    print(f"Locus: downloading the embedding model {model} (first run only) ...", file=sys.stderr, flush=True)


def _onnx_cache_dir() -> str:
    """Where ONNX models live: FASTEMBED_CACHE_PATH, else ~/.cache/locus/onnx. fastembed's own default is the
    system temp directory, which a reboot empties, so the model would be downloaded again."""
    explicit = os.environ.get("FASTEMBED_CACHE_PATH")
    if explicit:
        return explicit
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "locus", "onnx")


# ONNX builds that fastembed does not list but whose files are published: registered when first loaded (no remote
# code). multilingual-e5-small was measured on 2026-11-12 to match PyTorch (cosine >= 0.9999999) on 976 passages once
# the text is stripped; fastembed adds no query/passage prefixes, so LocalEmbedder adds them (local_prefixes).
_CUSTOM_ONNX: dict[str, dict[str, object]] = {
    # The dynamic int8 file of the same repo (118 MB instead of 470 MB). Measured 2026-11-12 against fp32 on 900 paired
    # English/Hebrew questions: recall@5 within 0.33 points (every McNemar p >= 0.37), mean cosine 0.9988.
    "intfloat/multilingual-e5-small": {
        "dim": 384,
        "pooling": "MEAN",
        "normalization": True,
        "model_file": "onnx/model_qint8_avx512_vnni.onnx",
    },
}


def _custom_spec(model: str) -> tuple[str, dict[str, object]] | None:
    for name, spec in _CUSTOM_ONNX.items():
        if name.lower() == model.lower():
            return name, spec
    return None


def _register_custom_onnx(text_embedding: object, model: str) -> str:
    """Register *model* with fastembed when it is one of _CUSTOM_ONNX; returns the name to load it by."""
    found = _custom_spec(model)
    if found is None:
        return model
    name, spec = found
    from fastembed.common.model_description import ModelSource, PoolingType

    try:
        text_embedding.add_custom_model(  # type: ignore[attr-defined]
            model=name,
            pooling=PoolingType[str(spec["pooling"])],
            normalization=bool(spec["normalization"]),
            sources=ModelSource(hf=name),
            dim=int(spec["dim"]),  # type: ignore[call-overload]
            model_file=str(spec["model_file"]),
            license="mit",
        )
    except ValueError:  # already registered in this process
        pass
    return name


def local_backend(model: str) -> tuple[str, object | None]:
    """("onnx", TextEmbedding) when fastembed is installed and offers *model*, else ("torch", None).

    LOCUS_LOCAL_BACKEND=torch forces sentence-transformers; =onnx insists on fastembed. The ONNX route is the
    default `local` extra (about 530 MB installed with its model); the torch route stays for models fastembed does not
    offer (bge-m3, multilingual-e5-small, a model folder) and needs the `local-torch` extra."""
    choice = (os.environ.get(LOCAL_BACKEND_ENV) or "auto").strip().lower()
    if choice not in ("auto", "onnx", "torch"):
        raise RuntimeError(f"{LOCAL_BACKEND_ENV}={choice!r} is not valid; use auto, onnx or torch.")
    if choice == "torch":
        return "torch", None
    prepare_hf_download()  # fastembed imports huggingface_hub, which reads HF_HUB_DISABLE_XET once, at import
    try:
        from fastembed import TextEmbedding
    except ModuleNotFoundError as exc:
        if exc.name != "fastembed":  # fastembed is there but one of its own dependencies is not
            raise RuntimeError(_fastembed_broken(exc)) from exc
        if choice == "onnx":
            raise
        return "torch", None
    except ImportError as exc:  # installed but cannot load: a DLL, an architecture, a Python version
        raise RuntimeError(_fastembed_broken(exc)) from exc
    try:
        offered = {entry["model"].lower() for entry in TextEmbedding.list_supported_models()}
    except Exception:  # noqa: BLE001 - a fastembed without the listing: let the model load decide
        offered = {model.lower()}
    offered |= {name.lower() for name in _CUSTOM_ONNX}
    if model.lower() in offered:
        return "onnx", TextEmbedding
    if choice == "onnx":
        raise RuntimeError(
            f"{LOCAL_BACKEND_ENV}=onnx, but fastembed has no ONNX build of {model}. "
            "Unset it to use sentence-transformers."
        )
    return "torch", None


@contextlib.contextmanager
def _quiet_fastembed() -> Iterator[None]:
    try:
        from loguru import logger
    except ImportError:
        yield
        return
    logger.disable("fastembed")
    try:
        yield
    finally:
        logger.enable("fastembed")


def _fastembed_broken(exc: BaseException) -> str:
    return (
        f"fastembed is installed but cannot be loaded ({type(exc).__name__}: {exc}). "
        f"Fix or reinstall it, or set {LOCAL_BACKEND_ENV}=torch to use sentence-transformers "
        '(extra `local-torch`: uv tool install --reinstall --python 3.12 "locusfiles[local-torch]").'
    )


class _OnnxModel:
    """fastembed (ONNX Runtime) behind the two members LocalEmbedder uses from a SentenceTransformer:
    `encode(...).tolist()` and `max_seq_length`."""

    def __init__(self, model: str, text_embedding: object) -> None:
        model = _register_custom_onnx(text_embedding, model)
        cache_dir = _onnx_cache_dir()
        try:
            # Already downloaded: no request to huggingface.co. Only a missing model is fetched. fastembed logs a
            # "could not find the model" ERROR for the first, expected miss; keep that out of a new user's terminal.
            with _quiet_fastembed():
                self._model = text_embedding(model, cache_dir=cache_dir, local_files_only=True)  # type: ignore[operator]
        except Exception:  # noqa: BLE001 - not downloaded yet or incomplete
            _announce_download(model)
            self._model = text_embedding(model, cache_dir=cache_dir)  # type: ignore[operator]
        from .chunk_policy import KNOWN_LOCAL_WINDOWS

        self.max_seq_length = KNOWN_LOCAL_WINDOWS.get(model.rstrip("/").rsplit("/", 1)[-1].lower())

    def encode(self, texts: list[str], normalize_embeddings: bool = True, batch_size: int = 32):  # noqa: ANN201
        import numpy as np

        if not texts:
            return np.zeros((0, 0), dtype="float32")
        # strip(): a trailing space adds a lone "_" token on this route that PyTorch's tokenizer drops, which moved
        # 40 of 1,800 vectors by up to 0.7% in the 2026-11-12 check. Sorting by length first: fastembed pads each batch
        # to its longest text, so unsorted input took 1.8 times as long (24.6 s against 13.7 s for 976 passages).
        clean = [t.strip() for t in texts]
        order = sorted(range(len(clean)), key=lambda i: len(clean[i]))
        sorted_vectors = np.asarray(
            list(self._model.embed([clean[i] for i in order], batch_size=batch_size)), dtype="float32"
        )
        vectors = np.empty_like(sorted_vectors)
        vectors[order] = sorted_vectors
        if normalize_embeddings:
            norms = np.linalg.norm(vectors, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            vectors = vectors / norms
        return vectors


class LocalEmbedder:
    """An embedding model running on this machine: fastembed (ONNX Runtime) by default, or
    sentence-transformers (PyTorch) for the models fastembed does not offer. No text egress after the model
    download."""

    _BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "
    _doc_prefix = ""  # set per model in __init__; a class default keeps objects built without __init__ working

    def __init__(self, model: str, device: str | None = None) -> None:
        # `device` None = sentence-transformers' own choice (MPS on Apple silicon); the shared helper passes "cpu".
        # It does not apply to the ONNX route. A missing `local` extra fails here, outside the try: get_embedder
        # words that one.
        prepare_hf_download()
        _backend, text_embedding = local_backend(model)
        try:
            if text_embedding is not None:
                self._model = _OnnxModel(model, text_embedding)
            else:
                self._model = self._load_torch(model, device)
        except ImportError:
            raise  # a dependency of the model stack, not the download: get_embedder or a traceback
        except Exception as exc:  # the download or the load: httpx/requests errors, OSError, HF's own
            raise LocalModelUnavailable(local_model_error(model, exc)) from exc
        self.model = model
        self.name = f"local:{model}"
        self._query_prefix, self._doc_prefix = local_prefixes(model)
        # The model's input window in tokens (sentence-transformers' max_seq_length), which decides the
        # default chunk size of a new collection (chunk_policy.py). None when the model does not say.
        window = getattr(self._model, "max_seq_length", None)
        self.window_tokens: int | None = window if isinstance(window, int) and window > 0 else None
        # One encode at a time. The MCP server's warm-up thread (mcp_server.start_warmup) and the first
        # tool call can reach the model together; two concurrent `encode` calls on one
        # SentenceTransformer deadlock or abort the process on some devices (Lane M, 2026-10-04: every
        # local stdio client shape hung or lost the connection on 0.1.3). The lock makes the second
        # caller wait for the first instead.
        self._encode_lock = threading.Lock()

    @staticmethod
    def _load_torch(model: str, device: str | None):  # noqa: ANN205
        from sentence_transformers import SentenceTransformer

        kwargs = {} if device is None else {"device": device}
        try:
            # A model that is already on this computer loads without any request to huggingface.co (no network
            # check at every start, no unauthenticated-request warning); only a missing model is fetched.
            return SentenceTransformer(model, local_files_only=True, **kwargs)
        except ImportError:
            raise
        except Exception:  # noqa: BLE001 - not downloaded yet, incomplete, or a test double without the argument
            _announce_download(model)
            return SentenceTransformer(model, **kwargs)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if self._doc_prefix:
            texts = [self._doc_prefix + t for t in texts]
        with self._encode_lock:
            return self._model.encode(texts, normalize_embeddings=True, batch_size=32).tolist()

    def embed_query(self, text: str) -> list[float]:
        with self._encode_lock:
            vec = self._model.encode([self._query_prefix + text], normalize_embeddings=True)
        return vec.tolist()[0]


class ManagedEmbedder:
    """Calls Locus's own hosted embed-relay with a per-user LOCUS_API_KEY instead of a Voyage key.

    Request/response shape must match locus-web/app/api/embed-relay/route.ts exactly:
    POST {relay_url} with header "Authorization: Bearer <key>", body {"texts": [...], "purpose":
    "document" | "query", "input_type": "document" | "query"}, expects {"embeddings": [[float, ...],
    ...]} in the same order as the input texts. `purpose` lets the relay keep search working (one
    short query text) after an indexing budget is used up; relays that predate it ignore the field.

    `input_type` (RET-002) is Voyage's own document/query hint, which the relay validates and
    forwards: "document" for every chunk, "query" for every search, whatever the `purpose`. It
    follows the collection's policy (see `follow_collection_policy`): under INPUT_TYPE_NONE, a
    managed collection built before RET-002, it is left out of both calls so that collection keeps
    one kind of vector.

    Every failure is raised as a typed error from `errors.py` (RET-008): the relay's structured
    `{error, code, reason?, retry_after?}` body when it sends one, today's refusal texts otherwise.
    """

    # A managed collection that records no policy was built before RET-002, without input_type.
    unrecorded_input_type_policy = INPUT_TYPE_NONE

    def __init__(
        self,
        api_key: str | None,
        relay_url: str,
        batch_size: int = 128,
        billing_url: str | None = None,
        timeout: float = 60,
    ) -> None:
        if not api_key:
            raise RuntimeError(
                "LOCUS_API_KEY is not set. Get one from your dashboard after subscribing, "
                "or use LOCUS_EMBEDDER=local to run fully on-device with no key."
            )
        self._api_key = api_key
        self._relay_url = relay_url
        self._billing_url = billing_url
        self._timeout = timeout
        self.batch_size = batch_size
        self.name = "managed:voyage-4"  # relay's current backing model; bump if the relay changes it
        self.input_type_policy = INPUT_TYPE_V1  # new collections; follow_collection_policy may change it

    def use_input_type_policy(self, policy: str) -> None:
        """Embed the way a collection with *policy* was built (see follow_collection_policy)."""
        if policy not in INPUT_TYPE_POLICIES:
            raise ValueError(f"unknown input_type policy {policy!r}")
        self.input_type_policy = policy

    def _call(self, texts: list[str], purpose: str, input_type: str) -> list[list[float]]:
        payload: dict[str, object] = {"texts": texts, "purpose": purpose}
        if self.input_type_policy == INPUT_TYPE_V1:
            payload["input_type"] = input_type
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self._relay_url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                # OPS-007: the engine version, as the relay-token call already sends it. The relay
                # records it (org last-seen) today; a minimum-version check can key off it later.
                "X-Locus-Version": __version__,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            # The relay's JSON says WHICH limit refused the call (trial expired, trial token budget
            # used up, daily quota, no subscription); without it an exhausted trial budget was
            # mislabelled "subscription isn't active".
            raise from_relay_http(
                exc.code,
                _read_error_body(exc),
                exc.headers,
                relay_url=self._relay_url,
                upgrade_url=self._billing_url,
            ) from exc
        except urllib.error.URLError as exc:
            raise ProviderUnavailable(f"Could not reach the Locus embed relay: {exc.reason}") from exc
        except (TimeoutError, ConnectionError, http.client.HTTPException) as exc:
            # A read timeout or a dropped connection is not a URLError; it used to escape as a bare
            # OSError and end the whole `locus index` run as if it were a disk error.
            raise ProviderUnavailable(f"The Locus embed relay did not answer ({type(exc).__name__}: {exc})") from exc
        try:
            embeddings = json.loads(raw)["embeddings"]
        except (ValueError, KeyError, TypeError) as exc:
            raise ProviderUnavailable("The Locus embed relay returned a response Locus could not read.") from exc
        if not isinstance(embeddings, list) or len(embeddings) != len(texts):
            raise ProviderUnavailable(
                f"The Locus embed relay returned {len(embeddings) if isinstance(embeddings, list) else 'no'} "
                f"vectors for {len(texts)} texts."
            )
        return embeddings

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for batch in _batched(texts, self.batch_size):
            out.extend(self._call(batch, "document", "document"))
        return out

    def embed_query(self, text: str) -> list[float]:
        # Only a query within the relay's limit is sent as purpose "query" (the carve-out that keeps
        # search working after an indexing budget is used up); a longer one — `locus query` has no
        # length cap — takes the normal document path for billing, exactly as before `purpose`
        # existed. Either way it is embedded as a query (input_type "query").
        purpose = "query" if len(text) <= QUERY_MAX_CHARS else "document"
        return self._call([text], purpose, "query")[0]


def _read_error_body(exc: urllib.error.HTTPError, limit: int = 4096) -> bytes:
    """At most *limit* bytes of an error response body; b"" if it cannot be read."""
    try:
        return exc.read(limit) or b""
    except (OSError, AttributeError, http.client.HTTPException):
        return b""


def expected_embedder_name(settings: Settings) -> str:
    """The `name` get_embedder(settings) would produce, without building it (a local model would be
    loaded, possibly downloaded). Lets read-only tools open the right collection."""
    kind = settings.embedder
    if kind == "voyage":
        return f"voyage:{settings.voyage_model}"
    if kind == "openai":
        return f"openai:{settings.openai_model}"
    if kind == "local":
        return f"local:{settings.local_model}"
    if kind == "managed":
        return "managed:voyage-4"
    if kind == "hash":
        return HashEmbedder().name
    raise RuntimeError(f"Unknown LOCUS_EMBEDDER={kind!r}; expected voyage | openai | local | managed | hash")


def get_embedder(settings: Settings) -> Embedder:
    kind = settings.embedder
    extras = {"voyage": "voyage", "openai": "openai", "local": "local"}
    try:
        if kind == "voyage":
            return VoyageEmbedder(settings.voyage_model)
        if kind == "openai":
            return OpenAIEmbedder(settings.openai_model)
        if kind == "local":
            from .shared_embedder import SharedEmbedderClient, idle_seconds, shared_embedder_enabled, socket_path_for

            if shared_embedder_enabled(
                data_dir=settings.data_dir
            ):  # ADR-089 draft: one model for every locus process (opt-in)
                return SharedEmbedderClient(
                    settings.local_model,
                    socket_path_for(settings.data_dir, settings.local_model),
                    idle=idle_seconds(),
                )
            return LocalEmbedder(settings.local_model)
        if kind == "managed":
            # hosted_url: an invalid LOCUS_API_BASE surfaces here, where the relay is actually used.
            return ManagedEmbedder(
                settings.managed_api_key,
                settings.hosted_url(settings.managed_relay_url),
                billing_url=settings.billing_url or None,
            )
        if kind == "hash":
            return HashEmbedder()
    except ModuleNotFoundError as exc:
        # uv first, then pipx, the way README.md installs from PyPI; the source-checkout form is last.
        extra = extras[kind]
        if kind == "local" and exc.name == "sentence_transformers" and settings.local_model != DEFAULT_LOCAL_MODEL:
            extra = "local-torch"  # a model fastembed does not offer needs the PyTorch route
        raise RuntimeError(
            f"LOCUS_EMBEDDER={kind!r} needs an optional dependency ({exc.name}). "
            "Install it with the tool you installed Locus with: "
            f'uv tool install --reinstall --python 3.12 "locusfiles[{extra}]", or with pipx: '
            f'pipx install --force "locusfiles[{extra}]". From a source checkout: pip install -e ".[{extra}]".'
        ) from exc
    raise RuntimeError(f"Unknown LOCUS_EMBEDDER={kind!r}; expected voyage | openai | local | managed | hash")


def embed_many(embedder: Embedder, texts: Iterable[str]) -> list[list[float]]:
    """Convenience wrapper so callers can pass any iterable."""
    return embedder.embed_documents(list(texts))
