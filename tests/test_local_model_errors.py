"""The on-device model cannot be loaded (UX-001 finding 1, 2026-10-02): a first run that cannot reach
huggingface.co (offline, a proxy that refuses it, HF_HUB_OFFLINE set before any copy exists) used to
give `locus index` a 105-line httpx traceback. Now `LocalEmbedder` raises LocalModelUnavailable with
what failed and what to do, `index` / `watch` / `query` print it after `error:` (exit 2), and
`locus serve` answers with it, retries on the next request and writes nothing to stdout.

Offline: `sentence_transformers` is a fake module whose SentenceTransformer raises what
huggingface_hub raises (httpx.ProxyError, its LocalEntryNotFoundError, a plain OSError). No model is
downloaded, no network is used."""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from types import ModuleType

import httpx
import pytest

from locus import cli, config
from locus import mcp_server as m
from locus.config import get_settings
from locus.ingest.embedder import LocalEmbedder, LocalModelUnavailable, get_embedder, local_model_error

MODEL = config.DEFAULT_LOCAL_MODEL
HF_VARS = ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "LOCUS_LOCAL_MODEL", "HTTPS_PROXY", "https_proxy")


class _LocalEntryNotFound(FileNotFoundError):
    """Shaped like huggingface_hub.errors.LocalEntryNotFoundError: a FileNotFoundError, which cli.main
    used to report as "no such folder"."""


class _FakeVectors(list):
    def tolist(self) -> list:
        return list(self)


class _FakeModel:
    def encode(self, texts, **_kwargs):  # noqa: ANN001 - sentence-transformers' signature
        return _FakeVectors([[1.0] + [0.0] * 7 for _ in texts])


class _FakeST:
    """Stands in for the sentence_transformers module: each SentenceTransformer(...) call raises the
    next queued exception, or returns a working fake model once the queue is empty."""

    def __init__(self, *failures: BaseException) -> None:
        self.failures = list(failures)
        self.calls: list[str] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        module = ModuleType("sentence_transformers")

        def sentence_transformer(model: str) -> _FakeModel:
            self.calls.append(model)
            if self.failures:
                raise self.failures.pop(0)
            return _FakeModel()

        module.SentenceTransformer = sentence_transformer  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "sentence_transformers", module)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """LOCUS_EMBEDDER=local, a data folder in tmp_path, no .env, no HF or proxy settings from the shell,
    and os.environ restored afterwards (get_settings' load_dotenv writes into it directly)."""
    saved = dict(os.environ)
    for key in list(os.environ):
        if key.startswith("LOCUS_") or key in HF_VARS:
            del os.environ[key]
    os.environ["LOCUS_EMBEDDER"] = "local"
    os.environ["LOCUS_DATA_DIR"] = str(tmp_path / "data")
    os.environ["LOCUS_WARMUP"] = "1"
    # These tests fake `sentence_transformers`. Deleting every LOCUS_ variable also deleted LOCUS_LOCAL_BACKEND, so on
    # a machine with fastembed installed the ONNX route ran instead and loaded (or downloaded) the real model, and
    # the proxy-refused watch case could hang forever. Pin the route the fakes stand in for.
    os.environ["LOCUS_LOCAL_BACKEND"] = "torch"
    monkeypatch.setattr(config, "_SOURCE_CHECKOUT", None)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    m._state.clear()
    get_settings.cache_clear()
    yield
    m._state.clear()
    get_settings.cache_clear()
    os.environ.clear()
    os.environ.update(saved)


def _proxy_refused() -> httpx.ProxyError:
    return httpx.ProxyError("403 Forbidden")


# --- the message --------------------------------------------------------------------------------


def test_a_refused_proxy_says_what_failed_why_and_what_to_do(monkeypatch):
    _FakeST(_proxy_refused()).install(monkeypatch)
    with pytest.raises(LocalModelUnavailable) as info:
        LocalEmbedder(MODEL)
    text = str(info.value)
    assert text.startswith(f"Locus could not load its on-device embedding model {MODEL} (ProxyError: 403 Forbidden).")
    assert "The first run with LOCUS_EMBEDDER=local downloads this model from huggingface.co" in text
    assert "proxy this computer uses did not let the download through" in text and "HTTPS_PROXY" in text
    # huggingface_hub re-raises a proxy error even when a copy is cached: HF_HUB_OFFLINE=1 loads it.
    assert "If the model is already downloaded on this computer, set HF_HUB_OFFLINE=1" in text
    assert "Then try again." in text
    assert text.endswith("Or use a Locus plan instead, with no model download: run `locus setup --managed`.")
    assert "LOCUS_LOCAL_MODEL" not in text  # the default model: nothing to check there
    assert isinstance(info.value, RuntimeError)
    assert isinstance(info.value.__cause__, httpx.ProxyError)


def test_a_proxy_error_wrapped_in_another_exception_is_still_called_a_proxy_error():
    try:
        try:
            raise _proxy_refused()
        except httpx.ProxyError as inner:
            raise OSError("We couldn't connect to 'https://huggingface.co' to load the files") from inner
    except OSError as outer:
        text = local_model_error(MODEL, outer)
    assert "OSError: We couldn't connect" in text and "did not let the download through" in text


