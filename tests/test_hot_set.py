"""Hot-set indexing: the modified-age table in the roots dry run, and `--order recent` / `--since` for staged runs.

Synthetic trees in tmp_path only; modified times are set with os.utime relative to now.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from locus import cli
from locus.config import DEFAULT_EXTENSIONS, Settings, get_settings
from locus.ingest.embedder import HashEmbedder
from locus.ingest.errors import AuthFailed
from locus.ingest.multiroot import Stage, index_roots, render_report
from locus.policy import Matcher, Policy
from locus.rootscan import AGE_BUCKETS, age_bucket, kind_group, render_roots_report, scan_roots

chromadb = pytest.importorskip("chromadb")

from locus.ingest.store import ChromaStore  # noqa: E402

DAY = 86400.0


@pytest.fixture(autouse=True)
def _fresh_settings():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return (tmp_path / "home").resolve()


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        collection="test",
        embedder="hash",
        voyage_model="",
        openai_model="",
        local_model="",
        chunk_tokens=100,
        overlap_tokens=10,
        max_file_mb=5,
        extensions=DEFAULT_EXTENSIONS,
    )


def _file(path: Path, text: str, age_days: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    when = time.time() - age_days * DAY
    os.utime(path, (when, when))
    return path


def _matcher(home: Path) -> Matcher:
    return Matcher(Policy(roots=(str(home),)), home)


# --- age table -------------------------------------------------------------------------------------------


def test_age_bucket_edges():
    now = 1_000_000_000.0
    assert [age_bucket(now - d * DAY, now) for d in (0, 7, 7.01, 30, 90, 365, 366)] == [0, 0, 1, 1, 2, 3, 4]
    assert age_bucket(now + 5 * DAY, now) == 0  # a future mtime counts as the newest


def test_kind_groups():
    assert [kind_group(e) for e in (".md", ".pdf", ".docx", ".py", ".csv", ".json", ".weird")] == [
        "docs",
        "docs",
        "docs",
        "code",
        "data",
        "data",
        "other",
    ]


def test_dry_run_age_table_counts_and_cumulative_shares(home, tmp_path):
    _file(home / "a" / "new.md", "x" * 100, 1)
    _file(home / "a" / "month.md", "x" * 300, 20)
    _file(home / "a" / "old.txt", "x" * 600, 900)
    _file(home / "b" / "tool.py", "print(1)\n", 40)
    _file(home / "b" / "table.csv", "a,b\n1,2\n", 400)
    report = scan_roots([home], _settings(tmp_path), _matcher(home))
    docs = report.age["docs"]
    assert [row[0] for row in docs] == [1, 1, 0, 0, 1]  # files per bucket
    assert [row[1] for row in docs] == [100, 300, 0, 0, 600]  # bytes per bucket
    assert [row[0] for row in report.age["code"]] == [0, 0, 1, 0, 0]
    assert [row[0] for row in report.age["data"]] == [0, 0, 0, 0, 1]
    assert sum(row[0] for rows in report.age.values() for row in rows) == report.total_files == 5

    text = render_roots_report(report)
    assert "By modified age" in text
    docs_block = text.split("  docs\n", 1)[1].split("  code\n", 1)[0].splitlines()
    assert len(docs_block) == len(AGE_BUCKETS)
    # cumulative share of docs files: 1/3, 2/3, 2/3, 2/3, 3/3; of bytes: 10%, 40%, 40%, 40%, 100%
    assert "33.3%  10.0%" in docs_block[0]
    assert "66.7%  40.0%" in docs_block[1]
    assert "100.0% 100.0%" in docs_block[4]
    assert "  all\n" in text


def test_age_table_names_no_path(home, tmp_path):
    _file(home / "secretproject" / "private-notes.md", "hello", 3)
    text = render_roots_report(scan_roots([home], _settings(tmp_path), _matcher(home)))
    assert "secretproject" not in text and "private-notes" not in text


# --- ordering and --since ---------------------------------------------------------------------------------


def _run(home, tmp_path, stage, embedder=None, roots=None):
    settings = _settings(tmp_path)
    embedder = embedder or HashEmbedder()
    store = ChromaStore(settings.data_dir, settings.collection, embedder.name)
    report = index_roots(roots or [home], embedder, store, settings, _matcher(home), stage, log=lambda _: None)
    return report, store


def _names(store) -> set[str]:
    return {Path(p).name for p in store.list_paths()}


def _tree(home: Path) -> None:
    # distinct content everywhere; folder order (a, b, c) is the reverse of recency
    _file(home / "a" / "oldest.md", "ancient notes about lemons", 300)
    _file(home / "b" / "middle.md", "middling notes about tahini", 40)
    _file(home / "c" / "newest.md", "fresh notes about zebras", 1)
    _file(home / "c" / "newer.md", "recent notes about pickles", 3)


def test_order_recent_with_max_files_keeps_the_newest(home, tmp_path):
    _tree(home)
    report, store = _run(home, tmp_path, Stage(order="recent", max_files=2))
    assert _names(store) == {"newest.md", "newer.md"}
    assert report.deferred_budget == 2 and report.remaining == 2


def test_walk_order_is_unchanged_by_default(home, tmp_path):
    _tree(home)
    _, store = _run(home, tmp_path, Stage(max_files=2))
    assert _names(store) == {"oldest.md", "middle.md"}


def test_recent_runs_resume_without_reembedding(home, tmp_path):
    _tree(home)
    _run(home, tmp_path, Stage(order="recent", max_files=2))
    report, store = _run(home, tmp_path, Stage(order="recent", max_files=2))
    assert report.stats.files_indexed == 2  # the two older ones; the first two were skipped as unchanged
    assert _names(store) == {"oldest.md", "middle.md", "newest.md", "newer.md"}
    report3, _ = _run(home, tmp_path, Stage(order="recent"))
    assert report3.stats.files_indexed == 0 and report3.remaining == 0


def test_order_recent_sorts_across_roots(home, tmp_path):
    _file(home / "r1" / "old.md", "old text one", 200)
    _file(home / "r2" / "new.md", "new text two", 2)
    _, store = _run(home, tmp_path, Stage(order="recent", max_files=1), roots=[home / "r1", home / "r2"])
    assert _names(store) == {"new.md"}


def test_since_leaves_older_files_and_counts_them(home, tmp_path):
    _tree(home)
    report, store = _run(home, tmp_path, Stage(since_days=10))
    assert _names(store) == {"newest.md", "newer.md"}
    assert report.deferred_since == 2 and report.remaining == 2
    assert "older than --since 2" in render_report(report)
    # a later run without --since picks up the rest and nothing else
    report2, store2 = _run(home, tmp_path, Stage())
    assert report2.stats.files_indexed == 2 and report2.remaining == 0
    assert len(_names(store2)) == 4


def test_since_does_not_count_already_indexed_old_files_as_remaining(home, tmp_path):
    _tree(home)
    _run(home, tmp_path, Stage())
    report, _ = _run(home, tmp_path, Stage(since_days=10))
    assert report.deferred_since == 0 and report.stats.files_indexed == 0


def test_since_and_max_files_together(home, tmp_path):
    _tree(home)
    report, store = _run(home, tmp_path, Stage(order="recent", since_days=100, max_files=1))
    assert _names(store) == {"newest.md"}
    assert report.deferred_budget == 2  # newer.md and middle.md hit the limit
    assert report.deferred_since == 1  # oldest.md is outside the window
    assert report.remaining == 3


def test_duplicates_with_recent_order_embed_the_newest_copy(home, tmp_path):
    _file(home / "a" / "copy-old.md", "same text in two places", 200)
    _file(home / "b" / "copy-new.md", "same text in two places", 2)
    report, store = _run(home, tmp_path, Stage(order="recent"))
    assert report.stats.files_indexed == 1 and report.duplicates == 1
    assert _names(store) == {"copy-new.md"}
    assert list(report.aliases.values()) == [[str(home / "a" / "copy-old.md")]]


def test_duplicate_outside_a_budget_is_not_lost(home, tmp_path):
    _file(home / "a" / "dup-old.md", "same text in two places", 200)
    _file(home / "b" / "dup-new.md", "same text in two places", 2)
    _file(home / "c" / "other.md", "something else entirely", 1)
    report, store = _run(home, tmp_path, Stage(order="recent", max_files=1))
    assert _names(store) == {"other.md"}
    report2, store2 = _run(home, tmp_path, Stage(order="recent"))
    assert _names(store2) == {"other.md", "dup-new.md"} and report2.duplicates == 1


class _FailsOnce(HashEmbedder):
    """Raises a run-stopping error on its Nth call, then works."""

    def __init__(self, fail_on: int):
        super().__init__()
        self.calls = 0
        self.fail_on = fail_on

    def embed_documents(self, texts):
        self.calls += 1
        if self.calls == self.fail_on:
            raise AuthFailed("key rejected")
        return super().embed_documents(texts)


def test_interrupted_recent_run_resumes_where_it_stopped(home, tmp_path):
    _tree(home)
    report, store = _run(home, tmp_path, Stage(order="recent"), embedder=_FailsOnce(fail_on=3))
    assert report.stats.stop is not None
    assert _names(store) == {"newest.md", "newer.md"}  # the two newest made it before the stop
    report2, store2 = _run(home, tmp_path, Stage(order="recent"))
    assert report2.stats.stop is None
    assert report2.stats.files_indexed == 2  # only the two that were not done
    assert len(_names(store2)) == 4


def test_unknown_order_is_rejected():
    with pytest.raises(ValueError):
        Stage(order="oldest")


def test_cli_order_and_since(home, tmp_path, monkeypatch, capsys):
    _tree(home)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LOCUS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LOCUS_EMBEDDER", "hash")
    get_settings.cache_clear()
    policy = tmp_path / "policy.toml"
    policy.write_text("", encoding="utf-8")
    with pytest.raises(SystemExit) as stop:
        cli.main(["index", "--roots", str(home), "--policy", str(policy), "--order", "recent", "--since", "10"])
    out = capsys.readouterr().out
    assert stop.value.code == 0
    assert "Indexed 2 file(s)" in out and "older than --since 2" in out


# --- time budget and the full-home scope guard ------------------------------------------------------------


class _Clock:
    """Fake wall clock: only moves when an embedder call spends time."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _Slow(HashEmbedder):
    """Each embed call costs *cost* fake seconds."""

    def __init__(self, clock: _Clock, cost: float):
        super().__init__()
        self.clock, self.cost = clock, cost

    def embed_documents(self, texts):
        self.clock.now += self.cost
        return super().embed_documents(texts)


