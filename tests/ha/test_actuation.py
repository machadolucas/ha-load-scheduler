"""Actuation, restart catch-up, and runtime persistence."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)

from custom_components.load_scheduler.const import DOMAIN, SAVE_DELAY, SUBENTRY_TYPE_LOAD


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
    hass: HomeAssistant,
    load_data: dict,
    cheap: tuple[int, ...],
    controlled_state: str | None = None,
) -> MockConfigEntry:
    hass.states.async_set("sensor.prices", "ok", _price_attributes(cheap))
    if controlled_state is not None:
        hass.states.async_set(load_data["controlled_entity"], controlled_state)
    subentry = ConfigSubentryData(
        subentry_type=SUBENTRY_TYPE_LOAD, title="Heater", unique_id=None, data=load_data
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"name": "Hub", "buy_price_entity": "sensor.prices"},
        unique_id="sensor.prices",
        subentries_data=[subentry],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _called_for(calls, entity_id: str) -> bool:
    return any(c.data.get("entity_id") == entity_id for c in calls)


async def test_actuates_on_when_now_is_in_a_period(hass: HomeAssistant) -> None:
    on = async_mock_service(hass, "homeassistant", "turn_on")
    async_mock_service(hass, "homeassistant", "turn_off")
    await _setup(
        hass,
        {
            "name": "Heater",
            "mode": "non_sequential",
            "target_minutes": 30,
            "controlled_entity": "input_boolean.heater",
        },
        cheap=(0, 1),  # cheapest two slots cover "now"
        controlled_state="off",
    )
    assert _called_for(on, "input_boolean.heater")


async def test_restart_catch_up_turns_off_a_load_left_on(hass: HomeAssistant) -> None:
    async_mock_service(hass, "homeassistant", "turn_on")
    off = async_mock_service(hass, "homeassistant", "turn_off")
    # Cheapest slots are hours away => nothing should be running now. The
    # controlled entity is still "on" (left on before a restart) => reconcile
    # must switch it off.
    await _setup(
        hass,
        {
            "name": "Heater",
            "mode": "non_sequential",
            "target_minutes": 30,
            "controlled_entity": "input_boolean.heater",
        },
        cheap=(20, 21),
        controlled_state="on",
    )
    assert _called_for(off, "input_boolean.heater")


async def test_informational_load_is_never_actuated(hass: HomeAssistant) -> None:
    on = async_mock_service(hass, "homeassistant", "turn_on")
    off = async_mock_service(hass, "homeassistant", "turn_off")
    await _setup(
        hass,
        {
            "name": "Dishwasher",
            "mode": "informational",
            "target_minutes": 30,
            "controlled_entity": "input_boolean.dw",
        },
        cheap=(0, 1),
        controlled_state="off",
    )
    assert on == []
    assert off == []


async def test_target_persists_across_reload(hass: HomeAssistant) -> None:
    entry = await _setup(
        hass,
        {"name": "Heater", "mode": "non_sequential", "target_minutes": 30},
        cheap=(0, 1),
    )
    subentry_id = next(iter(entry.subentries))
    reg = er.async_get(hass)
    number_id = reg.async_get_entity_id("number", DOMAIN, f"{subentry_id}_target")

    await hass.services.async_call(
        "number", "set_value", {"entity_id": number_id, "value": 45}, blocking=True
    )
    await hass.async_block_till_done()
    # Flush the debounced Store write, then reload the entry.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=SAVE_DELAY + 1))
    await hass.async_block_till_done()
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    number_id = reg.async_get_entity_id("number", DOMAIN, f"{subentry_id}_target")
    assert float(hass.states.get(number_id).state) == 45
