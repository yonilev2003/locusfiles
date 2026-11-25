"""Reinstall hints carry the pre-release flags while the installed version is a pre-release, and only then."""

from __future__ import annotations

import pytest

import locus
from locus import cli, hints


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("0.2.0rc3", True),
        ("0.2.0.dev1", True),
        ("0.2.0b2", True),
        ("0.2.0", False),
        ("0.1.4", False),
        ("1.0.0.post1", False),
    ],
)
def test_is_prerelease(version, expected):
    assert hints.is_prerelease(version) is expected


def test_a_prerelease_gets_the_flags_in_doctor_and_setup_hints(monkeypatch):
    monkeypatch.setattr(locus, "__version__", "0.2.0rc3")
    assert "--prerelease allow" in cli._local_extra_hint() and "--pip-args=--pre" in cli._local_extra_hint()
    assert "--prerelease allow" in cli._reinstall_hint()
    assert "--prerelease allow" in hints.uv_install("local-torch")


def test_a_final_release_keeps_the_plain_commands(monkeypatch):
    monkeypatch.setattr(locus, "__version__", "0.2.0")
    for text in (
        cli._local_extra_hint(),
        cli._reinstall_hint(),
        hints.uv_install("local"),
        hints.pipx_install("local"),
    ):
        assert "--prerelease" not in text and "--pre" not in text
    assert 'uv tool install --reinstall --python 3.12 "locusfiles[local]"' in cli._local_extra_hint()