def _timed(home, tmp_path, budget, clock, cost=10.0, order="recent"):
    settings = _settings(tmp_path)
    embedder = _Slow(clock, cost)
    store = ChromaStore(settings.data_dir, settings.collection, embedder.name)
    stage = Stage(order=order, budget_seconds=budget)
    report = index_roots([home], embedder, store, settings, _matcher(home), stage, log=lambda _: None, clock=clock)
    return report, store


def test_budget_stops_after_the_file_in_progress_and_keeps_the_newest(home, tmp_path):
    _tree(home)
    clock = _Clock()
    report, store = _timed(home, tmp_path, budget=15, clock=clock)  # 10 s per file: 2 files start before 15 s
    assert _names(store) == {"newest.md", "newer.md"}
    assert report.deferred_time == 2 and report.remaining == 2
    text = render_report(report)
    assert "Coverage (15 s budget spent): 2 file(s) indexed this run, 2 eligible file(s) remaining" in text
    assert "the rest is not indexed yet" in text


def test_budget_runs_resume_newest_first(home, tmp_path):
    _tree(home)
    clock = _Clock()
    _timed(home, tmp_path, budget=15, clock=clock)
    report, store = _timed(home, tmp_path, budget=15, clock=clock)
    assert report.stats.files_indexed == 2 and report.remaining == 0
    assert len(_names(store)) == 4