def test_offline_without_a_proxy_points_at_the_network(monkeypatch):
    _FakeST(OSError("We couldn't connect to 'https://huggingface.co' to load the files\nsecond line")).install(
        monkeypatch
    )
    with pytest.raises(LocalModelUnavailable) as info:
        LocalEmbedder(MODEL)
    text = str(info.value)
    assert "(OSError: We couldn't connect to 'https://huggingface.co' to load the files)." in text
    assert "second line" not in text  # the first line of the reason only
    assert "Check that this computer is online and can reach huggingface.co" in text
    assert "did not let the download through" not in text


@pytest.mark.parametrize("var", ["HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"])
def test_hf_offline_set_before_any_copy_exists_says_to_unset_it_once(monkeypatch, var):
    monkeypatch.setenv(var, "1")
    _FakeST(_LocalEntryNotFound("Cannot find an appropriate cached snapshot folder")).install(monkeypatch)
    with pytest.raises(LocalModelUnavailable) as info:
        LocalEmbedder(MODEL)
    text = str(info.value)
    assert "HF_HUB_OFFLINE (or TRANSFORMERS_OFFLINE) is set" in text and "Unset it for one run" in text
    assert "set HF_HUB_OFFLINE=1" not in text  # it already is
    assert "locus setup --managed" in text


def test_a_custom_model_name_is_named_as_something_to_check(monkeypatch):
    _FakeST(OSError("not a valid model identifier")).install(monkeypatch)
    with pytest.raises(LocalModelUnavailable) as info:
        LocalEmbedder("someone/not-a-model")
    text = str(info.value)
    assert "model someone/not-a-model (OSError: not a valid model identifier)" in text
    assert "LOCUS_LOCAL_MODEL is set: check that it names a sentence-transformers model" in text


def test_a_long_reason_is_cut(monkeypatch):
    _FakeST(OSError("x" * 500)).install(monkeypatch)
    with pytest.raises(LocalModelUnavailable) as info:
        LocalEmbedder(MODEL)
    first = str(info.value).splitlines()[0]
    assert len(first) < 300 and first.endswith("...).")


def test_a_missing_dependency_inside_the_model_stack_keeps_the_extra_hint(monkeypatch):
    """ImportError is not a download problem: get_embedder still names the missing module and extra."""
    _FakeST(ModuleNotFoundError("No module named 'torch'", name="torch")).install(monkeypatch)
    with pytest.raises(RuntimeError) as info:
        get_embedder(get_settings())
    assert not isinstance(info.value, LocalModelUnavailable)
    text = str(info.value)
    assert "needs an optional dependency (torch)" in text
    # A PyPI install first (uv, then pipx, as README.md installs it); the source checkout last.
    uv = text.index('uv tool install --reinstall --python 3.12 "locusfiles[local]"')
    pipx = text.index('pipx install --force "locusfiles[local]"')
    assert uv < pipx < text.index('From a source checkout: pip install -e ".[local]"')


def test_a_model_that_loads_is_unchanged(monkeypatch):
    _FakeST().install(monkeypatch)
    embedder = LocalEmbedder(MODEL)
    assert embedder.name == f"local:{MODEL}"
    assert embedder.embed_query("q") == [1.0] + [0.0] * 7


# --- the CLI ------------------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["index", "watch", "query"])
@pytest.mark.parametrize("failure", [_proxy_refused, lambda: _LocalEntryNotFound("no cached snapshot")])
def test_index_watch_and_query_print_the_error_without_a_traceback(tmp_path, monkeypatch, capsys, command, failure):
    _FakeST(failure()).install(monkeypatch)
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "a.md").write_text("hello\n")
    argv = [command, "what did I decide"] if command == "query" else [command, str(folder)]
    with pytest.raises(SystemExit) as info:
        cli.main(argv)
    assert info.value.code == 2  # a configuration-type stop, like a missing key
    out, err = capsys.readouterr()
    assert out == ""
    # The download notice ("Locus: downloading the embedding model ...") comes first, then the error line.
    assert f"error: Locus could not load its on-device embedding model {MODEL} (" in err
    assert "locus setup --managed" in err and "Traceback" not in err
    assert "no such folder" not in err  # a FileNotFoundError from the HF cache is not the user's folder


def test_doctor_shows_the_actionable_text_on_its_fail_line(monkeypatch, capsys):
    _FakeST(_proxy_refused()).install(monkeypatch)
    with pytest.raises(SystemExit) as info:
        cli.main(["doctor"])
    assert info.value.code == 1
    out = capsys.readouterr().out
    assert "[FAIL] embedder + store round-trip — LocalModelUnavailable: Locus could not load" in out
    assert "locus setup --managed" in out


def _doctor_fail_line(capsys, label: str) -> str:
    with pytest.raises(SystemExit) as info:
        cli.main(["doctor"])
    assert info.value.code == 1
    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith(f"[FAIL] {label}")]
    assert len(lines) == 1, lines
    return lines[0]


