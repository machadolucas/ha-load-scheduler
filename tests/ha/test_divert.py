"""M6: real-time divert, low-temp safety floor, and manual override."""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)

from custom_components.load_scheduler.const import DOMAIN, SAVE_DELAY, SUBENTRY_TYPE_LOAD


def _price_attrs(cheap: tuple[int, ...], n: int = 24) -> dict:
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
    hass: HomeAssistant, hub_extra: dict, load_data: dict, *, controlled: str, state: str
) -> MockConfigEntry:
    hass.states.async_set("sensor.prices", "ok", _price_attrs(cheap=(20, 21)))
    hass.states.async_set(controlled, state)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"name": "Hub", "buy_price_entity": "sensor.prices", **hub_extra},
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


async def test_divert_turns_on_solar_load_when_exporting(hass: HomeAssistant) -> None:
    on = async_mock_service(hass, "homeassistant", "turn_on")
    async_mock_service(hass, "homeassistant", "turn_off")
    hass.states.async_set("sensor.net", "-0.5")  # exporting 0.5 kWh
    await _setup(
        hass,
        {"net_energy_entity": "sensor.net", "net_export_threshold": 0.1},
        {
            "name": "Heater",
            "mode": "non_sequential",
            "target_minutes": 15,
            "controlled_entity": "input_boolean.heater",
            "allow_solar": True,
        },
        controlled="input_boolean.heater",
        state="off",
    )
    # Nothing is scheduled now (cheap slots are hours away), but live export with
    # no sell gate means the load is diverted on.
    assert any(c.data.get("entity_id") == "input_boolean.heater" for c in on)


async def test_low_temp_safety_floor_forces_heat(hass: HomeAssistant) -> None:
    on = async_mock_service(hass, "homeassistant", "turn_on")
    async_mock_service(hass, "homeassistant", "turn_off")
    hass.states.async_set("sensor.inside", "15")  # below the 18 °C floor
    await _setup(
        hass,
        {},
        {
            "name": "Floor",
            "mode": "non_sequential",
            "target_minutes": 0,
            "controlled_entity": "input_boolean.floor",
            "allow_solar": False,
            "temp_entity": "sensor.inside",
            "temp_min": 18,
        },
        controlled="input_boolean.floor",
        state="off",
    )
    assert any(c.data.get("entity_id") == "input_boolean.floor" for c in on)


async def test_manual_override_suppresses_control(hass: HomeAssistant) -> None:
    on = async_mock_service(hass, "homeassistant", "turn_on")
    async_mock_service(hass, "homeassistant", "turn_off")
    # Scheduled to run now; the controlled entity already matches (on), so no
    # command is issued at setup.
    hass.states.async_set("sensor.prices", "ok", _price_attrs(cheap=(0, 1)))
    hass.states.async_set("input_boolean.heater", "on")
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
                    "controlled_entity": "input_boolean.heater",
                },
            )
        ],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # User manually turns it off — a foreign change. The integration must back
    # off and NOT turn it back on despite the active scheduled period.
    hass.states.async_set("input_boolean.heater", "off")
    await hass.async_block_till_done()

    assert on == []


async def test_divert_does_not_flicker_satisfied_load(hass: HomeAssistant, freezer) -> None:
    """An idle diverted load (element drawing nothing, e.g. a full tank) is left
    powered, not flicked off — the relay-flicker regression. It costs nothing, the
    export still flows to other loads, and it draws again on its own thermostat."""
    on = async_mock_service(hass, "homeassistant", "turn_on")
    off = async_mock_service(hass, "homeassistant", "turn_off")
    hass.states.async_set("sensor.net", "-0.5")  # exporting
    hass.states.async_set("sensor.heater_power", "0")  # element idle (full tank)
    await _setup(
        hass,
        {"net_energy_entity": "sensor.net", "net_export_threshold": 0.1},
        {
            "name": "Heater",
            "mode": "non_sequential",
            "target_minutes": 15,
            "controlled_entity": "input_boolean.heater",
            "feedback_entity": "sensor.heater_power",
            "feedback_idle_w": 50,
            "allow_solar": True,
        },
        controlled="input_boolean.heater",
        state="off",
    )
    assert any(c.data.get("entity_id") == "input_boolean.heater" for c in on)

    # The switch reports on but its element stays idle. The actuator must NOT
    # switch it back off — not immediately, and not after the dwell elapses.
    off.clear()
    hass.states.async_set("input_boolean.heater", "on")
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=200))  # past the divert dwell
    hass.states.async_set("sensor.net", "-0.6")  # still exporting; re-evaluate
    await hass.async_block_till_done()
    assert not any(c.data.get("entity_id") == "input_boolean.heater" for c in off)


