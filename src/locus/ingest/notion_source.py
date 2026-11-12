"""Notion ingestion source: OAuth login + page sync into local Markdown cache.

Flow:
    1. `ensure_access_token()` — reuse a stored token, or run a local-loopback OAuth flow
       (same pattern as `gh auth login`): open the browser to Notion's authorize URL, catch
       the redirect on 127.0.0.1, exchange the code for a token, store it.
    2. `sync_notion()` — list every page the integration can see (`/v1/search`), fetch each
       page's blocks recursively (`/v1/blocks/{id}/children`), convert to Markdown, and write
       one `.md` file per page under `{data_dir}/sources/notion/{page_id}.md`. A manifest
       records each page's `last_edited_time` so an unchanged page is skipped on re-sync.

This module only writes plain files to disk. It never touches the vector store directly —
`locus sync-notion` (cli.py) points the existing, unmodified `index_folder()` at the folder
this module writes, so ingestion goes through the one proven parse/chunk/embed/store pipeline.

stdlib-only HTTP (urllib), mirroring the style of `ManagedEmbedder` in `embedder.py`.
"""

from __future__ import annotations

import http.server
import json
import os
import re
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import Settings

NOTION_VERSION = "2022-06-28"
AUTHORIZE_URL = "https://api.notion.com/v1/oauth/authorize"
TOKEN_URL = "https://api.notion.com/v1/oauth/token"
SEARCH_URL = "https://api.notion.com/v1/search"
BLOCKS_URL = "https://api.notion.com/v1/blocks/{block_id}/children"
PAGES_URL = "https://api.notion.com/v1/pages/{page_id}"

# Fixed loopback port: simpler and more reliable to register once in the Notion integration's
# "Redirect URIs" than a dynamic port (which would require registering a wildcard, which Notion
# does not support). Document this exact value in README.md / .env.example.
REDIRECT_PORT = 53682
REDIRECT_URI = f"http://127.0.0.1:{REDIRECT_PORT}/callback"


def _token_file(settings: Settings) -> Path:
    return settings.data_dir / "notion_token.json"


def _notion_folder(settings: Settings) -> Path:
    return settings.data_dir / "sources" / "notion"


def _manifest_file(settings: Settings) -> Path:
    return _notion_folder(settings) / ".sync_manifest.json"


# --------------------------------------------------------------------------------------
# OAuth (local loopback server, same pattern as `gh auth login`)
# --------------------------------------------------------------------------------------


class _CallbackResult:
    """Shared box the loopback HTTP handler drops the auth code (or error) into."""

    def __init__(self) -> None:
        self.code: str | None = None
        self.error: str | None = None
        self.event = threading.Event()


