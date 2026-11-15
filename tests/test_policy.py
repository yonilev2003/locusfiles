"""Policy file, default deny-list matcher, policy-aware walker and `locus index --roots --dry-run`.

Everything runs over a fake home in tmp_path; nothing reads the real home directory.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from locus import cli
from locus.config import DEFAULT_EXTENSIONS, Settings
from locus.ingest.indexer import SKIP_REASONS, iter_files
from locus.policy import (
    DENIED_SKIP_REASONS,
    FolderProfile,
    Matcher,
    Policy,
    PolicyError,
    decide,
    load_policy,
    parse_policy,
    secret_name,
)
from locus.rootscan import render_roots_report, scan_roots


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return (tmp_path / "home").resolve()


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


# (path relative to home, is_dir, expected reason or None)
DEFAULT_CASES = [
    ("Documents/notes/plan.md", False, None),
    ("Documents/tokenizer.py", False, None),
    ("Desktop/résumé.pdf", False, None),
    (".ssh", True, "secrets_dir"),
    (".ssh/config", False, "secrets_dir"),
    (".aws/credentials", False, "secrets_dir"),
    (".gnupg", True, "secrets_dir"),
    (".config/gcloud/creds.json", False, "secrets_dir"),
    (".config/gh/hosts.yml", False, "secrets_dir"),
    (".config/other/settings.md", False, None),
    ("Library/Keychains/login.keychain-db", False, "keychain"),
    ("Documents/vault.kdbx", False, "keychain"),
    ("Library/Application Support/Google/Chrome/Default/History", False, "browser_profile"),
    ("Library/Safari", True, "browser_profile"),
    ("library/application support/firefox", True, "browser_profile"),  # case-insensitive
    (".config/google-chrome/Default/Cookies", False, "browser_profile"),  # Linux
    (".config/chromium", True, "browser_profile"),
    ("snap/firefox/common/.mozilla", True, "browser_profile"),
    ("AppData/Local/Google/Chrome/User Data/Default/History", False, "browser_profile"),  # Windows
    ("AppData/Roaming/Mozilla/Firefox/Profiles", True, "browser_profile"),
    ("AppData/Roaming/Microsoft/Credentials/abc", False, "keychain"),
    (".local/share/keyrings/login.keyring", False, "keychain"),
    (".password-store/work/mail.gpg", False, "keychain"),
    ("Library/Preferences/x.plist", False, "library"),
    ("Library", True, "library"),
    ("proj/.venv/lib/python3.12/site-packages/x.py", False, "package_cache"),
    ("proj/site-packages", True, "package_cache"),
    ("proj/.gradle", True, "package_cache"),
    ("security-evidence-2026-10-25", True, "evidence"),
    ("security-evidence-2026-10-25/log.txt", False, "evidence"),
    ("proj/.env", False, "secret_file"),
    ("proj/.env.production", False, "secret_file"),
    ("proj/server.pem", False, "secret_file"),
    ("proj/deploy.key", False, "secret_file"),
    ("proj/id_ed25519", False, "secret_file"),
    ("proj/id_rsa.pub", False, "secret_file"),
    ("proj/api-token.txt", False, "secret_file"),
    ("proj/accessToken.json", False, "secret_file"),
    ("proj/credentials.json", False, "secret_file"),
    ("proj/my_password_list.md", False, "secret_file"),
    ("proj/.npmrc", False, "secret_file"),
    ("proj/key-notes.md", False, None),
]


@pytest.mark.parametrize("rel,is_dir,reason", DEFAULT_CASES)
def test_default_deny_list(home, rel, is_dir, reason):
    verdict = decide(home / rel, Policy(), home, is_dir=is_dir)
    assert verdict.reason == reason
    assert verdict.allowed == (reason is None)


def test_deny_list_does_not_apply_outside_home_except_name_rules(home, tmp_path):
    elsewhere = tmp_path / "volume"
    assert decide(elsewhere / "Library" / "x.md", Policy(), home).allowed
    assert decide(elsewhere / ".env", Policy(), home).reason == "secret_file"
    assert decide(elsewhere / "node_modules" / "site-packages" / "a.py", Policy(), home).reason == "package_cache"


@pytest.mark.parametrize(
    "name,expected",
    [
        ("tokenizer.py", False),
        ("tokens.md", True),
        ("API_KEY.txt", True),
        ("apikey.txt", True),
        ("passwd", True),
        ("secretary.md", False),
        ("my-secrets.md", True),
        ("notes.md", False),
        (".env", True),
        (".envrc", False),
    ],
)
def test_secret_name_words(name, expected):
    assert secret_name(name) is expected


def test_roots_confine(home, tmp_path):
    policy = Policy(roots=("~/Documents", str(tmp_path / "volume")))
    m = Matcher(policy, home)
    assert m.decide(home / "Documents" / "a.md").allowed
    assert m.decide(tmp_path / "volume" / "b.md").allowed
    assert m.decide(home / "Pictures" / "c.md").reason == "outside_roots"
    assert m.decide(home / "Documents-old" / "c.md").reason == "outside_roots"  # prefix is by folder, not by text


def test_no_roots_means_no_confinement(home):
    assert Matcher(Policy(), home).decide(home / "anywhere" / "a.md").allowed


def test_profiles_deepest_wins_and_excluded_denies(home):
    policy = Policy(
        folders=(
            FolderProfile("~/Downloads", "untrusted"),
            FolderProfile("~/Downloads/mine", "trusted"),
            FolderProfile("~/Private", "excluded"),
            FolderProfile("~/Archive", "read-only"),
        )
    )
    m = Matcher(policy, home)
    assert m.decide(home / "Downloads" / "a.pdf").profile == "untrusted"
    assert m.decide(home / "Downloads" / "mine" / "a.pdf").profile == "trusted"
    assert m.decide(home / "Archive" / "a.pdf").profile == "read-only"
    assert m.decide(home / "Documents" / "a.md").profile == "trusted"
    private = m.decide(home / "Private" / "deep" / "a.md")
    assert (private.allowed, private.reason) == (False, "profile_excluded")


def test_user_deny_paths(home):
    m = Matcher(Policy(deny_paths=("~/Documents/taxes",)), home)
    assert m.decide(home / "Documents" / "taxes" / "2025.pdf").reason == "user_deny"
    assert m.decide(home / "Documents" / "taxes-notes.md").allowed


def test_library_named_exceptions(home):
    exc = "~/Library/Mobile Documents/com~apple~CloudDocs"
    m = Matcher(Policy(library_allow=(exc,)), home)
    cloud = home / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
    assert m.decide(home / "Library", is_dir=True).allowed  # ancestor: walker may descend
    assert m.decide(home / "Library" / "Mobile Documents", is_dir=True).allowed
    assert m.decide(cloud / "notes" / "a.md").allowed
    assert m.decide(home / "Library" / "Mobile Documents" / "com~apple~Notes", is_dir=True).reason == "library"
    assert m.decide(home / "Library" / "Preferences", is_dir=True).reason == "library"
    assert m.decide(home / "Library" / "stray.md").reason == "library"  # a file in the ancestor is not allowed
    # an exception never overrides a more specific rule
    chrome = home / "Library" / "Application Support" / "Google" / "Chrome"
    assert Matcher(Policy(library_allow=("~/Library",)), home).decide(chrome, is_dir=True).reason == "browser_profile"


def test_default_rules_cannot_be_overridden_by_a_folder_profile(home):
    m = Matcher(Policy(folders=(FolderProfile("~/.ssh", "trusted"),), roots=("~",)), home)
    assert m.decide(home / ".ssh" / "config").reason == "secrets_dir"


# ---- the policy file ----

GOOD = """
version = 1
roots = ["~", "/Volumes/data"]
[[folder]]
path = "~/Downloads"
profile = "untrusted"
[library]
allow = ["~/Library/Mobile Documents"]
[deny]
paths = ["~/Private"]
"""


def test_parse_policy_roundtrip():
    p = parse_policy(GOOD)
    assert p.roots == ("~", "/Volumes/data")
    assert p.folders == (FolderProfile("~/Downloads", "untrusted"),)
    assert p.library_allow == ("~/Library/Mobile Documents",)
    assert p.deny_paths == ("~/Private",)


@pytest.mark.parametrize(
    "text,fragment",
    [
        ("colour = 1", "colour"),
        ("version = 2", "version"),
        ('roots = "~"', "roots"),
        ('roots = ["Documents"]', "Documents"),
        ('[[folder]]\npath = "~/x"\nprofile = "friendly"', "friendly"),
        ('[[folder]]\npath = "~/x"', "path and a profile"),
        ('[[folder]]\npath = "~/x"\nprofile = "trusted"\nextra = 1', "extra"),
        ("[deny]\nglobs = []", "globs"),
        ("roots = [", "TOML"),
    ],
)
def test_malformed_policy_is_an_error_naming_the_problem(text, fragment):
    with pytest.raises(PolicyError, match=fragment):
        parse_policy(text)


def test_load_policy_missing_file_is_defaults(tmp_path):
    assert load_policy(tmp_path / "nope.toml") == Policy()


def test_load_policy_reads_file(tmp_path):
    f = tmp_path / "policy.toml"
    f.write_text(GOOD, encoding="utf-8")
    assert load_policy(f).roots == ("~", "/Volumes/data")


# ---- the walker ----


def _build_home(home: Path) -> None:
    files = {
        "Documents/a.md": "alpha",
        "Documents/b.txt": "beta",
        "Documents/tokens.md": "x",
        "Downloads/c.pdf": "gamma",
        "Private/d.md": "delta",
        ".ssh/id_rsa": "k",
        "Library/Preferences/p.md": "p",
        "proj/.env": "S=1",
        "proj/readme.md": "r",
        "proj/site-packages/x.py": "x",
        "proj/node_modules/y.js": "y",
        "security-evidence-1/e.md": "e",
        "Pictures/photo.jpg": "j",
    }
    for rel, text in files.items():
        f = home / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")


def test_iter_files_without_policy_is_unchanged(home, tmp_path):
    _build_home(home)
    seen: list[str] = []
    got = {p.relative_to(home).as_posix() for p in iter_files(home, _settings(tmp_path), lambda r, _p: seen.append(r))}
    assert "Library/Preferences/p.md" in got and "proj/site-packages/x.py" in got and "Documents/tokens.md" in got
    assert not any(r.startswith("denied_") for r in seen)


def test_iter_files_with_policy_prunes_and_reports(home, tmp_path):
    _build_home(home)
    policy = Policy(roots=("~",), folders=(FolderProfile("~/Private", "excluded"),))
    seen: list[str] = []
    got = {
        p.relative_to(home).as_posix()
        for p in iter_files(home, _settings(tmp_path), lambda r, _p: seen.append(r), Matcher(policy, home))
    }
    assert got == {"Documents/a.md", "Documents/b.txt", "Downloads/c.pdf", "proj/readme.md"}
    for reason in (
        "denied_secrets_dir",
        "denied_library",
        "denied_secret_file",
        "denied_package_cache",
        "denied_evidence",
        "denied_profile_excluded",
    ):
        assert reason in seen, reason
    assert set(seen) <= set(SKIP_REASONS)
    assert set(DENIED_SKIP_REASONS) <= set(SKIP_REASONS)


def test_iter_files_denied_root_yields_nothing(home, tmp_path):
    _build_home(home)
    seen: list[str] = []
    out = list(iter_files(home / ".ssh", _settings(tmp_path), lambda r, _p: seen.append(r), Matcher(Policy(), home)))
    assert out == [] and seen == ["denied_secrets_dir"]


@pytest.mark.skipif(getattr(os, "geteuid", lambda: 1)() == 0, reason="needs a non-root user")
def test_unreadable_folder_is_counted(home, tmp_path):
    _build_home(home)
    locked = home / "Documents"
    locked.chmod(0)
    try:
        seen: list[str] = []
        list(iter_files(home, _settings(tmp_path), lambda r, _p: seen.append(r)))
    finally:
        locked.chmod(0o755)
    assert seen.count("unreadable_dir") == 1


# ---- the dry run ----


def _run(argv: list[str]) -> int:
    with pytest.raises(SystemExit) as stop:
        cli.main(argv)
    return stop.value.code


def test_scan_roots_counts_and_report_has_no_paths(home, tmp_path):
    _build_home(home)
    policy = Policy(roots=(str(home),), folders=(FolderProfile("~/Downloads", "untrusted"),))
    report = scan_roots([home], _settings(tmp_path), Matcher(policy, home))
    assert report.files == {".md": 3, ".txt": 1, ".pdf": 1}
    assert dict(report.by_profile) == {"trusted": 4, "untrusted": 1}
    assert report.excluded["denied_secrets_dir"] == 1
    text = render_roots_report(report)
    assert "Your home directory is a root" in text
    assert str(home) not in text
    assert "denied_secrets_dir" in text and ".md" in text


def test_report_text_names_no_folder_from_the_scan(home, tmp_path):
    _build_home(home)
    (home / "Zxq-secret-project").mkdir()
    (home / "Zxq-secret-project" / "n.md").write_text("n", encoding="utf-8")
    report = scan_roots([home], _settings(tmp_path), Matcher(Policy(roots=(str(home),)), home))
    text = render_roots_report(report)
    assert "Zxq" not in text and "photo" not in text and "proj" not in text


def test_cli_roots_dry_run(home, tmp_path, monkeypatch, capsys):
    _build_home(home)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LOCUS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LOCUS_EMBEDDER", "hash")
    policy = tmp_path / "policy.toml"
    policy.write_text('[[folder]]\npath = "~/Downloads"\nprofile = "untrusted"\n', encoding="utf-8")
    code = _run(["index", "--roots", str(home), "--policy", str(policy), "--dry-run"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Dry run over 1 root(s): 5 files would be indexed" in out
    assert "untrusted" in out and "denied_secret_file" in out
    assert not (tmp_path / "data" / "chroma").exists()  # nothing was opened or created


def test_cli_bad_policy_file_names_the_key(home, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LOCUS_DATA_DIR", str(tmp_path / "data"))
    home.mkdir(parents=True)
    bad = tmp_path / "policy.toml"
    bad.write_text("colour = 1\n", encoding="utf-8")
    assert _run(["index", "--roots", str(home), "--policy", str(bad), "--dry-run"]) == 2
    assert "colour" in capsys.readouterr().err
