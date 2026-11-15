"""S-08: ONNX Runtime's own usage telemetry is switched off before onnxruntime is imported.

ONNX Runtime 1.31 and later ships Microsoft's usage-telemetry uploader in its macOS and Linux builds (the collector
address `https://mobile.events.data.microsoft.com/OneCollector/1.0` is in the binaries; the Windows build has no such
uploader). It is on by default and starts at `import onnxruntime`: it creates a device-ID file and an event queue in
the user's profile and later uploads. It reads `ORT_DISABLE_TELEMETRY` once, at that import. `import locus` sets the
variable, so it must come before any import of onnxruntime or fastembed.

The behavioural tests run in child processes with a throw-away HOME, the CI variables ONNX Runtime treats as "do not
send" removed, and never import onnxruntime in the pytest process itself (that would create the files in the real
profile). A control shows the check can fail: where a plain `import onnxruntime` creates no state (a Windows build, an
ONNX Runtime before 1.31, or the package not installed) the behavioural tests are skipped, because there is nothing to
switch off.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"

# Variables ONNX Runtime reads to decide it runs in CI and so sends nothing; the control must not inherit them.
CI_VARIABLES = (
    "CI", "TF_BUILD", "GITHUB_ACTIONS", "GITLAB_CI", "CIRCLECI", "TRAVIS", "JENKINS_URL", "CODEBUILD_BUILD_ID",
    "BUILDKITE", "TEAMCITY_VERSION", "APPVEYOR", "BITBUCKET_BUILD_NUMBER", "SYSTEM_TEAMFOUNDATIONCOLLECTIONURI",
)  # fmt: skip


def child_env(home: Path, **extra: str) -> dict[str, str]:
    base = {
        k: v for k, v in os.environ.items()
        if k not in CI_VARIABLES and not k.startswith(("ORT_", "XDG_", "LOCUS_"))
    }  # fmt: skip
    home.mkdir(parents=True, exist_ok=True)
    return {
        **base,
        "PYTHONPATH": str(SRC),
        "PYTHONDONTWRITEBYTECODE": "1",
        "HOME": str(home),
        "USERPROFILE": str(home),
        "XDG_CACHE_HOME": str(home / ".cache"),
        **extra,
    }


def run_child(code: str, env: dict[str, str]) -> str:
    done = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)], env=env, capture_output=True, text=True, timeout=120
    )
    assert done.returncode == 0, done.stderr[-1500:]
    return done.stdout.strip()


def telemetry_files(home: Path) -> list[str]:
    """The device-ID file and the event queue ONNX Runtime keeps in the user's profile."""
    names = (p.name for p in home.rglob("*") if p.is_file())
    return sorted(n for n in names if n == "deviceid" or n.startswith("onnxruntime.db"))


@pytest.mark.parametrize("users_value", [None, "0", "false", "1"])
def test_importing_locus_forces_the_variable_on_whatever_the_environment_says(tmp_path, users_value):
    extra = {} if users_value is None else {"ORT_DISABLE_TELEMETRY": users_value}
    env = child_env(tmp_path / "home", **extra)
    assert run_child("import locus, os; print(os.environ['ORT_DISABLE_TELEMETRY'])", env) == "1"


@pytest.mark.skipif(importlib.util.find_spec("onnxruntime") is None, reason="onnxruntime is not installed")
def test_onnxruntime_leaves_no_telemetry_state_when_locus_is_imported_first(tmp_path):
    control_home = tmp_path / "control"
    run_child("import onnxruntime", child_env(control_home))
    if not telemetry_files(control_home):
        pytest.skip("a plain import of onnxruntime creates no telemetry state (Windows build or version before 1.31)")

    home = tmp_path / "guarded"
    run_child("import locus\nimport onnxruntime", child_env(home))
    assert telemetry_files(home) == []


@pytest.mark.skipif(importlib.util.find_spec("fastembed") is None, reason="fastembed is not installed")
def test_importing_the_local_embedder_stack_leaves_no_telemetry_state(tmp_path):
    """The route the default embedder takes: locus first, then fastembed (which imports onnxruntime)."""
    control_home = tmp_path / "control"
    run_child("import fastembed", child_env(control_home))
    if not telemetry_files(control_home):
        pytest.skip("a plain import of fastembed creates no telemetry state (Windows build or version before 1.31)")

    home = tmp_path / "guarded"
    run_child("import locus.ingest.embedder\nimport fastembed", child_env(home))
    assert telemetry_files(home) == []
