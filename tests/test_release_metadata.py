"""S-17, S-19: release metadata that says the same thing in every place it appears.

- the fastembed floor in server.json (what an MCP registry client runs with `uvx --with`) is the floor of the `local`
  extra in pyproject.toml, and the version server.json lists is the version of the package;
- NOTICE and THIRD_PARTY_LICENSES.md name the model the engine really downloads (config.DEFAULT_LOCAL_MODEL), with the
  licence the inventory script records for it.
"""

from __future__ import annotations

import importlib.util
import json
import re
import tomllib
from pathlib import Path

from locus import __version__
from locus.config import DEFAULT_LOCAL_MODEL

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "locus"


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _server_json() -> dict:
    return json.loads((ROOT / "server.json").read_text(encoding="utf-8"))


def _fastembed_spec(requirement: str) -> str:
    match = re.fullmatch(r"\s*fastembed\s*(.+?)\s*", requirement)
    assert match, requirement
    return match.group(1).replace(" ", "")


def test_server_json_fastembed_floor_is_the_local_extras_floor():
    from_extra = [r for r in _pyproject()["project"]["optional-dependencies"]["local"] if r.startswith("fastembed")]
    assert len(from_extra) == 1
    args = _server_json()["packages"][0]["runtimeArguments"]
    from_registry = [a["value"] for a in args if a["name"] == "--with" and a["value"].startswith("fastembed")]
    assert len(from_registry) == 1
    assert _fastembed_spec(from_registry[0]) == _fastembed_spec(from_extra[0])


def test_server_json_lists_the_version_the_package_has():
    server = _server_json()
    assert server["version"] == __version__
    assert [p["version"] for p in server["packages"]] == [__version__]


def test_notice_names_the_model_the_engine_downloads():
    notice = (PACKAGE / "NOTICE").read_text(encoding="utf-8")
    flat = " ".join(notice.split())  # the sentence wraps over lines
    assert f"downloads the {DEFAULT_LOCAL_MODEL} embedding model (MIT licence)" in flat
    assert "bge-small" not in notice and "BAAI" not in notice


def test_third_party_licences_list_the_default_model_as_mit():
    text = (PACKAGE / "THIRD_PARTY_LICENSES.md").read_text(encoding="utf-8")
    rows = [line for line in text.splitlines() if line.startswith(f"| {DEFAULT_LOCAL_MODEL} |")]
    assert len(rows) == 1 and "| MIT |" in rows[0] and f"https://huggingface.co/{DEFAULT_LOCAL_MODEL}" in rows[0]
    models_section = text.split("## Models downloaded at run time", 1)[1]
    assert "bge-small" not in models_section


def test_the_inventory_script_writes_the_same_model_row():
    spec = importlib.util.spec_from_file_location("licence_inventory", ROOT / "scripts" / "licence_inventory.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert [m[0] for m in module.RUNTIME_MODELS] == [DEFAULT_LOCAL_MODEL]
    assert module.RUNTIME_MODELS[0][1] == "MIT"
