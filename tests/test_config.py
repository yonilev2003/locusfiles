"""config.py: the API base is normalised and refused when unsafe, no hosted address is hard-coded elsewhere in the
engine, and the defaults are the local ones."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from locus import config
from locus.config import DEFAULT_API_BASE, api_url, get_settings, normalize_api_base

SRC = Path(__file__).resolve().parents[1] / "src" / "locus"


@pytest.fixture(autouse=True)
def _clean_env(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith("LOCUS_"):
            monkeypatch.delenv(key)
    monkeypatch.setattr(config, "_SOURCE_CHECKOUT", None)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, DEFAULT_API_BASE),
        ("", DEFAULT_API_BASE),
        ("  ", DEFAULT_API_BASE),
        ("https://example.com", "https://example.com"),
        ("https://example.com/", "https://example.com"),
        ("https://example.com/prefix//", "https://example.com/prefix"),
        ("http://localhost:3000", "http://localhost:3000"),
        ("http://127.0.0.1:8080/", "http://127.0.0.1:8080"),
    ],
)
def test_the_api_base_is_normalised(raw, expected):
    assert normalize_api_base(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "http://example.com",  # an API key must never cross a network in clear
        "ftp://example.com",
        "example.com",
        "https://",
        "https://example.com?x=1",
        "https://example.com#frag",
        "https://example.com?",
    ],
)
def test_an_unsafe_api_base_is_refused(raw):
    with pytest.raises(RuntimeError, match="LOCUS_API_BASE"):
        normalize_api_base(raw)


def test_api_url_joins_without_doubling_the_slash():
    assert api_url("https://example.com", "/api/x") == "https://example.com/api/x"
    assert api_url("https://example.com/p", "/api/x") == "https://example.com/p/api/x"


def test_a_bad_api_base_does_not_stop_local_use(monkeypatch):
    monkeypatch.setenv("LOCUS_API_BASE", "http://example.com")
    settings = get_settings()
    assert settings.embedder == "local" and settings.api_base_error
    with pytest.raises(RuntimeError):
        settings.hosted_url(settings.managed_relay_url)


def test_the_defaults_are_the_local_ones(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    settings = get_settings()
    assert settings.embedder == "local"
    assert settings.managed_api_key is None and settings.mcp_token is None
    assert settings.local_model == config.DEFAULT_LOCAL_MODEL


def test_hosted_addresses_live_in_config_only():
    """OPS-007: no other module may hard-code where Locus's hosted API lives. Prose may name the site; code must not
    build a URL from it."""
    url = re.compile(r"""https?://[^\s"']*(locusfiles\.com|vercel\.app|supabase\.co)""")
    offenders = [
        str(path.relative_to(SRC))
        for path in SRC.rglob("*.py")
        if path.name not in ("config.py", "licence.py")  # licence.py: the commercial-licence page, not an API address
        and url.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_the_hosted_switches_default_to_off():
    assert not config.feature_enabled(config.HOSTED_ENABLE_ENV)
    assert not config.feature_enabled(config.NOTION_ENABLE_ENV)
