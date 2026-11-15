"""Typed embedding failures (RET-008, master plan Ch.6 "unified rate-limit and error taxonomy").

Every embedder maps its failures onto these classes, so the indexer and the watcher decide what to
do from the class alone and never from a provider's exception name or HTTP text:

| class                 | indexer behaviour                                                        |
|-----------------------|--------------------------------------------------------------------------|
| RateLimited           | wait (at least `retry_after`) and retry the same batch, bounded; then one |
|                       | test request: skip the file if it passes, else stop (retry.py, indexer.py)|
| ProviderUnavailable   | retry a few times with backoff; then as RateLimited. `paused=True`, or a  |
|                       | `retry_after` longer than the retry cap: stop at once                     |
| BudgetExhausted       | stop the run once, with a resumable summary; no retries                   |
| SubscriptionInactive  | stop the run once                                                        |
| AuthFailed            | stop the run once                                                        |
| InputRejected         | this file fails; the run goes on (retrying the same request cannot help)  |
| ClientOutdated        | stop the run once: the relay needs a newer Locus (426, OPS-007)           |

The managed relay's error body is `{"error", "code", "reason"?, "retry_after"?}` (the relay's error contract). `code`
picks the class; `reason` only words the message. Relays deployed before that contract send only
`{"error"}`, so `from_relay_http` also recognises today's refusal texts by HTTP status and wording.

All classes subclass RuntimeError, so callers that already print a RuntimeError as a one-line
configuration error (cli.main, mcp_server) keep working unchanged.
"""

from __future__ import annotations

import email.utils
import json
import math
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from .. import __version__


class EmbedError(RuntimeError):
    """Base class. `retryable`: waiting and sending the same request again can succeed.
    `stops_run`: once this reaches the indexing loop (after any retries), later requests in the run
    would likely fail the same way, so the run stops instead of failing file by file — at once, or,
    for a failure that may be one request's own (RetryPolicy.could_be_request_specific), when a
    one-line test request fails too (retry.probe_service).
    `splittable`: a smaller request may get through where this one failed (a per-request or
    per-minute token limit), so retry.embed_with_retry may halve the batch (re-review NEW-3)."""

    kind = "embed_error"
    stops_run = True
    retryable = False
    splittable = False

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        reason: str | None = None,
        request_id: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.reason = reason
        self.request_id = request_id
        self.retry_after = retry_after


class RateLimited(EmbedError):
    """Too many requests right now; the same request can succeed after `retry_after` seconds."""

    kind = "rate_limited"
    retryable = True
    splittable = True  # a tokens-per-minute limit: half the tokens may fit (after the wait)


