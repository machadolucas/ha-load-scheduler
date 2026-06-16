"""Beyond-horizon forecast price: bet on a cheaper following-24h window.

The real day-ahead prices only ever reach ~tomorrow. A predictor sensor can
publish an *estimate* of the day after (from Finland wind + temperature + solar
forecasts). With a multi-day horizon the engine should defer an expensive
"today" to that forecast-cheaper window — minus a configurable confidence
margin so it only bets when the forecast is clearly better.
"""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.load_scheduler.const import DOMAIN, SUBENTRY_TYPE_LOAD


def _slots(start, count: int, price: float) -> list[dict]:
    """`count` hourly slots from `start` at a flat buy price."""
    return [
        {
            "start": (start + timedelta(hours=i)).isoformat(),
            "end": (start + timedelta(hours=i + 1)).isoformat(),
            "buy": price,
        }
        for i in range(count)
    ]


async def _setup(hass: HomeAssistant, load_data: dict, *, margin: float = 0.0) -> MockConfigEntry:
    now = dt_util.now().replace(minute=0, second=0, microsecond=0)
    # Real prices: the next 24 h are EXPENSIVE.
    hass.states.async_set(
        "sensor.prices",
        "ok",
        {"data_today": _slots(now, 24, 0.20), "data_tomorrow": []},
    )
    # Predictor forecast: the *following* 24 h (+24..+48 h) are CHEAP.
    hass.states.async_set(
        "sensor.forecast",
        "ok",
        {"data_today": _slots(now + timedelta(hours=24), 24, 0.05), "data_tomorrow": []},
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "name": "Hub",
            "buy_price_entity": "sensor.prices",
            "forecast_price_entity": "sensor.forecast",
            "forecast_price_margin": margin,
        },
        unique_id="sensor.prices",
        subentries_data=[
            ConfigSubentryData(
                subentry_type=SUBENTRY_TYPE_LOAD,
                title="Load",
                unique_id=None,
                data=load_data,
            )
        ],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _first_start(hass: HomeAssistant, entry: MockConfigEntry):
    subentry_id = next(iter(entry.subentries))
    sensor_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{subentry_id}_schedule")
    periods = hass.states.get(sensor_id).attributes["periods"]
    assert periods, "expected at least one scheduled period"
    return dt_util.parse_datetime(periods[0]["start"])


async def test_defers_to_forecast_cheaper_following_day(hass: HomeAssistant) -> None:
    # 48 h horizon + cheap forecast window => schedule in the forecast window.
    entry = await _setup(
        hass,
        {
            "name": "Heater",
            "mode": "non_sequential",
            "target_minutes": 60,
            "horizon_hours": 48,
        },
    )
    start = _first_start(hass, entry)
    assert start >= dt_util.now() + timedelta(hours=23)


async def test_margin_keeps_run_today_when_forecast_not_clearly_cheaper(
    hass: HomeAssistant,
) -> None:
    # Forecast 0.05 + margin 0.20 = 0.25 > real 0.20 => no benefit to deferring,
    # so the run stays in the (expensive but known) next-24 h window.
    entry = await _setup(
        hass,
        {
            "name": "Heater",
            "mode": "non_sequential",
            "target_minutes": 60,
            "horizon_hours": 48,
        },
        margin=0.20,
    )
    start = _first_start(hass, entry)
    assert start < dt_util.now() + timedelta(hours=24)
