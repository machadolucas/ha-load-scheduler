"""Unit tests for the pure competing-controller detection (``competing``).

Like the divert tests, the module is loaded directly from its file via importlib
so it runs with nothing but stdlib + pytest (importing the package would pull in
Home Assistant).
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "custom_components"
    / "load_scheduler"
    / "competing.py"
)
_spec = importlib.util.spec_from_file_location("ls_competing", _PATH)
competing = importlib.util.module_from_spec(_spec)
sys.modules["ls_competing"] = competing
_spec.loader.exec_module(competing)

ForeignEvent = competing.ForeignEvent
SOURCE_SCRIPTED = competing.SOURCE_SCRIPTED
SOURCE_UNKNOWN = competing.SOURCE_UNKNOWN
SOURCE_USER = competing.SOURCE_USER

HELSINKI = ZoneInfo("Europe/Helsinki")
# A plain UTC "now" for the non-DST cases; the tz argument only matters where a
# test says it does.
NOW = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)


def _ev(when: datetime, source: str = SOURCE_SCRIPTED, *, turned_on: bool = False) -> ForeignEvent:
    return ForeignEvent(
        when=when, turned_on=turned_on, in_active_period=not turned_on, source=source
    )


def _days_back(days: int, hour: int = 22, minute: int = 0) -> datetime:
    """An instant ``days`` before NOW's date at a fixed UTC time-of-day."""
    return (NOW - timedelta(days=days)).replace(hour=hour, minute=minute)


def _assess(events, now=NOW, tz=UTC):
    return competing.assess(events, now, tz)


def test_user_flips_do_not_count() -> None:
    # A person switching the heater on and off at the same time every evening is
    # not a competing controller, however regular they are.
    events = [_ev(_days_back(d), SOURCE_USER) for d in range(7)]
    verdict = _assess(events)
    assert verdict.competing is False
    assert verdict.count == 0
    assert verdict.last is None


def test_occasional_unknown_flips_do_not_trigger() -> None:
    # One unattributable flip a day at scattered times: no burst, no cluster.
    hours = (1, 6, 11, 16, 21, 3, 13)
    events = [_ev(_days_back(d, hour), SOURCE_UNKNOWN) for d, hour in enumerate(hours)]
    verdict = _assess(events)
    assert verdict.competing is False
    assert verdict.count == 7
    assert verdict.recurring_days == 1


def test_burst_of_scripted_flips_triggers() -> None:
    # Four flips in one afternoon, spread out enough that no two share a
    # time-of-day cluster — so it can only be the burst rule firing.
    day = _days_back(1)
    events = [_ev(day.replace(hour=h)) for h in (8, 12, 16, 20)]
    verdict = _assess(events)
    assert verdict.competing is True
    assert verdict.reason == "burst"
    assert verdict.count == 4
    assert verdict.scripted_count == 4


def test_three_flips_in_a_day_is_below_burst() -> None:
    day = _days_back(1)
    verdict = _assess([_ev(day.replace(hour=h)) for h in (8, 12, 16)])
    assert verdict.competing is False


def test_daily_scripted_flip_same_time_triggers_after_three_days() -> None:
    events = [_ev(_days_back(d)) for d in (1, 2, 3)]
    verdict = _assess(events)
    assert verdict.competing is True
    assert verdict.reason == "recurring"
    assert verdict.recurring_days == 3
    assert verdict.in_period_count == 3  # every one of them cut a scheduled run
    assert verdict.last == _days_back(1)


def test_two_days_is_not_recurring() -> None:
    verdict = _assess([_ev(_days_back(d)) for d in (1, 2)])
    assert verdict.competing is False
    assert verdict.recurring_days == 2


def test_unknown_source_recurrence_needs_five_days() -> None:
    # Four days of an unattributable evening flip could still be a punctual
    # human at the wall switch: not enough.
    four = [_ev(_days_back(d), SOURCE_UNKNOWN) for d in (1, 2, 3, 4)]
    assert _assess(four).competing is False
    five = [*four, _ev(_days_back(5), SOURCE_UNKNOWN)]
    verdict = _assess(five)
    assert verdict.competing is True
    assert verdict.reason == "recurring"
    assert verdict.recurring_days == 5


def test_events_decay_and_verdict_clears() -> None:
    events = [_ev(_days_back(d)) for d in (1, 2, 3)]
    assert _assess(events).competing is True
    # Same evidence, eight days later: it has all aged out of the window.
    verdict = _assess(events, now=NOW + timedelta(days=8))
    assert verdict.competing is False
    assert verdict.count == 0


def test_prune_caps_and_drops_old() -> None:
    old = [_ev(NOW - timedelta(days=9, hours=h)) for h in range(5)]
    recent = [_ev(NOW - timedelta(hours=h)) for h in range(60)]
    kept = competing.prune([*old, *recent], NOW)
    assert len(kept) == competing.MAX_EVENTS
    assert all(e.when >= NOW - timedelta(hours=competing.ROLLING_WINDOW_H) for e in kept)
    # The cap keeps the newest, and prune returns them oldest-first.
    assert kept[-1].when == NOW
    assert kept == sorted(kept, key=lambda e: e.when)


def test_dst_shift_same_local_time_still_clusters() -> None:
    # A 22:00 local automation is 20:00 UTC before Finland's spring-forward and
    # 19:00 UTC after it — an hour apart, well outside the tolerance. Clustering
    # in local time is what keeps the run looking like one habit, not two.
    local = [
        datetime(2026, 3, 28, 22, 0, tzinfo=HELSINKI),
        datetime(2026, 3, 29, 22, 0, tzinfo=HELSINKI),
        datetime(2026, 3, 30, 22, 0, tzinfo=HELSINKI),
    ]
    events = [_ev(dt.astimezone(UTC)) for dt in local]
    now = datetime(2026, 3, 31, 12, 0, tzinfo=HELSINKI).astimezone(UTC)

    assert competing.assess(events, now, HELSINKI).reason == "recurring"
    # Sanity: judged in UTC the same three flips scatter and nothing fires.
    assert competing.assess(events, now, UTC).competing is False
