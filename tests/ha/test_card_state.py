"""Schedule sensor exposes actual on/heating state for the card's dot."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

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


async def test_schedule_sensor_active_and_heating(hass: HomeAssistant) -> None:
    async_mock_service(hass, "homeassistant", "turn_on")
    async_mock_service(hass, "homeassistant", "turn_off")
    hass.states.async_set("sensor.prices", "ok", _prices())
    hass.states.async_set("switch.heater", "off")
    hass.states.async_set("sensor.heater_power", "0", {"unit_of_measurement": "W"})
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
                    "target_minutes": 60,
                    "controlled_entity": "switch.heater",
                    "feedback_entity": "sensor.heater_power",
                    "feedback_idle_w": 50,
                },
            )
        ],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    subentry_id = next(iter(entry.subentries))
    sid = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{subentry_id}_schedule")

    # Off (switch off, element drawing nothing).
    attrs = hass.states.get(sid).attributes
    assert attrs["active"] is False and attrs["heating"] is False

    # On but element idle (power below threshold) => active, not heating.
    hass.states.async_set("switch.heater", "on")
    hass.states.async_set("sensor.heater_power", "20", {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    attrs = hass.states.get(sid).attributes
    assert attrs["active"] is True and attrs["heating"] is False

    # Element drawing power => heating.
    hass.states.async_set("sensor.heater_power", "1500", {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    assert hass.states.get(sid).attributes["heating"] is True
