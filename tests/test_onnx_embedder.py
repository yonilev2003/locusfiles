"""The ONNX route of LocalEmbedder (fastembed), offline: `fastembed` is a fake module, no model, no network."""

import sys
import types

import pytest

from locus.config import DEFAULT_LOCAL_MODEL, get_settings
from locus.ingest import embedder as emb
from locus.ingest.embedder import LocalEmbedder, get_embedder

MODEL = DEFAULT_LOCAL_MODEL


class _FakeTextEmbedding:
    calls: list[dict] = []
    offered = {MODEL}
    cached = True  # False: local_files_only=True raises, as for a model that is not downloaded yet

    def __init__(self, model_name, cache_dir=None, local_files_only=False, **kwargs):
        if local_files_only and not type(self).cached:
            raise ValueError("model not on disk")
        type(self).calls.append({"model": model_name, "cache_dir": cache_dir, "local_only": local_files_only})
        self.embedded: list[str] = []

    @classmethod
    def list_supported_models(cls):
        return [{"model": name} for name in cls.offered]

    def embed(self, texts, batch_size=32):
        self.embedded.extend(texts)
        for i, _ in enumerate(texts):
            yield [3.0, 4.0, 0.0 + i * 0.0]  # norm 5


@pytest.fixture(autouse=True)
def fake_fastembed(monkeypatch, tmp_path):
    _FakeTextEmbedding.calls = []
    _FakeTextEmbedding.offered = {MODEL}
    _FakeTextEmbedding.cached = True
    module = types.ModuleType("fastembed")
    module.TextEmbedding = _FakeTextEmbedding  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fastembed", module)
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
    monkeypatch.setenv("LOCUS_LOCAL_MODEL", MODEL)
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