def _calls(calls, entity_id: str) -> int:
    return len([c for c in calls if c.data.get("entity_id") == entity_id])


_HEATER = {
    "name": "Heater",
    "mode": "non_sequential",
    "target_minutes": 15,
    "controlled_entity": "input_boolean.heater",
    "allow_solar": True,
}
_REACTIVE_HUB = {"net_energy_entity": "sensor.net", "net_export_threshold": 0.1}


async def test_net_sensor_unavailable_releases_diverted_load(hass: HomeAssistant) -> None:
    async_mock_service(hass, "homeassistant", "turn_on")
    off = async_mock_service(hass, "homeassistant", "turn_off")
    hass.states.async_set("sensor.net", "-0.5")
    entry = await _setup(
        hass, _REACTIVE_HUB, _HEATER, controlled="input_boolean.heater", state="off"
    )
    sid = next(iter(entry.subentries))
    actuator = entry.runtime_data.actuator
    assert sid in actuator._diverted
    hass.states.async_set("input_boolean.heater", "on")
    await hass.async_block_till_done()

    hass.states.async_set("sensor.net", "unavailable")
    await hass.async_block_till_done()

    # No reading, no surplus: divert lets go and the (empty) plan decides.
    assert sid not in actuator._diverted
    assert _calls(off, "input_boolean.heater") == 1


async def test_disabling_a_diverted_load_releases_it_even_with_net_unavailable(
    hass: HomeAssistant,
) -> None:
    async_mock_service(hass, "homeassistant", "turn_on")
    async_mock_service(hass, "homeassistant", "turn_off")
    hass.states.async_set("sensor.net", "-0.5")
    # The predicted sensor governs; the live net going away mustn't freeze divert.
    hass.states.async_set("sensor.pred", "-0.5")
    entry = await _setup(
        hass,
        {**_REACTIVE_HUB, "predicted_net_energy_entity": "sensor.pred"},
        _HEATER,
        controlled="input_boolean.heater",
        state="off",
    )
    sid = next(iter(entry.subentries))
    coordinator = entry.runtime_data
    actuator = coordinator.actuator
    assert sid in actuator._diverted
    hass.states.async_set("sensor.net", "unavailable")
    await hass.async_block_till_done()
    assert sid in actuator._diverted  # the live net isn't needed in predicted mode

    coordinator.runtime[sid].enabled = False
    actuator._update_divert()
    assert sid not in actuator._diverted
    # And the divert branch of the precedence respects `enabled` on its own.
    actuator._diverted.add(sid)
    assert actuator._desired_on(sid, coordinator.load_config(sid)) is False


async def test_predicted_only_hub_diverts_without_a_live_net_sensor(
    hass: HomeAssistant,
) -> None:
    on = async_mock_service(hass, "homeassistant", "turn_on")
    async_mock_service(hass, "homeassistant", "turn_off")
    hass.states.async_set("sensor.pred", "-0.5")
    await _setup(
        hass,
        {"predicted_net_energy_entity": "sensor.pred", "net_export_threshold": 0.1},
        _HEATER,
        controlled="input_boolean.heater",
        state="off",
    )
    assert _calls(on, "input_boolean.heater") == 1


