"""The ONNX route of LocalEmbedder (fastembed), offline: `fastembed` is a fake module, no model, no network."""

import sys
import time
import types

import pytest

from locus.config import DEFAULT_LOCAL_MODEL, get_settings
from locus.ingest import embedder as emb
from locus.ingest.embedder import LocalEmbedder, get_embedder

MODEL = "BAAI/bge-small-en-v1.5"
E5 = "intfloat/multilingual-e5-small"


class _FakeTextEmbedding:
    calls: list[dict] = []
    offered = {MODEL}
    registered: list[dict] = []
    cached = True  # False: local_files_only=True raises, as for a model that is not downloaded yet

    def __init__(self, model_name, cache_dir=None, local_files_only=False, **kwargs):
        if local_files_only and not type(self).cached:
            raise ValueError("model not on disk")
        type(self).calls.append({"model": model_name, "cache_dir": cache_dir, "local_only": local_files_only})
        self.embedded: list[str] = []

    @classmethod
    def list_supported_models(cls):
        return [{"model": name} for name in cls.offered]

    @classmethod
    def add_custom_model(cls, **kwargs):
        if any(r["model"] == kwargs["model"] for r in cls.registered):
            raise ValueError("already registered")
        cls.registered.append(kwargs)

    def embed(self, texts, batch_size=32):
        self.embedded.extend(texts)
        for t in texts:
            yield [float(len(t)), 1.0, 0.0]  # direction depends on the text, so a wrong order shows


@pytest.fixture(autouse=True)
def fake_fastembed(monkeypatch, tmp_path):
    _FakeTextEmbedding.calls = []
    _FakeTextEmbedding.offered = {MODEL}
    _FakeTextEmbedding.registered = []
    _FakeTextEmbedding.cached = True
    module = types.ModuleType("fastembed")
    module.TextEmbedding = _FakeTextEmbedding  # type: ignore[attr-defined]
    desc = types.ModuleType("fastembed.common.model_description")
    desc.ModelSource = lambda **kw: kw  # type: ignore[attr-defined]
    desc.PoolingType = {"MEAN": "MEAN", "CLS": "CLS"}  # type: ignore[attr-defined]
    common = types.ModuleType("fastembed.common")
    common.model_description = desc  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fastembed", module)
    monkeypatch.setitem(sys.modules, "fastembed.common", common)
    monkeypatch.setitem(sys.modules, "fastembed.common.model_description", desc)
    monkeypatch.setenv("LOCUS_LOCAL_BACKEND", "auto")
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(tmp_path / "onnx"))
    monkeypatch.delenv("HF_HUB_DISABLE_XET", raising=False)


def test_auto_uses_onnx_for_a_model_fastembed_offers():
    embedder = LocalEmbedder(MODEL)
    assert isinstance(embedder._model, emb._OnnxModel)
    assert embedder.name == f"local:{MODEL}"  # the name a torch-built collection recorded: same model name


def test_vectors_are_unit_length_and_the_query_gets_the_bge_instruction():
    embedder = LocalEmbedder(MODEL)
    vectors = embedder.embed_documents(["a", "b"])
    assert all(abs(sum(x * x for x in v) - 1) < 1e-6 for v in vectors)
    embedder.embed_query("what is x")
    assert embedder._model._model.embedded[-1] == emb.LocalEmbedder._BGE_QUERY_PREFIX + "what is x"


def test_window_is_known_so_new_collections_get_the_small_window_chunk_size():
    assert LocalEmbedder(MODEL).window_tokens == 512


def test_a_model_fastembed_does_not_offer_goes_to_torch(monkeypatch):
    seen = {}

    class Torch:
        max_seq_length = 8192

        def __init__(self, model, **kwargs):
            seen["model"] = model

    monkeypatch.setitem(sys.modules, "sentence_transformers", types.SimpleNamespace(SentenceTransformer=Torch))
    embedder = LocalEmbedder("BAAI/bge-m3")
    assert seen["model"] == "BAAI/bge-m3" and embedder.window_tokens == 8192


def test_torch_can_be_forced(monkeypatch):
    monkeypatch.setenv("LOCUS_LOCAL_BACKEND", "torch")
    monkeypatch.setitem(
        sys.modules, "sentence_transformers", types.SimpleNamespace(SentenceTransformer=lambda m, **k: object())
    )
    assert not isinstance(LocalEmbedder(MODEL)._model, emb._OnnxModel)


def test_forced_onnx_for_an_unoffered_model_says_so(monkeypatch):
    monkeypatch.setenv("LOCUS_LOCAL_BACKEND", "onnx")
    with pytest.raises(RuntimeError, match="no ONNX build"):
        LocalEmbedder("BAAI/bge-m3")


