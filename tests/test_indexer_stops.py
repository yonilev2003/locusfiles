"""RET-008 isolated tests: a fake embed relay (urlopen patched, no network) emitting 429 +
Retry-After, budget_exhausted, 402, 502, 503 paused and timeouts, driven through the real
ManagedEmbedder → typed errors → bounded backoff → indexer stop. Acceptance (master plan Ch.6):
exactly one user-facing message per run, zero relay calls after BudgetExhausted, and a re-run that
indexes only the files the stopped run did not get to."""

from __future__ import annotations

import email.message
import io
import json
import urllib.error
from pathlib import Path

import pytest

from locus.config import DEFAULT_EXTENSIONS, Settings
from locus.ingest.embedder import ManagedEmbedder
from locus.ingest.indexer import index_folder
from locus.ingest.retry import RetryPolicy, embed_with_retry

chromadb = pytest.importorskip("chromadb")

from locus.ingest.store import ChromaStore  # noqa: E402

RELAY_URL = "https://relay.invalid/api/embed-relay"
BILLING = "https://relay.invalid/account/billing"


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        collection="test",
        embedder="managed",
        voyage_model="",
        openai_model="",
        local_model="",
        chunk_tokens=100,
        overlap_tokens=10,
        max_file_mb=5,
        extensions=DEFAULT_EXTENSIONS,
    )


@pytest.fixture()
def corpus(tmp_path: Path) -> Path:
    root = tmp_path / "notes"
    root.mkdir()
    for name, text in [
        ("a.md", "Alpha project kickoff notes."),
        ("b.md", "Budget review for the beta launch."),
        ("c.md", "Customer interview summary."),
        ("d.md", "Decision log: pricing experiment."),
    ]:
        (root / name).write_text(text)
    return root


class _Resp:
    def __init__(self, payload: dict):
        self._raw = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._raw


class FakeRelay:
    """Answers each call from a script: "ok", "timeout", or (status, json_body, headers). Once the
    script is used up it answers `default`; a request whose texts match `fail_when` always gets
    `failure` (a file the provider never accepts)."""

    def __init__(self, *script, default="ok", fail_when=None, failure=None):
        self.script = list(script)
        self.default = default
        self.fail_when = fail_when
        self.failure = failure
        self.calls = 0
        self.sizes: list[int] = []

    def __call__(self, req, timeout=None):  # noqa: ARG002
        self.calls += 1
        texts = json.loads(req.data)["texts"]
        self.sizes.append(len(texts))
        if self.fail_when is not None and self.fail_when(texts):
            action = self.failure
        else:
            action = self.script.pop(0) if self.script else self.default
        if action == "ok":
            return _Resp({"embeddings": [[1.0, float(i + 1)] for i in range(len(texts))]})
        if action == "timeout":
            raise TimeoutError("The read operation timed out")
        status, payload, headers = action
        msg = email.message.Message()
        for key, value in headers.items():
            msg[key] = value
        raw = json.dumps(payload).encode("utf-8") if payload is not None else b""
        raise urllib.error.HTTPError(req.full_url, status, "error", msg, io.BytesIO(raw))


def _run(tmp_path, corpus, monkeypatch, relay, *, prune=False, jitter=1.0):
    monkeypatch.setattr("urllib.request.urlopen", relay)
    settings = _settings(tmp_path)
    embedder = ManagedEmbedder("lk_test", RELAY_URL, billing_url=BILLING)
    store = ChromaStore(settings.data_dir, settings.collection, embedder.name)
    sleeps: list[float] = []
    logged: list[str] = []
    policy = RetryPolicy(sleep=sleeps.append, jitter=lambda: jitter)
    stats = index_folder(corpus, embedder, store, settings, prune=prune, log=logged.append, retry=policy)
    return stats, store, sleeps, logged


def _stop_lines(logged: list[str]) -> list[str]:
    return [line for line in logged if "Stopped early" in line]


DAILY = (429, {"error": "Daily embedding quota exceeded", "code": "budget_exhausted",
               "reason": "daily_quota_exceeded", "retry_after": 3600}, {"Retry-After": "3600"})  # fmt: skip


