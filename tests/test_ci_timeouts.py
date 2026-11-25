"""Every CI job has a timeout, so a hung test cannot hold a runner for the default six hours."""

from __future__ import annotations

import re
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"


def _jobs(text: str) -> dict[str, str]:
    """job name -> its block, from the `jobs:` section (jobs sit at two spaces of indent)."""
    section = text.split("\njobs:\n", 1)[1]
    parts = re.split(r"(?m)^  ([A-Za-z0-9_-]+):\s*$", section)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts) - 1, 2)}


def test_every_job_in_every_workflow_has_timeout_minutes():
    missing = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for name, block in _jobs(path.read_text(encoding="utf-8")).items():
            if not re.search(r"(?m)^    timeout-minutes:\s*\d+", block):
                missing.append(f"{path.name}:{name}")
    assert missing == []
