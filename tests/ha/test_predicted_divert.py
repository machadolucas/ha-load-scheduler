"""Predicted-net-driven, load-aware real-time divert.

The divert decision runs off ``sensor.predicted_net_energy_current_15_min`` (the
projected interval-close net), and a load is only engaged if its own projected
consumption for the rest of the interval still leaves the interval in export.
"""

from __future__ import annotations

from datetime import timedelta

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


async def _setup(
    hass: HomeAssistant, predicted: str, *, draw_kw: float | None = None
) -> MockConfigEntry:
    async_mock_service(hass, "homeassistant", "turn_on")
    async_mock_service(hass, "homeassistant", "turn_off")
    hass.states.async_set("sensor.prices", "ok", _PRICES)
    hass.states.async_set("sensor.net", "-0.3", {"unit_of_measurement": "kWh"})  # exporting now
    hass.states.async_set("sensor.pred", predicted, {"unit_of_measurement": "kWh"})
    hass.states.async_set("sensor.sell", "0.01", {"unit_of_measurement": "EUR/kWh"})
    hass.states.async_set("input_boolean.floor", "off")
    load_data = {
        "name": "Floor",
        "mode": "non_sequential",
        "target_minutes": 30,
        "controlled_entity": "input_boolean.floor",
        "allow_solar": True,
    }
    if draw_kw is not None:
        load_data["draw_kw"] = draw_kw
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
                data=load_data,
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


async def test_load_aware_fit_allows_when_it_fits(hass: HomeAssistant, freezer) -> None:
    # 10 min left in the interval, 3 kW load => 0.5 kWh projected draw. Predicted
    # -0.7 export + 0.5 = -0.2, still <= -0.1 buffer => fits, divert proceeds.
    freezer.move_to("2026-06-17T12:05:00+00:00")
    entry = await _setup(hass, predicted="-0.7", draw_kw=3)
    sid = next(iter(entry.subentries))
    assert sid in entry.runtime_data.actuator._diverted


async def test_load_aware_fit_refuses_when_it_tips_interval(hass: HomeAssistant, freezer) -> None:
    # Same 0.5 kWh projected draw, but predicted only -0.3 export: adding the load
    # tips the interval to +0.2 net import, so it must NOT be diverted even though
    # the bare predicted net is still in export.
    freezer.move_to("2026-06-17T12:05:00+00:00")
    entry = await _setup(hass, predicted="-0.3", draw_kw=3)
    sid = next(iter(entry.subentries))
    assert sid not in entry.runtime_data.actuator._diverted


async def test_fast_shed_when_interval_flips_to_import(hass: HomeAssistant, freezer) -> None:
    # Engaged while it fits, then the prediction flips to import: the asymmetric
    # shed dwell (30s) lets it drop well before the 120s engage dwell elapses.
    freezer.move_to("2026-06-17T12:05:00+00:00")
    entry = await _setup(hass, predicted="-0.7", draw_kw=3)
    sid = next(iter(entry.subentries))
    actuator = entry.runtime_data.actuator
    assert sid in actuator._diverted

    freezer.tick(timedelta(seconds=35))  # past shed dwell, before engage dwell
    hass.states.async_set("sensor.pred", "0.3", {"unit_of_measurement": "kWh"})
    await hass.async_block_till_done()
    assert sid not in actuator._diverted
