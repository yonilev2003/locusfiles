"""Regression tests for the night-fix pass: policy matcher and scope guard (review F5, F6, F12, F13, F14)."""

from __future__ import annotations

import argparse
import math
import unicodedata
from pathlib import Path

import pytest

from locus.cli import _needs_scope
from locus.policy import FolderProfile, Matcher, Policy, secret_name


def _ns(**kw):
    base = dict(kinds=None, since=None, max_files=None, budget_seconds=None, all=False)
    return argparse.Namespace(**{**base, **kw})


@pytest.mark.parametrize("form", ["NFC", "NFD"])
def test_f5_deny_excluded_and_roots_ignore_unicode_spelling(tmp_path, form):
    home = tmp_path / "home"
    typed = unicodedata.normalize("NFC", "Café")
    on_disk = unicodedata.normalize(form, "Café")
    deny = Matcher(Policy(deny_paths=(f"~/{typed}",)), home)
    assert deny.decide(home / on_disk / "a.txt").reason == "user_deny"
    excluded = Matcher(Policy(folders=(FolderProfile(f"~/{typed}", "excluded"),)), home)
    assert excluded.decide(home / on_disk / "a.txt").reason == "profile_excluded"
    rooted = Matcher(Policy(roots=(f"~/{typed}",)), home)
    assert rooted.decide(home / on_disk / "a.txt").allowed
    assert rooted.decide(home / "Other" / "a.txt").reason == "outside_roots"


@pytest.mark.parametrize(
    "path",
    [
        "/Volumes/Backup/Users/me/.ssh/config.txt",
        "/Users/other/.aws/credentials.txt",
        "/Volumes/B/Users/me/Library/Keychains/x.txt",
        "/Volumes/B/Users/me/Library/Application Support/Firefox/Profiles/a.txt",
        "/Users/other/Library/Mail/V10/a.txt",
        "/Volumes/B/Users/me/.config/gh/hosts.txt",
    ],
)
def test_f6_credential_and_library_folders_are_denied_under_any_home(path):
    assert not Matcher(Policy(), Path("/Users/me")).decide(Path(path)).allowed


def test_f6_ordinary_folders_named_like_the_rules_stay_allowed():
    m = Matcher(Policy(), Path("/Users/me"))
    for p in ("/Volumes/Backup/Users/me/Documents/notes.txt", "/Users/me/Documents/Library/notes.txt",
              "/Users/me/Documents/config/gh/readme.txt", "/Users/me/work/ssh/readme.txt"):  # fmt: skip
        assert m.decide(Path(p)).allowed, p


def test_f6_own_library_allow_list_still_works():
    m = Matcher(Policy(library_allow=("~/Library/Mobile Documents",)), Path("/Users/me"))
    assert m.decide(Path("/Users/me/Library/Mobile Documents/n.txt")).allowed
    assert not m.decide(Path("/Users/me/Library/Mail/n.txt")).allowed


@pytest.mark.parametrize(
    "name",
    ["a.p8", "A.PPK", "x.GPG", "prod.tfvars", "c.ovpn", "terraform.tfstate", "terraform.tfstate.backup", "kubeconfig"],
)
def test_f14_more_secret_names(name):
    assert secret_name(name)


@pytest.mark.parametrize("name", ["tokenizer.py", "notes.txt", "kubeconfig-guide.md", "plan.asc.md"])
def test_f14_neighbours_are_not_secrets(name):
    assert not secret_name(name)


@pytest.mark.parametrize("bad", [math.inf, math.nan])
def test_f12_inf_and_nan_are_not_scopes(tmp_path, bad):
    home = tmp_path / "home"
    m = Matcher(Policy(), home)
    assert _needs_scope(_ns(since=bad), [home], m)
    assert _needs_scope(_ns(budget_seconds=bad), [home], m)
    assert not _needs_scope(_ns(since=30.0), [home], m)
    assert not _needs_scope(_ns(budget_seconds=60.0), [home], m)


def test_f12_a_parent_of_home_needs_a_scope_but_a_subfolder_does_not(tmp_path):
    home = tmp_path / "users" / "me"
    m = Matcher(Policy(), home)
    assert _needs_scope(_ns(), [tmp_path / "users"], m)
    assert _needs_scope(_ns(), [Path(tmp_path.anchor)], m)
    assert not _needs_scope(_ns(), [home / "Documents"], m)
    assert not _needs_scope(_ns(all=True), [home], m)


def test_f13_matcher_accepts_a_symlinked_root_and_its_target(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    m = Matcher(Policy(roots=(str(link),)), tmp_path)
    assert m.decide(real / "a.md").allowed
    assert m.decide(link / "a.md").allowed
    assert m.decide(tmp_path / "elsewhere" / "a.md").reason == "outside_roots"
