"""Unit tests for the pure rationale layer (package import; no hass needed)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.load_scheduler import engine
from custom_components.load_scheduler import rationale as rat
from custom_components.load_scheduler.engine import LoadParams, ScheduleMode, Slot

NOW = datetime(2026, 1, 15, 0, 0, tzinfo=UTC)


def make_slots(prices, *, slot_minutes=30, sell=None, excess=0.0, start=NOW):
    """Build a contiguous run of price slots starting at ``start``."""
    slots = []
    t = start
    for buy in prices:
        end = t + timedelta(minutes=slot_minutes)
        slots.append(Slot(start=t, end=end, buy=buy, sell=sell, excess_kwh=excess))
        t = end
    return slots


def window_for(slots):
    return (slots[0].start, slots[-1].end)


def params(slots, **kw):
    base = dict(
        mode=ScheduleMode.NON_SEQUENTIAL,
        target_minutes=60.0,
        window=window_for(slots),
    )
    base.update(kw)
    return LoadParams(**base)


def test_scheduled_non_sequential_reports_price_facts():
    slots = make_slots([0.10, 0.05, 0.20, 0.04])  # 4 × 30 min
    p = params(slots, target_minutes=60.0, cap=0.15)
    periods = engine.compute_plan(slots, p)
    r = rat.explain(slots, p, periods, now=NOW)

    assert r.skip_reason is None
    assert r.scheduled_minutes == 60.0  # two cheapest 30-min slots
    assert r.candidate_count == 4
    assert r.cap == 0.15
    assert r.cap_qualifying_count == 3  # 0.10, 0.05, 0.04 are <= 0.15
    assert r.cheapest_cost == 0.04
    assert r.solar_enabled is False
    assert r.solar_minutes == 0.0
    assert r.window_start == slots[0].start
    assert r.window_end == slots[-1].end


def test_solar_coverage_counts_solar_minutes():
    slots = make_slots([0.20, 0.20], sell=0.02, excess=5.0)
    p = params(slots, target_minutes=60.0, solar_enabled=True, draw_kw=2.0, cap=None)
    periods = engine.compute_plan(slots, p)
    r = rat.explain(slots, p, periods, now=NOW)

    assert r.skip_reason is None
    assert r.solar_enabled is True
    assert r.solar_excess_kwh == 10.0  # 2 slots × 5 kWh excess each
    assert r.solar_minutes == 60.0  # both scheduled slots run on solar


def test_already_satisfied_when_nothing_remains():
    slots = make_slots([0.05, 0.05])
    p = params(slots, target_minutes=0.0, min_service_minutes=0.0)
    periods = engine.compute_plan(slots, p)
    assert periods == []
    r = rat.explain(slots, p, periods, now=NOW)
    assert r.skip_reason == rat.SKIP_ALREADY_SATISFIED


def test_no_slots_in_window():
    slots = make_slots([0.05, 0.05])
    # A window entirely after the slots.
    far = slots[-1].end + timedelta(hours=2)
    p = params(slots, window=(far, far + timedelta(hours=1)))
    periods = engine.compute_plan(slots, p)
    assert periods == []
    r = rat.explain(slots, p, periods, now=NOW)
    assert r.skip_reason == rat.SKIP_NO_SLOTS
    assert r.candidate_count == 0


def test_all_above_cap():
    slots = make_slots([0.30, 0.40, 0.35])
    p = params(slots, target_minutes=60.0, min_service_minutes=0.0, cap=0.10)
    periods = engine.compute_plan(slots, p)
    assert periods == []
    r = rat.explain(slots, p, periods, now=NOW)
    assert r.skip_reason == rat.SKIP_ALL_ABOVE_CAP
    assert r.cap_qualifying_count == 0
    assert r.cheapest_cost == 0.30


def test_no_contiguous_block_for_sequential():
    # Need a 60-min (2-slot) block but only a single isolated slot is in window.
    slots = make_slots([0.05])
    p = params(
        slots,
        mode=ScheduleMode.SEQUENTIAL,
        target_minutes=60.0,
        window=window_for(slots),
    )
    periods = engine.compute_plan(slots, p)
    assert periods == []
    r = rat.explain(slots, p, periods, now=NOW)
    assert r.skip_reason == rat.SKIP_NO_CONTIGUOUS_BLOCK


def test_state_only_disabled():
    r = rat.state_only(ScheduleMode.NON_SEQUENTIAL, rat.SKIP_DISABLED)
    assert r.skip_reason == rat.SKIP_DISABLED
    assert r.scheduled_minutes == 0.0
    assert r.window_start is None
    assert r.boost is False


def test_state_only_boost_flag():
    r = rat.state_only(ScheduleMode.SEQUENTIAL, None, boost=True)
    assert r.boost is True
    assert r.skip_reason is None
