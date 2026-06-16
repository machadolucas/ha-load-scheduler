"""Interval-aware divert gate via a predicted end-of-interval net-energy sensor."""

from __future__ import annotations

from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.load_scheduler.const import DOMAIN, SUBENTRY_TYPE_LOAD

_PRICES = {
    "data_today": [
        {"start": "2026-06-17T00:00:00+03:00", "end": "2026-06-18T00:00:00+03:00", "buy": 0.20}
    ],
    "data_tomorrow": [],
}


async def _setup(hass: HomeAssistant, predicted: str) -> MockConfigEntry:
    async_mock_service(hass, "homeassistant", "turn_on")
    async_mock_service(hass, "homeassistant", "turn_off")
    hass.states.async_set("sensor.prices", "ok", _PRICES)
    hass.states.async_set("sensor.net", "-0.3", {"unit_of_measurement": "kWh"})  # exporting now
    hass.states.async_set("sensor.pred", predicted, {"unit_of_measurement": "kWh"})
    hass.states.async_set("sensor.sell", "0.01", {"unit_of_measurement": "EUR/kWh"})
    hass.states.async_set("input_boolean.floor", "off")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "name": "Hub",
            "buy_price_entity": "sensor.prices",
            "net_energy_entity": "sensor.net",
            "predicted_net_energy_entity": "sensor.pred",
            "live_sell_entity": "sensor.sell",
            "net_export_threshold": 0.1,
            "sell_threshold": 0.05,
        },
        unique_id="sensor.prices",
        subentries_data=[
            ConfigSubentryData(
                subentry_type=SUBENTRY_TYPE_LOAD,
                title="Floor",
                unique_id=None,
                data={
                    "name": "Floor",
                    "mode": "non_sequential",
                    "target_minutes": 30,
                    "controlled_entity": "input_boolean.floor",
                    "allow_solar": True,
                },
            )
        ],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_predicted_import_blocks_divert(hass: HomeAssistant) -> None:
    # Exporting now (-0.3) but predicted to import (+0.2) by interval end => the
    # gate must keep the load out of the diverted set.
    entry = await _setup(hass, predicted="0.2")
    sid = next(iter(entry.subentries))
    assert sid not in entry.runtime_data.actuator._diverted


async def test_predicted_export_allows_divert(hass: HomeAssistant) -> None:
    # Exporting now AND predicted to still export (-0.3) => divert proceeds.
    entry = await _setup(hass, predicted="-0.3")
    sid = next(iter(entry.subentries))
    assert sid in entry.runtime_data.actuator._diverted
