"""The end of `locus index` names the files that were left out and why; `design-tokens.md` is no longer refused."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("chromadb")

from locus.config import DEFAULT_EXTENSIONS, Settings  # noqa: E402
from locus.ingest.embedder import HashEmbedder  # noqa: E402
from locus.ingest.indexer import index_folder  # noqa: E402
from locus.ingest.store import ChromaStore  # noqa: E402
from locus.policy import Matcher, Policy, secret_name  # noqa: E402

# an HTML page that is one image inlined as base64 on a single very long line (Locus skips such lines as machine-made)
IMAGE_ONLY = '<html><body><img src="data:image/png;base64,' + "iVBORw0KGgoAAAANSUhEUg" * 400 + '"></body></html>'


def _run(tmp_path: Path, files: dict[str, str]):
    settings = Settings(
        data_dir=tmp_path / "data", collection="skips", embedder="hash", voyage_model="", openai_model="",
        local_model="", chunk_tokens=400, overlap_tokens=50, max_file_mb=5, extensions=DEFAULT_EXTENSIONS,
    )  # fmt: skip
    embedder = HashEmbedder()
    store = ChromaStore(settings.data_dir, settings.collection, embedder.name)
    root = tmp_path / "notes"
    root.mkdir()
    for name, text in files.items():
        (root / name).write_text(text, encoding="utf-8")
    log: list[str] = []
    matcher = Matcher(Policy(roots=(str(root),)), tmp_path)
    stats = index_folder(root, embedder, store, settings, log=log.append, matcher=matcher)
    return stats, log, store, root


def test_the_end_of_a_run_lists_skipped_files_by_reason_with_names(tmp_path):
    jwt = ".".join(
        [
            "eyJ" + "hbGciOiJIUzI1NiJ9",
            "eyJ" + "zdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ",
            "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV",
        ]
    )
    stats, log, store, root = _run(
        tmp_path,
        {
            "good.md": "A real note about the launch plan and the budget.",
            "image-only.html": IMAGE_ONLY,
            "session.md": f"debug: {jwt}\n",
            "passwords.txt": "hunter2",
        },
    )
    text = "\n".join(log)
    assert str(root / "image-only.html") in text and "HTML with no text of its own" in text
    assert "refused: the text looks like a credential: 1 file(s)" in text and str(root / "session.md") in text
    assert "refused: the file name looks like a credential: 1 file(s)" in text and str(root / "passwords.txt") in text
    assert [Path(p).name for p in store.list_paths()] == ["good.md"]
    assert stats.skipped_files["html_no_text"] == [str(root / "image-only.html")]


def test_only_the_first_names_are_shown_with_a_count_of_the_rest(tmp_path):
    files = {f"img{n}.html": IMAGE_ONLY for n in range(8)}
    _, log, _, _ = _run(tmp_path, files)
    line = next(ln for ln in log if "HTML with no text" in ln)
    assert "8 file(s)" in line and "and 3 more" in line


def test_a_markdown_note_named_like_design_tokens_is_indexed_but_real_credential_names_are_still_refused():
    assert not secret_name("design-tokens.md") and not secret_name("Token economics.md")
    assert not secret_name("designTokens.md") and secret_name("tokens.md") and secret_name("api-token.md")
    assert secret_name("github-token.md") and secret_name("design-tokens.txt")  # other file types stay refused
    for provider in (
        "discord-token.md",
        "stripe-token.md",
        "openai-token.md",
        "vercel token.md",
        "token-backup.md",
        "token-prod.md",
        "tokens-2024.md",
        "service-token.rst",
        "my_token.md",
    ):
        assert secret_name(provider), provider  # a provider or any unlisted word keeps the note out
    for refused in (
        "api-token.txt",
        "accessToken.json",
        "passwords.md",
        "secrets.md",
        "credentials.md",
        "cookies.txt",
        ".env",
        "id_rsa",
    ):
        assert secret_name(refused), refused


def test_a_design_tokens_note_gets_indexed(tmp_path):
    _, _, store, root = _run(tmp_path, {"design-tokens.md": "The color tokens of the brand: primary, accent, surface."})
    assert store.list_paths() == [str(root / "design-tokens.md")]
