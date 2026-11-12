"""Test-wide settings.

The legacy local-embedder tests fake `sentence_transformers` in sys.modules; they pin the PyTorch route so a
machine that also has fastembed installed does not send them down the ONNX route and a real download."""

import pytest


@pytest.fixture(autouse=True)
def _pin_local_backend_to_torch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCUS_LOCAL_BACKEND", "torch")
