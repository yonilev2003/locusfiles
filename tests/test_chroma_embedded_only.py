"""Locus runs Chroma embedded only (SEC-022): the Semgrep rule that guards it exists and is blocking, and the source
does not do what the rule forbids.

chromadb 1.5.9's HTTP server has open advisories with no fixed release; Locus is not exposed because it only uses the
in-process PersistentClient. `.semgrep/locus.yml` keeps it that way in CI; this test checks the rule is still ERROR,
that its patterns still hold (so deleting or weakening the rule fails here), and that no source file trips them.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / ".semgrep" / "locus.yml"
SRC = ROOT / "src"
FIXTURE = ROOT / "tests" / "semgrep" / "locus_chroma_fixture.py"
RULE_ID = "locus-chroma-embedded-only"


def _rule_block() -> str:
    text = RULES.read_text(encoding="utf-8")
    start = text.index(f"- id: {RULE_ID}")
    nxt = text.find("\n  - id:", start + 1)
    return text[start : nxt if nxt != -1 else len(text)]


def _forbidden_patterns() -> list[re.Pattern[str]]:
    """The rule's patterns as regexes: `pattern: chromadb.X(...)` calls and every `pattern-regex`."""
    found: list[re.Pattern[str]] = []
    for line in _rule_block().splitlines():
        line = line.strip()
        if line.startswith("- pattern:"):
            call = line.removeprefix("- pattern:").strip().removesuffix("(...)")
            found.append(re.compile(re.escape(call) + r"\("))
        elif line.startswith("- pattern-regex:"):
            raw = line.removeprefix("- pattern-regex:").strip()
            if raw.startswith("'") and raw.endswith("'"):  # YAML single-quoted: '' is one quote
                raw = raw[1:-1].replace("''", "'")
            found.append(re.compile(raw))
    return found


def test_the_rule_exists_and_is_blocking():
    block = _rule_block()
    assert "severity: ERROR" in block and "languages: [python]" in block
    assert len(_forbidden_patterns()) >= 8  # the three client calls and the five regexes: nothing quietly dropped


def test_the_rule_still_catches_what_it_is_for():
    forbidden = _forbidden_patterns()
    for line in (
        "remote = chromadb.HttpClient(host='localhost', port=8000)",
        "cloud = chromadb.CloudClient(api_key='x')",
        "import chromadb.server.fastapi",
        'asgi_target = "chromadb.app:app"',
        "import chromadb_rust_bindings",
        'subprocess.run(["chroma", "run", "--path", "/tmp/x"])',
        'subprocess.run("chroma run --path /tmp/x", shell=True)',
        'Settings(chroma_api_impl="chromadb.api.fastapi.FastAPI")',
    ):
        assert any(p.search(line) for p in forbidden), line
    assert not any(p.search("store = chromadb.PersistentClient(path='/tmp/x')") for p in forbidden)


def test_the_fixture_the_ci_job_tests_the_rule_with_is_still_there():
    text = FIXTURE.read_text(encoding="utf-8")
    assert f"# ruleid: {RULE_ID}" in text and f"# ok: {RULE_ID}" in text


def test_no_source_file_starts_a_chroma_server_or_uses_a_remote_client():
    forbidden = _forbidden_patterns()
    offenders = []
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for pattern in forbidden:
            if pattern.search(text):
                offenders.append((str(path.relative_to(ROOT)), pattern.pattern))
    assert offenders == []


def test_the_only_chroma_client_the_source_builds_is_the_embedded_one():
    clients = set()
    for path in SRC.rglob("*.py"):
        clients |= set(re.findall(r"\bchromadb\.([A-Za-z]*Client)\(", path.read_text(encoding="utf-8")))
    assert clients == {"PersistentClient"}
