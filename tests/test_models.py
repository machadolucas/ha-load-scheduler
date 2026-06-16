"""Unit tests for the config→params model (package import; no hass needed)."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

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


def test_horizon_window_overrides_daily_window():
    # With a multi-day horizon the window is now → now + N hours (so the engine
    # can defer to a cheaper next day), ignoring earliest/deadline.
    cfg = LoadConfig.from_subentry(
        {"name": "X", "earliest": "21:00:00", "deadline": "07:00:00", "horizon_hours": 48}
    )
    now = datetime(2026, 1, 15, 20, 0, tzinfo=UTC)
    start, end = build_load_params(cfg, now, target_minutes=60).window
    assert start == now
    assert end == now + timedelta(hours=48)
