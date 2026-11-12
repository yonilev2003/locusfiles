"""Runtime settings for Locus. Read once from the environment / .env via get_settings()."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import find_dotenv, load_dotenv

# What `locus index` picks up unless LOCUS_EXTENSIONS says otherwise. parsers.py can read a few more
# (.xml, .ini, .cfg); those are opt-in. Everything from .pptx down arrived
# in engine 0.1.2.
DEFAULT_EXTENSIONS: tuple[str, ...] = (
    ".md",
    ".txt",
    ".rst",
    ".pdf",
    ".docx",
    ".py",
    ".js",
    ".ts",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".csv",
    ".html",
    ".pptx",
    ".xlsx",
    ".odt",
    ".ods",
    ".odp",
    ".epub",
    ".rtf",
    ".markdown",
    ".mdx",
    ".org",
    ".tex",
    ".tsv",
    ".ipynb",
    # W2: plain-text source and subtitles; parsers.py guards against lockfiles, minified and generated files.
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
    ".tsx",
    ".jsx",
    ".mjs",
    ".mts",
    ".cjs",
    ".log",
)

# Directories that are never walked.
SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".locus",
        ".idea",
        ".vscode",
        "dist",
        "build",
        ".cache",
        ".Trash",
    }
)


def _normalize_ext(raw: str) -> str:
    """'.txt' and 'txt' both become '.txt' — Path.suffix always includes the leading dot,
    so a bare 'txt,md' in LOCUS_EXTENSIONS used to match nothing and silently index zero files."""
    e = raw.strip().lower()
    return e if e.startswith(".") else f".{e}"


# OPS-007: the one place the engine learns which host serves Locus's API. Every hosted URL the
# engine calls (embed relay, relay token, account status) and every Locus link it prints (the
# remote-MCP connector URL, the billing page) derives from LOCUS_API_BASE. Nothing else in src/
# may hard-code a Locus host; tests/test_config.py greps for it.
#
# The default mirrors locus-web/lib/site.ts's SITE_URL fallback. Since 0.1.1 it is the permanent
# domain (runbook D19, after the 10-01 cutover; https://locusfiles.com/api/health answered 200 on
# 2026-10-01). 0.1.0 shipped https://locus-rwhy.vercel.app, so keep that alias attached and serving
# /api/* indefinitely: every engine installed before 0.1.1 keeps calling it (lib/canonical-host.ts
# never redirects /api/*). Change this and lib/site.ts's fallback together
# (tests/test_domain_cutover_parity.py).
DEFAULT_API_BASE = "https://locusfiles.com"

# The on-device model when LOCUS_LOCAL_MODEL is not set (English-only bge-small).
DEFAULT_LOCAL_MODEL = "BAAI/bge-small-en-v1.5"

# Paths under the API base. They are locus-web routes (locus-web/app/...), not configuration.
EMBED_RELAY_PATH = "/api/embed-relay"
RELAY_TOKEN_PATH = "/api/relay/token"
MCP_RELAY_PATH = "/api/mcp/relay"
ACCOUNT_STATUS_PATH = "/api/support/account-status"
BILLING_PATH = "/account/billing"

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def normalize_api_base(raw: str | None) -> str:
    """LOCUS_API_BASE (or the default) as `scheme://host[:port][/prefix]`, without a trailing slash.

    https only, except plain http to a loopback host for local relay development: the managed API
    key travels in every request's Authorization header, so it must never cross a network in clear.
    """
    value = (raw or "").strip() or DEFAULT_API_BASE
    base = value.rstrip("/")
    parts = urlsplit(base)
    # "?" / "#" are refused even when empty: "https://x?" would yield "https://x?/api/embed-relay",
    # which silently requests the site root (review F9).
    if parts.scheme not in ("https", "http") or not parts.hostname or "?" in base or "#" in base:
        raise RuntimeError(f"LOCUS_API_BASE={value!r} is not a base URL such as https://example.com")
    if parts.scheme == "http" and parts.hostname not in _LOOPBACK_HOSTS:
        raise RuntimeError(
            f"LOCUS_API_BASE={value!r} must use https (plain http is allowed only for a localhost relay in development)"
        )
    return base


def api_url(base: str, path: str) -> str:
    """A locus-web URL under *base* (already normalized by normalize_api_base)."""
    return f"{base}{path}"


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    collection: str
    embedder: str  # voyage | openai | local | managed | hash
    voyage_model: str
    openai_model: str
    local_model: str
    chunk_tokens: int
    overlap_tokens: int
    max_file_mb: float
    extensions: tuple[str, ...]
    managed_api_key: str | None = None
    # True when LOCUS_CHUNK_TOKENS is set: it then wins over a collection's recorded chunk size (ADR-087).
    chunk_tokens_explicit: bool = False
    api_base: str = DEFAULT_API_BASE
    managed_relay_url: str = api_url(DEFAULT_API_BASE, EMBED_RELAY_PATH)
    mcp_token: str | None = None
    mcp_relay_token_url: str = api_url(DEFAULT_API_BASE, RELAY_TOKEN_PATH)
    mcp_relay_supabase_url: str = "https://dmtybqtqvnagwcffhuzr.supabase.co"
    mcp_relay_supabase_anon_key: str | None = None
    # SEC-021: pin the relay's request-signing public keys (comma-separated) instead of using the ones
    # /api/relay/token publishes. For a staging relay or a cautious user; unset in production.
    mcp_relay_verify_keys: str | None = None
    support_account_status_url: str = api_url(DEFAULT_API_BASE, ACCOUNT_STATUS_PATH)
    # Not called by the engine: the remote-MCP connector URL a user adds in a web AI app (served by
    # locus-web), and the page a budget or billing stop points to.
    mcp_relay_url: str = api_url(DEFAULT_API_BASE, MCP_RELAY_PATH)
    billing_url: str = api_url(DEFAULT_API_BASE, BILLING_PATH)
    # Set when LOCUS_API_BASE is invalid. The URLs derived from it are then "" (never a URL a request
    # could go to), and the error is raised only where one is used (hosted_url): a bad optional
    # variable must not break local-only commands (`locus stats`, `locus serve`'s local tools).
    api_base_error: str | None = None

    def hosted_url(self, url: str) -> str:
        """*url* (one of the hosted-URL fields above), or the LOCUS_API_BASE configuration error
        when it could not be derived. Call it wherever a hosted Locus URL is about to be used."""
        if url:
            return url
        raise RuntimeError(self.api_base_error or "No Locus API URL is configured (set LOCUS_API_BASE).")


# src/locus/config.py -> the repo root when running from a source checkout, else None. An installed
# engine (pipx/uv/venv) must not pick up whatever .env sits above site-packages (e.g. ~/.env).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SOURCE_CHECKOUT: Path | None = (
    _REPO_ROOT
    if (_REPO_ROOT / "pyproject.toml").is_file() and (_REPO_ROOT / "src" / "locus" / "config.py").is_file()
    else None
)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    load_dotenv(find_dotenv(usecwd=True))  # .env in the current dir or its parents
    if _SOURCE_CHECKOUT:  # fallback for editable installs only: never walk up from site-packages (F4)
        load_dotenv(_SOURCE_CHECKOUT / ".env")
    exts = os.getenv("LOCUS_EXTENSIONS")
    base_error: str | None = None
    try:
        base = normalize_api_base(os.getenv("LOCUS_API_BASE"))
    except RuntimeError as exc:
        base, base_error = "", str(exc)

    def hosted(path: str) -> str:
        return api_url(base, path) if base else ""

    return Settings(
        data_dir=Path(os.getenv("LOCUS_DATA_DIR", str(Path.home() / ".locus"))).expanduser(),
        collection=os.getenv("LOCUS_COLLECTION", "locus"),
        embedder=os.getenv("LOCUS_EMBEDDER", "local").strip().lower(),
        voyage_model=os.getenv("LOCUS_VOYAGE_MODEL", "voyage-4"),
        openai_model=os.getenv("LOCUS_OPENAI_MODEL", "text-embedding-3-small"),
        local_model=os.getenv("LOCUS_LOCAL_MODEL", DEFAULT_LOCAL_MODEL),
        managed_api_key=os.getenv("LOCUS_API_KEY"),
        api_base=base,
        api_base_error=base_error,
        # A per-URL override (local relay development and tests) still wins over LOCUS_API_BASE.
        managed_relay_url=os.getenv("LOCUS_RELAY_URL") or hosted(EMBED_RELAY_PATH),
        mcp_token=os.getenv("LOCUS_MCP_TOKEN"),
        mcp_relay_token_url=os.getenv("LOCUS_MCP_RELAY_TOKEN_URL") or hosted(RELAY_TOKEN_PATH),
        mcp_relay_supabase_url=os.getenv("LOCUS_MCP_RELAY_SUPABASE_URL", "https://dmtybqtqvnagwcffhuzr.supabase.co"),
        mcp_relay_supabase_anon_key=os.getenv("LOCUS_MCP_RELAY_SUPABASE_ANON_KEY"),
        mcp_relay_verify_keys=os.getenv("LOCUS_RELAY_VERIFY_KEYS"),
        support_account_status_url=os.getenv("LOCUS_SUPPORT_ACCOUNT_STATUS_URL") or hosted(ACCOUNT_STATUS_PATH),
        mcp_relay_url=hosted(MCP_RELAY_PATH),
        billing_url=hosted(BILLING_PATH),
        chunk_tokens=int(os.getenv("LOCUS_CHUNK_TOKENS", "700")),
        chunk_tokens_explicit="LOCUS_CHUNK_TOKENS" in os.environ,
        overlap_tokens=int(os.getenv("LOCUS_OVERLAP_TOKENS", "100")),
        max_file_mb=float(os.getenv("LOCUS_MAX_FILE_MB", "25")),
        extensions=tuple(_normalize_ext(e) for e in exts.split(",") if e.strip()) if exts else DEFAULT_EXTENSIONS,
    )
