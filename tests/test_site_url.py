"""SITE_URL: one constant for the public site address, with a LOCUS_SITE_URL override that must be https (http only to
a loopback host). The links the licence texts and the help article print derive from it."""

from __future__ import annotations

import pytest

from locus import config, licence, support


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, "https://locusfiles.com"),
        ("", "https://locusfiles.com"),
        ("https://example.org/", "https://example.org"),
        ("http://localhost:3000", "http://localhost:3000"),
        ("http://example.org", "https://locusfiles.com"),
        ("not a url", "https://locusfiles.com"),
        ("https://example.org?x=1", "https://locusfiles.com"),
    ],
)
def test_site_url_override_is_validated(raw, expected):
    assert config._site_url(raw) == expected


def test_default_site_url_feeds_the_licence_link_and_the_help_article():
    assert config.SITE_URL == "https://locusfiles.com"
    assert config.DEFAULT_API_BASE == config.SITE_URL
    assert licence.COMMERCIAL_URL == f"{config.SITE_URL}/commercial"
    text = next(a for a in support.load_articles() if a.id == "licence").body
    assert f"{config.SITE_URL}/commercial" in text and "{SITE_URL}" not in text
