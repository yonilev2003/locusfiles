"""Warm-up at `locus serve` start (2026-10-02 live test): the first remote query failed with "did not
answer within 25 seconds" because the first call to `_components()` loads the embedder and opens the
index. `start_warmup()` does that in a daemon thread at start. Offline: HashEmbedder, no network."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from locus import mcp_server as m
from locus.config import get_settings


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _fresh_state():
    m._state.clear()
    get_settings.cache_clear()
    yield
    m._state.clear()
    get_settings.cache_clear()


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> pytest.MonkeyPatch:
    monkeypatch.setenv("LOCUS_EMBEDDER", "hash")
    monkeypatch.setenv("LOCUS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LOCUS_COLLECTION", "warm")
    monkeypatch.delenv("LOCUS_WARMUP", raising=False)
    monkeypatch.delenv("LOCUS_API_BASE", raising=False)
    get_settings.cache_clear()
    return monkeypatch


def _index_one_file(tmp_path: Path) -> None:
    """Index one small file into the data dir the env fixture names (what `locus index` leaves behind)."""
    pytest.importorskip("chromadb")
    from locus.ingest.embedder import HashEmbedder
    from locus.ingest.indexer import index_folder
    from locus.ingest.store import ChromaStore

    root = tmp_path / "corpus"
    root.mkdir()
    (root / "plan.md").write_text("The launch date is October 1.\n")
    settings = get_settings()
    embedder = HashEmbedder()
    store = ChromaStore(settings.data_dir, settings.collection, embedder.name)
    index_folder(root, embedder, store, settings, log=lambda _: None)


def _join(thread: threading.Thread | None) -> None:
    assert thread is not None
    thread.join(timeout=30)
    assert not thread.is_alive()


def test_warmup_opens_the_index_so_the_first_search_does_nothing_slow(env, tmp_path, capsys):
    _index_one_file(tmp_path)
    assert "store" not in m._state

    _join(m.start_warmup())

    assert "store" in m._state and "embedder" in m._state and "settings" in m._state
    captured = capsys.readouterr()
    assert captured.out == ""  # constraint 5: nothing on stdout but the MCP protocol
    assert "warming up" in captured.err and "warm-up done" in captured.err


@pytest.mark.anyio
async def test_the_first_search_after_the_warmup_opens_nothing_again(env, tmp_path, monkeypatch):
    _index_one_file(tmp_path)
    opened: list[int] = []
    real_open_index = m.open_index

    def counting_open_index(settings):
        opened.append(1)
        return real_open_index(settings)

    monkeypatch.setattr(m, "open_index", counting_open_index)
    _join(m.start_warmup())
    assert opened == [1]

    out = await m.locus_search("launch date")

    assert out.startswith("Today is ") and "\n# 1 results for: launch date" in out and "plan.md" in out
    assert opened == [1]  # the search reused what the warm-up opened


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "off", " 0 "])
def test_warmup_is_skipped_with_locus_warmup_off(env, tmp_path, value):
    _index_one_file(tmp_path)
    env.setenv("LOCUS_WARMUP", value)
    assert m.start_warmup() is None
    assert m._state == {}


@pytest.mark.parametrize("value", ["1", "true", "yes", "", "anything"])
def test_any_other_locus_warmup_value_leaves_it_on(env, tmp_path, value):
    _index_one_file(tmp_path)
    env.setenv("LOCUS_WARMUP", value)
    _join(m.start_warmup())
    assert "store" in m._state


def test_warmup_never_blocks_the_caller_even_while_the_embedder_is_loading(env, tmp_path):
    """The MCP handshake and the relay connection must not wait for a model load: start_warmup()
    returns at once while the open is still in progress, and the thread is a daemon (it cannot keep
    a stopping server alive)."""
    _index_one_file(tmp_path)
    entered, release = threading.Event(), threading.Event()
    real_open_index = m.open_index

    def slow_open_index(settings):
        entered.set()
        assert release.wait(timeout=30)
        return real_open_index(settings)

    env.setattr(m, "open_index", slow_open_index)
    started = time.monotonic()
    thread = m.start_warmup()
    returned_after = time.monotonic() - started
    try:
        assert thread is not None and thread.daemon and thread.name == "locus-warmup"
        assert entered.wait(timeout=10)  # the embedder load has begun ...
        assert thread.is_alive() and "store" not in m._state  # ... is still running ...
        assert returned_after < 2  # ... and the caller was long gone
    finally:
        release.set()
    _join(thread)
    assert "store" in m._state


def test_a_warmup_and_a_first_request_open_the_index_exactly_once(env, tmp_path):
    """Without the lock in _components(), a request that arrives while the warm-up is still loading
    would load the embedder and open Chroma a second time."""
    _index_one_file(tmp_path)
    entered, release = threading.Event(), threading.Event()
    opened: list[int] = []
    real_open_index = m.open_index

    def slow_counting_open_index(settings):
        opened.append(1)
        entered.set()
        assert release.wait(timeout=30)
        return real_open_index(settings)

    env.setattr(m, "open_index", slow_counting_open_index)
    warm = m.start_warmup()
    assert entered.wait(timeout=10)
    results: list[tuple] = []
    request = threading.Thread(target=lambda: results.append(m._components()))  # the first tool call
    request.start()
    time.sleep(0.3)  # long enough for the request to reach the lock (without it, to call open_index too)
    release.set()
    request.join(timeout=30)
    _join(warm)

    assert opened == [1]
    assert len(results) == 1 and results[0][1] is m._state["store"]


def test_a_failing_warmup_is_logged_and_the_first_request_retries(env, tmp_path, capsys):
    """Anything unexpected in the warm-up (here a disk error) is logged to stderr and swallowed: the
    server keeps running, nothing is cached as an error, and the first real request does the work."""
    _index_one_file(tmp_path)
    real_open_index = m.open_index

    def broken_open_index(settings):
        raise OSError("disk went away")

    env.setattr(m, "open_index", broken_open_index)
    _join(m.start_warmup())
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "warm-up failed (OSError: disk went away)" in captured.err
    assert m._state == {}

    env.setattr(m, "open_index", real_open_index)
    embedder, store, _ = m._components()
    assert store.count() > 0 and embedder.name


@pytest.mark.anyio
async def test_a_configuration_problem_is_reported_at_start_and_by_the_first_search_as_before(env, tmp_path, capsys):
    _index_one_file(tmp_path)

    def not_configured(settings):
        raise RuntimeError("VOYAGE_API_KEY is not set")

    env.setattr(m, "open_index", not_configured)
    _join(m.start_warmup())
    err = capsys.readouterr().err
    assert "warm-up stopped: Locus is not configured: VOYAGE_API_KEY is not set" in err

    out = await m.locus_search("launch date")
    assert out.startswith("Locus is not configured: VOYAGE_API_KEY is not set")


def test_warmup_does_nothing_and_creates_nothing_when_nothing_is_indexed(env, tmp_path, capsys):
    """A first `locus serve` before any `locus index`: opening the index would create the data folder
    and an empty collection, which the locus_help check reports as "data directory missing"."""
    data_dir = tmp_path / "data"
    assert not data_dir.exists()
    _join(m.start_warmup())
    assert not data_dir.exists()
    assert m._state == {}
    captured = capsys.readouterr()
    assert captured.out == "" and "warm-up skipped: nothing is indexed yet" in captured.err


class _CountingEmbedder:
    name = "counting"

    def __init__(self) -> None:
        self.queries: list[str] = []

    def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        return [1.0, 0.0]


class _EmptyStore:
    def __init__(self) -> None:
        self.queries: list[int] = []

    def query(self, embedding, k=5, path_prefix=None):
        self.queries.append(k)
        return []


@pytest.mark.parametrize(
    ("embedder_kind", "expected_queries"),
    [("hash", 1), ("local", 1), ("voyage", 0), ("openai", 0), ("managed", 0)],
)
def test_warmup_runs_a_throwaway_query_only_through_an_embedder_that_runs_on_this_computer(
    env, tmp_path, embedder_kind, expected_queries
):
    """A hosted embedder is not called: a throwaway request would use the network and, for managed,
    spend the plan's quota, and there is no model to warm."""
    _index_one_file(tmp_path)  # makes the data dir exist; open_index below is a fake
    env.setenv("LOCUS_EMBEDDER", embedder_kind)
    get_settings.cache_clear()
    embedder, store = _CountingEmbedder(), _EmptyStore()
    env.setattr(m, "open_index", lambda settings: (embedder, store, None))

    _join(m.start_warmup())

    assert len(embedder.queries) == expected_queries
    assert len(store.queries) == expected_queries


