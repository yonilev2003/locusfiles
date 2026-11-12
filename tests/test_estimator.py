"""PROD-012: pre-index estimate. Offline; a socket guard proves it opens no connection."""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path

import pytest

from locus.config import DEFAULT_EXTENSIONS, Settings
from locus.ingest import estimator
from locus.ingest.estimator import (
    RATIOS_VERSION,
    FileEntry,
    estimate,
    overlap_factor,
    render_estimate,
    scan,
    text_tokens,
)
from locus.ingest.indexer import iter_files


def _settings(tmp_path: Path, **overrides) -> Settings:
    base = dict(
        data_dir=tmp_path / "data",
        collection="test",
        embedder="hash",
        voyage_model="",
        openai_model="",
        local_model="",
        chunk_tokens=100,
        overlap_tokens=10,
        max_file_mb=0.01,  # ~10 KB, so one fixture file is "too large"
        extensions=DEFAULT_EXTENSIONS,
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture()
def corpus(tmp_path: Path) -> Path:
    root = tmp_path / "notes"
    (root / "sub").mkdir(parents=True)
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / ".obsidian").mkdir()
    (root / "a.md").write_text("Pricing decision: we keep three tiers.\n\n" * 20)
    (root / "b.txt").write_text("Meeting notes about the launch.\n" * 10)
    (root / "sub" / "script.py").write_text("def add(a, b):\n    return a + b\n" * 15)
    (root / "sub" / "data.json").write_text(json.dumps({"k": list(range(100))}))
    (root / "big.md").write_text("x " * 20_000)  # over the 10 KB cap
    (root / "photo.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 100)  # unsupported type
    (root / ".env").write_text("SECRET=1")  # hidden file
    (root / "node_modules" / "pkg" / "index.js").write_text("module.exports = 1")
    (root / ".obsidian" / "workspace.json").write_text("{}")
    os.symlink(root / "a.md", root / "link.md")
    return root


@pytest.fixture()
def no_network(monkeypatch):
    """Any attempt to open a connection or resolve a name fails the test."""
    attempts: list[str] = []

    def _blocked(*args, **kwargs):
        attempts.append(repr(args[:2]))
        raise AssertionError(f"network access attempted: {args[:2]!r}")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked)
    return attempts


def test_scan_counts_exactly_what_the_indexer_would_walk(tmp_path, corpus):
    settings = _settings(tmp_path)
    result = scan(corpus, settings)
    assert sorted(f.path.name for f in result.files) == ["a.md", "b.txt", "data.json", "script.py"]
    assert [f.path for f in result.files] == list(iter_files(corpus.resolve(), settings))
    assert result.excluded == {
        "too_large": 1,
        "unsupported_type": 1,
        "hidden_file": 1,
        "symlink": 1,
        "skipped_dir": 1,
        "hidden_dir": 1,
    }
    assert set(result.skipped_dir_names) == {"node_modules", ".obsidian"}
    assert result.total_bytes == sum(f.path.stat().st_size for f in result.files)


def test_estimate_is_stable_and_makes_no_network_calls(tmp_path, corpus, no_network):
    settings = _settings(tmp_path)
    first = estimate(scan(corpus, settings).files, settings)
    second = estimate(scan(corpus, settings).files, settings)
    assert first.to_dict() == second.to_dict()
    assert first.sampled_files == first.files == 4  # a small folder is parsed in full
    assert first.tokens > 0 and first.tokens_low < first.tokens < first.tokens_high
    assert first.ratios_version == RATIOS_VERSION
    assert no_network == []


def test_cli_estimate_prints_the_report_and_json_without_network(tmp_path, corpus, no_network, monkeypatch, capsys):
    from locus import cli

    monkeypatch.setenv("LOCUS_EMBEDDER", "managed")  # must not build (or call) the managed embedder
    monkeypatch.delenv("LOCUS_API_KEY", raising=False)
    monkeypatch.setenv("LOCUS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LOCUS_MAX_FILE_MB", "0.01")
    cli.get_settings.cache_clear()
    try:
        with pytest.raises(SystemExit) as exc:
            cli.main(["estimate", str(corpus)])
        assert exc.value.code == 0
        text = capsys.readouterr().out
        with pytest.raises(SystemExit):
            cli.main(["estimate", str(corpus), "--json"])
        payload = json.loads(capsys.readouterr().out)
    finally:
        cli.get_settings.cache_clear()
    assert "Files that would be indexed: 4" in text
    assert "ASSUMPTION" in text and "no embedding fees" in text
    assert "Managed plans: a comparison with each plan's budget is not available in this version." in text
    assert "ADR" not in text  # review F6: user-facing copy never names an internal decision record
    assert "Secret scan: not built yet" in text
    assert "$" not in text  # no prices anywhere until A1
    assert payload["files"] == 4 and payload["assumption"] is True
    assert payload["plan_fit"] is None
    assert payload["excluded"]["too_large"] == 1
    assert not (tmp_path / "data").exists()  # estimating writes nothing either
    assert no_network == []


