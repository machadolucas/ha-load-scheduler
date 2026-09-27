"""Coexist (top-up) loads, boost toggle, and reality-based running sensor."""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
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


@pytest.mark.parametrize("coexist", [False, True])
async def test_cancel_boost_switches_our_run_off(hass: HomeAssistant, coexist: bool) -> None:
    # B1: cancelling a boost used to only back off — the override then blocked
    # every reconcile, so a normal load ran through the grace and a coexist one
    # (disowned at the same time) ran forever.
    on = async_mock_service(hass, "homeassistant", "turn_on")
    off = async_mock_service(hass, "homeassistant", "turn_off")
    entity = "input_boolean.floor"
    entry = await _setup(
        hass,
        {
            "name": "Floor",
            "mode": "non_sequential",
            "target_minutes": 30,
            "controlled_entity": entity,
            "coexist": coexist,
        },
        cheap=(20, 21),  # nothing scheduled now: the run is the boost's
        controlled_state="off",
    )
    subentry_id = next(iter(entry.subentries))
    coordinator = entry.runtime_data
    button_id = er.async_get(hass).async_get_entity_id("button", DOMAIN, f"{subentry_id}_boost")
    await hass.services.async_call("button", "press", {"entity_id": button_id}, blocking=True)
    await hass.async_block_till_done()
    assert _called_for(on, entity)
    hass.states.async_set(entity, "on")  # the relay confirms
    await hass.async_block_till_done()
    assert coordinator.runtime[subentry_id].driven is True

    await hass.services.async_call("button", "press", {"entity_id": button_id}, blocking=True)
    await hass.async_block_till_done()

    assert _called_for(off, entity)
    assert coordinator.runtime[subentry_id].boost_until is None
    assert coordinator.actuator._override_active(subentry_id) is True  # no re-grab
    # Ours until the off is seen; then released, and the echo isn't "manual".
    hass.states.async_set(entity, "off")
    await hass.async_block_till_done()
    assert coordinator.runtime[subentry_id].driven is False
    assert coordinator.foreign_log.get(subentry_id, []) == []


async def test_cancel_boost_leaves_an_external_coexist_run_alone(hass: HomeAssistant) -> None:
    # Boosting a coexist load somebody else already has on takes no ownership,
    # so cancelling it must not cut that external run short either.
    async_mock_service(hass, "homeassistant", "turn_on")
    off = async_mock_service(hass, "homeassistant", "turn_off")
    entity = "input_boolean.floor"
    entry = await _setup(
        hass,
        {
            "name": "Floor",
            "mode": "non_sequential",
            "target_minutes": 30,
            "controlled_entity": entity,
            "coexist": True,
        },
        cheap=(20, 21),
        controlled_state="on",  # an external comfort run
    )
    subentry_id = next(iter(entry.subentries))
    button_id = er.async_get(hass).async_get_entity_id("button", DOMAIN, f"{subentry_id}_boost")
    await hass.services.async_call("button", "press", {"entity_id": button_id}, blocking=True)
    await hass.async_block_till_done()
    await hass.services.async_call("button", "press", {"entity_id": button_id}, blocking=True)
    await hass.async_block_till_done()

    assert not _called_for(off, entity)
    assert entry.runtime_data.runtime[subentry_id].driven is False


@pytest.mark.parametrize("coexist", [False, True])
async def test_cancel_boost_retries_a_lost_off_until_observed(
    hass: HomeAssistant, freezer, coexist: bool
) -> None:
    # P2: the cancel's back-off made every reconcile "don't touch", so a lost
    # turn_off was never retried and the cancelled run kept going. The stop
    # request holds the load off and re-sends until the off is seen.
    async_mock_service(hass, "homeassistant", "turn_on")
    off = async_mock_service(hass, "homeassistant", "turn_off")
    entity = "input_boolean.floor"
    entry = await _setup(
        hass,
        {
            "name": "Floor",
            "mode": "non_sequential",
            "target_minutes": 30,
            "controlled_entity": entity,
            "coexist": coexist,
        },
        cheap=(20, 21),
        controlled_state="off",
    )
    subentry_id = next(iter(entry.subentries))
    coordinator = entry.runtime_data
    actuator = coordinator.actuator
    button_id = er.async_get(hass).async_get_entity_id("button", DOMAIN, f"{subentry_id}_boost")
    await hass.services.async_call("button", "press", {"entity_id": button_id}, blocking=True)
    await hass.async_block_till_done()
    hass.states.async_set(entity, "on")
    await hass.async_block_till_done()

    await hass.services.async_call("button", "press", {"entity_id": button_id}, blocking=True)
    await hass.async_block_till_done()
    sent = [c for c in off if c.data.get("entity_id") == entity]
    assert len(sent) == 1
    assert actuator.diagnostics(subentry_id)["stop_requested"] is not None

    # The relay never follows. The off is retried at the resend pace, even
    # though the override grace is running.
    for expected in (2, 3):
        freezer.tick(timedelta(seconds=61))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        assert len([c for c in off if c.data.get("entity_id") == entity]) == expected

    hass.states.async_set(entity, "off")  # finally lands
    await hass.async_block_till_done()
    assert actuator.diagnostics(subentry_id)["stop_requested"] is None
    assert coordinator.runtime[subentry_id].driven is False
    assert coordinator.foreign_log.get(subentry_id, []) == []
    # ...and the normal back-off now runs from the observed off.
    until = actuator._override_until[subentry_id]
    assert (until - dt_util.utcnow()).total_seconds() > 590


async def test_stop_request_expires_and_yields_to_a_manual_on(hass: HomeAssistant, freezer) -> None:
    async_mock_service(hass, "homeassistant", "turn_on")
    async_mock_service(hass, "homeassistant", "turn_off")
    entity = "input_boolean.floor"
    entry = await _setup(
        hass,
        {
            "name": "Floor",
            "mode": "non_sequential",
            "target_minutes": 30,
            "controlled_entity": entity,
        },
        cheap=(20, 21),
        controlled_state="on",  # non-coexist and on: the stop is ours to make
    )
    subentry_id = next(iter(entry.subentries))
    actuator = entry.runtime_data.actuator
    cfg = entry.runtime_data.load_config(subentry_id)
    hass.states.async_set(entity, "off")  # the setup reconcile's off lands
    await hass.async_block_till_done()
    hass.states.async_set(entity, "on", context=Context(user_id="u1"))
    await hass.async_block_till_done()

    await actuator.async_manual_stop(subentry_id)
    assert actuator._desired_on(subentry_id, cfg) is False  # outranks the grace
    # A relay that never confirms isn't held forever.
    freezer.tick(timedelta(seconds=901))
    actuator._desired_on(subentry_id, cfg)
    assert actuator.diagnostics(subentry_id)["stop_requested"] is None

    # A genuine manual on supersedes a stop request.
    hass.states.async_set(entity, "off", context=Context(user_id="u1"))
    await hass.async_block_till_done()
    actuator._stop_requested[subentry_id] = dt_util.utcnow()
    hass.states.async_set(entity, "on", context=Context(user_id="u1"))
    await hass.async_block_till_done()
    assert actuator.diagnostics(subentry_id)["stop_requested"] is None