@pytest.mark.anyio
async def test_the_warmup_reads_the_file_list_so_the_first_search_does_not(env, tmp_path, monkeypatch):
    """The first `locus_search` after a start used to read the metadata of EVERY chunk (freshness asks the store for
    the indexed files): 11 to 18 s with 72,000 chunks. The warm-up does that read while nobody is waiting."""
    from locus.ingest.store import ChromaStore

    _index_one_file(tmp_path)
    reads: list[int] = []
    real_pages = ChromaStore._pages

    def counting_pages(self, *args, **kwargs):
        reads.append(1)
        return real_pages(self, *args, **kwargs)

    monkeypatch.setattr(ChromaStore, "_pages", counting_pages)
    _join(m.start_warmup())
    read_by_warmup = len(reads)
    assert read_by_warmup >= 1

    out = await m.locus_search("launch date")

    assert "plan.md" in out
    assert len(reads) == read_by_warmup  # the search reused the list the warm-up read


def test_a_failing_file_list_does_not_stop_the_rest_of_the_warmup(env, tmp_path, capsys):
    _index_one_file(tmp_path)

    class _Store(_EmptyStore):
        def file_mtimes(self):
            raise OSError("metadata unreadable")

    embedder, store = _CountingEmbedder(), _Store()
    env.setattr(m, "open_index", lambda settings: (embedder, store, None))
    _join(m.start_warmup())
    err = capsys.readouterr().err
    assert "reading the file list failed (OSError)" in err
    assert embedder.queries == ["warm-up"] and store.queries == [1]  # the throwaway query still ran
    assert "warm-up done" in err


