"""Every test file that code, comments, CI or docs cite exists in this repository.

0.2.0's tree once cited 13 test files that were not in it (the relay's four among them), and ci.yml said relay tests
ran in CI. A claim that a test exists must be true: either the test is here or the comment does not say so.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CITE = re.compile(r"\btests/(test_[A-Za-z0-9_]+\.py)")


def _tracked_text_files() -> list[Path]:
    try:
        listed = subprocess.run(
            ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True, timeout=30
        ).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        pytest.skip("not a git checkout")
    suffixes = {".py", ".md", ".yml", ".yaml", ".toml", ".json", ".txt", ".cfg"}
    return [ROOT / name for name in listed if Path(name).suffix in suffixes and name != "uv.lock"]


def test_every_cited_test_file_exists():
    missing: dict[str, list[str]] = {}
    for path in _tracked_text_files():
        if path.name == Path(__file__).name or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for name in set(CITE.findall(text)):
            if not (ROOT / "tests" / name).is_file():
                missing.setdefault(name, []).append(str(path.relative_to(ROOT)))
    assert missing == {}, missing