def test_doctor_names_uv_then_pipx_then_the_checkout_when_the_local_extra_is_missing(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)  # `import` raises ImportError
    monkeypatch.setitem(sys.modules, "fastembed", None)
    line = _doctor_fail_line(capsys, "dependency: fastembed or sentence-transformers (LOCUS_EMBEDDER=local)")
    uv = line.index('uv tool install --reinstall --python 3.12 "locusfiles[local]"')
    pipx = line.index('pipx install --force "locusfiles[local]"')
    assert uv < pipx < line.index("From a source checkout: pip install -e '.[dev,local]'")


def test_doctor_names_uv_then_pipx_then_the_checkout_when_a_core_dependency_is_missing(monkeypatch, capsys):
    _FakeST().install(monkeypatch)
    monkeypatch.setitem(sys.modules, "docx", None)
    line = _doctor_fail_line(capsys, "dependency: docx")
    uv = line.index('uv tool install --reinstall --python 3.12 "locusfiles[local]"')
    pipx = line.index("pipx reinstall locusfiles")
    assert uv < pipx < line.index("From a source checkout: pip install -e '.[dev]'")


# --- locus serve --------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_serve_answers_with_the_error_writes_nothing_to_stdout_and_retries_next_time(monkeypatch, capsys):
    pytest.importorskip("chromadb")
    fake = _FakeST(_proxy_refused())
    fake.install(monkeypatch)

    first = await m.locus_search("what did I decide about pricing")
    assert first.startswith(f"Locus could not load its on-device embedding model {MODEL} (ProxyError")
    assert "Nothing was searched or read." in first
    assert "env block for the Locus server" in first
    assert "error" not in m._state  # not cached: the next request tries the download again
    assert capsys.readouterr().out == ""  # constraint 5

    # The network is back: the next request loads the model, with no restart.
    second = await m.locus_search("what did I decide about pricing")
    assert fake.calls == [MODEL, MODEL]
    assert "nothing indexed yet" in second
    assert capsys.readouterr().out == ""


@pytest.mark.anyio
async def test_diagnose_shows_the_model_error_on_one_line(tmp_path, monkeypatch):
    pytest.importorskip("chromadb")
    (tmp_path / "data").mkdir()
    _FakeST(_proxy_refused()).install(monkeypatch)
    report = await m.locus_diagnose_connection()
    [line] = [line for line in report.splitlines() if line.startswith("collection:")]
    assert line.startswith("collection: unavailable (Locus could not load its on-device embedding model")
    assert "locus setup --managed" in line


def test_the_warmup_logs_the_error_to_stderr_only(tmp_path, monkeypatch, capsys):
    (tmp_path / "data").mkdir()
    _FakeST(_proxy_refused()).install(monkeypatch)
    thread = m.start_warmup()
    assert isinstance(thread, threading.Thread)
    thread.join(timeout=30)
    assert not thread.is_alive()
    out, err = capsys.readouterr()
    assert out == ""
    assert "warm-up stopped: Locus could not load its on-device embedding model" in err
    assert "error" not in m._state


def test_the_cli_message_names_no_path_of_the_users(tmp_path, monkeypatch, capsys):
    """The message carries the model name and the exception's first line, never the data folder."""
    _FakeST(_proxy_refused()).install(monkeypatch)
    with pytest.raises(SystemExit):
        cli.main(["query", "x"])
    assert str(Path(tmp_path)) not in capsys.readouterr().err


def test_a_model_already_on_this_computer_is_loaded_offline_first_and_a_missing_one_is_fetched(monkeypatch):
    """No request to huggingface.co at every start: the first attempt is local_files_only, the second (only when
    the first fails) is the ordinary download."""
    import sys
    import types

    from locus.ingest.embedder import LocalEmbedder

    calls: list[dict] = []

    class Fake:
        max_seq_length = 512

        def __init__(self, model, **kw):
            calls.append(kw)
            if kw.get("local_files_only") and model == "not/downloaded":
                raise OSError("not in the cache")

    monkeypatch.setitem(sys.modules, "sentence_transformers", types.SimpleNamespace(SentenceTransformer=Fake))
    LocalEmbedder("cached/model")
    assert calls == [{"local_files_only": True}]
    calls.clear()
    LocalEmbedder("not/downloaded")
    assert calls == [{"local_files_only": True}, {}]


def test_the_isolation_pins_the_torch_route_even_when_fastembed_is_installed(monkeypatch):
    """With fastembed importable, the ONNX route would load the real model; the fakes in this file need torch."""
    fake = ModuleType("fastembed")
    fake.TextEmbedding = object  # type: ignore[attr-defined]  # would be used (and fail) if the route were not forced
    monkeypatch.setitem(sys.modules, "fastembed", fake)
    from locus.ingest.embedder import local_backend

    assert os.environ["LOCUS_LOCAL_BACKEND"] == "torch"
    assert local_backend(MODEL) == ("torch", None)