def test_rate_limit_waits_at_least_retry_after_then_succeeds(tmp_path, corpus, monkeypatch):
    relay = FakeRelay((429, {"error": "slow down", "code": "rate_limited", "retry_after": 7}, {"Retry-After": "7"}))
    stats, store, sleeps, logged = _run(tmp_path, corpus, monkeypatch, relay)
    assert stats.stop is None and not stats.errors
    assert stats.files_indexed == 4 and stats.retries == 1
    assert relay.calls == 5
    assert sleeps and sleeps[0] >= 7
    assert not _stop_lines(logged)


def test_budget_exhausted_stops_once_and_sends_nothing_more(tmp_path, corpus, monkeypatch):
    relay = FakeRelay("ok", DAILY)
    stats, store, sleeps, logged = _run(tmp_path, corpus, monkeypatch, relay)
    assert relay.calls == 2  # zero calls after the budget stop
    assert sleeps == []  # a budget is not waited out inside the run
    assert stats.files_indexed == 1
    assert stats.stop is not None and stats.stop.kind == "budget_exhausted"
    assert stats.files_pending == 3
    lines = _stop_lines(logged)
    assert len(lines) == 1  # exactly one user-facing message for the stop
    assert "The budget resets" in lines[0] and BILLING in lines[0]
    assert "1 file(s) indexed in this run; 3 not processed yet" in lines[0]
    assert "stopped=budget_exhausted pending=3" in stats.summary()

    # Resume: the next run indexes exactly the files the stopped run did not get to.
    relay2 = FakeRelay()
    monkeypatch.setattr("urllib.request.urlopen", relay2)
    settings = _settings(tmp_path)
    embedder = ManagedEmbedder("lk_test", RELAY_URL)
    again = index_folder(corpus, embedder, store, settings, log=lambda _m: None)
    assert again.files_indexed == 3 and again.files_skipped == 1
    assert relay2.calls == 3
    assert len(store.list_paths()) == 4


def test_legacy_relay_trial_budget_text_is_a_budget_stop(tmp_path, corpus, monkeypatch):
    trial = "Trial token budget (5,000,000 tokens) exhausted — subscribe to keep using the managed relay."
    relay = FakeRelay((402, {"error": trial}, {}))
    stats, _, sleeps, logged = _run(tmp_path, corpus, monkeypatch, relay)
    assert relay.calls == 1 and sleeps == []
    assert stats.stop.kind == "budget_exhausted" and stats.stop.error.scope == "trial"
    assert len(_stop_lines(logged)) == 1
    assert "Trial token budget" in stats.stop.message


BAD = (502, {"error": "Embedding provider error", "code": "provider_unavailable"}, {})


def test_provider_error_on_one_file_retries_then_skips_it_and_goes_on(tmp_path, corpus, monkeypatch):
    relay = FakeRelay(BAD, BAD, BAD)  # a.md's three tries fail; the relay is fine afterwards
    stats, _, sleeps, logged = _run(tmp_path, corpus, monkeypatch, relay)
    # unavailable_attempts on a.md (one chunk: nothing to halve), the one-line test request, then b, c, d
    assert relay.calls == 3 + 1 + 3
    assert relay.sizes[3] == 1
    assert len(sleeps) == 2
    assert stats.stop is None and stats.files_indexed == 3
    assert len(stats.errors) == 1 and "a.md" in stats.errors[0] and "ProviderUnavailable" in stats.errors[0]
    assert not _stop_lines(logged)


def test_provider_down_for_every_request_stops_at_the_first_file_after_a_failed_test_request(
    tmp_path, corpus, monkeypatch
):
    relay = FakeRelay(default=BAD)  # the relay is down for the whole run
    stats, _, sleeps, logged = _run(tmp_path, corpus, monkeypatch, relay)
    assert relay.calls == 3 + 1  # 3 tries on a.md, then the one-line test request; b, c, d never sent
    assert relay.sizes[-1] == 1
    assert len(sleeps) == 2
    assert stats.stop.kind == "provider_unavailable" and stats.files_pending == 4
    assert stats.errors == []  # a.md is pending, not a per-file error
    lines = _stop_lines(logged)
    assert len(lines) == 1 and "a one-line test request failed too" in lines[0]
    assert "not one file" not in lines[0] and "in a row" not in lines[0]
    assert "0 file(s) indexed in this run; 4 not processed yet" in lines[0]


