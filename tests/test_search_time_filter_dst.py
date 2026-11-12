"""Night 2, lane T3: the locus_search time filter around daylight-saving transitions, at the clock
boundaries, and on odd inputs. Facts, not features (ADR-091 draft).

Every test sets the process timezone itself ($TZ plus time.tzset(), restored afterwards), so the file
passes whatever TZ the pytest process starts with; CI runs it as `TZ=Asia/Jerusalem`, `TZ=UTC` and
`TZ=America/New_York` to show that. Real files get their mtimes from os.utime at exact UTC instants
(nanoseconds), go through the real indexer with the offline HashEmbedder, and are queried through the real
`locus_search` coroutine on both of its code paths: the ranked one (`store.query`, post-filter in Python) and
the no-query listing (`store.files_modified`, a Chroma `where`). The two must always agree.

Calendar facts used (tzdata 2026):
  Israel falls back on Sunday 2026-10-25 at 02:00 IDT (UTC+3) -> 01:00 IST (UTC+2): the local times
    01:00-01:59:59 occur twice, the transition is 2026-10-24T23:00:00Z, and 2026-10-25 is 25 hours long.
  Israel springs forward on Friday 2026-03-27 at 02:00 IST -> 03:00 IDT: 02:xx does not exist, the day is 23 h.
  New York falls back 2026-11-01 (06:00Z) and springs forward 2026-03-08 (07:00Z).
"""

# ruff: noqa: E501  (the case tables below keep one case per line; wrapping them hides the instants)
from __future__ import annotations

import asyncio
import datetime as _dt
import os
import re
import time
import zoneinfo
from datetime import datetime
from types import SimpleNamespace

import pytest

from locus import mcp_server as srv

pytest.importorskip("chromadb")
if not hasattr(time, "tzset"):  # Windows has no time.tzset(): the process timezone cannot be switched per test
    pytest.skip("needs time.tzset()", allow_module_level=True)

JER, NYC, UTC = "Asia/Jerusalem", "America/New_York", "UTC"
ALL_TZ = [JER, UTC, NYC]
S = 10**9  # nanoseconds per second
US = 10**3  # nanoseconds per microsecond


def _zone(name: str) -> zoneinfo.ZoneInfo:
    try:
        return zoneinfo.ZoneInfo(name)
    except zoneinfo.ZoneInfoNotFoundError:  # no tz database on this machine: nothing to test against
        pytest.skip(f"no tz database for {name}")


def _z(text: str) -> int:
    """A whole-second UTC instant 'YYYY-MM-DDTHH:MM:SSZ' as integer nanoseconds."""
    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()) * S


