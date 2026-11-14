#!/usr/bin/env python3
"""Write the engine's third-party licence inventory from installed package metadata (LEG-014).

    python scripts/licence_inventory.py                      # writes src/locus/THIRD_PARTY_LICENSES.md
    python scripts/licence_inventory.py --path /tmp/extra    # also look for metadata under /tmp/extra
    python scripts/licence_inventory.py --stdout             # print instead of writing

Lists every direct runtime dependency of the `locusfiles` package: [project].dependencies and every
[project].optional-dependencies extra except `dev` (test/lint tools are never installed for users).
For each one it reads the installed distribution's metadata (importlib.metadata) and records the
declared licence: License-Expression, else a one-line License field, else the License :: classifiers
mapped to SPDX ids. An extra that is not installed in this environment can be read from another
directory with --path (for example `pip install --no-deps --target DIR "openai>=1.30"`).

Fails (exit 1) when a dependency's metadata cannot be found, when no licence can be read from it, or
when the declared licence is copyleft or non-commercial (GPL/LGPL/AGPL/SSPL/EUPL/CC-*-NC, "proprietary").
None of these packages is bundled in the wheel or sdist: pip installs each from its own upstream
release, which carries its own licence and notices. The output ships in both archives (NOTICE points
to it) so the record travels with the package.

Stdlib only.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from datetime import date
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "src" / "locus" / "THIRD_PARTY_LICENSES.md"
SKIP_EXTRAS = ("dev",)

CLASSIFIER_SPDX = {
    "MIT License": "MIT",
    "Apache Software License": "Apache-2.0",
    "BSD License": "BSD",
    "ISC License (ISCL)": "ISC",
    "Python Software Foundation License": "PSF-2.0",
    "Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
}

NOT_ALLOWED = re.compile(
    r"\b(?:A|L)?GPL|General Public License|\bSSPL\b|\bEUPL\b|CC-BY(?:-SA)?-NC|Commons Clause|proprietary",
    re.IGNORECASE,
)

# Facts the metadata cannot express, checked by hand on the date in the output.
NOTES = {
    "pypdfium2": (
        "Its wheel bundles a PDFium binary; the licences of PDFium and the libraries built into it "
        "(abseil, FreeType, ICU, libjpeg-turbo, libpng, libtiff, OpenJPEG, zlib and others) ship in "
        "that wheel's licenses/ directory."
    ),
}

# Models the engine downloads at run time. Not a Python dependency, not bundled; listed so the
# record is complete. The id must match LOCUS_LOCAL_MODEL's default in src/locus/config.py.
RUNTIME_MODELS = (
    (
        "intfloat/multilingual-e5-small",
        "MIT",
        "https://huggingface.co/intfloat/multilingual-e5-small",
        "Downloaded from Hugging Face on first use when LOCUS_EMBEDDER=local (the `local` extra).",
    ),
)


def normalize(name: str) -> str:
    """PEP 503 project-name normalization."""
    return re.sub(r"[-_.]+", "-", name).lower()


def requirements(pyproject: Path = ROOT / "pyproject.toml") -> list[tuple[str, str, str]]:
    """(name, specifier, needed_for) for every direct runtime dependency, in pyproject order."""
    project = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
    rows: list[tuple[str, str, str]] = []
    groups = [("core", project.get("dependencies", []))]
    groups += [
        (f"extra `{extra}`", reqs)
        for extra, reqs in project.get("optional-dependencies", {}).items()
        if extra not in SKIP_EXTRAS
    ]
    for needed_for, reqs in groups:
        for req in reqs:
            m = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*([^;]*)", req)
            if not m:
                raise SystemExit(f"cannot parse requirement {req!r}")
            rows.append((m.group(1), m.group(2).strip(), needed_for))
    return rows


def _find(name: str, extra_paths: list[str]) -> metadata.Distribution | None:
    try:
        return metadata.distribution(name)
    except metadata.PackageNotFoundError:
        pass
    for dist in metadata.distributions(path=extra_paths) if extra_paths else ():
        if normalize(dist.metadata["Name"] or "") == normalize(name):
            return dist
    return None


def declared_licence(meta: metadata.PackageMetadata) -> str:
    expr = (meta.get("License-Expression") or "").strip()
    if expr:
        return expr
    field = (meta.get("License") or "").strip()
    if field and "\n" not in field and len(field) <= 100:
        return field
    classifiers = [c.split(" :: ")[-1] for c in meta.get_all("Classifier") or [] if c.startswith("License ::")]
    return " OR ".join(CLASSIFIER_SPDX.get(c, c) for c in classifiers)


def upstream(meta: metadata.PackageMetadata) -> str:
    urls: dict[str, str] = {}
    for entry in meta.get_all("Project-URL") or []:
        label, _, url = entry.partition(",")
        urls[label.strip().lower()] = url.strip()
    for label in ("repository", "source", "source code", "homepage", "home"):
        if urls.get(label):
            return urls[label]
    fallback = f"https://pypi.org/project/{normalize(meta['Name'] or '')}/"
    return (meta.get("Home-page") or "").strip() or next(iter(urls.values()), fallback)


def ships_notice(dist: metadata.Distribution) -> bool:
    return any(Path(str(f)).name.upper().startswith("NOTICE") for f in dist.files or [])


def inventory(extra_paths: list[str]) -> tuple[list[dict[str, str]], list[str]]:
    rows: list[dict[str, str]] = []
    errors: list[str] = []
    for name, spec, needed_for in requirements():
        dist = _find(name, extra_paths)
        if dist is None:
            errors.append(f"{name}: not installed here; install it or pass --path with its metadata")
            continue
        licence = declared_licence(dist.metadata)
        if not licence:
            errors.append(f"{name} {dist.version}: no licence in its metadata; check upstream and add a note")
        elif NOT_ALLOWED.search(licence):
            errors.append(f"{name} {dist.version}: licence {licence!r} is not allowed for a dependency")
        rows.append(
            {
                "name": name,
                "spec": spec or "any",
                "needed_for": needed_for,
                "version": dist.version,
                "licence": licence,
                "notice": "yes" if ships_notice(dist) else "no",
                "upstream": upstream(dist.metadata),
                "note": NOTES.get(normalize(name), ""),
            }
        )
    return rows, errors


def render(rows: list[dict[str, str]], on: str) -> str:
    lines = [
        "# Third-party licences: Locus engine (`locusfiles`)",
        "",
        f"Direct runtime dependencies of this package, generated from each installed package's metadata on {on}.",
        "None of them is bundled in this package: pip, pipx or uv installs each one from its own upstream",
        "release, which carries its own licence text and any NOTICE file. Test and lint tools are not listed",
        "because they are never installed for users. The Locus engine itself is licensed under",
        "FSL-1.1-ALv2 (see LICENSE and NOTICE).",
        "",
        "| Package | Needed for | Version range | Licence (as declared) | Checked version "
        "| Ships its own NOTICE | Upstream |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['name']} | {r['needed_for']} | `{r['spec']}` | {r['licence']} | {r['version']} | "
            f"{r['notice']} | {r['upstream']} |"
        )
    notes = [r for r in rows if r["note"]]
    if notes:
        lines += ["", "Notes:", ""]
        lines += [f"- {r['name']}: {r['note']}" for r in notes]
    lines += [
        "",
        "## Models downloaded at run time (not bundled)",
        "",
        "| Model | Licence | Source | When |",
        "|---|---|---|---|",
    ]
    lines += [f"| {m} | {lic} | {url} | {when} |" for m, lic, url, when in RUNTIME_MODELS]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--path", action="append", default=[], help="extra directory to read package metadata from")
    ap.add_argument("--output", type=Path, default=OUTPUT)
    ap.add_argument("--date", default=date.today().isoformat(), help="date written into the file (YYYY-MM-DD)")
    ap.add_argument("--stdout", action="store_true", help="print the inventory instead of writing it")
    args = ap.parse_args(argv)

    rows, errors = inventory(args.path)
    for err in errors:
        print(f"FAIL {err}", file=sys.stderr)
    if errors:
        return 1
    text = render(rows, args.date)
    if args.stdout:
        sys.stdout.write(text)
    else:
        args.output.write_text(text, encoding="utf-8")
        print(f"wrote {args.output} ({len(rows)} dependencies)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
