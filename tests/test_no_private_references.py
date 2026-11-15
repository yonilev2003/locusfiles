"""The public tree does not cite the private planning process: no ADR numbers, no "founder", no "Codex review", no
paths into a private docs/ folder, no pointers into the website's private repository (locus-web/).

Comments that cite a private document are noise to a stranger and tell them which internal files exist. The check
reads the files that ship or that a reader of the repository sees first. Ticket-style ids (RET-004, UX-005 and the
like) are a separate clean-up and are not checked here."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
THIS = "tests/test_no_private_references.py"
# data, not citations: "Founders" is a word inside a reversed-text fixture of the Hebrew PDF tests
SKIP = {THIS, "tests/test_hebrew_text_order.py", "CHANGELOG.md"}
# what the release check looks for in an archive: these names are its data, not references
CHECK_DATA = {"scripts/check_dist_contents.py"}
PATTERNS = {
    "an ADR number": re.compile(r"\bADR[- ]?\d+|\bADR draft\b|\bADR-"),
    "the word founder": re.compile(r"\b[Ff]ounder"),
    "a Codex review": re.compile(r"Codex review"),
    "the private website repository": re.compile(r"locus-web"),
    "a private docs path": re.compile(r"\bdocs/[A-Za-z0-9_-]+/[A-Za-z0-9_./-]+"),
}


def _tracked() -> list[str]:
    try:
        out = subprocess.run(
            ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True, timeout=30
        ).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        pytest.skip("not a git checkout")
    return out


def _checked(name: str) -> bool:
    top = name.split("/")[0]
    if name in SKIP or name.startswith(("tests/fixtures/", "src/locus/support_kb/")):
        return False
    return top in ("src", "scripts", ".github", "tests") or name in (
        "pyproject.toml", "README.md", "SECURITY.md", "CONTRIBUTING.md", "docs/package/README.md",
    )  # fmt: skip


def test_no_public_file_cites_the_private_planning_process():
    tracked = set(_tracked())
    found: list[str] = []
    for name in sorted(tracked):
        if not _checked(name) or Path(name).suffix not in {".py", ".md", ".yml", ".toml", ".txt"}:
            continue
        if name.startswith("src/locus/LICENSE") or name == "src/locus/NOTICE":
            continue
        for number, line in enumerate((ROOT / name).read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            for what, pattern in PATTERNS.items():
                if name in CHECK_DATA and what in ("the private website repository", "a private docs path"):
                    continue
                for hit in pattern.finditer(line):
                    if what == "a private docs path" and hit.group(0).rstrip(".,;:)") in tracked:
                        continue  # a path that exists in this repository (docs/assets/..., docs/package/README.md)
                    found.append(f"{name}:{number}: {what}: {line.strip()[:90]}")
    assert found == [], "\n" + "\n".join(found[:25])
