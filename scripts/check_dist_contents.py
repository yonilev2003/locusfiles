#!/usr/bin/env python3
"""Check a built sdist + wheel before anyone can publish them (IP-008).

    python scripts/check_dist_contents.py dist/

Fails (exit 1) when:
- an archive holds anything outside the allowlist. The repo is private and holds the master plan,
  business docs, locus-web/ and supabase/; the sdist and wheel are public the moment they are
  uploaded, so only the engine source and packaging metadata may be in them;
- a file that looks like a secret (.env, keys, a Chroma index) is in either archive, even inside
  an allowed directory;
- the wheel is missing a module that exists under src/locus/ (a packaging config that silently
  drops code), or a help article under src/locus/support_kb/ (locus_help reads them at run time,
  so a wheel without them imports fine and fails only when someone asks for help);
- a help article in the wheel mentions private repo context (the articles are public text that
  locus_help shows to users);
- either archive lacks the licence, the NOTICE or the third-party licence inventory (LEG-014): the
  sdist under src/locus/, the wheel under its .dist-info/licenses/;
- the wheel metadata disagrees with pyproject.toml (name, version), lacks the `locus` console
  script or the `relay`/`local` extras, or makes torch / sentence-transformers a core dependency
  (the customer install is `[relay]`, no torch: master plan Ch.8.4.2 item 2).

Stdlib only, so it runs on any Python >= 3.11 without installing anything.
"""

from __future__ import annotations

import argparse
import email.parser
import fnmatch
import re
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]

# Relative paths inside the sdist's top-level "<name>-<version>/" directory. hatchling adds
# PKG-INFO and .gitignore itself; LICENSE* is allowed so the licence commit needs no change here.
SDIST_ALLOWED = ("src/locus/*", "pyproject.toml", "docs/package/README.md", "PKG-INFO", ".gitignore", "LICENSE*")

# The PyPI long description and anything else public must not leak internal repo context (F8).
PRIVATE_MARKERS = (
    "claude.md",
    "agents.md",
    "docs/plan.md",
    "master-plan",
    "public_name_allowed",
    "adr-0",
    "working name",
    "origin/claude/",
    "agent-memory",
    "docs/eval/reports",
    "docs/ops/",
)


def private_markers(text: str) -> list[str]:
    low = text.lower()
    return [m for m in PRIVATE_MARKERS if m in low]


# Basenames never allowed in either archive, whatever directory they are in.
SECRET_LIKE = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "id_rsa*",
    "id_ed25519*",
    "*.sqlite3",
    "chroma.sqlite3",
    "notion_token.json",
    "*.token",
)

REQUIRED_EXTRAS = ("relay", "local")

# LEG-014: every archive carries the engine licence, the NOTICE and the third-party licence inventory
# (scripts/licence_inventory.py). pyproject's license-files puts them in the wheel's .dist-info/licenses/.
REQUIRED_LICENCE_FILES = ("LICENSE", "NOTICE", "THIRD_PARTY_LICENSES.md")
EXPECTED_LICENCE = "PolyForm-Noncommercial-1.0.0"  # the SPDX id pyproject.toml declares (decided 2026-11-14)
# Package data the engine reads at run time: locus_help's articles (src/locus/support.py KB_DIR).
HELP_ARTICLES_DIR = "support_kb"
HEAVY_CORE_DEPS = ("torch", "sentence-transformers", "sentence_transformers")