async def test_divert_honours_min_run_before_shedding(hass: HomeAssistant, freezer) -> None:
    async_mock_service(hass, "homeassistant", "turn_on")
    off = async_mock_service(hass, "homeassistant", "turn_off")
    hass.states.async_set("sensor.net", "-0.5")
    entry = await _setup(
        hass,
        _REACTIVE_HUB,
        {**_HEATER, "min_run_minutes": 30},
        controlled="input_boolean.heater",
        state="off",
    )
    sid = next(iter(entry.subentries))
    actuator = entry.runtime_data.actuator
    hass.states.async_set("input_boolean.heater", "on")
    await hass.async_block_till_done()

    freezer.tick(timedelta(seconds=200))  # past the divert dwell
    hass.states.async_set("sensor.net", "0.5")  # importing
    await hass.async_block_till_done()
    assert sid in actuator._diverted  # protected by min-run
    assert _calls(off, "input_boolean.heater") == 0

    freezer.tick(timedelta(minutes=30))
    hass.states.async_set("sensor.net", "0.6")
    await hass.async_block_till_done()
    assert sid not in actuator._diverted
    assert _calls(off, "input_boolean.heater") == 1


async def test_divert_honours_min_off_before_reengaging(hass: HomeAssistant, freezer) -> None:
    on = async_mock_service(hass, "homeassistant", "turn_on")
    async_mock_service(hass, "homeassistant", "turn_off")
    # Off for a long time already, so the first engage isn't gated.
    freezer.tick(timedelta(hours=-2))
    hass.states.async_set("input_boolean.heater", "off")
    freezer.tick(timedelta(hours=2))
    hass.states.async_set("sensor.net", "-0.5")
    entry = await _setup(
        hass,
        _REACTIVE_HUB,
        {**_HEATER, "min_off_minutes": 30},
        controlled="input_boolean.heater",
        state="off",
    )
    sid = next(iter(entry.subentries))
    actuator = entry.runtime_data.actuator
    assert _calls(on, "input_boolean.heater") == 1
    hass.states.async_set("input_boolean.heater", "on")
    await hass.async_block_till_done()

    freezer.tick(timedelta(seconds=200))
    hass.states.async_set("sensor.net", "0.5")  # importing: shed
    await hass.async_block_till_done()
    hass.states.async_set("input_boolean.heater", "off")  # our off confirms
    await hass.async_block_till_done()

    freezer.tick(timedelta(seconds=200))
    hass.states.async_set("sensor.net", "-0.6")  # exporting again, but too soon
    await hass.async_block_till_done()
    assert sid not in actuator._diverted
    assert _calls(on, "input_boolean.heater") == 1

    freezer.tick(timedelta(minutes=30))
    hass.states.async_set("sensor.net", "-0.7")
    await hass.async_block_till_done()
    assert sid in actuator._diverted
    assert _calls(on, "input_boolean.heater") == 2


_FLOOR = {
    "name": "Floor",
    "mode": "non_sequential",
    "target_minutes": 0,
    "controlled_entity": "input_boolean.floor",
    "allow_solar": False,
    "temp_entity": "sensor.inside",
    "temp_min": 18,
}


async def test_low_temp_floor_heats_without_price_data(hass: HomeAssistant) -> None:
    # C4: the safety floor sat below the plan-error check, so a dead price feed
    # plus a cold room meant no heat at all.
    on = async_mock_service(hass, "homeassistant", "turn_on")
    async_mock_service(hass, "homeassistant", "turn_off")
    hass.states.async_set("sensor.inside", "20")  # warm: nothing to do yet
    entry = await _setup(hass, {}, _FLOOR, controlled="input_boolean.floor", state="off")
    sid = next(iter(entry.subentries))
    hass.states.async_set("sensor.prices", "unavailable", {})
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert entry.runtime_data.data[sid].error == "no_price_data"
    assert _calls(on, "input_boolean.floor") == 0

    hass.states.async_set("sensor.inside", "15")  # the room gets cold
    await hass.async_block_till_done()
    assert _calls(on, "input_boolean.floor") == 1


