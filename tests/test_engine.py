"""Unit tests for the pure scheduling engine.

The engine has no Home Assistant dependency, so it is loaded directly from its
file via importlib — importing the package would pull in ``__init__.py`` (and
thus Home Assistant). This keeps ``pytest`` runnable with nothing but stdlib.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

_ENGINE_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "custom_components"
    / "load_scheduler"
    / "engine.py"
)
_spec = importlib.util.spec_from_file_location("ls_engine", _ENGINE_PATH)
engine = importlib.util.module_from_spec(_spec)
# Register before exec: dataclasses resolves the string annotations produced by
# ``from __future__ import annotations`` via ``sys.modules[cls.__module__]``.
sys.modules["ls_engine"] = engine
_spec.loader.exec_module(engine)

Slot = engine.Slot
LoadParams = engine.LoadParams
ScheduleMode = engine.ScheduleMode
RunSource = engine.RunSource


def make_slots(
    start: datetime,
    prices: list[float],
    *,
    slot_minutes: int = 15,
    sell: list[float] | None = None,
    excess: list[float] | None = None,
) -> list[Slot]:
    """Build a contiguous run of slots from ``prices`` (one per slot)."""
    slots: list[Slot] = []
    t = start
    for i, p in enumerate(prices):
        end = t + timedelta(minutes=slot_minutes)
        slots.append(
            Slot(
                start=t,
                end=end,
                buy=p,
                sell=None if sell is None else sell[i],
                excess_kwh=0.0 if excess is None else excess[i],
            )
        )
        t = end
    return slots


def full_window(slots: list[Slot]) -> tuple[datetime, datetime]:
    return (slots[0].start, slots[-1].end)


def total_minutes(periods) -> float:
    return sum(p.minutes for p in periods)


# --------------------------------------------------------------------------- #
# effective_cost
# --------------------------------------------------------------------------- #


def test_effective_cost_no_solar_returns_buy():
    s = Slot(
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 1, 0, 15, tzinfo=UTC),
        buy=10,
        sell=2,
        excess_kwh=1,
    )
    assert engine.effective_cost(s, draw_kw=4, solar_enabled=False) == 10


def test_effective_cost_binary_when_no_draw():
    s = Slot(
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 1, 0, 15, tzinfo=UTC),
        buy=10,
        sell=2,
        excess_kwh=1,
    )
    assert engine.effective_cost(s, draw_kw=None, solar_enabled=True) == 2


def test_effective_cost_full_coverage_is_sell():
    # 15 min @ 4 kW = 1 kWh load; 1 kWh excess => fully solar => sell price.
    s = Slot(
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 1, 0, 15, tzinfo=UTC),
        buy=10,
        sell=2,
        excess_kwh=1.0,
    )
    assert engine.effective_cost(s, draw_kw=4, solar_enabled=True) == pytest.approx(2)


def test_effective_cost_partial_coverage_blends():
    # load 1 kWh, excess 0.5 kWh => half solar, half grid.
    s = Slot(
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 1, 0, 15, tzinfo=UTC),
        buy=10,
        sell=2,
        excess_kwh=0.5,
    )
    assert engine.effective_cost(s, draw_kw=4, solar_enabled=True) == pytest.approx(6.0)


# --------------------------------------------------------------------------- #
# non-sequential
# --------------------------------------------------------------------------- #


def test_non_sequential_picks_cheapest_scattered():
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    slots = make_slots(start, [5, 1, 3, 2])  # 4x15min
    params = LoadParams(
        mode=ScheduleMode.NON_SEQUENTIAL, target_minutes=30, window=full_window(slots)
    )
    periods = engine.plan_non_sequential(slots, params)
    assert total_minutes(periods) == pytest.approx(30)
    # cheapest two slots are index 1 (price 1) and index 3 (price 2); not contiguous
    assert len(periods) == 2
    starts = sorted(p.start for p in periods)
    assert starts[0] == start + timedelta(minutes=15)
    assert starts[1] == start + timedelta(minutes=45)


def test_non_sequential_merges_contiguous():
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    slots = make_slots(start, [1, 1, 9, 9])
    params = LoadParams(
        mode=ScheduleMode.NON_SEQUENTIAL, target_minutes=30, window=full_window(slots)
    )
    periods = engine.plan_non_sequential(slots, params)
    assert len(periods) == 1  # the two cheap slots are adjacent => merged
    assert periods[0].minutes == pytest.approx(30)


def test_non_sequential_fractional_trim_does_not_split_run():
    # The priciest slot is time-FIRST; trimming the overshoot off it mid-run used
    # to leave a sub-minute gap that split one contiguous run into two periods.
    # The trim must come off the tail so the run stays merged.
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    slots = make_slots(start, [5, 1, 1, 1])  # first slot is the priciest
    params = LoadParams(
        mode=ScheduleMode.NON_SEQUENTIAL, target_minutes=47.3, window=full_window(slots)
    )
    periods = engine.plan_non_sequential(slots, params)
    assert len(periods) == 1  # one contiguous run, not split by the trim
    assert total_minutes(periods) == pytest.approx(47.3)


def test_non_sequential_trims_to_exact_minutes():
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    slots = make_slots(start, [1, 2, 9, 9])
    params = LoadParams(
        mode=ScheduleMode.NON_SEQUENTIAL, target_minutes=20, window=full_window(slots)
    )
    periods = engine.plan_non_sequential(slots, params)
    assert total_minutes(periods) == pytest.approx(20)


def test_non_sequential_cap_limits_discretionary():
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    slots = make_slots(start, [10, 1, 8, 2])
    params = LoadParams(
        mode=ScheduleMode.NON_SEQUENTIAL,
        target_minutes=45,
        window=full_window(slots),
        cap=5,
    )
    periods = engine.plan_non_sequential(slots, params)
    # Only the two slots <= cap (prices 1 and 2) qualify => 30 min, not 45.
    assert total_minutes(periods) == pytest.approx(30)


def test_min_service_overrides_cap_even_at_zero_target():
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    # Two cheap adjacent slots, then an isolated above-cap slot (price 8) that
    # the guarantee must still pick because only 3 slots can satisfy 45 min.
    slots = make_slots(start, [1, 1, 20, 8, 20])
    params = LoadParams(
        mode=ScheduleMode.NON_SEQUENTIAL,
        target_minutes=0,  # user set 0 (e.g. summer)
        window=full_window(slots),
        cap=5,
        min_service_minutes=45,
    )
    periods = engine.plan_non_sequential(slots, params)
    # Guarantee forces 45 min including the isolated slot above the cap (8 > 5).
    assert total_minutes(periods) == pytest.approx(45)
    costs = [round(p.avg_cost, 3) for p in periods]
    assert any(c > 5 for c in costs)


def test_non_sequential_empty_when_zero_target_and_no_min_service():
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    slots = make_slots(start, [1, 2, 3, 4])
    params = LoadParams(
        mode=ScheduleMode.NON_SEQUENTIAL, target_minutes=0, window=full_window(slots)
    )
    assert engine.plan_non_sequential(slots, params) == []


def test_non_sequential_window_filters_slots():
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    slots = make_slots(start, [1, 1, 1, 1])
    # Window only admits the last two slots.
    window = (start + timedelta(minutes=30), start + timedelta(minutes=60))
    params = LoadParams(mode=ScheduleMode.NON_SEQUENTIAL, target_minutes=60, window=window)
    periods = engine.plan_non_sequential(slots, params)
    assert total_minutes(periods) == pytest.approx(30)  # only 30 min available
    assert periods[0].start >= start + timedelta(minutes=30)


# --------------------------------------------------------------------------- #
# sequential
# --------------------------------------------------------------------------- #


def test_sequential_finds_cheapest_block():
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    slots = make_slots(start, [5, 1, 2, 3, 9, 1, 1])
    params = LoadParams(mode=ScheduleMode.SEQUENTIAL, target_minutes=30, window=full_window(slots))
    periods = engine.plan_sequential(slots, params)
    assert len(periods) == 1
    # cheapest 2-slot block is indices 5,6 (1+1) -> starts at minute 75
    assert periods[0].start == start + timedelta(minutes=75)
    assert periods[0].minutes == pytest.approx(30)


def test_sequential_multiple_runs_no_overlap():
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    slots = make_slots(start, [5, 1, 2, 3, 9, 1, 1])
    params = LoadParams(
        mode=ScheduleMode.SEQUENTIAL,
        target_minutes=30,
        window=full_window(slots),
        runs_per_day=2,
    )
    periods = engine.plan_sequential(slots, params)
    assert len(periods) == 2
    periods.sort(key=lambda p: p.start)
    # No overlap between the two runs.
    assert periods[0].end <= periods[1].start
    assert all(p.minutes == pytest.approx(30) for p in periods)


def test_sequential_separation_pushes_second_run_away():
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    slots = make_slots(start, [1, 1, 5, 5, 1, 1, 9, 9])
    params = LoadParams(
        mode=ScheduleMode.SEQUENTIAL,
        target_minutes=30,
        window=full_window(slots),
        runs_per_day=2,
        min_separation_minutes=30,
    )
    periods = engine.plan_sequential(slots, params)
    assert len(periods) == 2
    periods.sort(key=lambda p: p.start)
    gap = (periods[1].start - periods[0].end).total_seconds() / 60.0
    assert gap >= 30 - 1e-6


def test_sequential_block_longer_than_window_returns_nothing():
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    slots = make_slots(start, [1, 2])  # only 30 min available
    params = LoadParams(mode=ScheduleMode.SEQUENTIAL, target_minutes=60, window=full_window(slots))
    assert engine.plan_sequential(slots, params) == []


# --------------------------------------------------------------------------- #
# dispatch + informational
# --------------------------------------------------------------------------- #


def test_informational_uses_sequential_algorithm():
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    slots = make_slots(start, [5, 1, 1, 9])
    seq = engine.compute_plan(
        slots,
        LoadParams(mode=ScheduleMode.SEQUENTIAL, target_minutes=30, window=full_window(slots)),
    )
    info = engine.compute_plan(
        slots,
        LoadParams(mode=ScheduleMode.INFORMATIONAL, target_minutes=30, window=full_window(slots)),
    )
    assert [(p.start, p.end) for p in seq] == [(p.start, p.end) for p in info]


# --------------------------------------------------------------------------- #
# solar sourcing
# --------------------------------------------------------------------------- #


def test_non_sequential_prefers_solar_and_labels_source():
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    # Slot 2 is grid-cheap (buy 1); slot 0 has solar excess making it cheapest.
    slots = make_slots(
        start,
        [3, 9, 1, 9],
        sell=[0.2, 0.2, 0.2, 0.2],
        excess=[2.0, 0.0, 0.0, 0.0],
    )
    params = LoadParams(
        mode=ScheduleMode.NON_SEQUENTIAL,
        target_minutes=15,
        window=full_window(slots),
        solar_enabled=True,
        draw_kw=4,
    )
    periods = engine.plan_non_sequential(slots, params)
    assert len(periods) == 1
    # Solar slot (effective cost 0.2) beats the grid-cheap slot (1.0).
    assert periods[0].start == start
    assert periods[0].source == RunSource.SOLAR


# --------------------------------------------------------------------------- #
# min-run / min-off (compressor protection)
# --------------------------------------------------------------------------- #


def test_min_off_shapes_selection_instead_of_bridging():
    # Cheapest 3 slots are 0, 1, 3 (price 1), but slot 3 sits within min_off of
    # the first run. Bridging the gap afterwards used to run the load through the
    # unpriced 9 at slot 2 for 60 minutes; the selection now keeps the run legal
    # itself — one 45-min run — and prices every minute of it.
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    slots = make_slots(start, [1, 1, 9, 1, 9])
    params = LoadParams(
        mode=ScheduleMode.NON_SEQUENTIAL,
        target_minutes=45,
        window=full_window(slots),
        min_off_minutes=30,
    )
    periods = engine.compute_plan(slots, params)
    assert len(periods) == 1
    assert periods[0].minutes == pytest.approx(45)
    assert periods[0].avg_cost == pytest.approx((1 + 1 + 9) / 3)


def test_min_run_drops_short_fragment():
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    slots = make_slots(start, [1, 9, 9, 9])
    params = LoadParams(
        mode=ScheduleMode.NON_SEQUENTIAL,
        target_minutes=15,
        window=full_window(slots),
        min_run_minutes=30,
    )
    # The only run (15 min) is shorter than min_run, so nothing is scheduled.
    assert engine.compute_plan(slots, params) == []


# --------------------------------------------------------------------------- #
# window clipping
# --------------------------------------------------------------------------- #


def test_partly_elapsed_slot_is_clipped_to_the_window():
    # The window opens 10 min into the first 15-min slot: only 5 of its minutes
    # are still buyable, so the target must be topped up from the next slot.
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    slots = make_slots(start, [1, 1, 9, 9])
    params = LoadParams(
        mode=ScheduleMode.NON_SEQUENTIAL,
        target_minutes=20,
        window=(start + timedelta(minutes=10), slots[-1].end),
    )
    periods = engine.plan_non_sequential(slots, params)
    assert total_minutes(periods) == pytest.approx(20)
    assert periods[0].start == start + timedelta(minutes=10)  # not the slot boundary
    # The 20 minutes end inside the cheap pair, never spilling into the 9s.
    assert periods[-1].end <= slots[1].end


def test_slot_overrunning_the_window_is_clipped_at_the_deadline():
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    slots = make_slots(start, [9, 1], slot_minutes=60)
    deadline = start + timedelta(minutes=90)  # halfway through the cheap slot
    params = LoadParams(
        mode=ScheduleMode.NON_SEQUENTIAL, target_minutes=60, window=(start, deadline)
    )
    periods = engine.plan_non_sequential(slots, params)
    assert max(p.end for p in periods) <= deadline
    assert total_minutes(periods) == pytest.approx(60)


def test_clipping_preserves_the_solar_blend():
    # excess_kwh scales with the clipped span, so the covered fraction — and
    # therefore the effective price — is the same as for the whole slot.
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    slots = make_slots(start, [10], slot_minutes=60, sell=[2.0], excess=[4.0])
    whole = engine.effective_cost(slots[0], draw_kw=4, solar_enabled=True)
    clipped = engine._window_slots(slots, (start + timedelta(minutes=30), slots[0].end))
    assert clipped[0].minutes == pytest.approx(30)
    assert engine.effective_cost(clipped[0], draw_kw=4, solar_enabled=True) == pytest.approx(whole)


# --------------------------------------------------------------------------- #
# mixed slot resolutions (15-min day-ahead + hourly predictor forecast)
# --------------------------------------------------------------------------- #


def mixed_slots() -> list[Slot]:
    """Four 15-min slots at 10, then four hourly slots at 1 (the cheap region)."""
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    quarter = make_slots(start, [10, 10, 10, 10])
    hourly = make_slots(quarter[-1].end, [1, 1, 1, 1], slot_minutes=60)
    return quarter + hourly


def test_sequential_block_sized_in_minutes_not_slots():
    slots = mixed_slots()
    params = LoadParams(mode=ScheduleMode.SEQUENTIAL, target_minutes=120, window=full_window(slots))
    periods = engine.plan_sequential(slots, params)
    assert len(periods) == 1
    assert periods[0].minutes == pytest.approx(120)
    # Two hours of the cheap hourly region, not "120 min = 8 quarter-hours".
    assert periods[0].start == slots[4].start


def test_sequential_weights_block_cost_by_minutes():
    # An unweighted per-slot sum would rate the 4x15-min region (4 x 10 = 40)
    # against the 2 hourly slots (2 x 11 = 22) and wrongly prefer the hourly one;
    # per kWh the quarter-hours are cheaper for the same 60 minutes.
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    quarter = make_slots(start, [10, 10, 10, 10])
    hourly = make_slots(quarter[-1].end, [11, 11], slot_minutes=60)
    slots = quarter + hourly
    params = LoadParams(mode=ScheduleMode.SEQUENTIAL, target_minutes=60, window=full_window(slots))
    periods = engine.plan_sequential(slots, params)
    assert periods[0].start == start
    assert periods[0].minutes == pytest.approx(60)


# --------------------------------------------------------------------------- #
# min-run aware selection
# --------------------------------------------------------------------------- #


def test_min_run_keeps_the_minutes_instead_of_dropping_a_fragment():
    # Cheap slots at 0-1 and an isolated one at 5. Slot-at-a-time selection would
    # pick all three and then delete the lone 15-min run, delivering only 30 of
    # the 45 minutes. Run-unit selection buys one contiguous 45-min run instead.
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    slots = make_slots(start, [1, 1, 2, 9, 9, 1, 9])
    params = LoadParams(
        mode=ScheduleMode.NON_SEQUENTIAL,
        target_minutes=45,
        window=full_window(slots),
        min_run_minutes=30,
    )
    periods = engine.compute_plan(slots, params)
    assert len(periods) == 1
    assert periods[0].minutes == pytest.approx(45)
    assert periods[0].start == start
    assert total_minutes(periods) == pytest.approx(45)


def test_min_run_splits_into_whole_runs_and_honours_min_off():
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    slots = make_slots(start, [1, 1, 9, 9, 1, 1, 9, 9])
    params = LoadParams(
        mode=ScheduleMode.NON_SEQUENTIAL,
        target_minutes=60,
        window=full_window(slots),
        min_run_minutes=30,
        min_off_minutes=30,
    )
    periods = engine.compute_plan(slots, params)
    assert total_minutes(periods) == pytest.approx(60)
    assert all(p.minutes >= 30 - 1e-6 for p in periods)
    periods.sort(key=lambda p: p.start)
    for a, b in zip(periods, periods[1:], strict=False):
        assert (b.start - a.end).total_seconds() / 60.0 >= 30 - 1e-6


def test_min_service_may_overshoot_to_reach_a_legal_run_length():
    # Target is smaller than min_run, but the anti-starvation floor still has to
    # be delivered, so it is rounded up to one legal run rather than skipped.
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    slots = make_slots(start, [1, 2, 9, 9])
    params = LoadParams(
        mode=ScheduleMode.NON_SEQUENTIAL,
        target_minutes=15,
        window=full_window(slots),
        min_service_minutes=15,
        min_run_minutes=30,
    )
    periods = engine.compute_plan(slots, params)
    assert total_minutes(periods) == pytest.approx(30)
    assert periods[0].start == start


# --------------------------------------------------------------------------- #
# min-service is held to the accounting day
# --------------------------------------------------------------------------- #


def test_min_service_prefers_slots_before_its_deadline():
    # The cheapest slots are after midnight, but delivered-today resets there, so
    # the guaranteed minutes must land before it; discretionary ones need not.
    start = datetime(2026, 1, 1, 23, 0, tzinfo=UTC)
    slots = make_slots(start, [5, 6, 9, 9, 1, 1, 1, 1])  # midnight after slot 3
    midnight = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)
    params = LoadParams(
        mode=ScheduleMode.NON_SEQUENTIAL,
        target_minutes=60,
        window=full_window(slots),
        min_service_minutes=30,
        min_service_by=midnight,
    )
    periods = engine.plan_non_sequential(slots, params)
    assert total_minutes(periods) == pytest.approx(60)
    before = sum(
        (min(p.end, midnight) - p.start).total_seconds() / 60.0
        for p in periods
        if p.start < midnight
    )
    assert before == pytest.approx(30)  # exactly the floor, taken from the two cheapest
    assert periods[0].start == start  # the 5 and 6, i.e. cheapest before midnight


def test_min_service_falls_back_past_its_deadline_rather_than_going_unmet():
    # Only 15 min of the accounting day is left: the floor takes what it can
    # before midnight and the rest afterwards, still cap-exempt.
    start = datetime(2026, 1, 1, 23, 45, tzinfo=UTC)
    slots = make_slots(start, [9, 9, 9, 9])
    midnight = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)
    params = LoadParams(
        mode=ScheduleMode.NON_SEQUENTIAL,
        target_minutes=0,
        window=full_window(slots),
        min_service_minutes=30,
        min_service_by=midnight,
        cap=1.0,  # everything is above cap; the floor ignores it
    )
    periods = engine.plan_non_sequential(slots, params)
    assert total_minutes(periods) == pytest.approx(30)
    assert periods[0].start == start


def test_no_min_service_deadline_leaves_the_floor_free():
    start = datetime(2026, 1, 1, 23, 0, tzinfo=UTC)
    slots = make_slots(start, [5, 6, 9, 9, 1, 1, 1, 1])
    params = LoadParams(
        mode=ScheduleMode.NON_SEQUENTIAL,
        target_minutes=60,
        window=full_window(slots),
        min_service_minutes=30,
    )
    periods = engine.plan_non_sequential(slots, params)
    # Unconstrained, all 60 minutes come from the cheap post-midnight run.
    assert total_minutes(periods) == pytest.approx(60)
    assert periods[0].start == datetime(2026, 1, 2, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# regressions: in-progress runs, partial slots, min_off, multi-run, caps
# --------------------------------------------------------------------------- #


def _two_cheap_windows() -> list[Slot]:
    """00:00-06:00 in 15-min slots: 0.1 at 00:00-00:30 and 03:00-03:30, 0.2 at 05-06."""
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    prices = [1.0] * 24
    for i in (0, 1, 12, 13):
        prices[i] = 0.1
    for i in range(20, 24):
        prices[i] = 0.2
    return make_slots(start, prices)


def test_replan_mid_run_continues_the_run_to_min_run():
    slots = _two_cheap_windows()
    start = slots[0].start
    base = dict(mode=ScheduleMode.NON_SEQUENTIAL, min_run_minutes=30)
    fresh = engine.compute_plan(
        slots, LoadParams(target_minutes=60, window=full_window(slots), **base)
    )
    assert [(p.start.hour, p.start.minute, p.minutes) for p in fresh] == [(0, 0, 30), (3, 0, 30)]
    # Two minutes in: 58 left. Re-optimising that as one fresh block moved it to
    # 05:00-05:58 and switched the running load off after two minutes.
    now = start + timedelta(minutes=2)
    replan = engine.compute_plan(
        slots,
        LoadParams(target_minutes=58, window=(now, slots[-1].end), running_minutes=2, **base),
    )
    assert replan[0].start == now
    assert replan[0].end == start + timedelta(minutes=30)  # the run reaches min_run
    assert replan[1].start == start + timedelta(hours=3)
    assert total_minutes(replan) == pytest.approx(58)


def test_running_load_may_glue_a_tail_shorter_than_min_run():
    # 10 minutes left, below min_run: standalone it is illegal (nothing planned),
    # but the load is already on, so extending the current run is fine.
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    slots = make_slots(start, [0.5] * 8)
    base = dict(
        mode=ScheduleMode.NON_SEQUENTIAL,
        target_minutes=10,
        window=full_window(slots),
        min_run_minutes=30,
    )
    assert engine.compute_plan(slots, LoadParams(**base)) == []
    periods = engine.compute_plan(slots, LoadParams(**base, running_minutes=30))
    assert [(p.start, p.minutes) for p in periods] == [(start, pytest.approx(10))]


def test_running_load_is_pinned_to_min_run_even_past_the_target():
    # 5 minutes left but the run is only 10 minutes old: it keeps going to
    # min_run (overshooting by at most min_run - running), cap or not.
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    slots = make_slots(start, [5.0, 5.0, 0.1, 0.1])
    params = LoadParams(
        mode=ScheduleMode.NON_SEQUENTIAL,
        target_minutes=5,
        window=full_window(slots),
        min_run_minutes=30,
        cap=1.0,
        running_minutes=10,
    )
    periods = engine.compute_plan(slots, params)
    assert [(p.start, p.minutes) for p in periods] == [(start, pytest.approx(20))]


def test_safety_net_keeps_the_continuation_of_a_running_load():
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    stub = [engine.Period(start, start + timedelta(minutes=10))]
    assert engine.enforce_min_run_off(stub, 30, 0) == []
    kept = engine.enforce_min_run_off(stub, 30, 0, running_minutes=25, window_start=start)
    assert len(kept) == 1


def test_min_run_not_a_multiple_of_the_slot_keeps_the_remainder():
    # Hourly slots, min_run 30: marking a whole hour spent after a 30-min run
    # split the cheap stretch into three half-hours plus an expensive one.
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    slots = make_slots(start, [9, 9, 1, 1, 1, 9, 9], slot_minutes=60)
    params = LoadParams(
        mode=ScheduleMode.NON_SEQUENTIAL,
        target_minutes=120,
        window=full_window(slots),
        min_run_minutes=30,
    )
    periods = engine.compute_plan(slots, params)
    assert [(p.start, p.minutes) for p in periods] == [
        (start + timedelta(hours=2), pytest.approx(120))
    ]
    assert periods[0].avg_cost == pytest.approx(1)


def test_min_run_20_on_quarter_hours_runs_continuously():
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    slots = make_slots(start, [1] * 8)
    params = LoadParams(
        mode=ScheduleMode.NON_SEQUENTIAL,
        target_minutes=60,
        window=full_window(slots),
        min_run_minutes=20,
    )
    periods = engine.compute_plan(slots, params)
    assert len(periods) == 1  # not 20 on / 10 off
    assert periods[0].minutes == pytest.approx(60)


def test_partly_used_slot_remainder_is_still_buyable():
    # Only 40 minutes exist (15 + 15 + 10); target 40, min_run 20 used to get 20.
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    slots = make_slots(start, [1, 1, 1])
    params = LoadParams(
        mode=ScheduleMode.NON_SEQUENTIAL,
        target_minutes=40,
        window=(start, start + timedelta(minutes=40)),
        min_run_minutes=20,
    )
    assert total_minutes(engine.compute_plan(slots, params)) == pytest.approx(40)


def test_min_off_alone_skips_expensive_gaps_instead_of_bridging():
    # Alternating cheap/expensive quarter-hours. Post-hoc bridging produced one
    # 105-minute period whose avg_cost ignored 45 bridged minutes at 0.5.
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    slots = make_slots(start, [0.1, 0.5] * 8)
    params = LoadParams(
        mode=ScheduleMode.NON_SEQUENTIAL,
        target_minutes=60,
        window=full_window(slots),
        min_off_minutes=30,
    )
    periods = engine.compute_plan(slots, params)
    assert total_minutes(periods) == pytest.approx(60)
    assert all(p.avg_cost == pytest.approx(0.1) for p in periods)
    for a, b in zip(periods, periods[1:], strict=False):
        assert (b.start - a.end).total_seconds() / 60.0 >= 30 - 1e-6


def test_safety_net_prices_the_bridged_gap():
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    m = lambda n: start + timedelta(minutes=n)  # noqa: E731
    periods = [engine.Period(m(0), m(15), avg_cost=0.1), engine.Period(m(30), m(45), avg_cost=0.1)]
    out = engine.enforce_min_run_off(periods, 0, 30, gap_cost=lambda a, b: (0.5 * 15, 15.0))
    assert len(out) == 1
    assert out[0].avg_cost == pytest.approx((1.5 + 1.5 + 7.5) / 45)


def test_min_off_alone_still_hits_the_exact_target():
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    slots = make_slots(start, [1] * 8, slot_minutes=60)
    params = LoadParams(
        mode=ScheduleMode.NON_SEQUENTIAL,
        target_minutes=45,
        window=full_window(slots),
        min_off_minutes=30,
    )
    periods = engine.compute_plan(slots, params)
    assert [(p.start, p.minutes) for p in periods] == [(start, pytest.approx(45))]


def test_multi_run_sequential_splits_the_remaining_total():
    # 2 x 30 with 10 delivered: 50 left = a 20-min remainder + one whole run.
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    slots = make_slots(start, [1, 1, 9, 9, 2, 2, 9, 9])
    params = LoadParams(
        mode=ScheduleMode.SEQUENTIAL,
        target_minutes=50,
        run_minutes=30,
        runs_per_day=2,
        window=full_window(slots),
    )
    periods = engine.compute_plan(slots, params)
    assert sorted(p.minutes for p in periods) == [pytest.approx(20), pytest.approx(30)]
    assert periods[0].start == start  # the whole run takes the cheapest block


def test_multi_run_sequential_after_one_run_still_plans_the_second():
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    slots = make_slots(start, [1, 1, 9, 9])
    params = LoadParams(
        mode=ScheduleMode.SEQUENTIAL,
        target_minutes=30,  # 60 total - 30 delivered
        run_minutes=30,
        runs_per_day=2,
        window=full_window(slots),
    )
    periods = engine.compute_plan(slots, params)
    assert [(p.start, p.minutes) for p in periods] == [(start, pytest.approx(30))]


def test_running_sequential_cycle_continues_in_place():
    # The cheapest block is later, but a cycle in progress can't move there.
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    slots = make_slots(start, [9, 9, 1, 1])
    params = LoadParams(
        mode=ScheduleMode.SEQUENTIAL,
        target_minutes=20,  # 30-min cycle, 10 already run
        run_minutes=30,
        window=full_window(slots),
        running_minutes=10,
    )
    periods = engine.compute_plan(slots, params)
    assert [(p.start, p.minutes) for p in periods] == [(start, pytest.approx(20))]


def test_sequential_respects_the_cap():
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    slots = make_slots(start, [1.0, 1.0])
    params = LoadParams(
        mode=ScheduleMode.SEQUENTIAL, target_minutes=30, window=full_window(slots), cap=0.1
    )
    assert engine.compute_plan(slots, params) == []


def test_sequential_floor_is_cap_exempt_but_only_the_floor():
    # No cap-compliant 30-min block: only the 15-min floor runs, and before the
    # accounting-day boundary even though the post-midnight slots are cheaper.
    start = datetime(2026, 1, 1, 23, 0, tzinfo=UTC)
    midnight = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)
    slots = make_slots(start, [2.0, 2.0, 2.0, 2.0, 1.0, 1.0])
    params = LoadParams(
        mode=ScheduleMode.SEQUENTIAL,
        target_minutes=30,
        window=full_window(slots),
        cap=0.1,
        min_service_minutes=15,
        min_service_by=midnight,
    )
    periods = engine.compute_plan(slots, params)
    assert [p.minutes for p in periods] == [pytest.approx(15)]
    assert periods[0].end <= midnight


def test_sequential_block_below_min_run_is_rounded_up():
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    slots = make_slots(start, [1, 1, 9, 9])
    params = LoadParams(
        mode=ScheduleMode.SEQUENTIAL,
        target_minutes=10,
        window=full_window(slots),
        min_run_minutes=30,
    )
    periods = engine.compute_plan(slots, params)
    assert [(p.start, p.minutes) for p in periods] == [(start, pytest.approx(30))]


def test_small_floor_does_not_exempt_the_whole_slot_from_the_cap():
    # One expensive hourly slot: the 5-min floor runs, the other 40 minutes of
    # the 45 target are discretionary and above the cap.
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    slots = make_slots(start, [1.0], slot_minutes=60)
    params = LoadParams(
        mode=ScheduleMode.NON_SEQUENTIAL,
        target_minutes=45,
        window=full_window(slots),
        cap=0.1,
        min_service_minutes=5,
    )
    periods = engine.compute_plan(slots, params)
    assert total_minutes(periods) == pytest.approx(5)


def test_floor_split_remainder_is_still_used_when_under_cap():
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    slots = make_slots(start, [0.05], slot_minutes=60)
    params = LoadParams(
        mode=ScheduleMode.NON_SEQUENTIAL,
        target_minutes=45,
        window=full_window(slots),
        cap=0.1,
        min_service_minutes=5,
    )
    periods = engine.compute_plan(slots, params)
    assert [(p.start, p.minutes) for p in periods] == [(start, pytest.approx(45))]


def test_min_run_floor_only_exempts_one_legal_run():
    # min_run 30, floor 5, target 45 in one expensive stretch: the floor rounds
    # up to one 30-min run; the remaining 15 are discretionary and above cap.
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    slots = make_slots(start, [1.0] * 4)
    params = LoadParams(
        mode=ScheduleMode.NON_SEQUENTIAL,
        target_minutes=45,
        window=full_window(slots),
        cap=0.1,
        min_service_minutes=5,
        min_run_minutes=30,
    )
    assert total_minutes(engine.compute_plan(slots, params)) == pytest.approx(30)


def test_sequential_floor_fallback_is_rounded_up_to_min_run():
    # Every price is above the cap: the 5-min floor falls back uncapped, but a
    # 5-min block would only be dropped by the min_run safety net.
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    slots = make_slots(start, [1.0] * 8)
    params = LoadParams(
        mode=ScheduleMode.SEQUENTIAL,
        target_minutes=60,
        window=full_window(slots),
        cap=0.1,
        min_service_minutes=5,
        min_run_minutes=30,
    )
    periods = engine.compute_plan(slots, params)
    assert [p.minutes for p in periods] == [pytest.approx(30)]


def _seq_replan_at_2(prices, *, target, floor=0.0, cap=None):
    """First plan, then the replan two minutes in (delivered 2, running 2)."""
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    slots = make_slots(start, prices)
    base = dict(mode=ScheduleMode.SEQUENTIAL, run_minutes=target, min_run_minutes=30, cap=cap)
    first = engine.compute_plan(
        slots,
        LoadParams(
            target_minutes=target, min_service_minutes=floor, window=full_window(slots), **base
        ),
    )
    now = start + timedelta(minutes=2)
    replan = engine.compute_plan(
        slots,
        LoadParams(
            target_minutes=max(0.0, target - 2),
            min_service_minutes=max(0.0, floor - 2),
            window=(now, slots[-1].end),
            running_minutes=2,
            **base,
        ),
    )
    return start, first, replan


@pytest.mark.parametrize(
    ("target", "floor", "cap"),
    [(10, 0, None), (0, 5, None), (60, 5, 0.1)],
    ids=["small_target_rounded_up", "floor_only", "floor_fallback_above_cap"],
)
def test_sequential_replan_keeps_the_committed_min_run_and_nothing_more(target, floor, cap):
    # Each first plan is one 30-min run (min_run round-up / uncapped floor
    # fallback). The replan at minute 2 used to size the pin from the target:
    # cut to minute 10, dropped entirely, or stretched to 60 above the cap.
    start, first, replan = _seq_replan_at_2([1.0] * 8, target=target, floor=floor, cap=cap)
    assert [(p.start, p.minutes) for p in first] == [(start, pytest.approx(30))]
    assert [(p.start, p.end) for p in replan] == [
        (start + timedelta(minutes=2), start + timedelta(minutes=30))
    ]


def test_sequential_continuation_past_min_run_is_cap_checked():
    # Under the cap the cycle runs on to its full length.
    start, _, replan = _seq_replan_at_2([0.2] * 8, target=60, cap=0.5)
    assert [(p.start, p.end) for p in replan] == [
        (start + timedelta(minutes=2), start + timedelta(minutes=60))
    ]


def test_non_sequential_min_run_holds_with_nothing_left_to_deliver():
    # Target and floor both met (e.g. a divert run): still protected to min_run.
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    slots = make_slots(start, [1.0] * 4)
    params = LoadParams(
        mode=ScheduleMode.NON_SEQUENTIAL,
        target_minutes=0,
        window=full_window(slots),
        min_run_minutes=30,
        cap=0.1,
        running_minutes=5,
    )
    periods = engine.compute_plan(slots, params)
    assert [(p.start, p.minutes) for p in periods] == [(start, pytest.approx(25))]


@pytest.mark.parametrize("mode", [ScheduleMode.NON_SEQUENTIAL, ScheduleMode.SEQUENTIAL])
def test_restart_is_offered_mid_slot_when_the_off_time_expires(mode):
    # One hourly slot, just stopped, min_off 10: minutes 10-40 are legal, but
    # starts were only tried on slot boundaries so nothing was planned.
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    slots = make_slots(start, [1.0], slot_minutes=60)
    params = LoadParams(
        mode=mode,
        target_minutes=30,
        window=full_window(slots),
        min_off_minutes=10,
        stopped_minutes=0.0,
    )
    periods = engine.compute_plan(slots, params)
    assert len(periods) == 1
    assert periods[0].minutes == pytest.approx(30)
    assert (periods[0].start - start).total_seconds() / 60.0 == pytest.approx(10, abs=0.01)
    # None — no observed stop — means no guard at all.
    free = engine.compute_plan(slots, LoadParams(**{**params.__dict__, "stopped_minutes": None}))
    assert free[0].start == start


# --------------------------------------------------------------------------- #
# DST: the coordinator passes a *local* window; arithmetic must stay in UTC
# --------------------------------------------------------------------------- #

HELSINKI = ZoneInfo("Europe/Helsinki")


def _real_minutes(periods) -> float:
    return sum(
        (p.end.astimezone(UTC) - p.start.astimezone(UTC)).total_seconds() / 60.0 for p in periods
    )


@pytest.mark.parametrize("day", [29, 25], ids=["spring_2026-03-29", "autumn_2026-10-25"])
@pytest.mark.parametrize(
    "extra",
    [
        dict(mode=ScheduleMode.NON_SEQUENTIAL),
        dict(mode=ScheduleMode.NON_SEQUENTIAL, min_run_minutes=30),
        dict(mode=ScheduleMode.NON_SEQUENTIAL, min_off_minutes=30),
        dict(mode=ScheduleMode.SEQUENTIAL, run_minutes=120),
    ],
    ids=["plain", "min_run", "min_off", "sequential"],
)
@pytest.mark.parametrize("flat", [False, True], ids=["cheap_across_change", "from_window_start"])
def test_plans_across_dst_run_real_minutes_inside_the_window(day, extra, flat):
    # Both changes happen at 01:00 UTC. The cheap two hours straddle it; or, with
    # flat prices, the plan starts at a mid-slot window start just before it.
    month = 3 if day == 29 else 10
    t0 = datetime(2026, month, day - 1, 21, 0, tzinfo=UTC)
    prices = [1.0] * 44  # 21:00 → 08:00 UTC
    if not flat:
        for i in range(12, 20):  # 00:00-02:00 UTC
            prices[i] = 0.1
    slots = make_slots(t0, prices)
    local_start = (
        datetime(2026, month, day, 1, 7, tzinfo=HELSINKI)
        if not flat
        else datetime(2026, month, day, 0, 53, tzinfo=UTC).astimezone(HELSINKI)
    )
    window = (local_start, datetime(2026, month, day, 9, 0, tzinfo=HELSINKI))
    params = LoadParams(target_minutes=120, window=window, **extra)
    periods = engine.compute_plan(slots, params)
    assert _real_minutes(periods) == pytest.approx(120)
    assert all(window[0] <= p.start and p.end <= window[1] for p in periods)
    if not flat:
        cheap = datetime(2026, month, day, 0, 0, tzinfo=UTC)
        assert [(p.start, p.end) for p in periods] == [(cheap, cheap + timedelta(hours=2))]
    else:
        # In UTC: PEP 495 makes an inter-zone == with an ambiguous local time
        # (the repeated autumn hour) always False.
        assert periods[0].start.astimezone(UTC) == window[0].astimezone(UTC)


# --------------------------------------------------------------------------- #
# replay: replan every minute, like the coordinator, and watch the switch
# --------------------------------------------------------------------------- #


def _replay(slots, make_params, minutes, *, on_at_start=False, delivered_lag=None):
    """Drive the plan minute by minute; return the on-runs as (start, end) minutes.

    ``delivered_lag`` None freezes delivered at 0 (idle feedback); an int makes
    delivered the on-time up to that many minutes ago (the coordinator's cache).
    """
    t0 = slots[0].start
    # ``on_at_start``: switched on (externally) a minute before the replay.
    on, on_since, off_since = on_at_start, -1 if on_at_start else None, None
    runs: list[tuple[int, int]] = []
    for k in range(minutes):
        delivered = 0.0
        if delivered_lag is not None:
            upto = k - delivered_lag
            spans = [*runs, (on_since, k)] if on else runs
            delivered = sum(max(0, min(b, upto) - a) for a, b in spans)
        now = t0 + timedelta(minutes=k)
        params = make_params(
            now,
            delivered,
            running=float(k - on_since) if on else 0.0,
            stopped=float(k - off_since) if not on and off_since is not None else None,
        )
        plan = engine.compute_plan(slots, params)
        want = any(p.start <= now < p.end for p in plan)
        if want and not on:
            on, on_since = True, k
        elif on and not want:
            on, off_since = False, k
            runs.append((on_since, k))
    if on:
        runs.append((on_since, minutes))
    return runs


def _sequential_2x60(slots):
    def make(now, delivered, *, running, stopped):
        return LoadParams(
            mode=ScheduleMode.SEQUENTIAL,
            target_minutes=max(0.0, 120 - delivered),
            run_minutes=60,
            runs_per_day=2,
            min_separation_minutes=60,
            window=(now, slots[-1].end),
            running_minutes=running,
            stopped_minutes=stopped,
        )

    return make


def test_replay_sequential_with_idle_feedback_never_overruns_or_merges_cycles():
    # Delivered frozen at 0: sizing the pin from delivered re-pinned a full hour
    # every minute (720 minutes on, cap ignored). Each cycle now ends at run
    # length and the next keeps its separation.
    slots = make_slots(datetime(2026, 1, 1, tzinfo=UTC), [0.2] * 48)
    runs = _replay(slots, _sequential_2x60(slots), 720)
    assert runs, "the load should run"
    assert all(b - a <= 60 for a, b in runs)
    assert all(nxt[0] - prev[1] >= 60 for prev, nxt in zip(runs, runs[1:], strict=False))


def test_replay_sequential_with_lagging_delivered_keeps_two_separate_cycles():
    # A 2-minute delivered cache used to glue cycle two straight onto cycle one.
    slots = make_slots(datetime(2026, 1, 1, tzinfo=UTC), [0.2] * 48)
    runs = _replay(slots, _sequential_2x60(slots), 720, delivered_lag=2)
    assert len(runs) == 2
    assert all(58 <= b - a <= 60 for a, b in runs)
    assert runs[1][0] - runs[0][1] >= 60


def test_replay_non_sequential_pin_stops_at_min_run_despite_stale_delivered():
    # Started externally in an above-cap stretch, delivered frozen at 0: the
    # pin carries the run to min_run and no further; restarts keep min_off.
    start = datetime(2026, 1, 1, tzinfo=UTC)
    slots = make_slots(start, [1.0] * 8 + [0.1] * 16)

    def make(now, delivered, *, running, stopped):
        return LoadParams(
            mode=ScheduleMode.NON_SEQUENTIAL,
            target_minutes=max(0.0, 120 - delivered),
            window=(now, slots[-1].end),
            cap=0.5,
            min_run_minutes=30,
            min_off_minutes=30,
            running_minutes=running,
            stopped_minutes=stopped,
        )

    runs = _replay(slots, make, 360, on_at_start=True)
    assert runs[0] == (-1, 29)  # exactly min_run, then off in the dear stretch
    assert all(nxt[0] - prev[1] >= 30 for prev, nxt in zip(runs, runs[1:], strict=False))


# DST correctness is handled at the boundary, not here: price_source normalises
# all slots to UTC (a DST-free zone) before they reach the engine, and the
# window resolver anchors to local wall-clock. The engine therefore only ever
# does DST-free arithmetic. See test_price_source.py (UTC normalisation across a
# transition) and test_windows.py (wall-clock anchoring / real elapsed).
