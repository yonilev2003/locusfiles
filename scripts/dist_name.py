#!/usr/bin/env python3
"""The engine's PyPI distribution name lives in exactly one place: `[project].name` in pyproject.toml.

    python scripts/dist_name.py get          # print it
    python scripts/dist_name.py set NAME     # rewrite it (validated), print the new name

The public name is the maintainer's pick. Candidates checked read-only
on 2026-09-23 as unregistered on PyPI: locusfiles, locus-files, locus-engine (`locus` itself is
taken by an unrelated spatial-index library). Until then the name stays `locus`, which cannot be
published. `.github/workflows/package.yml` can rehearse a candidate with `set` in its own
checkout (workflow_dispatch input `dist_name`); nothing is committed or published by that.

Changing the distribution name does not change the console script (`locus`) or the import
package (`locus`); the engine reads its own version from `locus.__version__`, never by looking
the distribution up by name.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"

# PEP 508 / core-metadata "Name" field.
_VALID = re.compile(r"^([A-Z0-9]|[A-Z0-9][A-Z0-9._-]*[A-Z0-9])$", re.IGNORECASE)
_NAME_LINE = re.compile(r'^(name\s*=\s*)"[^"\n]*"(\s*(?:#.*)?)$')


def get_name(pyproject: Path = PYPROJECT) -> str:
    return tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["name"]


def set_name(new: str, pyproject: Path = PYPROJECT) -> str:
    if not _VALID.match(new):
        raise ValueError(f"not a valid distribution name: {new!r}")
    lines = pyproject.read_text(encoding="utf-8").splitlines(keepends=True)
    in_project = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("["):
            in_project = stripped == "[project]"
            continue
        m = _NAME_LINE.match(line.rstrip("\r\n")) if in_project else None
        if m:
            ending = line[len(line.rstrip("\r\n")) :]
            lines[i] = f'{m.group(1)}"{new}"{m.group(2)}{ending}'
            break
    else:
        raise ValueError(f'no `name = "..."` line in the [project] table of {pyproject}')
    pyproject.write_text("".join(lines), encoding="utf-8")
    written = get_name(pyproject)
    if written != new:  # pragma: no cover - defensive: the rewrite must round-trip through tomllib
        raise RuntimeError(f"rewrite produced {written!r}, expected {new!r}")
    return written


def main(argv: list[str]) -> int:
    if argv[:1] == ["get"] and len(argv) == 1:
        print(get_name())
        return 0
    if argv[:1] == ["set"] and len(argv) == 2:
        try:
            print(set_name(argv[1]))
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