async def test_low_temp_floor_has_hysteresis(hass: HomeAssistant) -> None:
    async_mock_service(hass, "homeassistant", "turn_on")
    off = async_mock_service(hass, "homeassistant", "turn_off")
    hass.states.async_set("sensor.inside", "17.9")
    await _setup(hass, {}, _FLOOR, controlled="input_boolean.floor", state="off")
    hass.states.async_set("input_boolean.floor", "on")
    await hass.async_block_till_done()

    hass.states.async_set("sensor.inside", "18.1")  # just above temp_min: hold
    await hass.async_block_till_done()
    hass.states.async_set("sensor.inside", "18.4")
    await hass.async_block_till_done()
    assert _calls(off, "input_boolean.floor") == 0

    hass.states.async_set("sensor.inside", "18.5")  # temp_min + hysteresis: release
    await hass.async_block_till_done()
    assert _calls(off, "input_boolean.floor") == 1


async def test_feedback_entity_is_not_watched_by_the_actuator(hass: HomeAssistant) -> None:
    async_mock_service(hass, "homeassistant", "turn_on")
    async_mock_service(hass, "homeassistant", "turn_off")
    entry = await _setup(
        hass,
        _REACTIVE_HUB,
        {**_HEATER, "feedback_entity": "sensor.heater_power"},
        controlled="input_boolean.heater",
        state="off",
    )
    watched = entry.runtime_data.actuator._watched_entities()
    assert "sensor.heater_power" not in watched
    assert "input_boolean.heater" in watched


@pytest.mark.parametrize("coexist", [False, True])
async def test_floor_release_without_price_data_switches_our_run_off(
    hass: HomeAssistant, coexist: bool
) -> None:
    # P1: with no usable plan, a released floor returned "don't touch", so the
    # heat it started stayed on indefinitely.
    async_mock_service(hass, "homeassistant", "turn_on")
    off = async_mock_service(hass, "homeassistant", "turn_off")
    hass.states.async_set("sensor.inside", "20")
    entry = await _setup(
        hass,
        {},
        {**_FLOOR, "coexist": coexist},
        controlled="input_boolean.floor",
        state="off",
    )
    sid = next(iter(entry.subentries))
    hass.states.async_set("sensor.prices", "unavailable", {})
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    hass.states.async_set("sensor.inside", "15")
    await hass.async_block_till_done()
    hass.states.async_set("input_boolean.floor", "on")  # our floor run confirms
    await hass.async_block_till_done()
    assert entry.runtime_data.runtime[sid].driven is True

    hass.states.async_set("sensor.inside", "19")  # past temp_min + hysteresis
    await hass.async_block_till_done()
    assert _calls(off, "input_boolean.floor") == 1
    hass.states.async_set("input_boolean.floor", "off")
    await hass.async_block_till_done()
    assert entry.runtime_data.runtime[sid].driven is False


async def test_floor_release_without_price_data_leaves_an_external_run(
    hass: HomeAssistant,
) -> None:
    async_mock_service(hass, "homeassistant", "turn_on")
    off = async_mock_service(hass, "homeassistant", "turn_off")
    hass.states.async_set("sensor.inside", "15")
    # Somebody else already has the coexist floor on: the floor doesn't own it.
    entry = await _setup(
        hass, {}, {**_FLOOR, "coexist": True}, controlled="input_boolean.floor", state="on"
    )
    sid = next(iter(entry.subentries))
    hass.states.async_set("sensor.prices", "unavailable", {})
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert entry.runtime_data.runtime[sid].driven is False

    hass.states.async_set("sensor.inside", "19")
    await hass.async_block_till_done()
    assert _calls(off, "input_boolean.floor") == 0


