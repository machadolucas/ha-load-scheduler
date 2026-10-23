"""Live buy-price overrides do not depend on the target or scheduling window."""

from datetime import timedelta

import pytest
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import Context
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.load_scheduler.const import DOMAIN, SAVE_DELAY, SUBENTRY_TYPE_LOAD

ENTITY = "switch.floor"


def prices(buy=0.05):
    now = dt_util.utcnow()
    return {
        "data_today": [
            {
                "start": now - timedelta(minutes=1),
                "end": now + timedelta(minutes=14),
                "buy": buy,
                "sell": -1,
            },
            {
                "start": now + timedelta(minutes=14),
                "end": now + timedelta(minutes=29),
                "buy": 0.2,
                "sell": -1,
            },
        ]
    }


async def setup(hass, *, config=None, price=0.05, state="off"):
    on, off = [], []

    async def echo(call):
        (on if call.service == "turn_on" else off).append(call)
        hass.states.async_set(
            ENTITY, "on" if call.service == "turn_on" else "off", context=call.context
        )

    hass.services.async_register("homeassistant", "turn_on", echo)
    hass.services.async_register("homeassistant", "turn_off", echo)
    hass.states.async_set("sensor.prices", "ok", prices(price))
    hass.states.async_set(ENTITY, state)
    data = {
        "name": "Floor",
        "target_minutes": 0,
        "controlled_entity": ENTITY,
        "keep_on_price": 0.05,
        "allow_solar": False,
        **(config or {}),
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"name": "Hub", "buy_price_entity": "sensor.prices"},
        unique_id="sensor.prices",
        subentries_data=[
            ConfigSubentryData(
                subentry_type=SUBENTRY_TYPE_LOAD,
                title="Floor",
                unique_id=None,
                data=data,
            )
        ],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, next(iter(entry.subentries)), on, off


@pytest.mark.parametrize(
    ("price", "threshold", "expected"),
    [
        (0.05, 0.05, "on"),
        (0.04, 0.05, "on"),
        (0.06, 0.05, "off"),
        (0, 0, "on"),
        (-0.02, -0.01, "on"),
        (0, None, "off"),
    ],
)
async def test_current_buy_price_not_sell_controls_zero_target(hass, price, threshold, expected):
    entry, sid, _, _ = await setup(hass, price=price, config={"keep_on_price": threshold})
    assert not entry.runtime_data.data[sid].periods
    assert hass.states.get(ENTITY).state == expected


async def test_ignores_window_cap_and_idle_feedback(hass):
    tomorrow = (dt_util.now() + timedelta(hours=3)).strftime("%H:%M:%S")
    hass.states.async_set("binary_sensor.heating", "off")
    entry, sid, _, _ = await setup(
        hass,
        config={
            "target_minutes": 30,
            "earliest": tomorrow,
            "price_cap": -1,
            "feedback_entity": "binary_sensor.heating",
        },
    )
    assert entry.runtime_data.data[sid].active_period(dt_util.utcnow()) is None
    assert hass.states.get(ENTITY).state == "on"


async def test_exact_price_boundary_releases_without_source_event(hass, freezer):
    entry, sid, _, off = await setup(hass)
    sensor = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{sid}_schedule")
    attrs = hass.states.get(sensor).attributes
    assert attrs["config"]["keep_on_price"] == 0.05
    assert attrs["current_buy_price"] == 0.05
    boundary = entry.runtime_data.keep_on_price_slots()[0].end
    freezer.move_to(boundary)
    async_fire_time_changed(hass, boundary)
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).state == "off"
    assert off
    assert hass.states.get(sensor).attributes["current_buy_price"] == 0.2


async def test_future_price_interval_engages_without_source_event(hass, freezer):
    entry, _, on, _ = await setup(hass, price=0.2)
    attrs = prices(0.2)
    attrs["data_today"][1]["buy"] = 0.01
    hass.states.async_set("sensor.prices", "ok", attrs)
    await hass.async_block_till_done()
    assert not on
    boundary = entry.runtime_data.keep_on_price_slots()[1].start
    freezer.move_to(boundary)
    async_fire_time_changed(hass, boundary)
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).state == "on"


@pytest.mark.parametrize(
    "failure", ["unavailable", "unknown", "missing", "gap", "nan", "malformed"]
)
async def test_unusable_current_price_releases_our_coexist_run(hass, failure):
    entry, sid, _, off = await setup(hass, config={"coexist": True})
    assert entry.runtime_data.runtime_for(sid).driven
    attrs = prices()
    if failure == "missing":
        hass.states.async_remove("sensor.prices")
    else:
        if failure == "gap":
            attrs["data_today"] = attrs["data_today"][1:]
        elif failure == "nan":
            attrs["data_today"][0]["buy"] = "nan"
        elif failure == "malformed":
            attrs = {}
        hass.states.async_set(
            "sensor.prices", failure if failure in ("unknown", "unavailable") else "ok", attrs
        )
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).state == "off"
    assert off
    await hass.async_block_till_done()
    assert not entry.runtime_data.runtime_for(sid).driven, str(
        entry.runtime_data.actuator.diagnostics(sid)
    )


async def test_above_threshold_resumes_existing_schedule(hass):
    entry, sid, _, off = await setup(hass, config={"target_minutes": 60})
    hass.states.async_set("sensor.prices", "ok", prices(0.1))
    await hass.async_block_till_done()
    assert entry.runtime_data.data[sid].active_period(dt_util.utcnow()) is not None
    assert hass.states.get(ENTITY).state == "on"
    assert not off


async def test_disable_and_informational_loads_do_not_keep_on(hass):
    entry, sid, _, _ = await setup(hass)
    await entry.runtime_data.async_set_enabled(sid, False)
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).state == "off"


async def test_informational_load_ignores_keep_on(hass):
    _, _, on, _ = await setup(hass, config={"mode": "informational"})
    assert not on
    assert hass.states.get(ENTITY).state == "off"


async def test_manual_off_and_stop_take_precedence(hass):
    entry, sid, on, _ = await setup(hass)
    hass.states.async_set(ENTITY, "off", context=Context(user_id="person"))
    await hass.async_block_till_done()
    hass.states.async_set("sensor.prices", "ok", prices(0.01))
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).state == "off"
    assert len(on) == 1
    assert entry.runtime_data.actuator._desired_on(sid, entry.runtime_data.load_config(sid)) is None


async def test_coexist_ownership_survives_reload_then_price_increase(hass, freezer):
    entry, sid, _, off = await setup(hass, config={"coexist": True})
    freezer.tick(timedelta(seconds=SAVE_DELAY + 1))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.runtime_for(sid).driven
    hass.states.async_set("sensor.prices", "ok", prices(0.2))
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).state == "off"
    assert off


async def test_coexist_external_run_is_not_claimed_or_switched_off(hass):
    entry, sid, _, off = await setup(hass, config={"coexist": True}, state="on")
    assert not entry.runtime_data.runtime_for(sid).driven, entry.runtime_data.actuator.diagnostics(
        sid
    )
    hass.states.async_set("sensor.prices", "ok", prices(0.2))
    await hass.async_block_till_done()
    assert not off
    assert hass.states.get(ENTITY).state == "on"
