"""Solar forecast-only effective cost (M4): excess re-prices slots at sell."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.load_scheduler.const import DOMAIN, SUBENTRY_TYPE_LOAD
from custom_components.load_scheduler.coordinator import LoadSchedulerCoordinator
from custom_components.load_scheduler.engine import Period, RunSource, Slot


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


async def test_solar_allocation_by_priority(hass: HomeAssistant) -> None:
    # Two solar loads compete for the same surplus (two slots). The higher
    # priority load (target 30 min) claims both solar slots; the lower-priority
    # load then sees no excess and falls back to a grid slot.
    base = dt_util.now().replace(second=0, microsecond=0)
    hass.states.async_set("sensor.prices", "ok", _price_attrs(base))
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
                title="High",
                unique_id=None,
                data={
                    "name": "High",
                    "mode": "non_sequential",
                    "target_minutes": 30,
                    "allow_solar": True,
                    "draw_kw": 5.0,
                    "priority": 10,
                },
            ),
            ConfigSubentryData(
                subentry_type=SUBENTRY_TYPE_LOAD,
                title="Low",
                unique_id=None,
                data={
                    "name": "Low",
                    "mode": "non_sequential",
                    "target_minutes": 15,
                    "allow_solar": True,
                    "draw_kw": 5.0,
                    "priority": 1,
                },
            ),
        ],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    reg = er.async_get(hass)
    by_title = {sub.title: sid for sid, sub in entry.subentries.items()}

    def source_of(title: str) -> str:
        sid = by_title[title]
        sensor_id = reg.async_get_entity_id("sensor", DOMAIN, f"{sid}_schedule")
        return hass.states.get(sensor_id).attributes["periods"][0]["source"]

    assert source_of("High") == "solar"
    assert source_of("Low") == "grid"


def _solar_slot() -> Slot:
    t0 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    return Slot(start=t0, end=t0 + timedelta(hours=1), buy=0.2, sell=0.05, excess_kwh=4.0)


def test_consume_excess_charges_a_run_already_under_way() -> None:
    # The engine clips the in-progress slot to `now`, so the period starts
    # mid-slot. Matching on the slot boundary would leave that slot's excess
    # untouched and let a lower-priority load claim the same kWh twice.
    slot = _solar_slot()
    residual = {slot.start: 4.0}
    # A 4 kW load running the last half-hour of the slot uses 2 kWh of it.
    running = [Period(slot.start + timedelta(minutes=30), slot.end, RunSource.SOLAR, 0.05)]
    LoadSchedulerCoordinator._consume_excess(residual, [slot], running, 4.0)
    assert residual[slot.start] == pytest.approx(2.0)


def test_consume_excess_ignores_a_period_that_misses_the_slot() -> None:
    slot = _solar_slot()
    residual = {slot.start: 4.0}
    elsewhere = [Period(slot.end, slot.end + timedelta(minutes=30), RunSource.GRID, 0.2)]
    LoadSchedulerCoordinator._consume_excess(residual, [slot], elsewhere, 4.0)
    assert residual[slot.start] == pytest.approx(4.0)