def test_budget_larger_than_the_work_indexes_everything(home, tmp_path):
    _tree(home)
    report, store = _timed(home, tmp_path, budget=10_000, clock=_Clock())
    assert len(_names(store)) == 4 and report.deferred_time == 0
    assert "budget not used up" in render_report(report) and "0 eligible file(s) remaining" in render_report(report)


def test_zero_budget_indexes_nothing_and_counts_everything(home, tmp_path):
    _tree(home)
    report, store = _timed(home, tmp_path, budget=0, clock=_Clock())
    assert _names(store) == set() and report.deferred_time == 4


def test_no_budget_prints_no_coverage_line(home, tmp_path):
    _tree(home)
    report, _ = _run(home, tmp_path, Stage())
    assert "Coverage" not in render_report(report)


def _cli_env(home, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LOCUS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LOCUS_EMBEDDER", "hash")
    get_settings.cache_clear()
    policy = tmp_path / "policy.toml"
    policy.write_text("", encoding="utf-8")
    return str(policy)


def test_cli_full_home_without_scope_asks_for_one(home, tmp_path, monkeypatch, capsys):
    _tree(home)
    policy = _cli_env(home, tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as stop:
        cli.main(["index", "--roots", str(home), "--policy", policy])
    captured = capsys.readouterr()
    assert stop.value.code == 2
    assert "files would be indexed" in captured.out and "--budget-seconds" in captured.err
    assert not (tmp_path / "data" / "chroma").exists()  # nothing was indexed


def test_cli_full_home_with_all_runs(home, tmp_path, monkeypatch, capsys):
    _tree(home)
    policy = _cli_env(home, tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as stop:
        cli.main(["index", "--roots", str(home), "--policy", policy, "--all"])
    assert stop.value.code == 0
    assert "Indexed 4 file(s)" in capsys.readouterr().out


def test_cli_budget_seconds_counts_as_a_scope(home, tmp_path, monkeypatch, capsys):
    _tree(home)
    policy = _cli_env(home, tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as stop:
        cli.main(["index", "--roots", str(home), "--policy", policy, "--order", "recent", "--budget-seconds", "600"])
    assert stop.value.code == 0
    out = capsys.readouterr().out
    assert "Indexed 4 file(s)" in out and "Coverage (600 s budget not used up)" in out


def test_cli_subfolder_root_needs_no_scope(home, tmp_path, monkeypatch, capsys):
    _tree(home)
    policy = _cli_env(home, tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as stop:
        cli.main(["index", "--roots", str(home / "c"), "--policy", policy])
    assert stop.value.code == 0
    assert "Indexed 2 file(s)" in capsys.readouterr().out
