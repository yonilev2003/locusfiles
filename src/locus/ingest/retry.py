"""Bounded exponential backoff with jitter around embedding calls (RET-008).

Only `retryable` typed errors (errors.py) are retried: RateLimited up to `rate_limited_attempts`
tries, a non-paused ProviderUnavailable up to `unavailable_attempts`. Every wait is at least the
server's `retry_after`; a `retry_after` longer than `max_delay` is not waited out inside the run —
the error is raised so the run stops cleanly and can be resumed later. Retries happen per request
batch, so a failure in batch 3 of a file never re-sends batches 1 and 2.

When a batch still fails after its retries with an error a smaller request can get past
(`RetryPolicy.may_split`: a rate limit such as a tokens-per-minute limit, or the provider refusing
the request itself over a per-request size limit, which today's relay reports as a 502 "Embedding
provider error"), the batch is halved and re-sent, up to `max_splits` times per file, with one try
per halved request, each after the wait a rate limit calls for (`RetryPolicy.split_wait`: at least
its `retry_after`). Timeouts, unreachable providers and other outages are never halved.

What still fails is raised, and the indexer decides whether it is this file's problem or the whole
run's: it sends one short fixed text (`probe_service`). If that goes through, the service is
answering and the file's own request is the problem (the file is skipped, the run goes on); if it
fails too, the service is the problem and the run stops (or the watcher pauses).
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .embedder import Embedder
from .errors import EmbedError, ProviderUnavailable, RateLimited, as_embed_error

Logger = Callable[[str], None]


@dataclass(frozen=True)
class RetryPolicy:
    rate_limited_attempts: int = 5  # total tries per batch, first one included (plan Ch.6 RET-008: max 5)
    unavailable_attempts: int = 3
    base_delay: float = 2.0  # seconds before the 2nd try; doubles each time
    # Longest single wait. Equals the relay's MAX_PROVIDER_RETRY_AFTER_SECONDS (lib/relay-errors.ts):
    # beyond it the engine should stop and let the user resume, not sit silently for longer.
    max_delay: float = 300.0
    # After a batch's retries run out on an error that may be the request's own fault: halve it and
    # re-send, at most this many times per file (128 texts → 8: the master plan's 128 × 700-token
    # batch against a 10K tokens-per-minute account needs four halvings), one try per halved request.
    max_splits: int = 4
    split_attempts: int = 1
    sleep: Callable[[float], None] = field(default=time.sleep, compare=False)
    jitter: Callable[[], float] = field(default=random.random, compare=False)

    def attempts_for(self, err: EmbedError) -> int:
        return self.rate_limited_attempts if isinstance(err, RateLimited) else self.unavailable_attempts

    def delay(self, attempt: int, retry_after: float | None) -> float | None:
        """Seconds to wait after failed try number *attempt* (1-based); None = do not wait, stop."""
        if retry_after is not None and retry_after > self.max_delay:
            return None
        backoff = min(self.max_delay, self.base_delay * (2 ** (attempt - 1)))
        jittered = backoff * (0.5 + self.jitter() / 2)  # "equal jitter": half fixed, half random
        return min(self.max_delay, max(jittered, retry_after or 0.0))

    def split_wait(self, err: EmbedError) -> float:
        """Seconds to wait before a halved re-send after *err* (re-review NEW-2): at least the
        server's `retry_after`, and a backoff after a rate limit that named no wait (a per-minute
        token limit has to roll over before even a smaller request fits). 0 after a provider
        that refused the request itself: waiting does not make a request smaller."""
        if not isinstance(err, RateLimited) and err.retry_after is None:
            return 0.0
        return self.delay(1, err.retry_after) or 0.0

    def may_split(self, err: EmbedError) -> bool:
        """True when halving the batch and re-sending may get past *err*: a request-specific failure
        that is `splittable` (errors.py). Timeouts, unreachable providers and other outages are not:
        a smaller request cannot fix them, and each halved request is one more reservation the relay
        may keep counted (re-review NEW-3)."""
        return err.splittable and self.could_be_request_specific(err)

    def could_be_request_specific(self, err: EmbedError) -> bool:
        """True when *err*, after its retries, may still be caused by the request that got it rather
        than by the account or the service: a rate limit or a (non-paused) provider failure that did
        not come with a wait longer than `max_delay`. Budget, billing, key and pause refusals, and a
        server that asked for a long wait, concern every request and stop the run at once."""
        if not isinstance(err, RateLimited | ProviderUnavailable) or not err.retryable:
            return False
        return err.retry_after is None or err.retry_after <= self.max_delay


DEFAULT_RETRY = RetryPolicy()
NO_RETRY = RetryPolicy(rate_limited_attempts=1, unavailable_attempts=1, max_splits=0)

# What probe_service sends: short, fixed, and never refused for its content or size.
PROBE_TEXT = "Locus connection check."


def probe_service(embedder: Embedder, err: EmbedError, policy: RetryPolicy = DEFAULT_RETRY) -> EmbedError | None:
    """After a request-specific failure *err* (`policy.could_be_request_specific`) outlasted its
    retries on one file: is it that file's request or the service? Sends PROBE_TEXT once, with no
    retry. None: the service embedded it, so the file's request is the problem — skip that file and
    go on. Otherwise the error to stop (or pause) with: the probe's own refusal when it is run-wide
    (budget, billing, key, operator pause, a long wait — it says more than the file's error), else
    *err*.

    Re-review NEW-1: a streak of "three different files in a row" stood in for this test before.
    Files skipped as unchanged send nothing and did not break the streak, so a few always-failing
    files anywhere in a folder stopped every re-run (and --prune with it) and paused `locus watch`
    every 15 min. Probing on each such failure also stops an outage after one file's retries rather
    than three files' worth of reservations (NEW-3)."""
    try:
        embedder.embed_documents([PROBE_TEXT])
    except Exception as exc:  # noqa: BLE001 - any failure means the service did not answer
        probed = as_embed_error(exc)
        if probed is not None and probed.stops_run and not policy.could_be_request_specific(probed):
            return probed
        return err
    return None


def _embed_batch(
    embedder: Embedder,
    batch: list[str],
    policy: RetryPolicy,
    log: Logger | None,
    on_retry: Callable[[], None] | None,
    attempts: int | None = None,
) -> list[list[float]]:
    """One request batch with bounded backoff; `attempts` overrides the policy's try count."""
    attempt = 1
    while True:
        try:
            return embedder.embed_documents(batch)
        except Exception as exc:
            typed = as_embed_error(exc)
            if typed is None:
                raise
            limit = attempts if attempts is not None else policy.attempts_for(typed)
            wait = policy.delay(attempt, typed.retry_after) if typed.retryable and attempt < limit else None
            if wait is None:
                if typed is exc:
                    raise
                raise typed from exc
            if log is not None:
                what = "rate-limited" if isinstance(typed, RateLimited) else "embedding service unavailable"
                log(f"{what}; waiting {wait:.0f} s before try {attempt + 1}/{limit}")
            if on_retry is not None:
                on_retry()
            policy.sleep(wait)
            attempt += 1


def embed_with_retry(
    embedder: Embedder,
    texts: list[str],
    policy: RetryPolicy = DEFAULT_RETRY,
    log: Logger | None = None,
    on_retry: Callable[[], None] | None = None,
) -> list[list[float]]:
    """embedder.embed_documents(texts), one provider-sized batch at a time, retrying each batch and
    halving a batch that keeps failing (see the module docstring).

    Raises the typed error (never the raw SDK exception, when it is a recognised provider failure)
    once retries and halvings run out or the error is not retryable.
    """
    size = getattr(embedder, "batch_size", None)
    size = size if isinstance(size, int) and size > 0 else max(len(texts), 1)
    out: list[list[float]] = []
    splits = 0
    just_split = False
    start = 0
    while start < len(texts):
        batch = texts[start : start + size]
        try:
            vectors = _embed_batch(
                embedder, batch, policy, log, on_retry, policy.split_attempts if just_split else None
            )
        except EmbedError as err:
            if len(batch) > 1 and splits < policy.max_splits and policy.may_split(err):
                splits += 1
                size = (len(batch) + 1) // 2
                just_split = True
                wait = policy.split_wait(err)
                if log is not None:
                    after = f" after {wait:.0f} s" if wait else ""
                    log(f"the same request kept failing ({err.kind}); re-sending it in requests of {size} texts{after}")
                if on_retry is not None:
                    on_retry()
                if wait:
                    policy.sleep(wait)
                continue
            raise
        out.extend(vectors)
        start += len(batch)
        just_split = False
    return out