def test_a_cached_model_is_loaded_without_the_network_and_without_a_notice(capsys):
    LocalEmbedder(MODEL)
    assert _FakeTextEmbedding.calls == [{"model": MODEL, "cache_dir": emb._onnx_cache_dir(), "local_only": True}]
    assert capsys.readouterr().err == ""


def test_a_missing_model_is_announced_on_stderr_then_downloaded(capsys):
    _FakeTextEmbedding.cached = False
    LocalEmbedder(MODEL)
    assert "downloading the embedding model" in capsys.readouterr().err
    assert _FakeTextEmbedding.calls[-1]["local_only"] is False


def test_the_onnx_cache_survives_a_reboot_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("FASTEMBED_CACHE_PATH")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert emb._onnx_cache_dir() == str(tmp_path / "xdg" / "locus" / "onnx")


def test_xet_is_off_unless_the_user_chose(monkeypatch):
    LocalEmbedder(MODEL)
    import os

    assert os.environ["HF_HUB_DISABLE_XET"] == "1"
    monkeypatch.setenv("HF_HUB_DISABLE_XET", "0")
    LocalEmbedder(MODEL)
    assert os.environ["HF_HUB_DISABLE_XET"] == "0"


def test_without_either_stack_the_message_names_the_right_extra(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCUS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LOCUS_SHARED_EMBEDDER", "0")
    monkeypatch.setitem(sys.modules, "fastembed", None)
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)
    monkeypatch.setenv("LOCUS_EMBEDDER", "local")
    monkeypatch.setenv("LOCUS_LOCAL_MODEL", DEFAULT_LOCAL_MODEL)
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match=r"locusfiles\[local\]"):
        get_embedder(get_settings())
    monkeypatch.setenv("LOCUS_LOCAL_MODEL", "BAAI/bge-m3")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match=r"locusfiles\[local-torch\]"):
        get_embedder(get_settings())

    get_settings.cache_clear()


def test_a_typo_in_the_backend_setting_is_refused(monkeypatch):
    monkeypatch.setenv("LOCUS_LOCAL_BACKEND", "onxx")
    with pytest.raises(RuntimeError, match="not valid"):
        LocalEmbedder(MODEL)


def test_a_fastembed_that_is_installed_but_cannot_load_is_not_called_missing(monkeypatch):
    class Broken(types.ModuleType):
        def __getattr__(self, name):
            raise ImportError("DLL load failed while importing onnxruntime_pybind11_state")

    monkeypatch.setitem(sys.modules, "fastembed", Broken("fastembed"))
    with pytest.raises(RuntimeError, match="installed but cannot be loaded"):
        LocalEmbedder(MODEL)


def test_an_empty_batch_returns_no_vectors():
    assert LocalEmbedder(MODEL).embed_documents([]) == []


def test_the_model_name_is_matched_ignoring_case():
    assert isinstance(LocalEmbedder(MODEL.lower())._model, emb._OnnxModel)


def test_offline_mode_does_not_announce_a_download(monkeypatch, capsys):
    _FakeTextEmbedding.cached = False
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    LocalEmbedder(MODEL)
    assert capsys.readouterr().err == ""


def test_the_shared_helper_socket_depends_on_the_backend(monkeypatch, tmp_path):
    from locus.ingest.shared_embedder import socket_path_for

    monkeypatch.setenv("LOCUS_LOCAL_BACKEND", "onnx")
    onnx = socket_path_for(tmp_path, MODEL)
    monkeypatch.setenv("LOCUS_LOCAL_BACKEND", "torch")
    assert socket_path_for(tmp_path, MODEL) != onnx


def test_prefixes_follow_the_model_family():
    assert emb.local_prefixes(E5) == ("query: ", "passage: ")
    assert emb.local_prefixes("/models/multilingual-e5-base/") == ("query: ", "passage: ")
    assert emb.local_prefixes(MODEL) == (emb.LocalEmbedder._BGE_QUERY_PREFIX, "")
    assert emb.local_prefixes("BAAI/bge-m3") == ("", "")


def test_the_e5_prefixes_reach_the_model_on_both_sides():
    embedder = LocalEmbedder(E5)
    embedder.embed_documents(["alpha"])
    embedder.embed_query("beta")
    seen = embedder._model._model.embedded
    assert "passage: alpha" in seen and "query: beta" in seen


def test_vectors_come_back_in_the_order_the_texts_went_in_although_batches_are_sorted():
    embedder = LocalEmbedder(MODEL)
    texts = ["bbbbbb", "a", "cccc", "dd"]
    vectors = embedder.embed_documents(texts)
    for text, vec in zip(texts, vectors, strict=True):
        assert abs(vec[0] / vec[1] - len(text)) < 1e-6


