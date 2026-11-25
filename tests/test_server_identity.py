"""The initialize answer names the server (name, title, description, website, version, icon), and the icon is the
original logo file, byte for byte. Offline: an in-memory client."""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path

from mcp import Client

from locus import __version__
from locus import mcp_server as srv
from locus.config import SITE_URL

LOGO = Path(__file__).resolve().parent.parent / "docs" / "assets" / "logo.svg"
EXPECTED_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" fill="none"><rect width="32" height="32" rx="9" '
    'fill="#0f172a"/><g transform="translate(4.5 4.5) scale(0.9583)" stroke="#ffffff" stroke-width="2.4" '
    'stroke-linecap="round"><path d="M9 2.5v5.5M15 2.5v5.5"/><rect x="6" y="8" width="12" height="7" rx="3.5"/>'
    '<path d="M12 15v3.5M8.5 21.5h7"/></g></svg>'
)


def _initialize_result():
    async def go():
        async with Client(srv.mcp) as client:
            return client.server_info

    return asyncio.run(go())


def test_the_initialize_answer_carries_name_title_description_website_version_and_icon():
    info = _initialize_result()
    assert info.name == "locus" and info.title == "Locus" and info.version == __version__
    assert info.website_url == SITE_URL
    assert "file path" in info.description and "chunk number" in info.description
    assert info.icons and info.icons[0].mime_type == "image/svg+xml"
    data = info.icons[0].src.split("base64,", 1)[1]
    assert base64.b64decode(data).decode() == EXPECTED_SVG


def test_the_logo_file_is_exactly_the_original_icon():
    assert LOGO.read_text(encoding="utf-8") == EXPECTED_SVG
    assert srv.LOGO_SVG == EXPECTED_SVG