def test_a_run_wide_refusal_of_the_test_request_is_what_the_stop_reports(tmp_path, corpus, monkeypatch):
    """a.md outlasts its retries on a 502; the test request then gets the daily budget refusal.
    The stop is a budget stop (reset time, billing link), not an outage."""
    relay = FakeRelay(BAD, BAD, BAD, DAILY)
    stats, _, _, logged = _run(tmp_path, corpus, monkeypatch, relay)
    assert relay.calls == 4
    assert stats.stop.kind == "budget_exhausted" and stats.files_pending == 4
    line = _stop_lines(logged)[0]
    assert "The budget resets" in line and BILLING in line


def test_one_file_that_always_fails_does_not_block_the_folder_or_its_re_runs(tmp_path, corpus, monkeypatch):
    """RET-008 review F1 repro: only b.md's request always gets a 502 (e.g. a Voyage 400 the relay
    reports as 502). The rest of the folder is indexed, and a re-run gets past b.md to new files."""
    relay = FakeRelay(fail_when=lambda texts: any("Budget review" in t for t in texts), failure=BAD)
    stats, store, _, logged = _run(tmp_path, corpus, monkeypatch, relay)
    assert stats.stop is None and not _stop_lines(logged)
    assert stats.files_indexed == 3 and len(stats.errors) == 1 and "b.md" in stats.errors[0]
    assert {Path(p).name for p in store.list_paths()} == {"a.md", "c.md", "d.md"}

    (corpus / "e.md").write_text("Engineering sync notes.")  # walks after b.md
    relay2 = FakeRelay(fail_when=lambda texts: any("Budget review" in t for t in texts), failure=BAD)
    again, store, _, logged = _run(tmp_path, corpus, monkeypatch, relay2, prune=True)
    assert again.stop is None and not _stop_lines(logged)
    assert again.files_indexed == 1 and again.files_skipped == 3 and len(again.errors) == 1
    assert {Path(p).name for p in store.list_paths()} == {"a.md", "c.md", "d.md", "e.md"}
    assert not any("skipped --prune" in line for line in logged)


def _vault(tmp_path: Path, names: str, poison: str) -> Path:
    """One note per letter of *names*; the ones in *poison* hold text the provider never accepts."""
    root = tmp_path / "vault"
    root.mkdir()
    for name in names:
        body = "A poison paragraph the provider never accepts." if name in poison else "An ordinary paragraph."
        (root / f"{name}.md").write_text(f"Note {name}. {body}")
    return root


def _poison_relay() -> FakeRelay:
    return FakeRelay(fail_when=lambda texts: any("poison" in t for t in texts), failure=BAD)


def _error_names(stats) -> list[str]:
    return sorted(Path(entry.split(": ", 1)[0]).name for entry in stats.errors)


def test_always_failing_files_spread_through_a_folder_never_stop_a_re_run(tmp_path, monkeypatch):
    """Re-review NEW-1 repro: b, d and f always get a 502. On the re-run a, c and e are unchanged
    and send nothing, so the old three-files-in-a-row streak saw b, d, f as consecutive and stopped
    at f on every re-run: --prune never ran again (g.md stayed indexed after its deletion) and the
    new h.md was never indexed. Each such failure is now checked with a one-line test request; it
    goes through, so the three are per-file errors and the run goes on."""
    root = _vault(tmp_path, "abcdefg", poison="bdf")
    stats, store, _, logged = _run(tmp_path, root, monkeypatch, _poison_relay())
    assert stats.stop is None and not _stop_lines(logged)
    assert stats.files_indexed == 4 and _error_names(stats) == ["b.md", "d.md", "f.md"]

    (root / "g.md").unlink()
    (root / "h.md").write_text("Note h. Written after the first run.")
    relay = _poison_relay()
    again, store, _, logged = _run(tmp_path, root, monkeypatch, relay, prune=True)
    assert again.stop is None and not _stop_lines(logged)
    assert again.files_indexed == 1 and again.files_skipped == 3 and again.files_removed == 1
    assert _error_names(again) == ["b.md", "d.md", "f.md"]
    assert {Path(p).name for p in store.list_paths()} == {"a.md", "c.md", "e.md", "h.md"}
    assert not any("skipped --prune" in line for line in logged)
    # bounded: 3 tries and one one-line test request per failing file, then h.md once
    assert relay.calls == 3 * (3 + 1) + 1


