"""Coexist (top-up) loads, boost toggle, and reality-based running sensor."""

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
    return {
        "data_today": [
            {
                "start": (base + timedelta(minutes=15 * i)).isoformat(),
                "end": (base + timedelta(minutes=15 * (i + 1))).isoformat(),
                "buy": 0.01 if i in cheap else 0.20,
                "sell": 0.005,
            }
            for i in range(n)
        ],
        "data_tomorrow": [],
    }


async def _setup(
    hass: HomeAssistant,
    load_data: dict,
    cheap: tuple[int, ...],
    controlled_state: str | None = None,
) -> MockConfigEntry:
    hass.states.async_set("sensor.prices", "ok", _price_attributes(cheap))
    if controlled_state is not None:
        hass.states.async_set(load_data["controlled_entity"], controlled_state)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"name": "Hub", "buy_price_entity": "sensor.prices"},
        unique_id="sensor.prices",
        subentries_data=[
            ConfigSubentryData(
                subentry_type=SUBENTRY_TYPE_LOAD, title="Floor", unique_id=None, data=load_data
            )
        ],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _called_for(calls, entity_id: str) -> bool:
    return any(c.data.get("entity_id") == entity_id for c in calls)


async def test_coexist_load_left_on_is_not_switched_off(hass: HomeAssistant) -> None:
    # A normal load left on outside its window gets reconciled off (see
    # test_actuation). A coexist (top-up) load must NOT: the integration only
    # switches off runs it started, so an external/comfort run is left alone.
    async_mock_service(hass, "homeassistant", "turn_on")
    off = async_mock_service(hass, "homeassistant", "turn_off")
    await _setup(
        hass,
        {
            "name": "Floor",
            "mode": "non_sequential",
            "target_minutes": 30,
            "controlled_entity": "input_boolean.floor",
            "coexist": True,
        },
        cheap=(20, 21),  # cheapest slots far from now => no scheduled run
        controlled_state="on",  # turned on by something else
    )
    assert not _called_for(off, "input_boolean.floor")


async def test_boost_button_toggles_on_and_off(hass: HomeAssistant) -> None:
    async_mock_service(hass, "homeassistant", "turn_on")
    async_mock_service(hass, "homeassistant", "turn_off")
    entry = await _setup(
        hass,
        {
            "name": "Floor",
            "mode": "non_sequential",
            "target_minutes": 30,
            "controlled_entity": "input_boolean.floor",
        },
        cheap=(20, 21),  # nothing scheduled now
        controlled_state="off",
    )
    subentry_id = next(iter(entry.subentries))
    coordinator = entry.runtime_data
    button_id = er.async_get(hass).async_get_entity_id("button", DOMAIN, f"{subentry_id}_boost")
    assert coordinator.runtime[subentry_id].boost_until is None

    # First press arms the boost.
    await hass.services.async_call("button", "press", {"entity_id": button_id}, blocking=True)
    await hass.async_block_till_done()
    assert coordinator.runtime[subentry_id].boost_until is not None

    # Second press, while the boost is still active, cancels it.
    await hass.services.async_call("button", "press", {"entity_id": button_id}, blocking=True)
    await hass.async_block_till_done()
    assert coordinator.runtime[subentry_id].boost_until is None


async def test_boost_cancel_backs_off_so_divert_cannot_regrab(hass: HomeAssistant) -> None:
    # Cancelling a boost is an explicit stop. It must leave the load ineligible
    # for an immediate re-grab by the plan or the real-time divert (the summer
    # white-night solar-export case), not just clear the boost.
    async_mock_service(hass, "homeassistant", "turn_on")
    async_mock_service(hass, "homeassistant", "turn_off")
    entry = await _setup(
        hass,
        {
            "name": "Floor",
            "mode": "non_sequential",
            "target_minutes": 30,
            "controlled_entity": "input_boolean.floor",
            "allow_solar": True,
        },
        cheap=(20, 21),
        controlled_state="off",
    )
    subentry_id = next(iter(entry.subentries))
    coordinator = entry.runtime_data
    button_id = er.async_get(hass).async_get_entity_id("button", DOMAIN, f"{subentry_id}_boost")

    await hass.services.async_call("button", "press", {"entity_id": button_id}, blocking=True)
    await hass.async_block_till_done()
    await hass.services.async_call("button", "press", {"entity_id": button_id}, blocking=True)
    await hass.async_block_till_done()

    assert coordinator.runtime[subentry_id].boost_until is None
    actuator = coordinator.actuator
    cfg = coordinator.load_config(subentry_id)
    assert actuator._override_active(subentry_id) is True
    assert actuator._desired_on(subentry_id, cfg) is None  # don't touch
    assert actuator._eligible_for_divert(subentry_id, cfg) is False


async def test_running_sensor_reflects_controlled_entity(hass: HomeAssistant) -> None:
    # No scheduled period now, but a manual on of the contactor must show as
    # "running" (the sensor reflects reality, not the plan).
    entry = await _setup(
        hass,
        {
            "name": "Floor",
            "mode": "non_sequential",
            "target_minutes": 30,
            "controlled_entity": "input_boolean.floor",
        },
        cheap=(20, 21),
        controlled_state="off",
    )
    subentry_id = next(iter(entry.subentries))
    bs_id = er.async_get(hass).async_get_entity_id(
        "binary_sensor", DOMAIN, f"{subentry_id}_running"
    )
    assert hass.states.get(bs_id).state == "off"

    hass.states.async_set("input_boolean.floor", "on")
    await hass.async_block_till_done()
    assert hass.states.get(bs_id).state == "on"
