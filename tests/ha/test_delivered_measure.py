"""On-time measurement used for recorder-backed 'delivered today'."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.load_scheduler.const import DOMAIN, SUBENTRY_TYPE_LOAD
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


def test_state_on_binary_feedback_fallback() -> None:
    """A feedback_entity may be a binary_sensor: non-numeric state with a
    threshold configured falls back to the on/off check instead of always
    reading as not-delivering (mirrors sensor.py's live-dot fallback)."""
    assert _state_on("on", 50) is True
    assert _state_on("heating", 50) is True
    assert _state_on("off", 50) is False
    assert _state_on("unknown", 50) is False
    assert _state_on("unavailable", 50) is False


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


def test_on_minutes_binary_feedback_with_threshold() -> None:
    """A power threshold over recorded on/off states still counts the "on"
    span, via _state_on's binary fallback, instead of reading 0 all day."""
    start = datetime(2026, 6, 16, 0, 0, tzinfo=UTC)
    end = start + timedelta(hours=1)
    states = [
        _FakeState("off", start),
        _FakeState("on", start + timedelta(minutes=15)),  # heating 00:15-01:00 = 45
    ]
    assert _on_minutes(states, start, end, 50) == 45.0


def _hub_entry(hass: HomeAssistant, load_data: dict) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"name": "Hub", "buy_price_entity": "sensor.prices"},
        unique_id="sensor.prices",
        subentries_data=[
            ConfigSubentryData(
                subentry_type=SUBENTRY_TYPE_LOAD,
                title="Heater",
                unique_id=None,
                data=load_data,
            )
        ],
    )
    entry.add_to_hass(hass)
    return entry


async def test_delivered_targets_dead_feedback_falls_back_to_controlled(
    hass: HomeAssistant,
) -> None:
    """An unavailable feedback sensor must not zero delivered-minutes all day —
    the target degrades to the controlled entity (no threshold)."""
    async_mock_service(hass, "homeassistant", "turn_on")
    async_mock_service(hass, "homeassistant", "turn_off")
    hass.states.async_set("sensor.prices", "ok", {"data_today": [], "data_tomorrow": []})
    hass.states.async_set("switch.heater", "on")
    hass.states.async_set("sensor.heater_power", "unavailable")
    entry = _hub_entry(
        hass,
        {
            "name": "Heater",
            "mode": "non_sequential",
            "target_minutes": 60,
            "controlled_entity": "switch.heater",
            "feedback_entity": "sensor.heater_power",
            "feedback_idle_w": 50,
        },
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    subentry_id = next(iter(entry.subentries))
    coordinator = entry.runtime_data
    targets = coordinator._delivered_targets()
    assert targets == [(subentry_id, "switch.heater", None)]


async def test_delivered_targets_live_feedback_used_directly(hass: HomeAssistant) -> None:
    """A live (non-dead) feedback sensor is used as-is, with its idle-W threshold."""
    async_mock_service(hass, "homeassistant", "turn_on")
    async_mock_service(hass, "homeassistant", "turn_off")
    hass.states.async_set("sensor.prices", "ok", {"data_today": [], "data_tomorrow": []})
    hass.states.async_set("switch.heater", "on")
    hass.states.async_set("sensor.heater_power", "0.0")
    entry = _hub_entry(
        hass,
        {
            "name": "Heater",
            "mode": "non_sequential",
            "target_minutes": 60,
            "controlled_entity": "switch.heater",
            "feedback_entity": "sensor.heater_power",
            "feedback_idle_w": 50,
        },
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    subentry_id = next(iter(entry.subentries))
    coordinator = entry.runtime_data
    targets = coordinator._delivered_targets()
    assert targets == [(subentry_id, "sensor.heater_power", 50)]