def test_token_math_follows_the_versioned_ratios(tmp_path):
    assert text_tokens("a" * 400, "prose") == pytest.approx(100)
    assert text_tokens("a" * 320, "code") == pytest.approx(100)
    hebrew = "שלום עולם " * 10  # non-Latin script counts far denser than English
    assert text_tokens(hebrew, "prose") > text_tokens("hello you " * 10, "prose") * 2
    assert overlap_factor(_settings(tmp_path, chunk_tokens=700, overlap_tokens=100)) == pytest.approx(1 + 100 / 700)
    assert overlap_factor(_settings(tmp_path, chunk_tokens=90, overlap_tokens=90)) == pytest.approx(1 + 30 / 90)


def test_unsampled_files_use_the_measured_per_type_rate(tmp_path, monkeypatch):
    root = tmp_path / "many"
    root.mkdir()
    for i in range(30):
        (root / f"n{i:02}.md").write_text("Plain English note about project work. " * 30)
    settings = _settings(tmp_path, max_file_mb=5)
    entries = scan(root, settings).files
    full = estimate(entries, settings)
    monkeypatch.setattr(estimator, "SAMPLE_ALL_BELOW", 10)
    monkeypatch.setattr(estimator, "MIN_SAMPLE_PER_KIND", 5)
    sampled = estimate(entries, settings)
    assert sampled.sampled_files == 5
    assert sampled.tokens == pytest.approx(full.tokens, rel=0.05)  # identical files: sample = whole


def test_time_limit_falls_back_to_the_fixed_ratios(tmp_path):
    root = tmp_path / "t"
    root.mkdir()
    (root / "a.md").write_text("word " * 400)
    settings = _settings(tmp_path, max_file_mb=5)
    entries = scan(root, settings).files
    est = estimate(entries, settings, sample_seconds=0)
    assert est.sampled_files == 0 and est.sample_cut_short
    ratio = estimator.RATIOS["prose"]
    expected = entries[0].size * ratio.chars_per_byte * overlap_factor(settings) / ratio.chars_per_token
    assert est.tokens == pytest.approx(expected)


def test_unparseable_files_are_measured_as_empty_not_fatal(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    (root / "broken.pdf").write_bytes(b"%PDF-1.4 not really a pdf")
    settings = _settings(tmp_path, max_file_mb=5)
    est = estimate(scan(root, settings).files, settings)
    assert est.kinds["pdf"].unparsed == 1 and est.tokens == 0


def test_docx_is_parsed_and_counted(tmp_path):
    docx = pytest.importorskip("docx")
    root = tmp_path / "d"
    root.mkdir()
    document = docx.Document()
    for _ in range(20):
        document.add_paragraph("Quarterly review: revenue grew and churn fell.")
    document.save(str(root / "review.docx"))
    settings = _settings(tmp_path, max_file_mb=5)
    est = estimate(scan(root, settings).files, settings)
    assert est.kinds["docx"].measured and est.tokens > 100


def test_plan_fit_stays_pending_until_a1(tmp_path, corpus):
    settings = _settings(tmp_path)
    est = estimate(scan(corpus, settings).files, settings)
    assert estimator.PLAN_TOKEN_BUDGETS_PENDING_A1 == {}  # no plan numbers before the pricing ADR
    assert est.plan_fit() is None
    # The mechanism itself works once a table exists (numbers here are test data, not plans).
    fit = est.plan_fit({"test-plan": int(est.tokens * 4)})
    assert fit == {"test-plan": pytest.approx(0.25, rel=1e-3)}


def test_render_lists_types_exclusions_and_local_cpu_time(tmp_path, corpus):
    settings = _settings(tmp_path)
    result = scan(corpus, settings)
    text = render_estimate(result, estimate(result.files, settings), settings)
    assert ".md" in text and ".py" in text
    assert "1 file over the size limit" in text and "1 file of an unsupported type" in text
    assert "node_modules" in text
    assert "Estimated CPU time" in text


def test_iter_files_reports_every_exclusion_reason(tmp_path, corpus):
    seen: list[str] = []
    list(iter_files(corpus, _settings(tmp_path), on_skip=lambda reason, _p: seen.append(reason)))
    from locus.ingest.indexer import SKIP_REASONS

    assert set(seen) <= set(SKIP_REASONS)
    assert set(seen) == {"too_large", "unsupported_type", "hidden_file", "symlink", "skipped_dir", "hidden_dir"}


def test_estimate_accepts_plain_entries(tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("hello world " * 50)
    est = estimate([FileEntry(f, f.stat().st_size)], _settings(tmp_path, max_file_mb=5))
    assert est.files == 1 and est.tokens > 0
