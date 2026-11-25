"""The version has one source (`__version__`); every install pin in the README, server.json and the journey script
names that same release candidate."""

from __future__ import annotations

import json
import re
from pathlib import Path

from locus import __version__

ROOT = Path(__file__).resolve().parent.parent
RELEASE = re.compile(r"\b0\.\d+\.\d+(?:rc\d+)?\b")


def test_server_json_names_the_current_version():
    data = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    assert data["version"] == __version__
    assert all(pkg["version"] == __version__ for pkg in data["packages"])


def test_install_pins_in_the_readme_and_the_journey_script_name_the_current_release():
    for name in ("README.md", "scripts/user_journey.py"):
        text = (ROOT / name).read_text(encoding="utf-8")
        pins = re.findall(r"locusfiles(?:\[[a-z-]+\])?==([0-9][0-9a-z.]*)|locusfiles@v([0-9][0-9a-z.]*)", text)
        pins = [a or b for a, b in pins]
        assert pins and set(pins) == {__version__}, (name, pins)


def test_the_changelog_has_a_section_since_the_previous_candidate():
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert re.search(r"### Since `0\.2\.0rc2`", text)
