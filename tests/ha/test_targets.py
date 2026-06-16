"""kWh/EV target mode (number shown in kWh) and dynamic-remaining (delivered)."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.load_scheduler.const import DOMAIN, SUBENTRY_TYPE_LOAD


def _prices() -> dict:
    base = dt_util.now().replace(second=0, microsecond=0)
    return {
        "data_today": [
            {
                "start": (base + timedelta(minutes=15 * i)).isoformat(),
                "end": (base + timedelta(minutes=15 * (i + 1))).isoformat(),
                "buy": 0.10,
            }
            for i in range(24)
        ],
        "data_tomorrow": [],
    }


async def _setup(hass: HomeAssistant, load_data: dict) -> MockConfigEntry:
    hass.states.async_set("sensor.prices", "ok", _prices())
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"name": "Hub", "buy_price_entity": "sensor.prices"},
        unique_id="sensor.prices",
        subentries_data=[
            ConfigSubentryData(
                subentry_type=SUBENTRY_TYPE_LOAD,
                title="Load",
                unique_id=None,
                data=load_data,
            )
        ],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_kwh_target_number_shows_and_sets_kwh(hass: HomeAssistant) -> None:
    # default target 60 min, 10 kW => the number reads 10 kWh.
    entry = await _setup(
        hass,
        {
            "name": "EV",
            "mode": "non_sequential",
            "target_minutes": 60,
            "target_type": "kwh",
            "draw_kw": 10.0,
        },
    )
    subentry_id = next(iter(entry.subentries))
    number_id = er.async_get(hass).async_get_entity_id("number", DOMAIN, f"{subentry_id}_target")
    state = hass.states.get(number_id)
    assert state.attributes["unit_of_measurement"] == "kWh"
    assert float(state.state) == 10.0  # 60 min @ 10 kW

    # Set 20 kWh => 120 min internally => reads back 20 kWh.
    await hass.services.async_call(
        "number", "set_value", {"entity_id": number_id, "value": 20}, blocking=True
    )
    await hass.async_block_till_done()
    assert float(hass.states.get(number_id).state) == 20.0


async def test_delivered_today_shrinks_the_plan(hass: HomeAssistant) -> None:
    # Already ran 2 h today; with a 60-min target nothing more is scheduled.
    hass.states.async_set("sensor.delivered", "2", {"unit_of_measurement": "h"})
    entry = await _setup(
        hass,
        {
            "name": "Heater",
            "mode": "non_sequential",
            "target_minutes": 60,
            "delivered_entity": "sensor.delivered",
        },
    )
    subentry_id = next(iter(entry.subentries))
    sensor_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{subentry_id}_schedule")
    assert hass.states.get(sensor_id).attributes["periods"] == []