def test_adjacent_failing_files_on_a_first_run_do_not_stop_it(tmp_path, monkeypatch):
    """Re-review NEW-1: three always-failing files next to each other in walk order stopped the
    first run too, and the stop message said "the problem is not one file" although it was."""
    root = _vault(tmp_path, "abcde", poison="bcd")
    stats, store, _, logged = _run(tmp_path, root, monkeypatch, _poison_relay())
    assert stats.stop is None and not _stop_lines(logged)
    assert stats.files_indexed == 2 and _error_names(stats) == ["b.md", "c.md", "d.md"]
    assert {Path(p).name for p in store.list_paths()} == {"a.md", "e.md"}


def test_a_request_the_provider_refuses_as_too_large_is_halved_until_it_fits(tmp_path, monkeypatch):
    root = tmp_path / "big"
    root.mkdir()
    paragraphs = [f"Paragraph {i}: " + ("lorem ipsum dolor sit amet " * 12) for i in range(6)]
    (root / "long.md").write_text("\n\n".join(paragraphs))
    too_large = (502, {"error": "Embedding provider error", "code": "provider_unavailable"}, {})
    relay = FakeRelay(fail_when=lambda texts: len(texts) > 2, failure=too_large)
    monkeypatch.setattr("urllib.request.urlopen", relay)
    settings = _settings(tmp_path)
    embedder = ManagedEmbedder("lk_test", RELAY_URL, batch_size=8)
    store = ChromaStore(settings.data_dir, settings.collection, embedder.name)
    stats = index_folder(root, embedder, store, settings, log=lambda _m: None, retry=RetryPolicy(sleep=lambda _s: None))
    n = stats.chunks
    assert n >= 5 and stats.files_indexed == 1 and stats.stop is None and not stats.errors
    assert store.count() == n
    # 3 tries at full size, then one try per halving until a request of <= 2 texts goes through
    assert relay.sizes[:3] == [n, n, n] and relay.sizes[3] == (n + 1) // 2
    assert all(size <= 2 for size in relay.sizes[-((n + 1) // 2) :])


class _AlwaysRefusing:
    """Every request fails with `error`; records requests and waits in `events`."""

    def __init__(self, error, events: list[str], batch_size: int = 16) -> None:
        self.error = error
        self.events = events
        self.batch_size = batch_size

    def embed_documents(self, texts):
        self.events.append(f"req{len(texts)}")
        raise self.error


def test_a_halved_re_send_waits_as_long_as_the_server_asked():
    """Re-review NEW-2: after a batch's five tries on a 429 with Retry-After: 30, the halved
    re-sends went out back to back (req8, req4, req2, req1 with no wait), four requests the server
    had asked not to get for 30 s."""
    from locus.ingest.errors import RateLimited

    events: list[str] = []
    policy = RetryPolicy(sleep=lambda s: events.append(f"sleep{s:.0f}"), jitter=lambda: 1.0)
    limited = _AlwaysRefusing(RateLimited("slow down", retry_after=30), events)
    with pytest.raises(RateLimited):
        embed_with_retry(limited, [f"text {i}" for i in range(16)], policy)
    # 5 tries at 16, then max_splits (4) halvings, one try each: 16 → 8 → 4 → 2 → 1, each after the wait
    tail = ["sleep30", "req8", "sleep30", "req4", "sleep30", "req2", "sleep30", "req1"]
    assert events == [*["req16", "sleep30"] * 4, "req16", *tail]

    events.clear()  # a rate limit that names no wait: back off before the smaller request too
    no_hint = _AlwaysRefusing(RateLimited("slow down"), events, batch_size=4)
    with pytest.raises(RateLimited):
        embed_with_retry(no_hint, ["a", "b", "c", "d"], policy)
    tries = ["req4", "sleep2", "req4", "sleep4", "req4", "sleep8", "req4", "sleep16", "req4"]
    assert events == [*tries, "sleep2", "req2", "sleep2", "req1"]


@pytest.mark.parametrize(
    "outage",
    ["timeout", (502, {"error": "Embedding provider unreachable"}, {})],
    ids=["client-timeout", "provider-unreachable"],
)
def test_an_outage_is_not_halved_into_more_reserved_requests(tmp_path, monkeypatch, outage):
    """Re-review NEW-3: halving a request that timed out or never reached the provider cannot help,
    and the relay keeps each such request's reservation counted, so halving multiplied what an
    outage reserved (3 x 128 texts became 3 x 128 + 64 + 32 + 16 + 8 per file)."""
    root = tmp_path / "big"
    root.mkdir()
    paragraphs = [f"Paragraph {i}: " + ("lorem ipsum dolor sit amet " * 12) for i in range(6)]
    (root / "long.md").write_text("\n\n".join(paragraphs))
    relay = FakeRelay(default=outage)
    monkeypatch.setattr("urllib.request.urlopen", relay)
    settings = _settings(tmp_path)
    embedder = ManagedEmbedder("lk_test", RELAY_URL, batch_size=8)
    store = ChromaStore(settings.data_dir, settings.collection, embedder.name)
    stats = index_folder(root, embedder, store, settings, log=lambda _m: None, retry=RetryPolicy(sleep=lambda _s: None))
    n = relay.sizes[0]
    assert n >= 5
    assert relay.sizes == [n, n, n, 1]  # three tries at full size, the one-line test request, nothing halved
    assert stats.stop is not None and stats.stop.kind == "provider_unavailable"


def test_request_specific_failures_are_told_apart_from_run_wide_ones():
    from locus.ingest.errors import (
        AuthFailed,
        BudgetExhausted,
        EmbedError,
        ProviderUnavailable,
        RateLimited,
        SubscriptionInactive,
    )

    policy = RetryPolicy()
    assert policy.could_be_request_specific(RateLimited("x"))
    assert policy.could_be_request_specific(ProviderUnavailable("x", retry_after=30))
    assert not policy.could_be_request_specific(ProviderUnavailable("x", paused=True))
    assert not policy.could_be_request_specific(RateLimited("x", retry_after=policy.max_delay + 1))
    for err in (BudgetExhausted("x"), AuthFailed("x"), SubscriptionInactive("x"), EmbedError("x")):
        assert not policy.could_be_request_specific(err)


def test_probe_service_says_whether_to_skip_the_file_or_what_to_stop_with():
    from locus.ingest.errors import BudgetExhausted, ProviderUnavailable, RateLimited
    from locus.ingest.retry import PROBE_TEXT, probe_service

    class _Embedder:
        def __init__(self, exc: Exception | None) -> None:
            self.exc = exc
            self.sent: list[list[str]] = []

        def embed_documents(self, texts):
            self.sent.append(texts)
            if self.exc is not None:
                raise self.exc
            return [[1.0] for _ in texts]

    err = ProviderUnavailable("Locus embed relay returned HTTP 502: Embedding provider error.")
    answering = _Embedder(None)
    assert probe_service(answering, err) is None  # the service answered: skip the file, go on
    assert answering.sent == [[PROBE_TEXT]]  # one short request, no retry
    budget = BudgetExhausted("daily budget used up")
    assert probe_service(_Embedder(budget), err) is budget  # a run-wide refusal is the stop reason
    assert probe_service(_Embedder(RateLimited("slow down")), err) is err
    assert probe_service(_Embedder(ValueError("not a provider error")), err) is err


def test_timeout_is_retried_like_an_outage(tmp_path, corpus, monkeypatch):
    relay = FakeRelay("timeout")
    stats, _, sleeps, _ = _run(tmp_path, corpus, monkeypatch, relay)
    assert stats.stop is None and stats.files_indexed == 4 and len(sleeps) == 1


def test_retry_after_longer_than_the_cap_stops_instead_of_waiting(tmp_path, corpus, monkeypatch):
    relay = FakeRelay((503, {"error": "busy", "code": "rate_limited", "retry_after": 3600}, {}))
    stats, _, sleeps, logged = _run(tmp_path, corpus, monkeypatch, relay)
    assert relay.calls == 1 and sleeps == []
    assert stats.stop.kind == "rate_limited"
    assert "It asked to wait 3600 s" in _stop_lines(logged)[0]


def test_paused_relay_stops_without_retrying(tmp_path, corpus, monkeypatch):
    relay = FakeRelay((503, {"error": "Paused", "code": "paused", "retry_after": 600}, {}))
    stats, _, sleeps, _ = _run(tmp_path, corpus, monkeypatch, relay)
    assert relay.calls == 1 and sleeps == []
    assert stats.stop.kind == "provider_unavailable" and stats.stop.error.paused


@pytest.mark.parametrize(
    ("status", "payload", "kind"),
    [
        (401, {"error": "Invalid or revoked API key"}, "auth_failed"),
        (402, {"error": "No active subscription"}, "subscription_inactive"),
    ],
)
def test_key_and_subscription_refusals_stop_at_once(tmp_path, corpus, monkeypatch, status, payload, kind):
    relay = FakeRelay((status, payload, {}))
    stats, _, sleeps, logged = _run(tmp_path, corpus, monkeypatch, relay)
    assert relay.calls == 1 and sleeps == []
    assert stats.stop.kind == kind and len(_stop_lines(logged)) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {
            "error": "Locus 9.9.9 or newer is required. Update: uv tool upgrade locusfiles (or pipx upgrade "
            "locusfiles), then restart Locus.",
            "code": "client_outdated",
            "reason": "below_minimum",
        },
        None,  # a bare 426 with no JSON body
    ],
)
def test_client_outdated_stops_once_with_update_locus_and_sends_nothing_more(tmp_path, corpus, monkeypatch, payload):
    """OPS-007: a 426 stops the run at the first file. No retry, no halving, no test request."""
    relay = FakeRelay("ok", (426, payload, {}))
    stats, _, sleeps, logged = _run(tmp_path, corpus, monkeypatch, relay)
    assert relay.calls == 2 and relay.sizes == [1, 1] and sleeps == []
    assert stats.stop.kind == "client_outdated" and stats.files_pending == 3 and stats.errors == []
    lines = _stop_lines(logged)
    assert len(lines) == 1
    assert "Update Locus" in lines[0] and "uv tool upgrade locusfiles" in lines[0]
    assert "a one-line test request" not in lines[0]
    assert "stopped=client_outdated pending=3" in stats.summary()


def test_input_rejected_fails_only_that_file(tmp_path, corpus, monkeypatch):
    relay = FakeRelay((400, {"error": "each text must be at most 6000 characters", "code": "input_rejected"}, {}))
    stats, _, sleeps, _ = _run(tmp_path, corpus, monkeypatch, relay)
    assert stats.stop is None and sleeps == []
    assert stats.files_indexed == 3 and len(stats.errors) == 1
    assert "InputRejected" in stats.errors[0]


def test_no_prune_after_a_budget_stop(tmp_path, corpus, monkeypatch):
    stats, store, _, _ = _run(tmp_path, corpus, monkeypatch, FakeRelay())
    before = set(store.list_paths())
    (corpus / "a.md").write_text("Alpha project kickoff notes, revised.")
    stats, store, _, logged = _run(tmp_path, corpus, monkeypatch, FakeRelay(DAILY), prune=True)
    assert stats.stop is not None
    assert set(store.list_paths()) == before  # nothing pruned from a partial walk
    assert any("skipped --prune" in line for line in logged)


def test_retry_happens_per_batch_so_earlier_batches_are_not_resent(monkeypatch):
    relay = FakeRelay("ok", (429, {"error": "x", "code": "rate_limited", "retry_after": 1}, {}))
    monkeypatch.setattr("urllib.request.urlopen", relay)
    embedder = ManagedEmbedder("lk_test", RELAY_URL, batch_size=2)
    vectors = embed_with_retry(embedder, ["a", "b", "c", "d"], RetryPolicy(sleep=lambda _s: None))
    assert len(vectors) == 4
    assert relay.calls == 3  # batch 1 once, batch 2 twice — never batch 1 again


def test_backoff_is_exponential_jittered_bounded_and_honours_retry_after():
    low = RetryPolicy(jitter=lambda: 0.0)
    high = RetryPolicy(jitter=lambda: 1.0)
    assert [high.delay(n, None) for n in (1, 2, 3, 4)] == [2.0, 4.0, 8.0, 16.0]
    assert [low.delay(n, None) for n in (1, 2, 3)] == [1.0, 2.0, 4.0]  # jitter keeps at least half
    assert high.delay(20, None) == high.max_delay
    assert low.delay(1, 30) == 30  # never sooner than the server asked
    assert high.delay(1, high.max_delay + 1) is None  # longer than the cap: stop, resume later


def test_cli_exits_3_with_one_stop_message_on_stderr(tmp_path, corpus, monkeypatch, capsys):
    from locus import cli

    monkeypatch.setenv("LOCUS_ENABLE_HOSTED", "1")  # managed embeddings are off unless switched on
    monkeypatch.setenv("LOCUS_EMBEDDER", "managed")
    monkeypatch.setenv("LOCUS_API_KEY", "lk_test")
    monkeypatch.setenv("LOCUS_RELAY_URL", RELAY_URL)
    monkeypatch.setenv("LOCUS_DATA_DIR", str(tmp_path / "data"))
    relay = FakeRelay(DAILY)
    monkeypatch.setattr("urllib.request.urlopen", relay)
    cli.get_settings.cache_clear()
    try:
        with pytest.raises(SystemExit) as exc:
            cli.main(["index", str(corpus), "--yes"])  # --yes: consent (UX-005) given up front
    finally:
        cli.get_settings.cache_clear()
    assert exc.value.code == cli.EXIT_STOPPED == 3
    out, err = capsys.readouterr()
    assert err.count("Stopped early") == 1 and "Stopped early" not in out
    assert "stopped=budget_exhausted pending=4" in out
    assert relay.calls == 1


def test_cli_hints_keep_the_users_flags(tmp_path, corpus, monkeypatch, capsys):
    """Review F7: the "how to continue" hints dropped --prune (and the stop message dropped --yes),
    so a script following them exited 2 instead of resuming, or silently stopped pruning."""
    from locus import cli

    monkeypatch.setenv("LOCUS_ENABLE_HOSTED", "1")  # managed embeddings are off unless switched on
    monkeypatch.setenv("LOCUS_EMBEDDER", "managed")
    monkeypatch.setenv("LOCUS_API_KEY", "lk_test")
    monkeypatch.setenv("LOCUS_RELAY_URL", RELAY_URL)
    monkeypatch.setenv("LOCUS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("urllib.request.urlopen", FakeRelay(DAILY))
    monkeypatch.setattr("sys.stdin", io.StringIO(""))  # not a terminal
    cli.get_settings.cache_clear()
    try:
        with pytest.raises(SystemExit) as exc:
            cli.main(["index", str(corpus), "--prune", "--yes"])
        assert exc.value.code == cli.EXIT_STOPPED
        assert f"re-run `locus index {corpus} --prune --yes`" in capsys.readouterr().err
        with pytest.raises(SystemExit) as exc:
            cli.main(["index", str(corpus), "--prune"])  # no --yes, no terminal: refused, nothing sent
        assert exc.value.code == cli.EXIT_NEEDS_YES
        assert f"`locus index {corpus} --prune --yes`" in capsys.readouterr().err
    finally:
        cli.get_settings.cache_clear()
