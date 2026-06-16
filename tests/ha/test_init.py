"""Integration setup: a hub + one load produces entities and a computed plan."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.load_scheduler.const import DOMAIN, SUBENTRY_TYPE_LOAD


def _price_attributes() -> dict:
    """A 6-hour run of 15-min slots from ~now, with two cheap slots in the middle."""
    base = dt_util.now().replace(second=0, microsecond=0) - timedelta(minutes=15)
    today = []
    for i in range(24):
        start = base + timedelta(minutes=15 * i)
        end = start + timedelta(minutes=15)
        buy = 0.01 if i in (10, 11) else 0.20  # two cheap, contiguous slots
        today.append(
            {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "buy": buy,
                "sell": 0.005,
            }
        )
    return {"data_today": today, "data_tomorrow": []}


async def _setup(hass: HomeAssistant, load_data: dict) -> MockConfigEntry:
    hass.states.async_set("sensor.prices", "ok", _price_attributes())
    subentry = ConfigSubentryData(
        subentry_type=SUBENTRY_TYPE_LOAD,
        title="Heater",
        unique_id=None,
        data=load_data,
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"name": "Hub", "buy_price_entity": "sensor.prices"},
        unique_id="sensor.prices",
        subentries_data=[subentry],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _entity_id(hass: HomeAssistant, subentry_id: str, platform: str, key: str) -> str:
    reg = er.async_get(hass)
    eid = reg.async_get_entity_id(platform, DOMAIN, f"{subentry_id}_{key}")
    assert eid is not None, f"{platform}.{key} not registered"
    return eid


async def test_setup_creates_entities_and_plan(hass: HomeAssistant) -> None:
    entry = await _setup(hass, {"name": "Heater", "mode": "non_sequential", "target_minutes": 30})
    subentry_id = next(iter(entry.subentries))

    # All four per-load entities exist.
    for platform, key in (
        ("binary_sensor", "running"),
        ("sensor", "schedule"),
        ("number", "target"),
        ("switch", "enabled"),
    ):
        _entity_id(hass, subentry_id, platform, key)

    # The schedule sensor reflects a computed plan: target 30 min over the two
    # contiguous cheap slots = one 30-minute period.
    sched = hass.states.get(_entity_id(hass, subentry_id, "sensor", "schedule"))
    assert sched.attributes["target_minutes"] == 30
    assert sched.attributes["status"] == "ok"
    assert len(sched.attributes["periods"]) == 1


async def test_disable_switch_empties_plan(hass: HomeAssistant) -> None:
    entry = await _setup(hass, {"name": "Heater", "mode": "non_sequential", "target_minutes": 30})
    subentry_id = next(iter(entry.subentries))
    switch_id = _entity_id(hass, subentry_id, "switch", "enabled")
    sensor_id = _entity_id(hass, subentry_id, "sensor", "schedule")

    await hass.services.async_call("switch", "turn_off", {"entity_id": switch_id}, blocking=True)
    await hass.async_block_till_done()

    sched = hass.states.get(sensor_id)
    assert sched.attributes["enabled"] is False
    assert sched.attributes["periods"] == []


async def test_no_price_data_reports_status(hass: HomeAssistant) -> None:
    # No sensor.prices state set => price source unusable.
    subentry = ConfigSubentryData(
        subentry_type=SUBENTRY_TYPE_LOAD,
        title="Heater",
        unique_id=None,
        data={"name": "Heater", "mode": "non_sequential", "target_minutes": 30},
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"name": "Hub", "buy_price_entity": "sensor.prices"},
        unique_id="sensor.prices",
        subentries_data=[subentry],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    subentry_id = next(iter(entry.subentries))
    sched = hass.states.get(_entity_id(hass, subentry_id, "sensor", "schedule"))
    assert sched.attributes["status"] == "no_price_data"


async def test_failsafe_runs_without_price_data(hass: HomeAssistant) -> None:
    # No sensor.prices, but a failsafe start time is configured => a fixed-time
    # run is scheduled instead of erroring out.
    subentry = ConfigSubentryData(
        subentry_type=SUBENTRY_TYPE_LOAD,
        title="Heater",
        unique_id=None,
        data={
            "name": "Heater",
            "mode": "non_sequential",
            "target_minutes": 60,
            "failsafe_start": "23:00:00",
        },
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"name": "Hub", "buy_price_entity": "sensor.prices"},
        unique_id="sensor.prices",
        subentries_data=[subentry],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    subentry_id = next(iter(entry.subentries))
    sched = hass.states.get(_entity_id(hass, subentry_id, "sensor", "schedule"))
    assert sched.attributes["status"] == "ok"
    assert len(sched.attributes["periods"]) == 1