def _make_handler(expected_state: str, result: _CallbackResult) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - required name by http.server
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            params = urllib.parse.parse_qs(parsed.query)
            state = params.get("state", [None])[0]
            if state != expected_state:
                result.error = "state mismatch (possible CSRF) — aborting"
            elif "error" in params:
                result.error = params["error"][0]
            else:
                result.code = params.get("code", [None])[0]
                if not result.code:
                    result.error = "no authorization code in redirect"

            body = (
                b"<html><body>Locus: Notion authorization complete. You can close this tab.</body></html>"
                if not result.error
                else f"<html><body>Locus: Notion authorization failed: {result.error}</body></html>".encode()
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            result.event.set()

        def log_message(self, *args: Any) -> None:  # silence default stdout logging
            pass

    return Handler


def _run_oauth_flow(client_id: str, client_secret: str, timeout_s: float = 180.0) -> str:
    """Runs the local-loopback OAuth flow and returns a Notion access token."""
    state = secrets.token_urlsafe(24)
    result = _CallbackResult()
    handler_cls = _make_handler(state, result)
    server = http.server.HTTPServer(("127.0.0.1", REDIRECT_PORT), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        query = urllib.parse.urlencode(
            {
                "client_id": client_id,
                "redirect_uri": REDIRECT_URI,
                "response_type": "code",
                "owner": "user",
                "state": state,
            }
        )
        authorize_url = f"{AUTHORIZE_URL}?{query}"
        print(f"Opening browser to authorize Locus with Notion:\n  {authorize_url}")
        opened = webbrowser.open(authorize_url)
        if not opened:
            print("Could not open a browser automatically — open the URL above manually.")

        if not result.event.wait(timeout=timeout_s):
            raise RuntimeError("Timed out waiting for the Notion OAuth redirect.")
    finally:
        server.shutdown()
        thread.join(timeout=5)

    if result.error:
        raise RuntimeError(f"Notion OAuth failed: {result.error}")
    assert result.code is not None

    return _exchange_code_for_token(result.code, client_id, client_secret)


def _exchange_code_for_token(code: str, client_id: str, client_secret: str) -> str:
    body = json.dumps({"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI}).encode()
    credentials = f"{client_id}:{client_secret}".encode()
    import base64

    req = urllib.request.Request(
        TOKEN_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Basic {base64.b64encode(credentials).decode()}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise RuntimeError(f"Notion token exchange failed: HTTP {exc.code} {detail}") from exc
    token = payload.get("access_token")
    if not token:
        raise RuntimeError(f"Notion token exchange response had no access_token: {payload}")
    return str(token)


def ensure_access_token(settings: Settings) -> str:
    """Returns a stored Notion access token, running the OAuth flow first if none exists.

    Mirrors config.py's env-var pattern: NOTION_ACCESS_TOKEN in .env short-circuits everything
    (useful for CI / a token obtained out-of-band). Otherwise we fall back to the on-disk token
    file written by a previous `locus sync-notion` run, and finally to a fresh OAuth flow.
    """
    env_token = os.getenv("NOTION_ACCESS_TOKEN")
    if env_token:
        return env_token

    token_path = _token_file(settings)
    if token_path.exists():
        try:
            data = json.loads(token_path.read_text())
            token = data.get("access_token")
            if token:
                return str(token)
        except (json.JSONDecodeError, OSError):
            pass  # fall through to a fresh OAuth flow

    client_id = os.getenv("NOTION_CLIENT_ID")
    client_secret = os.getenv("NOTION_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise RuntimeError(
            "NOTION_CLIENT_ID / NOTION_CLIENT_SECRET are not set. Create a Notion public "
            "integration at https://www.notion.so/my-integrations and put its OAuth "
            "credentials in .env (see .env.example)."
        )

    token = _run_oauth_flow(client_id, client_secret)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(token_path.parent, 0o700)  # the data dir; see store.py's ChromaStore.__init__ (SEC-020)
    # Create with 0600 atomically (not write-then-chmod): write-then-chmod leaves the
    # token briefly readable at the process umask's default mode (often 0644) between
    # the two calls, on filesystems that support POSIX permissions at all.
    fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    fchmod = getattr(os, "fchmod", None)  # absent on Windows before Python 3.13
    try:
        # os.open's mode applies only when it creates the file: a token file left by an older
        # version (or restored from a backup) keeps its wider mode unless reset on the descriptor.
        if fchmod is not None:
            fchmod(fd, 0o600)
    except OSError:
        os.close(fd)
        raise
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps({"access_token": token}))
    return token


# --------------------------------------------------------------------------------------
# Notion API
# --------------------------------------------------------------------------------------


def _notion_request(token: str, url: str, method: str = "GET", body: dict[str, Any] | None = None) -> dict[str, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise RuntimeError(f"Notion API {method} {url} failed: HTTP {exc.code} {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach the Notion API: {exc.reason}") from exc


@dataclass
class NotionPage:
    id: str
    title: str
    url: str
    last_edited_time: str


def list_pages(token: str) -> list[NotionPage]:
    """Lists every page the integration has been granted access to."""
    pages: list[NotionPage] = []
    body: dict[str, Any] = {"filter": {"value": "page", "property": "object"}, "page_size": 100}
    while True:
        result = _notion_request(token, SEARCH_URL, method="POST", body=body)
        for item in result.get("results", []):
            pages.append(
                NotionPage(
                    id=item["id"],
                    title=_extract_title(item),
                    url=item.get("url", ""),
                    last_edited_time=item.get("last_edited_time", ""),
                )
            )
        if not result.get("has_more"):
            break
        body["start_cursor"] = result["next_cursor"]
    return pages


def _extract_title(page: dict[str, Any]) -> str:
    props = page.get("properties", {})
    for prop in props.values():
        if prop.get("type") == "title":
            rich = prop.get("title", [])
            text = "".join(t.get("plain_text", "") for t in rich)
            if text:
                return text
    return "Untitled"


def fetch_blocks(token: str, block_id: str) -> list[dict[str, Any]]:
    """Fetches a block's children recursively (Notion pages are trees of blocks)."""
    blocks: list[dict[str, Any]] = []
    url = BLOCKS_URL.format(block_id=block_id)
    cursor: str | None = None
    while True:
        full_url = f"{url}?start_cursor={cursor}" if cursor else url
        result = _notion_request(token, full_url)
        for block in result.get("results", []):
            if block.get("has_children"):
                block["_children"] = fetch_blocks(token, block["id"])
            blocks.append(block)
        if not result.get("has_more"):
            break
        cursor = result.get("next_cursor")
    return blocks


# --------------------------------------------------------------------------------------
# Block → Markdown conversion
# --------------------------------------------------------------------------------------


def _rich_text_to_markdown(rich_text: list[dict[str, Any]]) -> str:
    parts = []
    for t in rich_text:
        text = t.get("plain_text", "")
        annotations = t.get("annotations", {})
        if annotations.get("code"):
            text = f"`{text}`"
        if annotations.get("bold"):
            text = f"**{text}**"
        if annotations.get("italic"):
            text = f"*{text}*"
        if annotations.get("strikethrough"):
            text = f"~~{text}~~"
        href = t.get("href")
        if href:
            text = f"[{text}]({href})"
        parts.append(text)
    return "".join(parts)


def block_to_markdown(block: dict[str, Any], indent: int = 0) -> str:
    """Converts one Notion block (plus any nested children) to a Markdown string.

    Covers the common block types; anything unrecognized is skipped gracefully rather than
    raising, since a Notion workspace can contain block types (embeds, synced blocks, columns,
    databases inline, etc.) we don't need to render for search purposes.
    """
    btype = block.get("type")
    data = block.get(btype, {}) if btype else {}
    pad = "  " * indent
    lines: list[str] = []

    if btype == "paragraph":
        text = _rich_text_to_markdown(data.get("rich_text", []))
        if text:
            lines.append(f"{pad}{text}")
    elif btype in ("heading_1", "heading_2", "heading_3"):
        level = int(btype[-1])
        text = _rich_text_to_markdown(data.get("rich_text", []))
        lines.append(f"{pad}{'#' * level} {text}")
    elif btype == "bulleted_list_item":
        text = _rich_text_to_markdown(data.get("rich_text", []))
        lines.append(f"{pad}- {text}")
    elif btype == "numbered_list_item":
        text = _rich_text_to_markdown(data.get("rich_text", []))
        lines.append(f"{pad}1. {text}")
    elif btype == "to_do":
        text = _rich_text_to_markdown(data.get("rich_text", []))
        box = "x" if data.get("checked") else " "
        lines.append(f"{pad}- [{box}] {text}")
    elif btype == "quote":
        text = _rich_text_to_markdown(data.get("rich_text", []))
        lines.append(f"{pad}> {text}")
    elif btype == "code":
        text = "".join(t.get("plain_text", "") for t in data.get("rich_text", []))
        lang = data.get("language", "")
        lines.append(f"{pad}```{lang}\n{text}\n{pad}```")
    elif btype == "divider":
        lines.append(f"{pad}---")
    else:
        # Rare/unsupported block type (embed, table, synced_block, column_list, ...): skip.
        return ""

    for child in block.get("_children", []):
        child_md = block_to_markdown(child, indent + 1)
        if child_md:
            lines.append(child_md)

    return "\n".join(lines)


def page_to_markdown(page: NotionPage, blocks: list[dict[str, Any]]) -> str:
    header = f"# {page.title}\n\n<!-- notion-source: {page.url} -->\n"
    body = "\n\n".join(md for block in blocks if (md := block_to_markdown(block)))
    return f"{header}\n{body}\n"


# --------------------------------------------------------------------------------------
# Sync: Notion -> {data_dir}/sources/notion/*.md, idempotent via a manifest
# --------------------------------------------------------------------------------------


_SAFE_ID_RE = re.compile(r"[^a-zA-Z0-9_-]")


def _page_filename(page_id: str) -> str:
    return f"{_SAFE_ID_RE.sub('', page_id)}.md"


def _load_manifest(settings: Settings) -> dict[str, str]:
    path = _manifest_file(settings)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_manifest(settings: Settings, manifest: dict[str, str]) -> None:
    path = _manifest_file(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True))


@dataclass
class NotionSyncStats:
    pages_seen: int = 0
    pages_written: int = 0
    pages_skipped: int = 0

    def summary(self) -> str:
        return f"seen={self.pages_seen} written={self.pages_written} skipped={self.pages_skipped}"


def sync_notion(settings: Settings, token: str | None = None) -> NotionSyncStats:
    """Fetches every accessible Notion page and writes/updates its Markdown cache file.

    Idempotent: a page whose `last_edited_time` matches the manifest from the previous sync is
    left untouched. Returns stats; does not touch the vector store — callers run `index_folder`
    on the resulting folder separately (see `locus sync-notion` in cli.py).
    """
    token = token or ensure_access_token(settings)
    folder = _notion_folder(settings)
    folder.mkdir(parents=True, exist_ok=True)
    # Page text lands under the data dir before `locus sync-notion` builds the store (whose init
    # also sets 0700), so a first sync that fails midway must not leave it readable by other
    # local accounts (SEC-020).
    os.chmod(settings.data_dir, 0o700)
    manifest = _load_manifest(settings)
    stats = NotionSyncStats()

    pages = list_pages(token)
    for page in pages:
        stats.pages_seen += 1
        if manifest.get(page.id) == page.last_edited_time:
            stats.pages_skipped += 1
            continue

        blocks = fetch_blocks(token, page.id)
        markdown = page_to_markdown(page, blocks)
        (folder / _page_filename(page.id)).write_text(markdown, encoding="utf-8")
        manifest[page.id] = page.last_edited_time
        stats.pages_written += 1

    _save_manifest(settings, manifest)
    return stats
