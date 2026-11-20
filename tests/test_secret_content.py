"""Content-based secret detection (reason `secret-content`): a credential of a well-known shape is never indexed and
never returned by locus_read_file, whatever the file is called. Fixtures are assembled from fragments so this file
itself holds no credential. Offline, HashEmbedder."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("chromadb")

from locus import mcp_server as srv  # noqa: E402
from locus.config import DEFAULT_EXTENSIONS, Settings  # noqa: E402
from locus.ingest.embedder import HashEmbedder  # noqa: E402
from locus.ingest.indexer import IndexStats, index_file, index_folder  # noqa: E402
from locus.ingest.store import ChromaStore  # noqa: E402
from locus.policy import secret_content  # noqa: E402

_PEM = "-----BEGIN " + "RSA PRIVATE KEY-----\nMIIEow\n-----END " + "RSA PRIVATE KEY-----"
_AWS = "AKI" + "A" + "ABCDEFGHIJKLMNOP"
_GH = "gh" + "p_" + "a" * 36
_SK = "sk" + "-" + "A1b2" * 10
_JWT = "ey" + "J" + "a" * 12 + ".ey" + "J" + "b" * 12 + "." + "c" * 12
_COOKIES = "".join(f".example.com\tTRUE\t/\tFALSE\t1893456000\tname{i}\tvalue{i}\n" for i in range(3))

POSITIVE = {
    "pem": _PEM,
    "aws": f"key id: {_AWS}",
    "github": f"token {_GH}",
    "sk": f"OPENAI={_SK}",
    "jwt": f"Authorization: Bearer {_JWT}",
    "netscape-header": "# Netscape HTTP Cookie File\n",
    "netscape-lines": _COOKIES,
}


@pytest.mark.parametrize("name", sorted(POSITIVE))
def test_known_shapes_are_detected(name):
    assert secret_content(POSITIVE[name])


@pytest.mark.parametrize(
    "text",
    [
        "Meeting notes: we decided on the pricing tiers. The sky is blue and sk-learn is a library.",
        "def f(x):\n    return x + 1  # AKIA is not followed by sixteen capitals here",
        "ghp_short and eyJ.eyJ.x are not tokens",
        "Two cookie lines\n.a.com\tTRUE\t/\tFALSE\t1\tn\tv\n.a.com\tTRUE\t/\tFALSE\t1\tm\tw\n",
        "I wrote about a private key in prose, and -----BEGIN-----alone is nothing.",
        "",
    ],
)
def test_ordinary_text_is_not_flagged(text):
    assert not secret_content(text)


def _setup(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data", collection="secretcontent", embedder="hash", voyage_model="", openai_model="",
        local_model="", chunk_tokens=100, overlap_tokens=10, max_file_mb=5, extensions=DEFAULT_EXTENSIONS,
    )  # fmt: skip
    embedder = HashEmbedder()
    store = ChromaStore(settings.data_dir, settings.collection, embedder.name)
    root = tmp_path / "corpus"
    root.mkdir()
    return settings, embedder, store, root


def test_indexer_skips_a_harmless_name_with_a_credential_inside(tmp_path):
    settings, embedder, store, root = _setup(tmp_path)
    (root / "notes.md").write_text("ordinary note about the zebra release " * 5)
    (root / "innocent.md").write_text("my notes\n" + _PEM + "\n" + "more text " * 20)
    lines: list[str] = []
    stats = IndexStats()
    index_file(root / "innocent.md", root, embedder, store, settings, stats, log=lines.append)
    assert stats.denied == 1 and stats.files_indexed == 0
    assert any("secret-content" in line for line in lines)
    assert not any(_PEM[:20] in line for line in lines)
    index_folder(root, embedder, store, settings, log=lambda _m: None)
    assert [p for p in store.list_paths() if p.endswith("innocent.md")] == []
    assert any(p.endswith("notes.md") for p in store.list_paths())


def test_read_file_refuses_a_file_that_became_a_credential(tmp_path, monkeypatch):
    settings, embedder, store, root = _setup(tmp_path)
    f = root / "later.md"
    f.write_text("harmless text about pricing " * 10)
    index_folder(root, embedder, store, settings, log=lambda _m: None)
    f.write_text("harmless text about pricing " * 10 + f"\n{_AWS}\n")  # changed after indexing
    monkeypatch.setattr(srv, "_components", lambda: (embedder, store, settings))
    out = asyncio.run(srv.locus_read_file(path=str(f.resolve())))
    assert "secret-content" in out and _AWS not in out
