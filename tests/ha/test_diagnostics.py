"""The diagnostics dump carries the actuator's in-memory state per load."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import Context, HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.load_scheduler.const import DOMAIN, SUBENTRY_TYPE_LOAD
from custom_components.load_scheduler.diagnostics import async_get_config_entry_diagnostics

ENTITY = "input_boolean.heater"


async def test_diagnostics_include_actuator_state(hass: HomeAssistant) -> None:
    async_mock_service(hass, "homeassistant", "turn_on")
    async_mock_service(hass, "homeassistant", "turn_off")
    base = dt_util.now().replace(second=0, microsecond=0)
    hass.states.async_set(
        "sensor.prices",
        "ok",
        {
            "data_today": [
                {
                    "start": (base + timedelta(minutes=15 * i)).isoformat(),
                    "end": (base + timedelta(minutes=15 * (i + 1))).isoformat(),
                    "buy": 0.01 if i in (20, 21) else 0.20,
                }
                for i in range(24)
            ],
            "data_tomorrow": [],
        },
    )
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
    hass.states.async_set(ENTITY, "on", context=Context(user_id="u1"))  # manual on
    await hass.async_block_till_done()

    dump = await async_get_config_entry_diagnostics(hass, entry)
    actuator = dump["loads"][next(iter(entry.subentries))]["actuator"]
    assert actuator["override_active"] is True
    assert actuator["override_until"] is not None
    assert actuator["on_since"] is not None
    assert actuator["diverted"] is False
    assert actuator["floor_active"] is False
    assert actuator["pending_command"] is None