def test_text_is_stripped_before_it_is_embedded():
    embedder = LocalEmbedder(MODEL)
    embedder.embed_documents(["a question?  ", " padded "])
    assert embedder._model._model.embedded == ["padded", "a question?"] or set(embedder._model._model.embedded) == {
        "a question?",
        "padded",
    }


def test_e5_small_is_registered_with_fastembed_once_and_loaded_on_onnx():
    LocalEmbedder(E5)
    LocalEmbedder(E5)  # a second load in the same process must not fail on "already registered"
    assert len(_FakeTextEmbedding.registered) == 1
    reg = _FakeTextEmbedding.registered[0]
    assert reg["model"] == E5 and reg["dim"] == 384 and reg["normalization"] is True and reg["pooling"] == "MEAN"
    assert reg["model_file"] == "onnx/model_qint8_avx512_vnni.onnx"


E5 = "intfloat/multilingual-e5-small"


def test_the_download_notice_gives_the_size_and_says_to_run_again_if_interrupted(capsys):
    emb._announce_download(E5)
    err = capsys.readouterr().err
    assert "about 134 MB" in err and "run the same command again" in err


def test_the_torch_route_states_no_size_because_its_download_is_larger(tmp_path, capsys):
    emb._announce_download(E5, "torch")
    err = capsys.readouterr().err
    assert "134" not in err and "MB" not in err and "run the same command again" in err
    blobs = tmp_path / ("models--" + E5.replace("/", "--")) / "blobs"
    blobs.mkdir(parents=True)
    (blobs / "abc.incomplete").write_bytes(b"x" * 5_000_000)
    with emb._download_progress(E5, str(tmp_path), interval=0.01, route="torch"):
        time.sleep(0.2)
    err = capsys.readouterr().err
    assert "model download 5 MB ..." in err and "134" not in err


def test_download_progress_reports_megabytes_from_the_partial_files_on_stderr_only(tmp_path, capsys):
    blobs = tmp_path / ("models--" + E5.replace("/", "--")) / "blobs"
    blobs.mkdir(parents=True)
    (blobs / "abc.incomplete").write_bytes(b"x" * 5_000_000)
    with emb._download_progress(E5, str(tmp_path), interval=0.01):
        time.sleep(0.2)
    captured = capsys.readouterr()
    assert "model download 5 MB of about 134 MB" in captured.err
    assert captured.out == ""


def test_download_progress_mentions_bytes_an_earlier_attempt_left(tmp_path, capsys):
    blobs = tmp_path / ("models--" + E5.replace("/", "--")) / "blobs"
    blobs.mkdir(parents=True)
    (blobs / "abc.incomplete").write_bytes(b"x" * 3_000_000)
    with emb._download_progress(E5, str(tmp_path), interval=60):
        pass
    captured = capsys.readouterr()
    assert "left 3 MB on disk" in captured.err
    assert captured.out == ""


def test_download_progress_is_silent_offline_and_when_nothing_is_cached(monkeypatch, tmp_path, capsys):
    with emb._download_progress(E5, str(tmp_path), interval=60):
        pass
    assert capsys.readouterr().err == ""
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    with emb._download_progress(E5, str(tmp_path), interval=0.01):
        time.sleep(0.05)
    assert capsys.readouterr().err == ""


def test_a_timeout_during_the_download_gets_a_friendly_retry_message_not_a_raw_error(monkeypatch):
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("TRANSFORMERS_OFFLINE", raising=False)

    class ReadTimeout(Exception):  # stands in for httpx.ReadTimeout / urllib3's ReadTimeoutError
        pass

    try:
        try:
            raise ReadTimeout("HTTPSConnectionPool(host='huggingface.co', port=443): Read timed out. (read timeout=10)")
        except ReadTimeout as inner:
            raise RuntimeError("An error happened while trying to locate the file on the Hub") from inner
    except RuntimeError as exc:
        text = emb.local_model_error(E5, exc)
    assert "timed out" in text and "Run the same command again" in text and "continues where it stopped" in text
    assert "ReadTimeout" not in text and "HTTPSConnectionPool" not in text and "RuntimeError" not in text


def test_a_non_timeout_failure_keeps_the_existing_message():
    text = emb.local_model_error(E5, OSError("no route to host"))
    assert "OSError" in text and "Check that this computer is online" in text


def test_download_progress_does_not_repeat_an_identical_line(tmp_path, capsys):
    blobs = tmp_path / ("models--" + E5.replace("/", "--")) / "blobs"
    blobs.mkdir(parents=True)
    (blobs / "abc.incomplete").write_bytes(b"x" * 5_000_000)
    with emb._download_progress(E5, str(tmp_path), interval=0.01):
        time.sleep(0.3)  # many ticks, no new bytes
    lines = [ln for ln in capsys.readouterr().err.splitlines() if ln.startswith("Locus: model download")]
    assert len(lines) == 1