def _iso(ns: int, tz: str) -> str:
    """What the result header must print for that instant in *tz* (zoneinfo as the independent oracle)."""
    return datetime.fromtimestamp(ns // S, _zone(tz)).isoformat(timespec="seconds")


@pytest.fixture
def set_tz():
    saved = os.environ.get("TZ")

    def _set(name: str) -> None:
        _zone(name)
        os.environ["TZ"] = name
        time.tzset()

    yield _set
    if saved is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = saved
    time.tzset()


# ---------------------------------------------------------------------------------------------------------
# The corpus: one folder per series, files whose mtime is an exact instant.
# ---------------------------------------------------------------------------------------------------------

# (label, tz, first day, next day, [lo, hi) in UTC, hours): a calendar day as a model sends it, naive.
DAY_CASES = [
    ("jer_fall_back_day", JER, "2026-10-25", "2026-10-26", "2026-10-24T21:00:00Z", "2026-10-25T22:00:00Z", 25),
    ("jer_spring_fwd_day", JER, "2026-03-27", "2026-03-28", "2026-03-26T22:00:00Z", "2026-03-27T21:00:00Z", 23),
    ("jer_day_before", JER, "2026-10-24", "2026-10-25", "2026-10-23T21:00:00Z", "2026-10-24T21:00:00Z", 24),
    ("jer_day_after", JER, "2026-10-26", "2026-10-27", "2026-10-25T22:00:00Z", "2026-10-26T22:00:00Z", 24),
    ("nyc_fall_back_day", NYC, "2026-11-01", "2026-11-02", "2026-11-01T04:00:00Z", "2026-11-02T05:00:00Z", 25),
    ("nyc_spring_fwd_day", NYC, "2026-03-08", "2026-03-09", "2026-03-08T05:00:00Z", "2026-03-09T04:00:00Z", 23),
    ("utc_day", UTC, "2026-10-25", "2026-10-26", "2026-10-25T00:00:00Z", "2026-10-26T00:00:00Z", 24),
]

# The Jerusalem fall-back night, one file per instant (UTC), straddling both occurrences of 01:xx.
FOLD_INSTANTS = [
    "2026-10-24T20:59:59Z",  # 23:59:59 IDT (24th)
    "2026-10-24T21:00:00Z",  # 00:00:00 IDT
    "2026-10-24T21:30:00Z",  # 00:30:00 IDT
    "2026-10-24T21:59:59Z",  # 00:59:59 IDT
    "2026-10-24T22:00:00Z",  # 01:00:00 IDT  (first 01:00)
    "2026-10-24T22:10:00Z",  # 01:10 IDT
    "2026-10-24T22:30:00Z",  # 01:30 IDT     (first 01:30)
    "2026-10-24T22:49:59Z",  # 01:49:59 IDT
    "2026-10-24T22:50:00Z",  # 01:50 IDT
    "2026-10-24T22:59:59Z",  # 01:59:59 IDT  (last second before the transition)
    "2026-10-24T23:00:00Z",  # 01:00:00 IST  (second 01:00; the transition instant)
    "2026-10-24T23:10:00Z",  # 01:10 IST
    "2026-10-24T23:29:59Z",  # 01:29:59 IST
    "2026-10-24T23:30:00Z",  # 01:30 IST     (second 01:30)
    "2026-10-24T23:59:59Z",  # 01:59:59 IST
    "2026-10-25T00:00:00Z",  # 02:00:00 IST
    "2026-10-25T00:59:59Z",  # 02:59:59 IST
    "2026-10-25T01:00:00Z",  # 03:00:00 IST
]

# Jerusalem spring-forward morning of 2026-03-27 (02:00 IST -> 03:00 IDT at 00:00Z).
SPRING_INSTANTS = [
    "2026-03-26T22:00:00Z",  # 00:00 IST
    "2026-03-26T23:00:00Z",  # 01:00 IST
    "2026-03-26T23:59:59Z",  # 01:59:59 IST
    "2026-03-27T00:00:00Z",  # 03:00:00 IDT (02:00 never shows on a clock)
    "2026-03-27T00:29:59Z",  # 03:29:59 IDT
    "2026-03-27T00:30:00Z",  # 03:30:00 IDT
    "2026-03-27T00:59:59Z",  # 03:59:59 IDT
    "2026-03-27T01:00:00Z",  # 04:00:00 IDT
    "2026-03-27T02:00:00Z",  # 05:00:00 IDT
]

# Offsets around T = 2026-06-15T12:00:00Z and T + 1 h, for the inclusive/exclusive and sub-second tests.
BND_T = _z("2026-06-15T12:00:00Z")
BND_OFFSETS = {
    "m1s": -S,
    "m1ms": -1_000_000,
    "m1us": -US,
    "zero": 0,
    "p1us": US,
    "p1ms": 1_000_000,
    "p1s": S,
    "e_m1us": 3600 * S - US,
    "e0": 3600 * S,
    "e_p1us": 3600 * S + US,
}

# For the empty-window text: two files per zone, one before and one after the transition, the window falls between.
NOHIT = {
    JER: ("2026-10-24T20:00:00Z", "2026-10-25T00:30:00Z"),
    NYC: ("2026-11-01T04:00:00Z", "2026-11-01T08:00:00Z"),
    UTC: ("2026-10-24T23:00:00Z", "2026-10-25T01:00:00Z"),
}

# 1234567890.9999999 s, 100 ns under 2009-02-13T23:31:31Z. A double near 1.2e9 has a spacing of 238 ns, so that
# literal IS the double 1234567891.0 (checked below): st_mtime, the indexer's "mtime" and the filter all see
# 23:31:31 exactly, although the filesystem keeps the nanoseconds (the file is stamped with explicit ns).
FLOAT_FILE_NS = 1_234_567_890_999_999_900


def _write(folder, name: str, ns: int) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.md"
    path.write_text(f"marker note {name}")
    os.utime(path, ns=(ns, ns))


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    from locus.config import DEFAULT_EXTENSIONS, Settings
    from locus.ingest.embedder import HashEmbedder
    from locus.ingest.indexer import index_folder
    from locus.ingest.store import ChromaStore

    base = tmp_path_factory.mktemp("dst")
    root = base / "corpus"
    series: dict[str, dict[str, int]] = {"day": {}, "fold": {}, "spring": {}, "bnd": {}}
    for label, _tz, _d1, _d2, lo_s, hi_s, _hours in DAY_CASES:
        lo, hi = _z(lo_s), _z(hi_s)
        for kind, ns in {
            "before": lo - S,
            "just_before": lo - US,
            "first": lo,
            "mid": lo + (hi - lo) // 2,
            "just_last": hi - US,
            "last": hi - S,
            "after": hi,
        }.items():
            series["day"][f"{label}__{kind}"] = ns
    for text in FOLD_INSTANTS:
        series["fold"]["fold_" + re.sub(r"\W", "", text)] = _z(text)
    for text in SPRING_INSTANTS:
        series["spring"]["spring_" + re.sub(r"\W", "", text)] = _z(text)
    for kind, delta in BND_OFFSETS.items():
        series["bnd"][f"bnd_{kind}"] = BND_T + delta
    for tzname, (a, b) in NOHIT.items():
        tag = re.sub(r"\W", "_", tzname)
        series[f"nohit_{tag}"] = {f"{tag}__a": _z(a), f"{tag}__b": _z(b)}
    for folder, files in series.items():
        for name, ns in files.items():
            _write(root / folder, name, ns)
    float_path = root / "float" / "float_file.md"
    float_path.parent.mkdir()
    float_path.write_text("marker note float")
    os.utime(float_path, ns=(FLOAT_FILE_NS, FLOAT_FILE_NS))

    settings = Settings(
        data_dir=base / "data",
        collection="dsttest",
        embedder="hash",
        voyage_model="",
        openai_model="",
        local_model="",
        chunk_tokens=800,
        overlap_tokens=10,
        max_file_mb=5,
        extensions=DEFAULT_EXTENSIONS,
    )
    embedder = HashEmbedder()
    store = ChromaStore(settings.data_dir, settings.collection, embedder.name)
    index_folder(root, embedder, store, settings, log=lambda _: None)
    return SimpleNamespace(
        root=root, embedder=embedder, store=store, settings=settings, series=series, float_path=float_path
    )


@pytest.fixture
def idx(corpus):
    srv._state.clear()
    srv._state.update(embedder=corpus.embedder, store=corpus.store, settings=corpus.settings)
    yield corpus
    srv._state.clear()


def _search(**kwargs) -> str:
    return asyncio.run(srv.locus_search(**kwargs))


def _names(out: str) -> set[str]:
    return {m.group(1).rsplit("/", 1)[-1].removesuffix(".md") for m in re.finditer(r"^## \d+\. (\S+)", out, re.M)}


def _window(idx, series: str, expected: set[str], **window) -> tuple[str, str]:
    """Run one window through both code paths, assert both return exactly *expected*, return both outputs."""
    folder = str(idx.root / series)
    assert len(expected) <= 25, "a window wider than top_k=25 would be truncated, not wrong"
    listing = _search(path_prefix=folder, top_k=25, **window)
    ranked = _search(query="marker note", path_prefix=folder, top_k=25, **window)
    for label, out in (("listing", listing), ("ranked", ranked)):
        assert not out.startswith("Error"), (label, window, out[:300])
        assert _names(out) == expected, (label, window, sorted(_names(out) ^ expected))
        if not expected:
            assert "No indexed file" in out, (label, out)
    return listing, ranked


def _inside(files: dict[str, int], lo: int, hi: int | None = None) -> set[str]:
    return {n for n, t in files.items() if t >= lo and (hi is None or t < hi)}


def _before(files: dict[str, int], hi: int) -> set[str]:
    return {n for n, t in files.items() if t < hi}


# ---------------------------------------------------------------------------------------------------------
# 2. A calendar day as a model sends it ("yesterday" on 2026-10-26 is [2026-10-25, 2026-10-26)), 23/24/25 h.
# ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("case", DAY_CASES, ids=[c[0] for c in DAY_CASES])
def test_a_naive_calendar_day_is_exactly_that_local_day_even_when_it_is_23_or_25_hours_long(idx, set_tz, case):
    label, tz, day, next_day, lo_s, hi_s, hours = case
    set_tz(tz)
    lo, hi = _z(lo_s), _z(hi_s)
    assert (hi - lo) // S == hours * 3600  # the hard-coded edges really are 23/24/25 h apart
    # the independent oracle (zoneinfo) agrees with the hard-coded edges, so the process tz is what we think
    assert datetime.fromisoformat(day).replace(tzinfo=_zone(tz)).timestamp() * S == lo
    assert datetime.fromisoformat(next_day).replace(tzinfo=_zone(tz)).timestamp() * S == hi
    expected = _inside(idx.series["day"], lo, hi)
    listing, ranked = _window(idx, "day", expected, modified_after=day, modified_before=next_day)
    inside = {f"{label}__{k}" for k in ("first", "mid", "just_last", "last")}
    outside = {f"{label}__{k}" for k in ("before", "just_before", "after")}
    assert inside <= expected and not (outside & expected)  # first/last second in; one second (or 1 µs) off: out
    for out in (listing, ranked):
        shown = f"modified on or after {_iso(lo, tz)}; modified before {_iso(hi, tz)}"
        assert shown in out.splitlines()[1]  # both ISO strings carry the offset of their own side of the change


def test_yesterday_sent_on_the_day_after_the_fall_back_covers_25_hours_and_the_header_says_so(idx, set_tz, monkeypatch):
    set_tz(JER)
    monkeypatch.setattr(
        srv, "_local_now", lambda: datetime.fromisoformat("2026-10-26T09:00:00+02:00").astimezone(_zone(JER))
    )
    lo, hi = _z("2026-10-24T21:00:00Z"), _z("2026-10-25T22:00:00Z")
    listing, _ranked = _window(
        idx, "day", _inside(idx.series["day"], lo, hi), modified_after="2026-10-25", modified_before="2026-10-26"
    )
    assert listing.splitlines()[0] == "Today is 2026-10-26 (Asia/Jerusalem, UTC+02:00)."
    assert listing.splitlines()[1].startswith(
        "Filters applied: modified on or after 2026-10-25T00:00:00+03:00; modified before 2026-10-26T00:00:00+02:00;"
    )
    assert "jer_fall_back_day__last" in _names(listing) and "jer_fall_back_day__after" not in _names(listing)


# ---------------------------------------------------------------------------------------------------------
# 1. Windows that start, end and sit inside the repeated hour (Jerusalem 2026-10-25 01:00-01:59:59).
# ---------------------------------------------------------------------------------------------------------

# (id, after, before, lo UTC or None, hi UTC or None, shown after, shown before)
FOLD_NAIVE = [
    # A naive 01:xx is ambiguous. datetime.fromisoformat(...).astimezone() uses fold=0 (PEP 495), i.e. the FIRST
    # occurrence (still IDT, +03:00). That is the sensible default (the earlier real instant, deterministic, shown
    # with its offset in "Filters applied") but it means the second 01:xx can only be reached with an offset.
    (
        "repeated_hour_whole",
        "2026-10-25T01:00",
        "2026-10-25T02:00",
        "22:00:00Z",
        "00:00:00Z",
        "2026-10-25T01:00:00+03:00",
        "2026-10-25T02:00:00+02:00",
    ),  # fmt: skip
    (
        "starts_at_01_30",
        "2026-10-25T01:30",
        "2026-10-25T02:00",
        "22:30:00Z",
        "00:00:00Z",
        "2026-10-25T01:30:00+03:00",
        "2026-10-25T02:00:00+02:00",
    ),  # fmt: skip
    (
        "ends_at_01_30",
        "2026-10-25T00:30",
        "2026-10-25T01:30",
        "21:30:00Z",
        "22:30:00Z",
        "2026-10-25T00:30:00+03:00",
        "2026-10-25T01:30:00+03:00",
    ),  # fmt: skip
    (
        "inside_repeated_hour",
        "2026-10-25T01:10",
        "2026-10-25T01:50",
        "22:10:00Z",
        "22:50:00Z",
        "2026-10-25T01:10:00+03:00",
        "2026-10-25T01:50:00+03:00",
    ),  # fmt: skip
    (
        "up_to_02_00",
        "2026-10-25T00:00",
        "2026-10-25T02:00",
        "21:00:00Z",
        "00:00:00Z",
        "2026-10-25T00:00:00+03:00",
        "2026-10-25T02:00:00+02:00",
    ),  # fmt: skip
    (
        "from_02_00",
        "2026-10-25T02:00",
        "2026-10-25T03:00",
        "00:00:00Z",
        "01:00:00Z",
        "2026-10-25T02:00:00+02:00",
        "2026-10-25T03:00:00+02:00",
    ),  # fmt: skip
]


def _fold_utc(hhmmss: str, *, next_day: bool) -> int:
    return _z(f"2026-10-{'25' if next_day else '24'}T{hhmmss}")


@pytest.mark.parametrize("case", FOLD_NAIVE, ids=[c[0] for c in FOLD_NAIVE])
def test_naive_windows_around_the_repeated_hour_mean_the_first_occurrence(idx, set_tz, case):
    _id, after, before, lo_t, hi_t, shown_after, shown_before = case
    set_tz(JER)
    lo = _fold_utc(lo_t, next_day=lo_t.startswith(("00", "01")))
    hi = _fold_utc(hi_t, next_day=hi_t.startswith(("00", "01")))
    listing, ranked = _window(
        idx, "fold", _inside(idx.series["fold"], lo, hi), modified_after=after, modified_before=before
    )
    assert (_iso(lo, JER), _iso(hi, JER)) == (shown_after, shown_before)  # hard-coded text == the oracle
    for out in (listing, ranked):
        assert f"modified on or after {shown_after}; modified before {shown_before}" in out.splitlines()[1]


# (id, after, before, lo UTC, hi UTC): explicit offsets name one instant each, in every process timezone.
FOLD_EXPLICIT = [
    ("first_01_30_to_second_01_30", "2026-10-25T01:30+03:00", "2026-10-25T01:30+02:00", "2026-10-24T22:30:00Z", "2026-10-24T23:30:00Z"),
    ("second_hour_only_in_Z", "2026-10-24T23:00:00Z", "2026-10-25T00:00:00Z", "2026-10-24T23:00:00Z", "2026-10-25T00:00:00Z"),
    ("first_hour_only_in_plus3", "2026-10-25T01:00:00+03:00", "2026-10-25T02:00:00+03:00", "2026-10-24T22:00:00Z", "2026-10-24T23:00:00Z"),
    ("second_01_30_to_03_00_plus2", "2026-10-25T01:30+02:00", "2026-10-25T03:00:00+02:00", "2026-10-24T23:30:00Z", "2026-10-25T01:00:00Z"),
    ("transition_instant_is_inclusive_after", "2026-10-24T23:00:00Z", "2026-10-24T23:00:01Z", "2026-10-24T23:00:00Z", "2026-10-24T23:00:01Z"),
    ("transition_instant_is_exclusive_before", "2026-10-24T22:00:00Z", "2026-10-24T23:00:00Z", "2026-10-24T22:00:00Z", "2026-10-24T23:00:00Z"),
]  # fmt: skip


@pytest.mark.parametrize("tz", ALL_TZ)
@pytest.mark.parametrize("case", FOLD_EXPLICIT, ids=[c[0] for c in FOLD_EXPLICIT])
def test_explicit_offsets_pick_one_of_the_two_01_30s_in_any_process_timezone(idx, set_tz, tz, case):
    _id, after, before, lo_s, hi_s = case
    set_tz(tz)
    lo, hi = _z(lo_s), _z(hi_s)
    listing, _ranked = _window(
        idx, "fold", _inside(idx.series["fold"], lo, hi), modified_after=after, modified_before=before
    )
    # the "Filters applied" line prints each instant in the process zone with that zone's offset at that instant
    assert f"modified on or after {_iso(lo, tz)}; modified before {_iso(hi, tz)}" in listing.splitlines()[1]


def test_the_two_01_30_files_are_told_apart_by_their_offset_in_the_modified_at(idx, set_tz):
    set_tz(JER)
    out = _search(
        path_prefix=str(idx.root / "fold"),
        top_k=25,
        modified_after="2026-10-24T22:00:00Z",
        modified_before="2026-10-25T00:00:00Z",
    )
    first = [ln for ln in out.splitlines() if "fold_20261024T223000Z" in ln][0]
    second = [ln for ln in out.splitlines() if "fold_20261024T233000Z" in ln][0]
    assert "modified_at 2026-10-25T01:30:00+03:00" in first
    assert "modified_at 2026-10-25T01:30:00+02:00" in second  # same wall clock, one hour apart, only the offset says so


def test_a_naive_time_in_the_spring_forward_gap_lands_after_the_gap_and_an_empty_gap_hour_is_an_error(idx, set_tz):
    set_tz(JER)
    # Israel 2026-03-27: 02:00 IST jumps to 03:00 IDT. PEP 495: a time in the gap is read with the offset in force
    # before it (+02:00), so 02:30 means 00:30Z, which the clock then shows as 03:30 IDT. Sensible (monotonic), and
    # the "Filters applied" line shows what was meant.
    lo, hi = _z("2026-03-27T00:30:00Z"), _z("2026-03-27T01:00:00Z")
    listing, _ = _window(
        idx,
        "spring",
        _inside(idx.series["spring"], lo, hi),
        modified_after="2026-03-27T02:30",
        modified_before="2026-03-27T04:00",
    )
    assert "modified on or after 2026-03-27T03:30:00+03:00; modified before 2026-03-27T04:00:00+03:00" in listing
    # the whole non-existent hour 02:00-03:00 is an empty window (both ends are 00:00Z): an Error the model can read
    gap = _search(query="marker", modified_after="2026-03-27T02:00", modified_before="2026-03-27T03:00")
    assert gap.startswith("Error:") and "window is empty" in gap
    assert "2026-03-27T03:00:00+03:00) is not before modified_before (2026-03-27T03:00:00+03:00)" in gap
    # a window spanning the jump is two real hours long, not three
    span = _inside(idx.series["spring"], _z("2026-03-26T23:00:00Z"), _z("2026-03-27T01:00:00Z"))
    _window(idx, "spring", span, modified_after="2026-03-27T01:00", modified_before="2026-03-27T04:00")
    assert len(span) == 6  # 23:00Z .. 00:59:59Z; the hour 01:00Z is excluded


# ---------------------------------------------------------------------------------------------------------
# 3. Inclusive after, exclusive before, sub-second values, float precision (explicit Z: process-tz independent).
# ---------------------------------------------------------------------------------------------------------

BND_WINDOWS = [
    # (id, after, before, expected offsets present)
    ("closed_open_hour", "2026-06-15T12:00:00Z", "2026-06-15T13:00:00Z", {"zero", "p1us", "p1ms", "p1s", "e_m1us"}),
    ("after_is_inclusive_to_the_microsecond", "2026-06-15T12:00:00.000001Z", None, {"p1us", "p1ms", "p1s", "e_m1us", "e0", "e_p1us"}),
    ("before_is_exclusive_to_the_microsecond", None, "2026-06-15T12:00:00.000001Z", {"m1s", "m1ms", "m1us", "zero"}),
    ("before_exactly_on_a_file", None, "2026-06-15T12:00:00Z", {"m1s", "m1ms", "m1us"}),
    ("after_exactly_on_a_file", "2026-06-15T13:00:00Z", None, {"e0", "e_p1us"}),
    ("one_microsecond_window", "2026-06-15T12:00:00Z", "2026-06-15T12:00:00.000001Z", {"zero"}),
    ("fractional_seconds_with_offset", "2026-06-15T15:00:00.000001+03:00", "2026-06-15T16:00:00.000+03:00", {"p1us", "p1ms", "p1s", "e_m1us"}),
]  # fmt: skip


@pytest.mark.parametrize("tz", ALL_TZ)
@pytest.mark.parametrize("case", BND_WINDOWS, ids=[c[0] for c in BND_WINDOWS])
def test_after_is_inclusive_before_is_exclusive_down_to_the_microsecond(idx, set_tz, tz, case):
    _id, after, before, offsets = case
    set_tz(tz)
    kwargs = {k: v for k, v in (("modified_after", after), ("modified_before", before)) if v}
    _window(idx, "bnd", {f"bnd_{o}" for o in offsets}, **kwargs)


def test_a_mtime_of_1234567890_9999999_is_compared_as_the_float_it_is(idx, set_tz):
    set_tz(JER)
    stored = idx.float_path.stat().st_mtime
    assert 1234567890.9999999 == 1234567891.0  # the literal in the case's name is not a distinct double
    assert 1234567890.999999 < stored <= 1234567891.0
    under_the_second = stored < 1234567891.0  # False wherever st_mtime rounds to the double 1234567891.0
    folder = str(idx.root / "float")
    for kwargs, present in (
        ({"modified_after": "2009-02-13T23:31:30.999999Z"}, True),
        ({"modified_after": "2009-02-13T23:31:31Z"}, not under_the_second),
        ({"modified_before": "2009-02-13T23:31:31Z"}, under_the_second),
        ({"modified_before": "2009-02-13T23:31:30.999999Z"}, False),
    ):
        for out in (_search(path_prefix=folder, **kwargs), _search(query="marker", path_prefix=folder, **kwargs)):
            assert ("float_file" in _names(out)) is present, (kwargs, stored)


def test_parse_when_keeps_microseconds_exactly(set_tz):
    set_tz(UTC)
    assert (
        srv._parse_when("2026-06-15T12:00:00.000001Z", "x")
        == datetime(2026, 6, 15, 12, 0, 0, 1, tzinfo=_dt.UTC).timestamp()
    )
    assert (
        srv._parse_when("2026-06-15T12:00:00.5Z", "x")
        == datetime(2026, 6, 15, 12, 0, 0, 500000, tzinfo=_dt.UTC).timestamp()
    )
    set_tz(JER)
    assert (
        srv._parse_when("2026-06-15T15:00:00.000001", "x")
        == datetime(2026, 6, 15, 12, 0, 0, 1, tzinfo=_dt.UTC).timestamp()
    )


# ---------------------------------------------------------------------------------------------------------
# 4. The header: today, zone name, offset; the filters line; _tz_name; _utc_offset.
# ---------------------------------------------------------------------------------------------------------

HEADER_CASES = [
    # (tz, instant UTC, expected first line)
    (JER, "2026-10-24T22:59:59Z", "Today is 2026-10-25 (Asia/Jerusalem, UTC+03:00)."),  # 01:59:59 IDT, already the 25th
    (
        JER,
        "2026-10-24T23:00:00Z",
        "Today is 2026-10-25 (Asia/Jerusalem, UTC+02:00).",
    ),  # 01:00:00 IST, same date, new offset
    (JER, "2026-10-24T20:59:59Z", "Today is 2026-10-24 (Asia/Jerusalem, UTC+03:00)."),  # 23:59:59 IDT, still the 24th
    (JER, "2026-03-26T23:59:59Z", "Today is 2026-03-27 (Asia/Jerusalem, UTC+02:00)."),  # 01:59:59 IST
    (JER, "2026-03-27T00:00:00Z", "Today is 2026-03-27 (Asia/Jerusalem, UTC+03:00)."),  # 03:00:00 IDT
    (NYC, "2026-11-01T05:59:59Z", "Today is 2026-11-01 (America/New_York, UTC-04:00)."),  # 01:59:59 EDT
    (NYC, "2026-11-01T06:00:00Z", "Today is 2026-11-01 (America/New_York, UTC-05:00)."),  # 01:00:00 EST
    (NYC, "2026-11-01T03:59:59Z", "Today is 2026-10-31 (America/New_York, UTC-04:00)."),  # 23:59:59 EDT on the 31st
    (UTC, "2026-10-24T23:00:00Z", "Today is 2026-10-24 (UTC, UTC+00:00)."),
]


@pytest.mark.parametrize("case", HEADER_CASES, ids=[f"{c[0]}@{c[1]}" for c in HEADER_CASES])
def test_the_header_shows_the_offset_in_force_at_that_instant(idx, set_tz, monkeypatch, case):
    tz, instant, expected = case
    set_tz(tz)
    now = datetime.fromisoformat(instant.replace("Z", "+00:00")).astimezone(_zone(tz))
    monkeypatch.setattr(srv, "_local_now", lambda: now)
    assert _search(query="marker", modified_after="2026-01-01").splitlines()[0] == expected
    assert srv._search_header(None, None, None, None).splitlines() == [expected]  # no filters: only the date line


def test_filters_applied_prints_both_sides_of_the_transition_with_their_own_offsets(idx, set_tz):
    for tz, after, before, shown in (
        (
            JER,
            "2026-10-25T00:30",
            "2026-10-25T03:00",
            "2026-10-25T00:30:00+03:00; modified before 2026-10-25T03:00:00+02:00",
        ),
        (
            NYC,
            "2026-11-01T00:30",
            "2026-11-01T03:00",
            "2026-11-01T00:30:00-04:00; modified before 2026-11-01T03:00:00-05:00",
        ),
        (
            UTC,
            "2026-10-25T00:30",
            "2026-10-25T03:00",
            "2026-10-25T00:30:00+00:00; modified before 2026-10-25T03:00:00+00:00",
        ),
    ):
        set_tz(tz)
        out = _search(query="marker", modified_after=after, modified_before=before)
        assert f"Filters applied: modified on or after {shown}." in out, (tz, out[:300])


def test_tz_name_prefers_the_env_variable_then_the_localtime_link_then_the_abbreviation(set_tz, monkeypatch):
    aware = datetime(2026, 10, 25, 12, 0, tzinfo=_zone(JER))
    set_tz(JER)
    assert srv._tz_name(aware) == "Asia/Jerusalem"
    os.environ["TZ"] = ":Asia/Jerusalem"  # POSIX allows a leading colon
    assert srv._tz_name(aware) == "Asia/Jerusalem"
    os.environ["TZ"] = "  Europe/Paris  "
    assert srv._tz_name(aware) == "Europe/Paris"
    os.environ["TZ"] = "IST-2IDT"  # a POSIX rule string is passed through as the user wrote it
    assert srv._tz_name(aware) == "IST-2IDT"
    for blank in ("", "   ", ":"):  # blank or colon-only falls through to the next source
        os.environ["TZ"] = blank
        monkeypatch.setattr(srv.os.path, "realpath", lambda _p: "/var/db/timezone/zoneinfo/America/New_York")
        assert srv._tz_name(aware) == "America/New_York", repr(blank)
        monkeypatch.undo()
    os.environ.pop("TZ")
    with monkeypatch.context() as m:
        m.setattr(srv.os.path, "realpath", lambda _p: "/usr/share/zoneinfo/Asia/Jerusalem")
        assert srv._tz_name(aware) == "Asia/Jerusalem"  # TZ unset: the zoneinfo name behind /etc/localtime
    with monkeypatch.context() as m:
        m.setattr(srv.os.path, "realpath", lambda _p: "/etc/localtime")  # not a link into zoneinfo
        assert srv._tz_name(aware) == "IST"
    with monkeypatch.context() as m:

        def boom(_p):
            raise OSError("no /etc/localtime")

        m.setattr(srv.os.path, "realpath", boom)
        assert srv._tz_name(aware) == "IST"
    with monkeypatch.context() as m:
        m.setattr(srv.os.path, "realpath", lambda _p: "/etc/localtime")
        assert srv._tz_name(datetime(2026, 1, 1, tzinfo=_dt.UTC).replace(tzinfo=_dt.UTC)) == "UTC"


@pytest.mark.parametrize(
    ("tz", "instant", "expected"),
    [
        (JER, "2026-10-24T22:59:59Z", "UTC+03:00"),
        (JER, "2026-10-24T23:00:00Z", "UTC+02:00"),
        (UTC, "2026-10-24T23:00:00Z", "UTC+00:00"),
        (NYC, "2026-11-01T05:59:59Z", "UTC-04:00"),
        (NYC, "2026-11-01T06:00:00Z", "UTC-05:00"),
        ("Asia/Kolkata", "2026-10-25T00:00:00Z", "UTC+05:30"),
        ("Asia/Kathmandu", "2026-10-25T00:00:00Z", "UTC+05:45"),
        ("America/St_Johns", "2026-11-05T12:00:00Z", "UTC-03:30"),
        ("Pacific/Kiritimati", "2026-10-25T00:00:00Z", "UTC+14:00"),
        ("Pacific/Pago_Pago", "2026-10-25T00:00:00Z", "UTC-11:00"),
    ],
)
def test_utc_offset_text_including_half_hour_zones(tz, instant, expected):
    now = datetime.fromisoformat(instant.replace("Z", "+00:00")).astimezone(_zone(tz))
    assert srv._utc_offset(now) == expected


# ---------------------------------------------------------------------------------------------------------
# 5. Edge inputs: the tool must always answer with text (a result or an "Error:" line), never raise.
# ---------------------------------------------------------------------------------------------------------

# (id, text, verdict): ok = a normal result; error = an "Error:" line; any = either (Python-version dependent).
EDGE_INPUTS = [
    ("date_only", "2026-10-25", "ok"),
    ("date_with_Z_time", "2026-10-25T01:30:00Z", "ok"),
    ("fractional_seconds", "2026-10-25T01:30:00.123456", "ok"),
    ("fractional_seconds_Z", "2026-10-25T01:30:00.5Z", "ok"),
    ("fractional_comma", "2026-10-25T01:30:00,5", "any"),
    ("basic_format", "20261025", "ok"),
    ("basic_format_with_time", "20261025T0130", "ok"),
    ("week_date", "2026-W43-7", "ok"),
    ("week_date_basic", "2026W437", "ok"),
    ("space_separator", "2026-10-25 01:30", "ok"),
    ("leap_day_2028", "2028-02-29", "ok"),
    ("leap_day_2027_invalid", "2027-02-29", "error"),
    ("hour_24", "2026-10-25T24:00", "any"),
    ("hour_24_seconds", "2026-10-25T24:00:00", "any"),
    ("month_13", "2026-13-01", "error"),
    ("day_32", "2026-10-32", "error"),
    ("offset_with_seconds", "2026-10-25T01:30+03:00:30", "any"),
    ("offset_out_of_range", "2026-10-25T01:30:00-24:00", "error"),
    ("surrounding_whitespace", "  2026-10-25  ", "ok"),
    ("empty", "", "error"),
    ("blank", "   ", "error"),
    ("words", "yesterday", "error"),
    ("long_garbage_5000", "x" * 5000, "error"),
    ("date_then_5000_zeros", "2026-10-25" + "0" * 5000, "any"),
    ("arabic_indic_digits", "٢٠٢٦-١٠-٢٥", "error"),
    ("fullwidth_digits", "２０２６-１０-２５", "error"),
    ("pre_epoch", "1969-12-31T23:59:59Z", "ok"),
    ("year_1900", "1900-01-01", "ok"),
    ("year_2100", "2100-01-01", "ok"),
    ("sql_ish", "2026-10-25'; DROP TABLE x;--", "error"),
    ("null_byte", "2026-10-25\x00", "any"),
]

# Years at the edge of datetime: with an explicit offset these used to raise ValueError out of the tool (defect D1).
RANGE_EDGES = [
    ("year_0001_naive", "0001-01-01"),
    ("year_0001_Z", "0001-01-01T00:00:00Z"),
    ("year_0001_plus14", "0001-01-01T00:00:00+14:00"),
    ("year_0001_minus14", "0001-01-01T00:00:00-14:00"),
    ("year_9999_naive", "9999-12-31"),
    ("year_9999_Z", "9999-12-31T23:59:59Z"),
    ("year_9999_minus5", "9999-12-31T23:59:59-05:00"),
    ("year_9999_plus5", "9999-12-31T23:59:59+05:00"),
    ("year_9999_micro_Z", "9999-12-31T23:59:59.999999Z"),
]


@pytest.mark.parametrize("which", ["modified_after", "modified_before"])
@pytest.mark.parametrize("case", EDGE_INPUTS, ids=[c[0] for c in EDGE_INPUTS])
def test_odd_time_inputs_get_text_back_never_a_traceback(idx, set_tz, case, which):
    _id, text, verdict = case
    set_tz(JER)
    out = _search(query="marker", **{which: text})
    assert isinstance(out, str)
    if verdict == "ok":
        assert out.startswith("Today is "), out[:200]
    elif verdict == "error":
        assert out.startswith("Error:"), out[:200]
    else:
        assert out.startswith(("Today is ", "Error:")), out[:200]
    assert "Traceback" not in out


# Defect D1 (fixed in the commit after this file was added): an ISO time in year 0001 or 9999 with an offset
# raised ValueError out of locus_search in every timezone, because the result header prints the filter with
# datetime.fromtimestamp, which cannot show year 0 or 10000 in local time.
@pytest.mark.parametrize("which", ["modified_after", "modified_before"])
@pytest.mark.parametrize("tz", ALL_TZ)
@pytest.mark.parametrize("case", RANGE_EDGES, ids=[c[0] for c in RANGE_EDGES])
def test_dates_at_the_edge_of_the_calendar_get_text_back_never_a_traceback(idx, set_tz, tz, case, which):
    _id, text = case
    set_tz(tz)
    out = _search(query="marker", **{which: text})
    assert isinstance(out, str)
    assert out.startswith(("Today is ", "Error:")), out[:200]


@pytest.mark.parametrize("which", ["modified_after", "modified_before"])
@pytest.mark.parametrize("text", ["0001-01-01", "9999-12-31", "0001-01-01T00:00:00Z", "9999-12-31T23:59:59Z"])
def test_an_unrepresentable_date_is_an_error_that_names_the_parameter(idx, set_tz, text, which):
    set_tz(JER)
    out = _search(query="marker", **{which: text})
    assert out.startswith(f"Error: {which} {text!r} ")


def test_parsed_values_of_the_ordinary_edge_inputs(set_tz):
    set_tz(JER)
    jer = _zone(JER)
    for text, expected in (
        ("2026-10-25", datetime(2026, 10, 25, tzinfo=jer)),
        ("20261025", datetime(2026, 10, 25, tzinfo=jer)),
        ("2026-W43-7", datetime(2026, 10, 25, tzinfo=jer)),  # ISO week 43, day 7 (Sunday) of 2026
        ("2028-02-29", datetime(2028, 2, 29, tzinfo=jer)),
        ("  2026-10-25T01:30  ", datetime(2026, 10, 25, 1, 30, tzinfo=jer)),  # fold=0: the first 01:30
        ("2026-10-25T01:30:00Z", datetime(2026, 10, 25, 1, 30, tzinfo=_dt.UTC)),
        ("2026-10-25T01:30:00.5Z", datetime(2026, 10, 25, 1, 30, 0, 500000, tzinfo=_dt.UTC)),
    ):
        assert srv._parse_when(text, "x") == expected.timestamp(), text
    for bad in ("2027-02-29", "2026-13-01", "٢٠٢٦-١٠-٢٥", "", "  "):
        with pytest.raises(ValueError):
            srv._parse_when(bad, "x")
    assert srv._parse_when(None, "x") is None


@pytest.mark.parametrize("tz", ALL_TZ)
def test_equal_and_reversed_windows_are_an_error_not_an_empty_listing(idx, set_tz, tz):
    set_tz(tz)
    for after, before in (
        ("2026-10-25T01:30", "2026-10-25T01:30"),  # after == before
        ("2026-10-25T01:30+03:00", "2026-10-24T22:30:00Z"),  # the same instant spelled two ways
        ("2026-10-26", "2026-10-25"),  # after > before
        ("2026-10-25T01:30+02:00", "2026-10-25T01:30+03:00"),  # reversed by the one hour the clock change adds
    ):
        out = _search(query="marker", modified_after=after, modified_before=before)
        assert out.startswith("Error:") and "window is empty" in out, (after, before, out[:200])
        assert "Traceback" not in out


# ---------------------------------------------------------------------------------------------------------
# 6. A window with no hits states the date range the index covers, across the change.
# ---------------------------------------------------------------------------------------------------------

NOHIT_WINDOWS = {
    # tz -> windows that fall strictly between file a and file b: (after, before)
    JER: [("2026-10-25T01:10", "2026-10-25T01:20"), ("2026-10-24T23:10:00Z", "2026-10-24T23:20:00Z")],
    NYC: [("2026-11-01T01:10", "2026-11-01T01:20"), ("2026-11-01T06:10:00Z", "2026-11-01T06:20:00Z")],
    UTC: [("2026-10-25T00:10", "2026-10-25T00:20"), ("2026-10-25T00:10:00Z", "2026-10-25T00:20:00Z")],
}


@pytest.mark.parametrize("tz", ALL_TZ)
@pytest.mark.parametrize("with_query", [True, False])
def test_an_empty_window_across_the_change_reports_the_range_with_the_right_offset_on_each_end(
    idx, set_tz, tz, with_query
):
    set_tz(tz)
    a_s, b_s = NOHIT[tz]
    folder = "nohit_" + re.sub(r"\W", "_", tz)
    for after, before in NOHIT_WINDOWS[tz]:
        kwargs = {"query": "marker"} if with_query else {}
        out = _search(path_prefix=str(idx.root / folder), modified_after=after, modified_before=before, **kwargs)
        assert "No indexed file inside" in out, out[:400]
        assert "The 2 indexed file(s)" in out
        # file a is before the change, file b after it: each end of the range prints its own offset
        assert f"modified between {_iso(_z(a_s), tz)} and {_iso(_z(b_s), tz)}" in out, out[:500]
        assert "Nothing was missed by ranking: the filter excluded every file." in out
        assert out.splitlines()[0].startswith("Today is ")
        assert out.splitlines()[1].startswith("Filters applied: modified on or after ")
    if tz != UTC:  # the two ends really straddle the change in this zone: the offsets differ
        assert _iso(_z(a_s), tz)[-6:] != _iso(_z(b_s), tz)[-6:]


def test_the_empty_window_range_of_the_jerusalem_pair_is_written_out(idx, set_tz):
    set_tz(JER)
    out = _search(
        path_prefix=str(idx.root / "nohit_Asia_Jerusalem"),
        modified_after="2026-10-25T01:10",
        modified_before="2026-10-25T01:20",
    )
    assert "modified between 2026-10-24T23:00:00+03:00 and 2026-10-25T02:30:00+02:00" in out
    assert (
        "Filters applied: modified on or after 2026-10-25T01:10:00+03:00; modified before 2026-10-25T01:20:00+03:00;"
        in out
    )


# ---------------------------------------------------------------------------------------------------------
# A recorded mtime that no calendar can show (a corrupt or extracted file; tmpfs and ext4 keep 64-bit seconds).
# ---------------------------------------------------------------------------------------------------------


# Defect D2 (fixed in the same commit as D1): modified_at and the empty-window range formatted a stored mtime
# unguarded, so one such file made every search that returned it raise ValueError.
@pytest.mark.parametrize(
    "mtime",
    [
        pytest.param(253402300800.0, id="year_10000"),
        pytest.param(1e12, id="year_33658"),
        pytest.param(-62135596800.0, id="year_0"),
        pytest.param(100000000000.0, id="year_5138_is_fine"),
    ],
)
def test_a_file_whose_recorded_mtime_cannot_be_shown_is_still_found(tmp_path, set_tz, mtime):
    from locus.ingest.embedder import HashEmbedder
    from locus.ingest.store import ChromaStore

    set_tz(JER)
    embedder = HashEmbedder()
    store = ChromaStore(tmp_path / "data", "oddmtime", embedder.name)
    vec = embedder.embed_documents(["odd mtime chunk"])[0]
    store.upsert(
        ids=["a"],
        embeddings=[vec],
        documents=["odd mtime chunk"],
        metadatas=[{"path": "/x/odd.md", "chunk_index": 0, "mtime": mtime}],
    )
    srv._state.clear()
    srv._state.update(embedder=embedder, store=store, settings=None)
    try:
        ranked = _search(query="odd mtime chunk")
        assert ranked.startswith("Today is ") and "/x/odd.md" in ranked and "modified_at" in ranked
        # excluded by the window, so the empty-window text names the range the index covers, odd mtime included
        empty = _search(query="odd mtime chunk", modified_after="2099-01-01", modified_before="2099-02-01")
        assert empty.startswith("Today is ") and "No indexed file" in empty
    finally:
        srv._state.clear()
