"""W2: the unsupported_type breakdown in the dry run, and the plain-text formats added with noise guards.

Everything runs over a fake home in tmp_path; nothing reads the real home directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from locus.config import DEFAULT_EXTENSIONS, Settings
from locus.ingest.parsers import TEXT_EXTENSIONS, noise_name_reason, parse_file
from locus.policy import Matcher, Policy
from locus.rootscan import UNSUPPORTED_TOP_N, render_roots_report, scan_roots

NEW_TEXT = [
    ".htm",
    ".sql",
    ".sh",
    ".c",
    ".h",
    ".cc",
    ".cpp",
    ".hpp",
    ".go",
    ".rs",
    ".swift",
    ".java",
    ".kt",
    ".rb",
    ".php",
    ".srt",
    ".vtt",
]


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        collection="test",
        embedder="hash",
        voyage_model="",
        openai_model="",
        local_model="",
        chunk_tokens=100,
        overlap_tokens=10,
        max_file_mb=5,
        extensions=DEFAULT_EXTENSIONS,
    )


def _write(root: Path, rel: str, size: int = 10) -> None:
    f = root / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"x" * size)


# ---- (1) unsupported_type breakdown ----


def test_unsupported_breakdown_counts_extensions_and_megabytes(tmp_path):
    home = (tmp_path / "home").resolve()
    for i in range(3):
        _write(home, f"a/f{i}.zzz", 1024 * 1024)
    _write(home, "b/x.qqq", 10)
    _write(home, "b/noext", 10)
    _write(home, "b/ok.md")
    report = scan_roots([home], _settings(tmp_path), Matcher(Policy(roots=(str(home),)), home))
    assert dict(report.unsupported) == {".zzz": 3, ".qqq": 1, "(none)": 1}
    assert report.unsupported_bytes[".zzz"] == 3 * 1024 * 1024
    assert report.excluded["unsupported_type"] == 5
    text = render_roots_report(report)
    assert ".zzz" in text and "3.0" in text and "top 3 of 3" in text


def test_unsupported_breakdown_is_top_n_with_rest_summed_and_leaks_no_names(tmp_path):
    home = (tmp_path / "home").resolve()
    for i in range(UNSUPPORTED_TOP_N + 5):
        _write(home, f"Zxq-private-{i}/file.e{i:02d}")
    _write(home, "Zxq-private-0/my holiday notes v2.final text")  # suffix is a sentence, not an extension
    report = scan_roots([home], _settings(tmp_path), Matcher(Policy(roots=(str(home),)), home))
    text = render_roots_report(report)
    assert f"top {UNSUPPORTED_TOP_N} of {UNSUPPORTED_TOP_N + 6}" in text
    assert "(other 6)" in text
    assert "(odd)" in report.unsupported
    assert "Zxq" not in text and "holiday" not in text and "final text" not in text and str(home) not in text


# ---- (2) added formats ----


@pytest.mark.parametrize("ext", NEW_TEXT)
def test_new_text_formats_are_default_and_parsed(tmp_path, ext):
    assert ext in DEFAULT_EXTENSIONS and ext in TEXT_EXTENSIONS
    path = tmp_path / f"file{ext}"
    path.write_text("héllo world\nsecond line\n", encoding="utf-8")
    assert parse_file(path) == "héllo world\nsecond line\n"


def test_opt_in_formats_stay_opt_in():
    assert not {".ini", ".cfg", ".xml", ".css", ".eml", ".jsonl", ".plist", ".strings"} & set(DEFAULT_EXTENSIONS)


@pytest.mark.parametrize(
    "name,reason",
    [
        ("package-lock.json", "lockfile"),
        ("Pipfile.lock", "lockfile"),
        ("pnpm-lock.yaml", "lockfile"),
        ("app.min.js", "generated"),
        ("vendor.bundle.js", "generated"),
        ("api.pb.go", "generated"),
        ("svc_pb2.py", "generated"),
        ("schema.generated.ts", "generated"),
        ("main.go", None),
        ("package.json", None),
        ("admin.js", None),
    ],
)
def test_noise_name_reason(name, reason):
    assert noise_name_reason(Path(name)) == reason


@pytest.mark.parametrize("name", ["package-lock.json", "app.min.js", "api.pb.go"])
def test_noisy_names_parse_to_none(tmp_path, name):
    path = tmp_path / name
    path.write_text('{"a": 1}\n', encoding="utf-8")
    assert parse_file(path) is None


@pytest.mark.parametrize("ext", [".js", ".go", ".html", ".sql", ".tsx", ".jsx", ".mjs", ".mts", ".cjs"])
def test_minified_files_are_skipped_but_long_lines_in_data_files_are_not(tmp_path, ext):
    path = tmp_path / f"bundle{ext}"
    path.write_text("a=1;" * 1000 + "\n", encoding="utf-8")
    assert parse_file(path) is None
    data = tmp_path / "data.json"
    data.write_text("[" + ",".join(["1"] * 3000) + "]\n", encoding="utf-8")
    assert parse_file(data) is not None


def test_dry_run_counts_noise_names_apart_from_files_it_would_index(tmp_path):
    home = (tmp_path / "home").resolve()
    _write(home, "p/main.go")
    _write(home, "p/package-lock.json")
    _write(home, "p/app.min.js")
    report = scan_roots([home], _settings(tmp_path), Matcher(Policy(roots=(str(home),)), home))
    assert dict(report.files) == {".go": 1}
    assert report.excluded["noise_name"] == 2


def test_log_over_one_megabyte_is_skipped_and_small_log_is_read(tmp_path):
    big = tmp_path / "big.log"
    big.write_text("".join(f"event {i} word{i}\n" for i in range(120_000)), encoding="utf-8")
    assert big.stat().st_size > 1024 * 1024
    assert parse_file(big) is None
    small = tmp_path / "small.log"
    small.write_text(
        "".join(f"event {chr(97 + i % 26)}{chr(97 + i // 26)} happened\n" for i in range(100)), encoding="utf-8"
    )
    assert parse_file(small) is not None


def test_repetitive_log_is_skipped_even_with_changing_numbers(tmp_path):
    rep = tmp_path / "rep.log"
    rep.write_text(
        "".join(f"2026-10-31 12:00:{i % 60:02d} heartbeat ok id={i}\n" for i in range(500)), encoding="utf-8"
    )
    assert parse_file(rep) is None
    varied = tmp_path / "varied.log"
    varied.write_text("".join(f"{i}: line about topic{i % 97 * 'x'}-{i}\n" for i in range(100)), encoding="utf-8")
    assert parse_file(varied) is not None