async def test_floor_release_with_a_valid_plan_falls_through_to_it(
    hass: HomeAssistant,
) -> None:
    async_mock_service(hass, "homeassistant", "turn_on")
    off = async_mock_service(hass, "homeassistant", "turn_off")
    hass.states.async_set("sensor.inside", "15")
    entry = await _setup(
        hass,
        {},
        {**_FLOOR, "target_minutes": 30},
        controlled="input_boolean.floor",
        state="off",
    )
    hass.states.async_set("input_boolean.floor", "on")
    await hass.async_block_till_done()
    # Cheap slots now: the plan wants it on anyway.
    hass.states.async_set("sensor.prices", "ok", _price_attrs(cheap=(0, 1)))
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    hass.states.async_set("sensor.inside", "19")
    await hass.async_block_till_done()
    assert _calls(off, "input_boolean.floor") == 0


@pytest.mark.parametrize("coexist", [False, True])
async def test_owned_floor_run_is_switched_off_after_a_restart_without_prices(
    hass: HomeAssistant, freezer, coexist: bool
) -> None:
    # The floor latch is in memory only; ownership is persisted. Back from a
    # restart with the room warm and no price data, the run we own must still
    # end — with nothing else in memory to say why it was on.
    async_mock_service(hass, "homeassistant", "turn_on")
    off = async_mock_service(hass, "homeassistant", "turn_off")
    hass.states.async_set("sensor.inside", "15")
    entry = await _setup(
        hass, {}, {**_FLOOR, "coexist": coexist}, controlled="input_boolean.floor", state="off"
    )
    sid = next(iter(entry.subentries))
    hass.states.async_set("input_boolean.floor", "on")  # our floor run confirms
    await hass.async_block_till_done()
    assert entry.runtime_data.runtime[sid].driven is True
    freezer.tick(timedelta(seconds=SAVE_DELAY + 1))  # flush the debounced Store
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    hass.states.async_set("sensor.inside", "20")
    hass.states.async_set("sensor.prices", "unavailable", {})
    off.clear()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.runtime_data.runtime[sid].driven is True  # restored
    assert entry.runtime_data.data[sid].error == "no_price_data"
    assert _calls(off, "input_boolean.floor") == 1


async def test_external_coexist_run_untouched_without_prices(hass: HomeAssistant) -> None:
    async_mock_service(hass, "homeassistant", "turn_on")
    off = async_mock_service(hass, "homeassistant", "turn_off")
    hass.states.async_set("sensor.inside", "20")
    hass.states.async_set("sensor.prices", "unavailable", {})
    hass.states.async_set("input_boolean.floor", "on")  # somebody else's run
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"name": "Hub", "buy_price_entity": "sensor.prices"},
        unique_id="sensor.prices",
        subentries_data=[
            ConfigSubentryData(
                subentry_type=SUBENTRY_TYPE_LOAD,
                title="Floor",
                unique_id=None,
                data={**_FLOOR, "coexist": True},
            )
        ],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    sid = next(iter(entry.subentries))
    assert entry.runtime_data.data[sid].error == "no_price_data"
    assert entry.runtime_data.runtime[sid].driven is False
    hass.states.async_set("sensor.inside", "21")  # any watched change: reconcile
    await hass.async_block_till_done()
    assert _calls(off, "input_boolean.floor") == 0


async def test_owned_run_with_unreadable_temp_and_no_prices_is_switched_off(
    hass: HomeAssistant,
) -> None:
    async_mock_service(hass, "homeassistant", "turn_on")
    off = async_mock_service(hass, "homeassistant", "turn_off")
    hass.states.async_set("sensor.inside", "15")
    entry = await _setup(hass, {}, _FLOOR, controlled="input_boolean.floor", state="off")
    sid = next(iter(entry.subentries))
    hass.states.async_set("input_boolean.floor", "on")
    await hass.async_block_till_done()
    hass.states.async_set("sensor.prices", "unavailable", {})
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert _calls(off, "input_boolean.floor") == 0  # still cold: floor holds

    hass.states.async_set("sensor.inside", "unavailable")
    await hass.async_block_till_done()
    assert entry.runtime_data.runtime[sid].driven is True
    assert _calls(off, "input_boolean.floor") == 1
