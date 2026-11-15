"""The stat-first resumable walk behind the freshness step (src/locus/ingest/scan.py).

Two promises: it yields what `iter_files` yields minus the matcher's verdict on single files, in the same order; and a
walk that is stopped anywhere and resumed from `resume_at` visits every file once, with nothing skipped and nothing
repeated. No index, no embedder: temp folders only.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from locus.config import DEFAULT_EXTENSIONS, Settings
from locus.ingest.indexer import iter_files
from locus.ingest.scan import StatScan, position_from_json, position_to_json
from locus.policy import Matcher, Policy


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data", collection="scan", embedder="hash", voyage_model="", openai_model="",
        local_model="", chunk_tokens=100, overlap_tokens=10, max_file_mb=0.001, extensions=DEFAULT_EXTENSIONS,
    )  # fmt: skip


@pytest.fixture()
def messy(tmp_path: Path) -> Path:
    """Every reason `iter_files` has to leave something out, and some names that sort awkwardly."""
    root = (tmp_path / "root").resolve()
    for name in (
        "a.md", "Aa.md", "B.TXT", "a-b.md", "c.png", ".hidden.md", "noext", "trailing.", "z.md",
        "sub/n.md", "sub/deep/d.md", "sub/deep/e.md", "sub-two/x.md", "zeta/z.md",
        "node_modules/pkg.md", ".git/h.md", ".hid/h.md", "denied/s.md", "credentials.md", "empty/.keep.md",
    ):  # fmt: skip
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"text of {name}")
    (root / "big.md").write_text("x" * 5000)  # over max_file_mb (0.001 MB)
    (root / "link.md").symlink_to(root / "a.md")
    (root / "linkdir").symlink_to(root / "sub", target_is_directory=True)
    (root / "broken.md").symlink_to(root / "missing-target.md")
    return root


def _matcher(root: Path, tmp_path: Path) -> Matcher:
    return Matcher(Policy(roots=(str(root),), deny_paths=(str(root / "denied"),)), tmp_path / "home")


def _never() -> bool:
    return False


def _walk(root, settings, matcher, stop=_never, resume=(), on_skip=None):
    scan = StatScan(root, settings, matcher, on_skip, stop, resume)
    return scan, list(scan.files())


class _StopAfter:
    """`stop()` that answers False for the first *calls* questions and True from then on."""

    def __init__(self, calls: int):
        self.calls, self.asked = calls, 0

    def __call__(self) -> bool:
        self.asked += 1
        return self.asked > self.calls


def test_yields_what_iter_files_yields_once_single_file_verdicts_are_applied(messy, tmp_path):
    settings, matcher = _settings(tmp_path), _matcher(messy, tmp_path)
    legacy = [str(p) for p in iter_files(messy, settings, None, matcher)]
    scan, found = _walk(messy, settings, matcher)
    kept = [(key, mtime) for key, mtime in found if matcher.decide(Path(key)).allowed]
    assert [key for key, _ in kept] == legacy  # same files, same order
    assert all(mtime == Path(key).stat().st_mtime for key, mtime in kept)
    assert scan.finished and scan.resume_at is None
    names = {Path(key).name for key, _ in found}
    assert {"a.md", "Aa.md", "B.TXT", "n.md", "d.md", "z.md"} <= names
    assert not names & {"c.png", ".hidden.md", "noext", "trailing.", "big.md", "link.md", "broken.md", "pkg.md", "h.md"}
    assert "s.md" not in names  # the denied folder is never entered
    assert "credentials.md" in names  # a single-file verdict is the caller's to ask, and it is: see `kept`
    assert "credentials.md" not in {Path(key).name for key, _ in kept}


def test_a_denied_root_yields_nothing(messy, tmp_path):
    matcher = Matcher(Policy(roots=(str(messy),), deny_paths=(str(messy),)), tmp_path / "home")
    scan, found = _walk(messy, _settings(tmp_path), matcher)
    assert found == [] and scan.finished


def test_works_without_a_matcher(messy, tmp_path):
    settings = _settings(tmp_path)
    legacy = [str(p) for p in iter_files(messy, settings, None, None)]
    _, found = _walk(messy, settings, None)
    assert [key for key, _ in found] == legacy


def test_stopping_anywhere_and_resuming_visits_every_file_exactly_once(messy, tmp_path):
    settings, matcher = _settings(tmp_path), _matcher(messy, tmp_path)
    _, full = _walk(messy, settings, matcher)
    for limit in range(0, 60):
        first, head = _walk(messy, settings, matcher, stop=_StopAfter(limit))
        if first.finished:
            assert head == full
            continue
        assert first.resume_at is not None
        second, tail = _walk(messy, settings, matcher, resume=first.resume_at)
        assert second.finished
        assert head + tail == full, f"stopped after {limit} questions"


def test_many_short_slices_cover_the_tree_once(messy, tmp_path):
    settings, matcher = _settings(tmp_path), _matcher(messy, tmp_path)
    _, full = _walk(messy, settings, matcher)
    seen, resume, slices = [], (), 0
    while True:
        scan, items = _walk(messy, settings, matcher, stop=_StopAfter(2), resume=resume)
        seen += items
        slices += 1
        assert slices < 200, "the walk is not making progress"
        if scan.finished:
            break
        resume = scan.resume_at
    assert seen == full and slices > 3


def test_a_position_survives_json_and_a_malformed_one_means_the_start(messy, tmp_path):
    settings, matcher = _settings(tmp_path), _matcher(messy, tmp_path)
    scan, _ = _walk(messy, settings, matcher, stop=_StopAfter(5))
    assert scan.resume_at
    assert position_from_json(position_to_json(scan.resume_at)) == scan.resume_at
    for bad in (None, "x", [["a", "b"]], [[2, "x"]], [[0]], [[0, 3]], {"k": 1}):
        assert position_from_json(bad) == ()


def test_resuming_inside_a_folder_that_has_since_vanished_carries_on_after_it(messy, tmp_path):
    settings, matcher = _settings(tmp_path), _matcher(messy, tmp_path)
    _, full = _walk(messy, settings, matcher)
    _, tail = _walk(messy, settings, matcher, resume=((1, "sub"), (1, "gone"), (0, "x.md")))
    assert [key for key, _ in tail] == [key for key, _ in full if "/sub-two/" in key or "/zeta/" in key]


def test_a_folder_the_os_will_not_list_is_reported_and_the_walk_goes_on(messy, tmp_path):
    if getattr(os, "geteuid", lambda: 1)() == 0:
        pytest.skip("root can list anything")
    heard: list[str] = []
    locked = messy / "sub-two"
    locked.chmod(0)
    try:
        scan, found = _walk(messy, _settings(tmp_path), _matcher(messy, tmp_path), on_skip=lambda r, p: heard.append(r))
    finally:
        locked.chmod(0o755)
    assert "unreadable_dir" in heard and scan.finished
    assert "z.md" in {Path(key).name for key, _ in found}
