"""M6: real-time divert, low-temp safety floor, and manual override."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.load_scheduler.const import DOMAIN, SUBENTRY_TYPE_LOAD


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
