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


def test_min_off_bridges_short_gaps():
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    # Cheapest 3 slots are 0, 1, 3 (price 1); the 15-min gap at slot 2 is < min_off.
    slots = make_slots(start, [1, 1, 9, 1, 9])
    params = LoadParams(
        mode=ScheduleMode.NON_SEQUENTIAL,
        target_minutes=45,
        window=full_window(slots),
        min_off_minutes=30,
    )
    periods = engine.compute_plan(slots, params)
    assert len(periods) == 1  # the short off-gap is bridged
    assert periods[0].minutes == pytest.approx(60)


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


# DST correctness is handled at the boundary, not here: price_source normalises
# all slots to UTC (a DST-free zone) before they reach the engine, and the
# window resolver anchors to local wall-clock. The engine therefore only ever
# does DST-free arithmetic. See test_price_source.py (UTC normalisation across a
# transition) and test_windows.py (wall-clock anchoring / real elapsed).
