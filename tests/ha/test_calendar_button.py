"""Calendar (visibility) and boost button (manual run-now)."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.load_scheduler.const import DOMAIN, SUBENTRY_TYPE_LOAD


def _price_attributes(cheap: tuple[int, ...], n: int = 24) -> dict:
    base = dt_util.now().replace(second=0, microsecond=0)
    today = [
        {
            "start": (base + timedelta(minutes=15 * i)).isoformat(),
            "end": (base + timedelta(minutes=15 * (i + 1))).isoformat(),
            "buy": 0.01 if i in cheap else 0.20,
            "sell": 0.005,
        }
        for i in range(n)
    ]
    return {"data_today": today, "data_tomorrow": []}


async def _setup(
    hass: HomeAssistant, load_data: dict, cheap: tuple[int, ...], controlled=None
) -> MockConfigEntry:
    hass.states.async_set("sensor.prices", "ok", _price_attributes(cheap))
    if controlled is not None:
        hass.states.async_set(load_data["controlled_entity"], controlled)
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
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_calendar_exposes_periods(hass: HomeAssistant) -> None:
    entry = await _setup(
        hass, {"name": "Heater", "mode": "non_sequential", "target_minutes": 30}, (0, 1)
    )
    reg = er.async_get(hass)
    cal_id = reg.async_get_entity_id("calendar", DOMAIN, f"{entry.entry_id}_calendar")
    assert cal_id is not None

    component = hass.data["calendar"]
    cal = component.get_entity(cal_id)
    now = dt_util.utcnow()
    events = await cal.async_get_events(hass, now - timedelta(hours=1), now + timedelta(days=1))
    assert len(events) == 1
    assert events[0].summary == "Heater"


async def test_boost_button_forces_run_now(hass: HomeAssistant) -> None:
    on = async_mock_service(hass, "homeassistant", "turn_on")
    async_mock_service(hass, "homeassistant", "turn_off")
    # Cheapest slots are hours away => nothing scheduled now. Boost must still
    # turn the load on immediately.
    entry = await _setup(
        hass,
        {
            "name": "Heater",
            "mode": "non_sequential",
            "target_minutes": 30,
            "controlled_entity": "input_boolean.heater",
        },
        cheap=(20, 21),
        controlled="off",
    )
    subentry_id = next(iter(entry.subentries))
    reg = er.async_get(hass)
    button_id = reg.async_get_entity_id("button", DOMAIN, f"{subentry_id}_boost")
    sensor_id = reg.async_get_entity_id("sensor", DOMAIN, f"{subentry_id}_schedule")

    await hass.services.async_call("button", "press", {"entity_id": button_id}, blocking=True)
    await hass.async_block_till_done()

    assert any(c.data.get("entity_id") == "input_boolean.heater" for c in on)
    # The schedule now reports a running period (the boost).
    assert hass.states.get(sensor_id).attributes["running"] is True