def test_the_warmup_says_where_its_time_went(env, tmp_path, capsys):
    """One stderr line with the steps, so a slow start can be read from the AI app's log (stderr only)."""
    _index_one_file(tmp_path)
    _join(m.start_warmup())
    captured = capsys.readouterr()
    assert captured.out == ""
    steps = [line for line in captured.err.splitlines() if line.startswith("locus_mcp: warm-up steps:")]
    assert len(steps) == 1
    for step in ("index opened", "file list", "first query"):
        assert step in steps[0]
    assert captured.err.index("warm-up steps:") < captured.err.index("warm-up done in")


@pytest.mark.parametrize(
    ("embedder_kind", "expected"),
    [("local", ["helper", "open_index"]), ("hash", ["open_index"]), ("voyage", ["open_index"])],
)
def test_the_helper_starts_before_the_index_opens(env, tmp_path, embedder_kind, expected):
    """With the shared helper the model load is the longest step and needs nothing from the index: it must start
    first and run while the index opens. Only a local embedder has a helper; `start_helper_early` decides that."""
    from locus.ingest import shared_embedder as se

    _index_one_file(tmp_path)
    env.setenv("LOCUS_EMBEDDER", embedder_kind)
    env.setenv("LOCUS_SHARED_EMBEDDER", "1")
    get_settings.cache_clear()
    order: list[str] = []
    env.setattr(se.SharedEmbedderClient, "start_in_background", lambda self: order.append("helper"))

    def fake_open_index(settings):
        order.append("open_index")
        return _CountingEmbedder(), _EmptyStore(), None

    env.setattr(m, "open_index", fake_open_index)
    _join(m.start_warmup())

    assert order == expected


