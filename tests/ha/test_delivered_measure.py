"""On-time measurement used for recorder-backed 'delivered today'."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.load_scheduler.coordinator import _on_minutes, _state_on


class _FakeState:
    def __init__(self, state: str, last_changed: datetime) -> None:
        self.state = state
        self.last_changed = last_changed


def test_state_on_with_power_threshold() -> None:
    assert _state_on("60", 50) is True  # element drawing power => delivering
    assert _state_on("40", 50) is False  # below idle threshold => idle
    assert _state_on("unavailable", 50) is False
    # No threshold => plain on/off entity.
    assert _state_on("on", None) is True
    assert _state_on("off", None) is False


def test_on_minutes_sums_on_durations_and_clamps_window() -> None:
    start = datetime(2026, 6, 16, 0, 0, tzinfo=UTC)
    end = start + timedelta(hours=2)
    states = [
        _FakeState("on", start - timedelta(hours=1)),  # already on before midnight
        _FakeState("off", start + timedelta(minutes=30)),
        _FakeState("on", start + timedelta(minutes=60)),  # on again until end
    ]
    # on 00:00–00:30 (30) + on 01:00–02:00 (60) = 90 min
    assert _on_minutes(states, start, end, None) == 90.0


def test_on_minutes_power_threshold() -> None:
    start = datetime(2026, 6, 16, 0, 0, tzinfo=UTC)
    end = start + timedelta(hours=1)
    states = [
        _FakeState("0", start),
        _FakeState("3000", start + timedelta(minutes=15)),  # heating 00:15–01:00 = 45
    ]
    assert _on_minutes(states, start, end, 50) == 45.0


def test_on_minutes_empty() -> None:
    start = datetime(2026, 6, 16, 0, 0, tzinfo=UTC)
    assert _on_minutes([], start, start + timedelta(hours=1), None) == 0.0
