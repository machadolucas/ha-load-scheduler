"""Coordinator robustness: dead optional sources, seams, midnight, failsafe."""

from __future__ import annotations

from datetime import datetime, time, timedelta

from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.load_scheduler.const import DOMAIN, SUBENTRY_TYPE_LOAD
from custom_components.load_scheduler.models import LoadConfig


def _slots(start, count: int, price: float, minutes: int = 60) -> list[dict]:
    step = timedelta(minutes=minutes)
    return [
        {
            "start": (start + i * step).isoformat(),
            "end": (start + (i + 1) * step).isoformat(),
            "buy": price,
        }
        for i in range(count)
    ]


async def _setup(hass: HomeAssistant, hub: dict, load: dict) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"name": "Hub", "buy_price_entity": "sensor.prices", **hub},
        unique_id="sensor.prices",
        subentries_data=[
            ConfigSubentryData(
                subentry_type=SUBENTRY_TYPE_LOAD, title="Load", unique_id=None, data=load
            )
        ],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


LOAD = {"name": "Heater", "mode": "non_sequential", "target_minutes": 60}


async def test_dead_sell_entity_keeps_the_buy_forecast(hass: HomeAssistant) -> None:
    now = dt_util.now().replace(minute=0, second=0, microsecond=0)
    hass.states.async_set("sensor.prices", "ok", {"data_today": _slots(now, 24, 0.1)})
    # Unavailable with no forecast attributes at all: unparseable.
    hass.states.async_set("sensor.sell", "unavailable", {})
    entry = await _setup(hass, {"sell_price_entity": "sensor.sell"}, LOAD)

    plan = next(iter(entry.runtime_data.data.values()))
    assert plan.error is None
    assert plan.periods, "a dead sell feed must not discard the buy forecast"


async def test_forecast_extension_does_not_overlap_an_hourly_real_tail(
    hass: HomeAssistant,
) -> None:
    now = dt_util.now().replace(minute=0, second=0, microsecond=0)
    hass.states.async_set("sensor.prices", "ok", {"data_today": _slots(now, 4, 0.2)})
    # Quarter-hourly forecast starting inside the last real hour.
    fc_start = now + timedelta(hours=3)
    hass.states.async_set(
        "sensor.forecast", "ok", {"data_today": _slots(fc_start, 12, 0.05, minutes=15)}
    )
    entry = await _setup(hass, {"forecast_price_entity": "sensor.forecast"}, LOAD)

    slots = entry.runtime_data._price_slots()
    for a, b in zip(slots, slots[1:], strict=False):
        assert b.start >= a.end, f"overlap: {a.start}-{a.end} vs {b.start}-{b.end}"
    assert slots[-1].end == fc_start + timedelta(minutes=15 * 12)


async def test_failsafe_keeps_todays_run_once_it_has_started(hass: HomeAssistant) -> None:
    entry = await _setup(hass, {}, {**LOAD, "failsafe_start": "23:00:00"})
    coord = entry.runtime_data
    sid = next(iter(entry.subentries))
    cfg = coord.load_config(sid)
    rt = coord.runtime_for(sid)

    local_today = dt_util.now().date()
    start = datetime.combine(local_today, time(23, 0), dt_util.get_default_time_zone())
    mid_run = start + timedelta(seconds=30)
    periods = coord._failsafe_periods(cfg, rt, mid_run)
    assert periods[0].start == dt_util.as_utc(start)
    assert periods[0].start <= dt_util.as_utc(mid_run) < periods[0].end

    after = start + timedelta(minutes=61)
    assert coord._failsafe_periods(cfg, rt, after)[0].start == dt_util.as_utc(
        start + timedelta(days=1)
    )


async def test_delivered_measurement_does_not_survive_midnight(hass: HomeAssistant) -> None:
    now = dt_util.now().replace(minute=0, second=0, microsecond=0)
    hass.states.async_set("sensor.prices", "ok", {"data_today": _slots(now, 24, 0.1)})
    hass.states.async_set("switch.heater", "off")
    async_mock_service(hass, "homeassistant", "turn_on")
    async_mock_service(hass, "homeassistant", "turn_off")
    entry = await _setup(hass, {}, {**LOAD, "controlled_entity": "switch.heater"})
    coord = entry.runtime_data
    sid = next(iter(entry.subentries))

    now_utc = dt_util.utcnow()
    coord._delivered_today = {sid: 120.0}
    coord._delivered_day = dt_util.as_local(now_utc).date() - timedelta(days=1)
    coord._delivered_at = now_utc  # fresh enough that the throttle would skip
    await coord._maybe_refresh_delivered(now_utc)
    assert coord._delivered_today.get(sid, 0.0) == 0.0


async def test_explicit_delivered_sensor_dropout_holds_last_value(hass: HomeAssistant) -> None:
    now = dt_util.now().replace(minute=0, second=0, microsecond=0)
    hass.states.async_set("sensor.prices", "ok", {"data_today": _slots(now, 24, 0.1)})
    hass.states.async_set("sensor.runtime", "30", {"unit_of_measurement": "min"})
    entry = await _setup(hass, {}, {**LOAD, "delivered_entity": "sensor.runtime"})
    coord = entry.runtime_data
    sid = next(iter(entry.subentries))
    cfg: LoadConfig = coord.load_config(sid)

    assert coord._delivered_minutes(cfg, sid) == 30.0
    hass.states.async_set("sensor.runtime", "unavailable", {})
    assert coord._delivered_minutes(cfg, sid) == 30.0


async def test_load_config_is_cached_per_subentry(hass: HomeAssistant) -> None:
    entry = await _setup(hass, {}, LOAD)
    coord = entry.runtime_data
    sid = next(iter(entry.subentries))
    assert coord.load_config(sid) is coord.load_config(sid)
