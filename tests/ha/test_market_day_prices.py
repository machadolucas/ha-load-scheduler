"""A price feed anchored to the *market* day, not the local one.

Nord Pool's delivery day runs on CET/CEST, so a sensor that publishes it as
``data_today`` starts that list at 01:00 Helsinki time and leaves the slots for
the first local hour of the day in ``data_yesterday``. Ignoring that list left
every load unschedulable between 00:00 and 01:00 — the cheapest quarter-hour of
the night was simply invisible.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.load_scheduler.const import DOMAIN, SUBENTRY_TYPE_LOAD

# The night of the incident, in UTC so the test is timezone-independent.
NOW = datetime.fromisoformat("2026-08-21T00:05:00+00:00")
MARKET_DAY_START = datetime.fromisoformat("2026-08-21T01:00:00+00:00")
CHEAP_START = datetime.fromisoformat("2026-08-21T00:45:00+00:00")


def _quarters(start: datetime, count: int, price: float, cheap: dict | None = None) -> list[dict]:
    """``count`` contiguous 15-min items, optionally one of them discounted."""
    out = []
    for i in range(count):
        t = start + timedelta(minutes=15 * i)
        buy = cheap["buy"] if cheap and t == cheap["start"] else price
        out.append(
            {
                "start": t.isoformat(),
                "end": (t + timedelta(minutes=15)).isoformat(),
                "buy": buy,
                "sell": buy / 2,
            }
        )
    return out


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    hass.states.async_set(
        "sensor.prices",
        "ok",
        {
            # The market day that just ended still holds the 00:00-01:00 slots.
            "data_yesterday": _quarters(
                MARKET_DAY_START - timedelta(hours=24),
                96,
                0.20,
                cheap={"start": CHEAP_START, "buy": 0.05},
            ),
            "data_today": _quarters(MARKET_DAY_START, 96, 0.20),
            "data_tomorrow": [],
            "tomorrow_valid": False,
        },
    )
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
                    "target_minutes": 15,
                    "min_service_minutes": 0,
                    "horizon_hours": 48,
                },
            )
        ],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _periods(hass: HomeAssistant, entry: MockConfigEntry) -> list[dict]:
    subentry_id = next(iter(entry.subentries))
    sensor_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{subentry_id}_schedule")
    return hass.states.get(sensor_id).attributes["periods"]


async def test_uses_cheap_slot_from_the_previous_market_day(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    entry = await _setup(hass)
    periods = _periods(hass, entry)
    assert periods, "expected a scheduled period"
    # Without the previous-day list the cheapest visible slot would be at or
    # after the market-day boundary; with it, the 00:45 quarter-hour wins.
    assert dt_util.parse_datetime(periods[0]["start"]) == CHEAP_START
    assert dt_util.parse_datetime(periods[0]["end"]) == CHEAP_START + timedelta(minutes=15)


async def test_elapsed_slots_are_not_scheduled(hass: HomeAssistant, freezer) -> None:
    # Yesterday's list is 24 h long; everything before `now` must be discarded.
    freezer.move_to(NOW)
    entry = await _setup(hass)
    for period in _periods(hass, entry):
        assert dt_util.parse_datetime(period["end"]) > NOW