class ProviderUnavailable(EmbedError):
    """The relay or the embedding provider failed or could not be reached. `paused`: an operator
    switch is on (FIN-012 kill switch); retrying inside this run would only hammer it.
    `provider_refused`: the provider answered this request with a refusal (the relay's 502
    "Embedding provider error", reason provider_error; it also covers a Voyage 400 over a
    per-request limit), the one case a smaller request can get past. A timeout, a dropped or
    refused connection, "provider unreachable" or an unreadable answer is no verdict on the
    request: halving it cannot help, and the relay keeps such a request's reservation counted
    because it cannot tell whether the provider billed it, so halving only multiplied what an
    outage reserved (re-review NEW-3)."""

    kind = "provider_unavailable"

    def __init__(self, message: str, *, paused: bool = False, provider_refused: bool = False, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.paused = paused
        self.provider_refused = provider_refused

    @property
    def retryable(self) -> bool:  # type: ignore[override]
        return not self.paused

    @property
    def splittable(self) -> bool:  # type: ignore[override]
        return self.provider_refused and not self.paused


class BudgetExhausted(EmbedError):
    """A token or request budget is used up. `scope` says which (trial, daily, daily_search,
    account_daily, provider_quota, unknown); `resets_at` is when it refills (None: it does not,
    e.g. a trial budget); `upgrade_url` is where the user can change plan."""

    kind = "budget_exhausted"

    def __init__(
        self,
        message: str,
        *,
        scope: str = "unknown",
        resets_at: datetime | None = None,
        upgrade_url: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.scope = scope
        self.resets_at = resets_at
        self.upgrade_url = upgrade_url


class SubscriptionInactive(EmbedError):
    """No active subscription, or the trial period ended."""

    kind = "subscription_inactive"

    def __init__(self, message: str, *, upgrade_url: str | None = None, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.upgrade_url = upgrade_url


class AuthFailed(EmbedError):
    """The API key is missing, invalid or revoked."""

    kind = "auth_failed"


class InputRejected(EmbedError):
    """The request itself was refused (too long, malformed, unsupported). A Locus bug or a version
    mismatch, not something the user caused; only this file fails."""

    kind = "input_rejected"
    stops_run = False


# How to update Locus, for both install channels (the relay's own 426 text names the same two).
UPDATE_COMMANDS = "`uv tool upgrade locusfiles` (or `pipx upgrade locusfiles`)"


class ClientOutdated(EmbedError):
    """The relay requires a newer Locus than this one (HTTP 426, code client_outdated,
    set by the operator's MIN_CLIENT_VERSION). Waiting or retrying
    cannot help; updating can. `detail` is the relay's own text, which names the minimum version."""

    kind = "client_outdated"

    def __init__(self, message: str, *, detail: str | None = None, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.detail = detail


def client_outdated_message(status: int, detail: str | None, *, service: str = "the Locus relay") -> str:
    """The "update Locus" text for a 426 / client_outdated answer, from the embed relay or the relay
    token endpoint alike. Adds the update commands when the relay's text does not already have them."""
    message = f"Update Locus: {service} does not accept this version ({__version__}) any more (HTTP {status})."
    if detail:
        message += f" The relay says: {detail.rstrip('.')}."
    if "upgrade locusfiles" not in (detail or ""):
        message += f" Run {UPDATE_COMMANDS}, then restart Locus."
    return message


# --- the managed relay (its /api/embed-relay route) ---------------------------------------------

_BUDGET_SCOPES = {
    "trial_token_budget_exceeded": "trial",
    "daily_quota_exceeded": "daily",
    "daily_query_cap_exceeded": "daily_search",
    "daily_query_token_cap_exceeded": "daily_search",
    "account_daily_ceiling": "account_daily",
}

_KNOWN_CODES = frozenset(
    {
        "auth_failed",
        "input_rejected",
        "subscription_inactive",
        "budget_exhausted",
        "rate_limited",
        "paused",
        "provider_unavailable",
        "client_outdated",
        "internal_error",
    }
)


def _printable(value: object, limit: int = 200) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = "".join(ch for ch in value if ch.isprintable()).strip()[:limit]
    return cleaned or None


def _header(headers: object, name: str) -> str | None:
    """Case-insensitive header lookup on an http.client.HTTPMessage, an httpx.Headers or a dict."""
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    if getter is not None:
        for key in (name, name.lower(), name.title()):
            value = getter(key)
            if value is not None:
                return str(value)
    if isinstance(headers, Mapping):
        for key, value in headers.items():
            if isinstance(key, str) and key.lower() == name.lower():
                return str(value)
    return None


# Longest Retry-After taken at face value. A server value beyond it (a typo, 1e12 s, JSON 1e400) is
# clamped, so date arithmetic on it cannot overflow and turn a typed stop into an untyped error.
MAX_RETRY_AFTER_SECONDS = 7 * 24 * 3600.0


def _usable_seconds(seconds: float) -> float | None:
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return min(seconds, MAX_RETRY_AFTER_SECONDS)


def parse_retry_after(value: object, now: datetime | None = None) -> float | None:
    """Seconds from a Retry-After value: a number of seconds or an HTTP date, at most
    MAX_RETRY_AFTER_SECONDS. None if unusable (negative, not finite, unparseable)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        try:
            return _usable_seconds(float(value))
        except OverflowError:  # an int too large for a float
            return MAX_RETRY_AFTER_SECONDS if value > 0 else None
    text = str(value).strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(text)
        except (TypeError, ValueError, OverflowError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - (now or datetime.now(UTC))).total_seconds()
        return _usable_seconds(max(0.0, seconds))
    return _usable_seconds(seconds)


def next_utc_midnight(now: datetime) -> datetime:
    day = now.astimezone(UTC).date() + timedelta(days=1)
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def _parse_body(raw: bytes | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def from_relay_http(
    status: int,
    body: bytes | None,
    headers: object = None,
    *,
    relay_url: str = "",
    upgrade_url: str | None = None,
    now: datetime | None = None,
) -> EmbedError:
    """Map one non-2xx answer from the embed relay onto a typed error.

    The structured `code` wins when present; otherwise (a relay deployed before the contract, or a
    platform error page) the HTTP status and today's refusal wording decide.
    """
    now = now or datetime.now(UTC)
    data = _parse_body(body)
    detail = _printable(data.get("error"))
    code = data.get("code") if data.get("code") in _KNOWN_CODES else None
    reason = _printable(data.get("reason"), 64)
    request_id = _printable(_header(headers, "x-request-id"), 64)
    retry_after = parse_retry_after(data.get("retry_after"), now)
    if retry_after is None:
        retry_after = parse_retry_after(_header(headers, "Retry-After"), now)
    common: dict[str, Any] = {"status": status, "code": code, "reason": reason, "request_id": request_id}
    ref = f" (request id {request_id})" if request_id else ""
    lower = (detail or "").lower()

    if code is None:
        # Pre-contract relay: classify from status and today's exact refusal texts (route.ts).
        if status == 401:
            code = "auth_failed"
        elif status in (402, 403):
            if "trial token budget" in lower:
                code, reason = "budget_exhausted", reason or "trial_token_budget_exceeded"
            else:
                code = "subscription_inactive"
                if "trial expired" in lower:
                    reason = reason or "trial_expired"
        elif status == 429:
            if "daily embedding quota" in lower:
                code, reason = "budget_exhausted", reason or "daily_quota_exceeded"
            elif "daily search limit" in lower:
                code, reason = "budget_exhausted", reason or "daily_query_cap_exceeded"
            else:
                code = "rate_limited"
        elif status in (400, 413, 422):
            code = "input_rejected"
        elif status == 426:  # Upgrade Required: whatever sent it, an update is the fix
            code = "client_outdated"
        elif status in (404, 405):
            return EmbedError(
                f"No Locus embed relay at {relay_url or 'the configured URL'} (HTTP {status}) — "
                "check LOCUS_API_BASE / LOCUS_RELAY_URL in .env.",
                **common,
            )
        elif status in (408, 425, 503):  # request timeout / too early / unavailable: transient
            code = "provider_unavailable"
        elif status >= 500:
            code = "provider_unavailable" if status != 500 else "internal_error"
        else:
            code = "input_rejected"
        common["code"] = code
        common["reason"] = reason

    if code == "client_outdated":
        return ClientOutdated(client_outdated_message(status, detail), detail=detail, **common)
    if code == "auth_failed":
        return AuthFailed("LOCUS_API_KEY was rejected (invalid or revoked) — check your dashboard.", **common)
    if code == "subscription_inactive":
        if detail:
            message = f"Locus embed relay refused the request (HTTP {status}): {detail}"
        else:
            where = f" at {upgrade_url}" if upgrade_url else " in your dashboard"
            message = f"Your Locus subscription isn't active — check billing{where}."
        return SubscriptionInactive(message, upgrade_url=upgrade_url, **common)
    if code == "budget_exhausted":
        scope = _BUDGET_SCOPES.get(reason or "", "unknown")
        resets_at: datetime | None = None
        if retry_after is not None:
            try:
                resets_at = now + timedelta(seconds=retry_after)
            except (OverflowError, ValueError):  # a `now` near datetime.max; never fail open here
                resets_at = None
        if resets_at is None and scope in ("daily", "daily_search", "account_daily"):
            resets_at = next_utc_midnight(now)  # the relay's daily budgets reset at 00:00 UTC
        prefix = "limit reached" if status == 429 else "refused the request"
        message = f"Locus embed relay {prefix} (HTTP {status}): {detail or 'the budget for this account is used up.'}"
        return BudgetExhausted(
            message, scope=scope, resets_at=resets_at, upgrade_url=upgrade_url, retry_after=retry_after, **common
        )
    if code == "rate_limited":
        message = f"Locus embed relay is rate-limiting requests (HTTP {status}): {detail or 'too many requests.'}"
        return RateLimited(message, retry_after=retry_after, **common)
    if code == "paused":
        message = f"Managed embedding is paused by Locus right now (HTTP {status}): {detail or 'try again later.'}{ref}"
        return ProviderUnavailable(message, paused=True, retry_after=retry_after, **common)
    if code == "input_rejected":
        message = (
            f"Locus embed relay rejected the request (HTTP {status}): {detail or 'invalid request'}. "
            f"Update Locus; if it persists, report it{ref}."
        )
        return InputRejected(message, **common)
    # provider_unavailable / internal_error
    suffix = f": {detail}" if detail else ""
    refused = code == "provider_unavailable" and (reason == "provider_error" or lower == "embedding provider error")
    return ProviderUnavailable(
        f"Locus embed relay returned HTTP {status}{suffix}.{ref}",
        provider_refused=refused,
        retry_after=retry_after,
        **common,
    )


# --- provider SDKs (the user's own Voyage or OpenAI key) ----------------------------------------

_SDK_RATE_LIMIT = frozenset({"RateLimitError", "RateLimitException"})
_SDK_AUTH = frozenset({"AuthenticationError", "PermissionDeniedError"})
_SDK_UNAVAILABLE = frozenset(
    {
        "APIConnectionError",
        "APITimeoutError",
        "Timeout",
        "TryAgain",
        "ServiceUnavailableError",
        "ServerError",
        "InternalServerError",
        "APIError",
    }
)
_SDK_INPUT = frozenset({"InvalidRequestError", "MalformedRequestError", "BadRequestError", "UnprocessableEntityError"})


def _sdk_code(exc: BaseException) -> str | None:
    code = getattr(exc, "code", None)
    if isinstance(code, str):
        return code
    body = getattr(exc, "body", None) or getattr(exc, "json_body", None)
    if isinstance(body, dict):
        inner = body.get("error", body)
        if isinstance(inner, dict) and isinstance(inner.get("code"), str):
            return inner["code"]
    return None


def _sdk_headers(exc: BaseException) -> object:
    headers = getattr(exc, "headers", None)
    if headers:
        return headers
    response = getattr(exc, "response", None)
    return getattr(response, "headers", None)


def from_provider_exception(exc: BaseException, provider: str = "the embedding provider") -> EmbedError | None:
    """Map a provider SDK exception (voyageai, openai) by class name, so this module never imports an
    optional SDK. None when the exception is not a recognised provider failure."""
    if isinstance(exc, EmbedError):
        return exc
    name = type(exc).__name__
    text = _printable(str(exc), 300) or name
    status = getattr(exc, "http_status", None) or getattr(exc, "status_code", None)
    status = status if isinstance(status, int) else None
    if name in _SDK_RATE_LIMIT:
        if _sdk_code(exc) == "insufficient_quota":
            return BudgetExhausted(
                f"Your {provider} account has no quota left ({text}). Check billing with {provider}.",
                scope="provider_quota",
                status=status,
            )
        if "payment method" in text.lower():
            # Voyage's 429 for an account with no card: it stays throttled to a few requests and
            # tokens per minute until one is added, so backing off only delays the same stop (review
            # F8; the original incident: 230 near-identical errors on a 239-file vault).
            return BudgetExhausted(
                f"{provider} limits this account until a payment method is added ({text}). Add one "
                f"with {provider}, or embed on this computer with LOCUS_EMBEDDER=local and a new "
                "LOCUS_COLLECTION.",
                scope="provider_quota",
                status=status,
            )
        retry_after = parse_retry_after(_header(_sdk_headers(exc), "Retry-After"))
        return RateLimited(
            f"{provider} is rate-limiting requests ({name}: {text})", retry_after=retry_after, status=status
        )
    if name in _SDK_AUTH:
        return AuthFailed(f"{provider} rejected the API key ({name}: {text}). Check the key in .env.", status=status)
    if name in _SDK_UNAVAILABLE:
        return ProviderUnavailable(f"{provider} is unavailable ({name}: {text})", status=status)
    if name in _SDK_INPUT:
        return InputRejected(f"{provider} rejected the request ({name}: {text})", status=status)
    return None


def as_embed_error(exc: BaseException) -> EmbedError | None:
    """`exc` itself when it is typed, a typed equivalent of a known provider exception, else None."""
    if isinstance(exc, EmbedError):
        return exc
    return from_provider_exception(exc)
