"""Solar forecast-only effective cost (M4): excess re-prices slots at sell."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.load_scheduler.const import DOMAIN, SUBENTRY_TYPE_LOAD


def _price_attrs(base) -> dict:
    # Uniform expensive buy (0.20), low sell (0.01) over 6 hours of 15-min slots.
    today = [
        {
            "start": (base + timedelta(minutes=15 * i)).isoformat(),
            "end": (base + timedelta(minutes=15 * (i + 1))).isoformat(),
            "buy": 0.20,
            "sell": 0.01,
        }
        for i in range(24)
    ]
    return {"data_today": today, "data_tomorrow": []}


def _solar_attrs(base, hot_halfhours: set[int]) -> dict:
    # 30-min periods; pv_estimate is average kW.
    return {
        "detailedForecast": [
            {
                "period_start": (base + timedelta(minutes=30 * i)).isoformat(),
                "pv_estimate": 5.0 if i in hot_halfhours else 0.0,
            }
            for i in range(12)
        ]
    }


async def _setup(hass: HomeAssistant, load_data: dict) -> MockConfigEntry:
    base = dt_util.now().replace(second=0, microsecond=0)
    hass.states.async_set("sensor.prices", "ok", _price_attrs(base))
    # Solar excess only during half-hour #2 = base+60..+90 min (price slots 4 & 5).
    hass.states.async_set("sensor.solar", "ok", _solar_attrs(base, {2}))
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "name": "Hub",
            "buy_price_entity": "sensor.prices",
            "solar_forecast_entity": ["sensor.solar"],
            "consumption_baseline_w": 0,
        },
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


def _schedule(hass: HomeAssistant, entry: MockConfigEntry) -> dict:
    sid = next(iter(entry.subentries))
    sensor_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{sid}_schedule")
    return hass.states.get(sensor_id).attributes


async def test_solar_excess_makes_slot_cheapest_and_sourced(hass: HomeAssistant) -> None:
    entry = await _setup(
        hass,
        {
            "name": "Heater",
            "mode": "non_sequential",
            "target_minutes": 15,
            "allow_solar": True,
            "draw_kw": 1.0,
        },
    )
    attrs = _schedule(hass, entry)
    # The single cheapest slot (by effective cost) is the solar one, priced at
    # the sell price (0.01) vs every grid slot at 0.20.
    assert len(attrs["periods"]) == 1
    assert attrs["periods"][0]["source"] == "solar"


async def test_allow_solar_false_ignores_excess(hass: HomeAssistant) -> None:
    entry = await _setup(
        hass,
        {
            "name": "Heater",
            "mode": "non_sequential",
            "target_minutes": 15,
            "allow_solar": False,
            "draw_kw": 1.0,
        },
    )
    attrs = _schedule(hass, entry)
    # With solar valuation off, every slot costs the same (buy); the earliest is
    # chosen and it is grid-sourced.
    assert attrs["periods"][0]["source"] == "grid"
