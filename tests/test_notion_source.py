"""Tests for the Notion ingestion source. All HTTP is mocked — never calls the real Notion API."""

from __future__ import annotations

import json
from pathlib import Path

from locus.config import Settings
from locus.ingest.notion_source import (
    NotionPage,
    block_to_markdown,
    page_to_markdown,
    sync_notion,
)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        collection="locus",
        embedder="hash",
        voyage_model="voyage-4",
        openai_model="text-embedding-3-small",
        local_model="BAAI/bge-small-en-v1.5",
        chunk_tokens=700,
        overlap_tokens=100,
        max_file_mb=25,
        extensions=(".md",),
    )


# --------------------------------------------------------------------------------------
# Block -> Markdown conversion
# --------------------------------------------------------------------------------------


def _rich(text: str) -> list[dict]:
    return [{"type": "text", "plain_text": text, "annotations": {}, "href": None}]


def test_paragraph_to_markdown():
    block = {"type": "paragraph", "paragraph": {"rich_text": _rich("hello world")}}
    assert block_to_markdown(block) == "hello world"


def test_heading_to_markdown():
    block = {"type": "heading_2", "heading_2": {"rich_text": _rich("Section")}}
    assert block_to_markdown(block) == "## Section"


def test_bulleted_list_item_to_markdown():
    block = {"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": _rich("item one")}}
    assert block_to_markdown(block) == "- item one"


def test_to_do_checked_and_unchecked():
    checked = {"type": "to_do", "to_do": {"rich_text": _rich("done"), "checked": True}}
    unchecked = {"type": "to_do", "to_do": {"rich_text": _rich("todo"), "checked": False}}
    assert block_to_markdown(checked) == "- [x] done"
    assert block_to_markdown(unchecked) == "- [ ] todo"


def test_code_block_to_markdown():
    block = {"type": "code", "code": {"rich_text": _rich("print(1)"), "language": "python"}}
    assert block_to_markdown(block) == "```python\nprint(1)\n```"


def test_quote_to_markdown():
    block = {"type": "quote", "quote": {"rich_text": _rich("wise words")}}
    assert block_to_markdown(block) == "> wise words"


def test_unsupported_block_type_is_skipped_not_raised():
    block = {"type": "synced_block", "synced_block": {}}
    assert block_to_markdown(block) == ""


def test_nested_children_are_indented_and_included():
    child = {"type": "paragraph", "paragraph": {"rich_text": _rich("nested")}}
    parent = {
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": _rich("parent")},
        "_children": [child],
    }
    md = block_to_markdown(parent)
    assert "- parent" in md
    assert "  nested" in md


def test_page_to_markdown_includes_title_and_source_url():
    page = NotionPage(id="abc123", title="My Page", url="https://notion.so/abc123", last_edited_time="t1")
    blocks = [{"type": "paragraph", "paragraph": {"rich_text": _rich("body text")}}]
    md = page_to_markdown(page, blocks)
    assert "# My Page" in md
    assert "https://notion.so/abc123" in md
    assert "body text" in md


# --------------------------------------------------------------------------------------
# Idempotent re-sync: unchanged pages are skipped
# --------------------------------------------------------------------------------------


def _page_response(page_id: str, title: str, last_edited: str) -> dict:
    return {
        "id": page_id,
        "url": f"https://notion.so/{page_id}",
        "last_edited_time": last_edited,
        "properties": {"title": {"type": "title", "title": [{"plain_text": title}]}},
    }


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_resync_skips_unchanged_pages(tmp_path, monkeypatch):
    settings = _settings(tmp_path)

    search_response = {
        "results": [_page_response("page1", "Page One", "2026-01-01T00:00:00.000Z")],
        "has_more": False,
    }
    blocks_response = {
        "results": [{"type": "paragraph", "paragraph": {"rich_text": _rich("v1")}, "has_children": False}],
        "has_more": False,
    }

    calls = {"blocks": 0}

    def fake_urlopen(req, timeout=30):
        url = req.full_url
        if "search" in url:
            return _FakeResponse(search_response)
        if "blocks" in url:
            calls["blocks"] += 1
            return _FakeResponse(blocks_response)
        raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setattr("locus.ingest.notion_source.urllib.request.urlopen", fake_urlopen)

    stats1 = sync_notion(settings, token="fake-token")
    assert stats1.pages_written == 1
    assert stats1.pages_skipped == 0
    assert calls["blocks"] == 1

    md_path = settings.data_dir / "sources" / "notion" / "page1.md"
    assert md_path.exists()
    assert "v1" in md_path.read_text()

    # Second sync: last_edited_time unchanged -> page must be skipped, no new block fetch.
    stats2 = sync_notion(settings, token="fake-token")
    assert stats2.pages_written == 0
    assert stats2.pages_skipped == 1
    assert calls["blocks"] == 1  # no additional fetch


def test_resync_rewrites_changed_pages(tmp_path, monkeypatch):
    settings = _settings(tmp_path)

    responses = {
        "count": 0,
    }

    def fake_urlopen(req, timeout=30):
        url = req.full_url
        if "search" in url:
            edited = "2026-01-01T00:00:00.000Z" if responses["count"] == 0 else "2026-02-01T00:00:00.000Z"
            responses["count"] += 1
            return _FakeResponse({"results": [_page_response("page1", "Page One", edited)], "has_more": False})
        if "blocks" in url:
            text = "v1" if responses["count"] <= 1 else "v2"
            return _FakeResponse(
                {
                    "results": [{"type": "paragraph", "paragraph": {"rich_text": _rich(text)}, "has_children": False}],
                    "has_more": False,
                }
            )
        raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setattr("locus.ingest.notion_source.urllib.request.urlopen", fake_urlopen)

    stats1 = sync_notion(settings, token="fake-token")
    assert stats1.pages_written == 1

    stats2 = sync_notion(settings, token="fake-token")
    assert stats2.pages_written == 1
    assert stats2.pages_skipped == 0

    md_path = settings.data_dir / "sources" / "notion" / "page1.md"
    assert "v2" in md_path.read_text()
