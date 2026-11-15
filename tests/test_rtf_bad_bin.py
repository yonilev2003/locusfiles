"""A malformed RTF with a negative \\bin count (`{\\rtf1 hello \\bin-6}`) made the reader rewind onto its own token and
loop for ever (killed after 60 s in the security self-assessment of 2026-11-15). The reader now refuses a negative
count and, as a backstop, any token that does not move the position forward. A refused file is one counted error in
the indexer and the run goes on. Each case runs in a child process under a time limit, so a regression fails."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
LIMIT = int(os.environ.get("LOCUS_TEST_RTF_LIMIT", "30"))


def _child(code: str, *args: str) -> dict:
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    try:
        done = subprocess.run(
            [sys.executable, "-c", textwrap.dedent(code), *args], env=env, capture_output=True, text=True, timeout=LIMIT
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"hung: still running after {LIMIT} s (the RTF reader loops)")
    assert done.returncode == 0, done.stderr[-1500:]
    return json.loads(done.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize(
    "document",
    [rb"{\rtf1 hello \bin-6}", rb"{\rtf1 hello \bin-1 x}", rb"{\rtf1 \bin-100000}", rb"{\rtf1 {\pict \bin-2 abc}}"],
)
def test_a_negative_bin_count_is_refused_quickly(document):
    out = _child(
        """
        import json, sys
        from locus.ingest.docformats import UnreadableFile, _rtf_text
        try:
            _rtf_text(bytes.fromhex(sys.argv[1]))
            result = "parsed"
        except UnreadableFile as exc:
            result = str(exc)
        print(json.dumps({"result": result}))
        """,
        document.hex(),
    )
    assert "negative" in out["result"] and "corrupt file" in out["result"]


def test_a_valid_bin_block_is_still_skipped_and_the_text_around_it_kept():
    from locus.ingest.docformats import _rtf_text

    data = rb"{\rtf1 before {\pict \bin5 \}{\}} after}"
    assert _rtf_text(data) == "before  after"
    assert _rtf_text(rb"{\rtf1 a \bin0 b}") == "a b"
    assert _rtf_text(rb"{\rtf1 a \bin99999 b}") == "a"  # a count past the end ends the document, as before


def test_the_indexer_counts_one_error_for_the_file_and_goes_on(tmp_path):
    root = tmp_path / "notes"
    root.mkdir()
    (root / "bad.rtf").write_bytes(rb"{\rtf1 hello \bin-6}")
    (root / "ok.md").write_text("a note about the zebra release")
    out = _child(
        """
        import json, sys
        from pathlib import Path
        from locus.config import DEFAULT_EXTENSIONS, Settings
        from locus.ingest.embedder import HashEmbedder
        from locus.ingest.indexer import index_folder
        from locus.ingest.store import ChromaStore
        work = Path(sys.argv[1])
        settings = Settings(data_dir=work / "data", collection="rtf", embedder="hash", voyage_model="", openai_model="",
                            local_model="", chunk_tokens=100, overlap_tokens=10, max_file_mb=5,
                            extensions=DEFAULT_EXTENSIONS)
        store = ChromaStore(settings.data_dir, settings.collection, HashEmbedder().name)
        lines = []
        stats = index_folder(work / "notes", HashEmbedder(), store, settings, log=lines.append)
        print(json.dumps({"indexed": stats.files_indexed, "errors": stats.errors, "log": lines}))
        """,
        str(tmp_path),
    )
    assert out["indexed"] == 1 and len(out["errors"]) == 1
    assert "bad.rtf" in out["errors"][0] and "negative \\bin length (corrupt file)" in out["errors"][0]