def normalize(name: str) -> str:
    """PEP 503 project-name normalization."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _secret_like(path: str) -> bool:
    base = PurePosixPath(path).name
    return any(fnmatch.fnmatch(base, pat) for pat in SECRET_LIKE)


def _pyproject(pyproject: Path) -> tuple[str, str]:
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
    name = data["name"]
    if "version" in data:
        version = data["version"]
    else:  # dynamic: [tool.hatch.version] path = src/locus/__init__.py
        init = (pyproject.parent / "src" / "locus" / "__init__.py").read_text(encoding="utf-8")
        m = re.search(r'^__version__\s*=\s*"([^"]+)"', init, re.MULTILINE)
        if not m:
            raise SystemExit("cannot find __version__ in src/locus/__init__.py")
        version = m.group(1)
    return name, version


def check_sdist(path: Path, name: str, version: str) -> list[str]:
    errors: list[str] = []
    stem = f"{re.sub(r'[-_.]+', '_', name).lower()}-{version}"
    if path.name != f"{stem}.tar.gz":
        errors.append(f"sdist file name {path.name!r} != expected {stem}.tar.gz")
    with tarfile.open(path, "r:gz") as tar:
        members = [m for m in tar.getmembers() if not m.isdir()]
    for m in members:
        parts = PurePosixPath(m.name).parts
        if not parts or parts[0] != stem:
            errors.append(f"sdist: {m.name} is outside the {stem}/ top-level directory")
            continue
        rel = "/".join(parts[1:])
        if not any(fnmatch.fnmatch(rel, pat) for pat in SDIST_ALLOWED):
            errors.append(f"sdist: {rel} is not on the allowlist {SDIST_ALLOWED}")
        if _secret_like(rel):
            errors.append(f"sdist: {rel} looks like a secret or a local index")
        if m.issym() or m.islnk():
            errors.append(f"sdist: {rel} is a link")
        if rel in (".gitignore", "docs/package/README.md", "PKG-INFO") and m.isfile():
            with tarfile.open(path, "r:gz") as tar:
                body = tar.extractfile(m.name).read().decode("utf-8", "replace")  # type: ignore[union-attr]
            errors.extend(f"sdist: {rel} mentions private repo context {hit!r}" for hit in private_markers(body))
    present = {"/".join(PurePosixPath(m.name).parts[1:]) for m in members}
    errors.extend(
        f"sdist: missing src/locus/{name} (LEG-014)"
        for name in REQUIRED_LICENCE_FILES
        if f"src/locus/{name}" not in present
    )
    return errors


def check_wheel(path: Path, name: str, version: str, src: Path | None) -> list[str]:
    errors: list[str] = []
    dist_info = f"{re.sub(r'[-_.]+', '_', name).lower()}-{version}.dist-info"
    if not path.name.startswith(f"{re.sub(r'[-_.]+', '_', name).lower()}-{version}-"):
        errors.append(f"wheel file name {path.name!r} does not match {name} {version}")
    with zipfile.ZipFile(path) as whl:
        names = [n for n in whl.namelist() if not n.endswith("/")]
        for n in names:
            top = n.split("/", 1)[0]
            if top not in ("locus", dist_info):
                errors.append(f"wheel: {n} is outside locus/ and {dist_info}/")
            if _secret_like(n):
                errors.append(f"wheel: {n} looks like a secret or a local index")
            if n.startswith(f"locus/{HELP_ARTICLES_DIR}/") and n.endswith(".md"):
                article = whl.read(n).decode("utf-8", "replace")
                errors.extend(f"wheel: {n} mentions private repo context {hit!r}" for hit in private_markers(article))
        try:
            meta_raw = whl.read(f"{dist_info}/METADATA").decode("utf-8")
            entry_points = whl.read(f"{dist_info}/entry_points.txt").decode("utf-8")
        except KeyError as exc:
            return [*errors, f"wheel: missing {exc.args[0]}"]

    licence_dir = {PurePosixPath(n).name for n in names if n.startswith(f"{dist_info}/licenses/")}
    errors.extend(
        f"wheel: missing {name} under {dist_info}/licenses/ (LEG-014)"
        for name in REQUIRED_LICENCE_FILES
        if name not in licence_dir
    )

    if src is not None:
        expected = {f"locus/{p.relative_to(src).as_posix()}" for p in src.rglob("*.py") if "__pycache__" not in p.parts}
        missing = sorted(expected - set(names))
        errors.extend(f"wheel: missing module {m} (present under src/)" for m in missing)
        articles = {f"locus/{p.relative_to(src).as_posix()}" for p in src.glob(f"{HELP_ARTICLES_DIR}/*.md")}
        errors.extend(f"wheel: missing help article {a} (present under src/)" for a in sorted(articles - set(names)))

    meta = email.parser.Parser().parsestr(meta_raw)
    if normalize(meta.get("Name", "")) != normalize(name):
        errors.append(f"METADATA Name {meta.get('Name')!r} != pyproject name {name!r}")
    if meta.get("Version") != version:
        errors.append(f"METADATA Version {meta.get('Version')!r} != {version!r}")
    body = meta.get_payload()
    errors.extend(
        f"METADATA long description mentions private repo context {hit!r}"
        for hit in private_markers(body if isinstance(body, str) else "")
    )
    extras = set(meta.get_all("Provides-Extra") or [])
    errors.extend(f"METADATA lacks extra {e!r}" for e in REQUIRED_EXTRAS if e not in extras)
    for req in meta.get_all("Requires-Dist") or []:
        req_name = normalize(re.split(r"[\s;<>=!~\[(]", req, maxsplit=1)[0])
        if req_name in {normalize(h) for h in HEAVY_CORE_DEPS} and "extra ==" not in req:
            errors.append(f"METADATA makes {req!r} a core dependency (customer install must not pull torch)")
    if not re.search(r"^\[console_scripts\][^\[]*^locus\s*=\s*locus\.cli:main\s*$", entry_points, re.M | re.S):
        errors.append("entry_points.txt lacks `locus = locus.cli:main`")
    return errors


def release_errors(wheel: Path) -> list[str]:
    """Upload-only checks: the licence must be PolyForm Noncommercial 1.0.0 (decided 2026-11-14), with its text and the
    Required Notice, never the pre-launch placeholder and never the earlier FSL text."""
    with zipfile.ZipFile(wheel) as whl:
        meta_name = next((n for n in whl.namelist() if n.endswith(".dist-info/METADATA")), None)
        if meta_name is None:
            return ["wheel: no METADATA"]
        meta = email.parser.Parser().parsestr(whl.read(meta_name).decode("utf-8"))
        licence_files = [n for n in whl.namelist() if "/licenses/" in n or n.rsplit("/", 1)[-1].startswith("LICENSE")]
    errors: list[str] = []
    declared = (meta.get("License-Expression") or meta.get("License") or "").strip()
    if declared != EXPECTED_LICENCE:
        errors.append(f"release: licence is {declared or 'unset'!r}; expected {EXPECTED_LICENCE!r}")
    if not licence_files:
        errors.append("release: the wheel carries no LICENSE file")
    for n in licence_files:
        with zipfile.ZipFile(wheel) as whl:
            text = whl.read(n).decode("utf-8", "replace")
        if "[LICENSOR]" in text:
            errors.append(f"release: {n} still has the [LICENSOR] placeholder")
        if n.rsplit("/", 1)[-1] == "LICENSE":
            if "# PolyForm Noncommercial License 1.0.0" not in text:
                errors.append(f"release: {n} is not the PolyForm Noncommercial License 1.0.0 text")
            if "Required Notice: Copyright" not in text:
                errors.append(f"release: {n} has no 'Required Notice: Copyright' line")
            if "Functional Source License" in text:
                errors.append(f"release: {n} still carries the earlier FSL text")
    return errors


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("dist", type=Path, help="directory holding exactly one .tar.gz and one .whl")
    ap.add_argument("--pyproject", type=Path, default=ROOT / "pyproject.toml")
    ap.add_argument("--src", type=Path, default=ROOT / "src" / "locus", help="compare wheel modules to this tree")
    ap.add_argument(
        "--release",
        action="store_true",
        help="also fail on anything that must be settled before an upload (licence A6 still undecided)",
    )
    args = ap.parse_args(argv)

    name, version = _pyproject(args.pyproject)
    sdists = sorted(args.dist.glob("*.tar.gz"))
    wheels = sorted(args.dist.glob("*.whl"))
    if len(sdists) != 1 or len(wheels) != 1:
        print(f"expected one sdist and one wheel in {args.dist}, found {sdists + wheels}", file=sys.stderr)
        return 1

    src = args.src if args.src.is_dir() else None
    errors = check_sdist(sdists[0], name, version) + check_wheel(wheels[0], name, version, src)
    if args.release:
        errors += release_errors(wheels[0])
    for err in errors:
        print(f"FAIL {err}")
    if errors:
        return 1
    print(f"OK   {sdists[0].name} and {wheels[0].name}: allowlisted contents, metadata matches {name} {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
