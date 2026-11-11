"""Pure tests for relative boost-end calculations."""

import importlib.util
import pathlib
import sys
from datetime import UTC, datetime, timedelta

import pytest

_BOOST_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "custom_components"
    / "load_scheduler"
    / "boost.py"
)
_spec = importlib.util.spec_from_file_location("ls_boost", _BOOST_PATH)
boost = importlib.util.module_from_spec(_spec)
sys.modules["ls_boost"] = boost
_spec.loader.exec_module(boost)
adjusted_end = boost.adjusted_end


NOW = datetime(2026, 11, 11, 12, tzinfo=UTC)


@pytest.mark.parametrize("delta", [float("nan"), float("inf"), -float("inf"), 0, 1e308, -1e308])
def test_adjusted_end_rejects_invalid_delta(delta: float) -> None:
    with pytest.raises(ValueError):
        adjusted_end(NOW, NOW + timedelta(minutes=60), delta)


def test_adjustment_is_relative_to_existing_end() -> None:
    end = NOW + timedelta(minutes=100)
    assert adjusted_end(NOW, end, 60) == NOW + timedelta(minutes=160)
    assert adjusted_end(NOW, end, -60) == NOW + timedelta(minutes=40)


@pytest.mark.parametrize("end", [None, NOW, NOW - timedelta(minutes=1)])
def test_adjusted_end_requires_active_boost(end: datetime | None) -> None:
    with pytest.raises(ValueError, match="active boost"):
        adjusted_end(NOW, end, 1)


def test_reduction_must_leave_positive_time_but_can_leave_under_five_minutes() -> None:
    assert adjusted_end(NOW, NOW + timedelta(minutes=6), -2) == NOW + timedelta(minutes=4)
    with pytest.raises(ValueError, match="leave the boost active"):
        adjusted_end(NOW, NOW + timedelta(minutes=5), -5)


def test_positive_adjustment_cannot_extend_beyond_24_hours_from_now() -> None:
    with pytest.raises(ValueError, match="24 hours"):
        adjusted_end(NOW, NOW + timedelta(minutes=1439), 2)
    # A legacy boost may already exceed 24 hours; a reduction remains allowed.
    assert adjusted_end(NOW, NOW + timedelta(minutes=1500), -60) == NOW + timedelta(minutes=1440)
