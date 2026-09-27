"""Unit tests for the config→params model (package import; no hass needed)."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from custom_components.load_scheduler.engine import ScheduleMode
from custom_components.load_scheduler.models import LoadConfig, build_load_params


def test_from_subentry_parses_core_fields():
    cfg = LoadConfig.from_subentry(
        {
            "name": "Heater",
            "mode": "sequential",
            "target_minutes": 120,
            "earliest": "21:00:00",
            "deadline": "07:00:00",
            "runs_per_day": 2,
            "min_separation_minutes": 30,
            "price_cap": 0.15,
            "min_service_minutes": 60,
            "controlled_entity": "switch.heater",
        }
    )
    assert cfg.mode is ScheduleMode.SEQUENTIAL
    assert cfg.target_minutes == 120
    assert cfg.earliest == time(21, 0)
    assert cfg.deadline == time(7, 0)
    assert cfg.runs_per_day == 2
    assert cfg.min_separation_minutes == 30
    assert cfg.cap == 0.15
    assert cfg.min_service_minutes == 60
    assert cfg.controlled_entity == "switch.heater"


def test_from_subentry_applies_defaults():
    cfg = LoadConfig.from_subentry({"name": "X"})
    assert cfg.mode is ScheduleMode.NON_SEQUENTIAL
    assert cfg.earliest is None
    assert cfg.deadline is None
    assert cfg.cap is None
    assert cfg.runs_per_day == 1
    assert not cfg.is_informational


def test_build_load_params_resolves_window_and_uses_runtime_target():
    cfg = LoadConfig.from_subentry(
        {"name": "X", "earliest": "21:00:00", "deadline": "07:00:00", "price_cap": 0.2}
    )
    now = datetime(2026, 1, 15, 20, 0, tzinfo=UTC)
    params = build_load_params(cfg, now, target_minutes=90)
    assert params.target_minutes == 90  # runtime override, not the config default
    assert params.cap == 0.2
    start, end = params.window
    assert start < end


def test_build_load_params_subtracts_delivered():
    cfg = LoadConfig.from_subentry({"name": "X", "min_service_minutes": 60})
    now = datetime(2026, 1, 15, 20, 0, tzinfo=UTC)
    params = build_load_params(cfg, now, target_minutes=120, delivered_minutes=30)
    assert params.target_minutes == 90  # 120 − 30 already delivered
    assert params.min_service_minutes == 30  # 60 − 30


def test_build_load_params_delivered_clamps_at_zero():
    cfg = LoadConfig.from_subentry({"name": "X", "min_service_minutes": 20})
    now = datetime(2026, 1, 15, 20, 0, tzinfo=UTC)
    params = build_load_params(cfg, now, target_minutes=60, delivered_minutes=100)
    assert params.target_minutes == 0
    assert params.min_service_minutes == 0


def test_horizon_alone_spans_the_whole_horizon():
    # No earliest/deadline: the window is now → now + N hours, so the engine can
    # defer an expensive today to a cheaper next day.
    cfg = LoadConfig.from_subentry({"name": "X", "horizon_hours": 48})
    now = datetime(2026, 1, 15, 20, 0, tzinfo=UTC)
    start, end = build_load_params(cfg, now, target_minutes=60).window
    assert start == now
    assert end == now + timedelta(hours=48)


def test_horizon_is_intersected_with_the_daily_window():
    # The wizard collects earliest/deadline *and* a horizon; all three apply.
    cfg = LoadConfig.from_subentry(
        {"name": "X", "earliest": "21:00:00", "deadline": "07:00:00", "horizon_hours": 48}
    )
    now = datetime(2026, 1, 15, 20, 0, tzinfo=UTC)
    start, end = build_load_params(cfg, now, target_minutes=60).window
    assert start == datetime(2026, 1, 15, 21, 0, tzinfo=UTC)  # earliest still honoured
    assert end == datetime(2026, 1, 16, 7, 0, tzinfo=UTC)  # deadline caps the horizon


def test_horizon_shorter_than_the_daily_window_wins():
    cfg = LoadConfig.from_subentry(
        {"name": "X", "earliest": "21:00:00", "deadline": "07:00:00", "horizon_hours": 2}
    )
    now = datetime(2026, 1, 15, 20, 0, tzinfo=UTC)
    start, end = build_load_params(cfg, now, target_minutes=60).window
    assert start == datetime(2026, 1, 15, 21, 0, tzinfo=UTC)
    assert end == now + timedelta(hours=2)


def test_empty_intersection_yields_a_zero_length_window():
    # Horizon ends before the daily window opens: nothing is schedulable, and the
    # engine must get a degenerate window rather than an inverted one.
    cfg = LoadConfig.from_subentry(
        {"name": "X", "earliest": "21:00:00", "deadline": "07:00:00", "horizon_hours": 0.25}
    )
    now = datetime(2026, 1, 15, 20, 0, tzinfo=UTC)
    start, end = build_load_params(cfg, now, target_minutes=60).window
    assert start == end


def test_min_service_by_is_the_next_local_midnight():
    cfg = LoadConfig.from_subentry({"name": "X", "min_service_minutes": 30, "horizon_hours": 48})
    now = datetime(2026, 1, 15, 20, 0, tzinfo=UTC)
    params = build_load_params(cfg, now, target_minutes=60)
    assert params.min_service_by == datetime(2026, 1, 16, 0, 0, tzinfo=UTC)


def test_min_service_by_never_exceeds_the_window():
    cfg = LoadConfig.from_subentry({"name": "X", "min_service_minutes": 30, "horizon_hours": 2})
    now = datetime(2026, 1, 15, 20, 0, tzinfo=UTC)
    params = build_load_params(cfg, now, target_minutes=60)
    assert params.min_service_by == now + timedelta(hours=2)


def test_min_service_by_absent_once_the_floor_is_delivered():
    cfg = LoadConfig.from_subentry({"name": "X", "min_service_minutes": 30})
    now = datetime(2026, 1, 15, 20, 0, tzinfo=UTC)
    params = build_load_params(cfg, now, target_minutes=60, delivered_minutes=30)
    assert params.min_service_by is None


def test_multi_run_sequential_subtracts_delivered_from_the_total():
    # 2 x 30: one finished run must leave the second one plannable, not shrink
    # every block to zero.
    cfg = LoadConfig.from_subentry({"name": "X", "mode": "sequential", "runs_per_day": 2})
    now = datetime(2026, 1, 15, 20, 0, tzinfo=UTC)
    params = build_load_params(cfg, now, target_minutes=30, delivered_minutes=30)
    assert params.target_minutes == 30  # 2 x 30 - 30
    assert params.run_minutes == 30


def test_single_run_and_non_sequential_keep_the_plain_target():
    now = datetime(2026, 1, 15, 20, 0, tzinfo=UTC)
    for data, run_minutes in (
        ({"name": "X", "runs_per_day": 2}, None),  # runs_per_day is sequential-only
        ({"name": "X", "mode": "sequential"}, 30),  # the run length pins a running cycle
    ):
        params = build_load_params(
            LoadConfig.from_subentry(data), now, target_minutes=30, delivered_minutes=10
        )
        assert params.target_minutes == 20
        assert params.run_minutes == run_minutes


def test_running_minutes_reach_the_engine_only_inside_the_window():
    now = datetime(2026, 1, 15, 20, 0, tzinfo=UTC)
    open_now = LoadConfig.from_subentry({"name": "X", "horizon_hours": 24})
    assert build_load_params(open_now, now, 60, running_minutes=12).running_minutes == 12
    # Window opens at 21:00: the plan can't continue a run that is on at 20:00.
    later = LoadConfig.from_subentry({"name": "X", "earliest": "21:00:00", "deadline": "07:00:00"})
    assert build_load_params(later, now, 60, running_minutes=12).running_minutes == 0


def test_stopped_minutes_are_measured_to_the_window_start():
    now = datetime(2026, 1, 15, 20, 0, tzinfo=UTC)
    open_now = LoadConfig.from_subentry({"name": "X", "horizon_hours": 24})
    assert build_load_params(open_now, now, 60, stopped_minutes=5).stopped_minutes == 5
    later = LoadConfig.from_subentry({"name": "X", "earliest": "21:00:00", "deadline": "07:00:00"})
    assert build_load_params(later, now, 60, stopped_minutes=5).stopped_minutes == 65
    # A running load has no stop to keep a distance from.
    assert (
        build_load_params(open_now, now, 60, running_minutes=3, stopped_minutes=5).stopped_minutes
        is None
    )


def test_horizon_is_real_hours_across_dst():
    helsinki = ZoneInfo("Europe/Helsinki")
    cfg = LoadConfig.from_subentry({"name": "X", "horizon_hours": 24})
    now = datetime(2026, 10, 24, 12, 0, tzinfo=helsinki)  # the 25h day follows
    start, end = build_load_params(cfg, now, target_minutes=60).window
    assert end.astimezone(UTC) - start.astimezone(UTC) == timedelta(hours=24)
