"""W1: real multi-root indexing under the policy matcher: staged, deduplicated, resumable.

Everything runs over a fake home in tmp_path with the hash embedder; nothing reads the real home.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from locus import cli
from locus.config import DEFAULT_EXTENSIONS, Settings, get_settings
from locus.ingest.embedder import HashEmbedder
from locus.ingest.multiroot import Stage, index_roots, render_report
from locus.policy import Matcher, Policy

chromadb = pytest.importorskip("chromadb")

from locus.ingest.store import ChromaStore  # noqa: E402


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


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _build(home: Path) -> None:
    _write(home / "a" / "plan.md", "The launch plan covers pricing and the waitlist.")
    _write(home / "b" / "plan-copy.md", "The launch plan covers pricing and the waitlist.")  # byte-identical
    _write(home / "b" / "notes.md", "Notes about the zebra crossing project.")
    _write(home / "b" / "todo.txt", "buy lemons and tahini for the hummus")
    _write(home / "c" / "data.csv", "name,qty\nlemon,3\n")
    _write(home / ".ssh" / "id_rsa.md", "PRIVATE KEY MATERIAL")  # denied
    _write(home / "a" / "api_key.txt", "token=abc")  # denied by name


def _run(home: Path, tmp_path: Path, stage: Stage | None = None, roots=None):
    settings = _settings(tmp_path)
    stage = stage or Stage()
    embedder = HashEmbedder()
    store = ChromaStore(settings.data_dir, settings.collection, embedder.name)
    matcher = Matcher(Policy(roots=tuple(str(r) for r in (roots or [home]))), home)
    report = index_roots(roots or [home], embedder, store, settings, matcher, stage, log=lambda _: None)
    return report, store


def test_indexes_all_roots_skips_denied_and_dedupes(home, tmp_path):
    _build(home)
    report, store = _run(home, tmp_path)
    paths = store.list_paths()
    assert report.stats.files_indexed == 4  # plan.md once, notes.md, todo.txt, data.csv
    assert report.duplicates == 1
    assert report.denied >= 2
    assert not any(".ssh" in p or "api_key" in p for p in paths)
    assert sum(p.endswith("plan.md") or p.endswith("plan-copy.md") for p in paths) == 1
    hit = store.query(HashEmbedder().embed_query("launch plan pricing waitlist"), k=1)[0]
    assert hit.path and hit.chunk_index == 0  # constraint 2: a real path + chunk_index


def test_duplicate_is_recorded_as_an_alias_and_report_names_no_path(home, tmp_path):
    _build(home)
    report, _ = _run(home, tmp_path)
    assert len(report.aliases) == 1
    canonical, alias = next(iter(report.aliases.items()))[0], next(iter(report.aliases.values()))[0]
    assert {Path(canonical).name, Path(alias).name} == {"plan.md", "plan-copy.md"}
    text = render_report(report)
    assert str(home) not in text and "plan" not in text
    assert "duplicate" in text.lower()


def test_second_run_is_resumable_and_skips_everything(home, tmp_path):
    _build(home)
    _run(home, tmp_path)
    report, _ = _run(home, tmp_path)
    assert report.stats.files_indexed == 0
    assert report.stats.files_skipped >= 4
    assert report.duplicates == 1  # still counted, not re-embedded


def test_kinds_stage_and_remaining_by_count(home, tmp_path):
    _build(home)
    report, store = _run(home, tmp_path, Stage(kinds=frozenset({".md"})))
    assert {Path(p).suffix for p in store.list_paths()} == {".md"}
    assert report.deferred_kind == {".txt": 1, ".csv": 1}
    assert report.remaining == 2
    # the next stage picks up the rest and nothing more
    report2, store2 = _run(home, tmp_path, Stage(skip_kinds=frozenset({".md"})))
    assert report2.stats.files_indexed == 2 and report2.remaining == 0
    assert {Path(p).suffix for p in store2.list_paths()} == {".md", ".txt", ".csv"}


def test_max_files_and_resume(home, tmp_path):
    _build(home)
    report, store = _run(home, tmp_path, Stage(max_files=2))
    assert report.stats.files_indexed == 2
    assert report.deferred_budget >= 1 and report.remaining == report.deferred_budget
    report2, store = _run(home, tmp_path, Stage(max_files=10))
    assert report2.remaining == 0
    assert len({p for p in store.list_paths()}) == 4


def test_max_mb_stops_before_the_next_file(home, tmp_path):
    _write(home / "x" / "big.md", "word " * 400_000)  # ~2 MB
    _write(home / "x" / "small.md", "tiny file")
    report, _ = _run(home, tmp_path, Stage(max_mb=1.0))
    assert report.stats.files_indexed == 1
    assert report.deferred_budget == 1


def test_changed_file_is_reindexed_and_dup_whose_twin_changed_is_not_lost(home, tmp_path):
    _build(home)
    _run(home, tmp_path)
    _write(home / "a" / "plan.md", "Completely different content now, about lemons.")
    report, store = _run(home, tmp_path)
    paths = store.list_paths()
    assert report.stats.files_indexed >= 2  # the changed file and the former duplicate, now unique
    assert any(p.endswith("plan-copy.md") for p in paths) and any(p.endswith("plan.md") for p in paths)


def test_cli_real_run(home, tmp_path, monkeypatch, capsys):
    _build(home)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LOCUS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LOCUS_EMBEDDER", "hash")
    get_settings.cache_clear()
    policy = tmp_path / "policy.toml"
    policy.write_text("", encoding="utf-8")
    with pytest.raises(SystemExit) as stop:
        cli.main(["index", "--roots", str(home), "--policy", str(policy), "--kinds", "md,txt", "--max-files", "10"])
    out = capsys.readouterr().out
    assert stop.value.code == 0
    assert "indexed" in out.lower() and "remaining" in out.lower() and str(home) not in out


def test_cli_hosted_embedder_is_refused(home, tmp_path, monkeypatch, capsys):
    home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LOCUS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LOCUS_EMBEDDER", "voyage")
    get_settings.cache_clear()
    try:
        with pytest.raises(SystemExit) as stop:
            cli.main(["index", "--roots", str(home)])
    finally:
        get_settings.cache_clear()
    assert stop.value.code == 2
    assert "hosted" in capsys.readouterr().err


def test_cli_prune_with_roots_is_refused(home, tmp_path, monkeypatch, capsys):
    home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LOCUS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LOCUS_EMBEDDER", "hash")
    with pytest.raises(SystemExit) as stop:
        cli.main(["index", "--roots", str(home), "--prune"])
    assert stop.value.code == 2
    assert "--prune" in capsys.readouterr().err


def test_unchanged_rerun_does_not_look_up_each_file_in_the_store(home: Path, tmp_path: Path, monkeypatch):
    """Skip check cost must not grow with files seen: a re-run that changes nothing reads the index in bulk."""
    for i in range(300):
        _write(home / f"d{i % 10}" / f"note{i}.md", f"note number {i} about subject {i * 7}")
    _write(home / "dup" / "copy.md", "note number 0 about subject 0")  # alias of note0.md
    _run(home, tmp_path)

    settings = _settings(tmp_path)
    embedder = HashEmbedder()
    store = ChromaStore(settings.data_dir, settings.collection, embedder.name)
    calls = {"get": 0}
    real_get = store._col.get

    def counting_get(*args, **kwargs):
        calls["get"] += 1
        return real_get(*args, **kwargs)

    monkeypatch.setattr(store._col, "get", counting_get)
    matcher = Matcher(Policy(roots=(str(home),)), home)
    report = index_roots([home], embedder, store, settings, matcher, Stage(), log=lambda _m: None)
    assert report.stats.files_indexed == 0
    assert report.stats.files_skipped >= 301
    assert calls["get"] < 20, f"{calls['get']} Chroma gets for {report.stats.files_seen} files"