def test_the_helper_is_not_started_for_a_local_embedder_that_runs_in_this_process(env, tmp_path):
    from locus.ingest import shared_embedder as se

    _index_one_file(tmp_path)
    env.setenv("LOCUS_EMBEDDER", "local")
    env.setenv("LOCUS_SHARED_EMBEDDER", "0")
    get_settings.cache_clear()
    started: list[int] = []
    env.setattr(se.SharedEmbedderClient, "start_in_background", lambda self: started.append(1))
    env.setattr(m, "open_index", lambda settings: (_CountingEmbedder(), _EmptyStore(), None))
    _join(m.start_warmup())
    assert started == []


def test_a_bad_setting_does_not_stop_the_server_from_starting(env):
    """A non-numeric LOCUS_CHUNK_TOKENS makes get_settings() raise. The server still starts (the first
    tool call reports the problem, as before); the warm-up just does not run."""
    env.setenv("LOCUS_CHUNK_TOKENS", "seven hundred")
    get_settings.cache_clear()
    assert m.start_warmup() is None


# -- where `locus serve` starts it ----------------------------------------------------------------


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    order: list[str] = []
    monkeypatch.setattr(m, "start_warmup", lambda: order.append("warmup"))
    monkeypatch.setattr(m.mcp, "run", lambda: order.append("run"))
    monkeypatch.setattr(m, "_run_relay", lambda settings: order.append("relay"))
    monkeypatch.setattr(m, "_run_http", lambda **kwargs: order.append("http"))
    return order


def test_stdio_starts_the_warmup_before_serving(env, calls):
    m.main("stdio")
    assert calls == ["warmup", "run"]


def test_relay_starts_the_warmup_before_connecting(env, calls):
    env.setenv("LOCUS_ENABLE_HOSTED", "1")  # the relay is off unless switched on
    env.setenv("LOCUS_API_KEY", "lk_test_not_a_key")
    env.setenv("LOCUS_MCP_RELAY_SUPABASE_ANON_KEY", "anon")
    get_settings.cache_clear()
    m.main("relay")
    assert calls == ["warmup", "relay"]


def test_relay_needs_only_the_api_key_for_the_warmup(env, calls):
    """G1: the public relay key comes from the token answer, so a fresh install has only LOCUS_API_KEY."""
    env.setenv("LOCUS_ENABLE_HOSTED", "1")
    env.setenv("LOCUS_API_KEY", "lk_test_not_a_key")
    env.delenv("LOCUS_MCP_RELAY_SUPABASE_ANON_KEY", raising=False)
    get_settings.cache_clear()
    m.main("relay")
    assert calls == ["warmup", "relay"]


def test_relay_without_its_configuration_does_not_start_a_warmup_it_would_throw_away(env, calls):
    env.setenv("LOCUS_ENABLE_HOSTED", "1")
    env.delenv("LOCUS_API_KEY", raising=False)
    env.delenv("LOCUS_MCP_RELAY_SUPABASE_ANON_KEY", raising=False)
    get_settings.cache_clear()
    m.main("relay")
    assert calls == ["relay"]  # _run_relay prints its error and exits 2 itself


def test_streamable_http_starts_the_warmup_only_after_its_token_check(env, calls):
    env.delenv("LOCUS_MCP_TOKEN", raising=False)
    get_settings.cache_clear()
    with pytest.raises(SystemExit):
        m.main("streamable-http")
    assert calls == []
    env.setenv("LOCUS_MCP_TOKEN", "t" * 32)
    get_settings.cache_clear()
    m.main("streamable-http")
    assert calls == ["warmup", "http"]
