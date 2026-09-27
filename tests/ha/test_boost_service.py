"""The `load_scheduler.boost` service: device targeting and duration."""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.load_scheduler.const import DOMAIN, SERVICE_BOOST, SUBENTRY_TYPE_LOAD

ENTITY = "input_boolean.heater"


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


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    """One load with nothing scheduled now, so any run must come from the boost."""
    hass.states.async_set("sensor.prices", "ok", _price_attributes(cheap=(20, 21)))
    hass.states.async_set(ENTITY, "off")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"name": "Hub", "buy_price_entity": "sensor.prices"},
        unique_id="sensor.prices",
        subentries_data=[
            ConfigSubentryData(
                subentry_type=SUBENTRY_TYPE_LOAD,
                title="Heater",
                unique_id=None,
                data={
                    "name": "Heater",
                    "mode": "non_sequential",
                    "target_minutes": 30,
                    "controlled_entity": ENTITY,
                },
            )
        ],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _device_id(hass: HomeAssistant, subentry_id: str) -> str:
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, subentry_id)})
    assert device is not None
    return device.id


async def test_boost_service_runs_load_for_requested_minutes(hass: HomeAssistant) -> None:
    on = async_mock_service(hass, "homeassistant", "turn_on")
    async_mock_service(hass, "homeassistant", "turn_off")
    entry = await _setup(hass)
    subentry_id = next(iter(entry.subentries))
    coordinator = entry.runtime_data

    await hass.services.async_call(
        DOMAIN,
        SERVICE_BOOST,
        {"device_id": _device_id(hass, subentry_id), "minutes": 90},
        blocking=True,
    )
    await hass.async_block_till_done()

    expected = dt_util.utcnow() + timedelta(minutes=90)
    boost_until = coordinator.runtime[subentry_id].boost_until
    assert boost_until is not None
    assert abs((boost_until - expected).total_seconds()) < 5
    # The plan and the actuator pick it up exactly as they do for the button.
    assert any(c.data.get("entity_id") == ENTITY for c in on)


async def test_boost_service_defaults_to_the_target_runtime(hass: HomeAssistant) -> None:
    async_mock_service(hass, "homeassistant", "turn_on")
    async_mock_service(hass, "homeassistant", "turn_off")
    entry = await _setup(hass)
    subentry_id = next(iter(entry.subentries))
    coordinator = entry.runtime_data

    await hass.services.async_call(
        DOMAIN, SERVICE_BOOST, {"device_id": _device_id(hass, subentry_id)}, blocking=True
    )
    await hass.async_block_till_done()

    expected = dt_util.utcnow() + timedelta(minutes=30)  # the load's target
    boost_until = coordinator.runtime[subentry_id].boost_until
    assert abs((boost_until - expected).total_seconds()) < 5


async def test_boost_service_rejects_a_foreign_device(hass: HomeAssistant) -> None:
    async_mock_service(hass, "homeassistant", "turn_on")
    async_mock_service(hass, "homeassistant", "turn_off")
    await _setup(hass)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, SERVICE_BOOST, {"device_id": "not-a-load-scheduler-device"}, blocking=True
        )


async def test_boost_service_accepts_an_area_target(hass: HomeAssistant) -> None:
    # services.yaml declares a `target:`, so the UI offers areas/floors/labels;
    # the schema used to reject all of them.
    async_mock_service(hass, "homeassistant", "turn_on")
    async_mock_service(hass, "homeassistant", "turn_off")
    entry = await _setup(hass)
    subentry_id = next(iter(entry.subentries))
    area = ar.async_get(hass).async_create("Utility")
    dr.async_get(hass).async_update_device(_device_id(hass, subentry_id), area_id=area.id)

    await hass.services.async_call(
        DOMAIN, SERVICE_BOOST, {"area_id": area.id, "minutes": 20}, blocking=True
    )
    await hass.async_block_till_done()

    boost_until = entry.runtime_data.runtime[subentry_id].boost_until
    expected = dt_util.utcnow() + timedelta(minutes=20)
    assert boost_until is not None
    assert abs((boost_until - expected).total_seconds()) < 5


async def test_boost_service_boosts_a_load_named_twice_once(hass: HomeAssistant) -> None:
    async_mock_service(hass, "homeassistant", "turn_on")
    async_mock_service(hass, "homeassistant", "turn_off")
    entry = await _setup(hass)
    subentry_id = next(iter(entry.subentries))
    coordinator = entry.runtime_data
    calls: list[str] = []
    real_boost = coordinator.async_boost

    async def _spy(sid, minutes):
        calls.append(sid)
        await real_boost(sid, minutes)

    coordinator.async_boost = _spy
    button_id = er.async_get(hass).async_get_entity_id("button", DOMAIN, f"{subentry_id}_boost")
    await hass.services.async_call(
        DOMAIN,
        SERVICE_BOOST,
        {"device_id": _device_id(hass, subentry_id), "entity_id": button_id},
        blocking=True,
    )
    assert calls == [subentry_id]


async def test_boost_service_rejects_a_foreign_entity(hass: HomeAssistant) -> None:
    async_mock_service(hass, "homeassistant", "turn_on")
    async_mock_service(hass, "homeassistant", "turn_off")
    await _setup(hass)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, SERVICE_BOOST, {"entity_id": "input_boolean.not_a_load"}, blocking=True
        )
