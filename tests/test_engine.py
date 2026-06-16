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


# DST correctness is handled at the boundary, not here: price_source normalises
# all slots to UTC (a DST-free zone) before they reach the engine, and the
# window resolver anchors to local wall-clock. The engine therefore only ever
# does DST-free arithmetic. See test_price_source.py (UTC normalisation across a
# transition) and test_windows.py (wall-clock anchoring / real elapsed).
