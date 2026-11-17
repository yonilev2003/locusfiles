"""Test-wide settings.

The legacy local-embedder tests fake `sentence_transformers` in sys.modules; they pin the PyTorch route so a
machine that also has fastembed installed does not send them down the ONNX route and a real download."""

import sys
import types

import pytest


@pytest.fixture(autouse=True)
def _pin_local_backend_to_torch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCUS_LOCAL_BACKEND", "torch")


@pytest.fixture(autouse=True)
def _allow_the_test_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    """LOCUS_EMBEDDER=hash is refused at runtime unless this is set (config.require_real_embedder); the tests use it."""
    monkeypatch.setenv("LOCUS_ALLOW_TEST_EMBEDDER", "1")


@pytest.fixture(autouse=True)
def _account_tool_is_off_again_after_each_test():
    """`mcp_server.main()` registers `locus_account_status` when a key is set; a test that did so must not change the
    tool list another test sees (the default server has five tools)."""
    yield
    server = sys.modules.get("locus.mcp_server")
    if server is not None and getattr(server, "_account_tool_registered", False):
        server.apply_tool_set(types.SimpleNamespace(managed_api_key=None))


@pytest.fixture(scope="session")
def demo_index(tmp_path_factory):
    """The demo vault indexed once with HashEmbedder (tests/demo_index.py); read-only for the tests that share it."""
    from .demo_index import build_demo_index

    return build_demo_index(tmp_path_factory.mktemp("demo-index"))
