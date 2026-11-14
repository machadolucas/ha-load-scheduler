"""The `load_scheduler.boost` service: device targeting and duration."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
import voluptuous as vol
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)

from custom_components.load_scheduler.const import (
    DOMAIN,
    SAVE_DELAY,
    SERVICE_ADJUST_BOOST,
    SERVICE_BOOST,
    SERVICE_CANCEL_BOOST,
    SUBENTRY_TYPE_LOAD,
)

ENTITY = "input_boolean.heater"


def _price_attributes(cheap: tuple[int, ...], n: int = 24) -> dict:
    base = dt_util.now().replace(second=0, microsecond=0)
    return {
        "data_today": [
            {
                "start": (base + timedelta(minutes=15 * i)).isoformat(),
                "end": (base + timedelta(minutes=15 * (i + 1))).isoformat(),
                "buy": 0.01 if i in cheap else 0.20,
                "sell": 0.005,
            }
            for i in range(n)
        ],
        "data_tomorrow": [],
    }


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    """One load with nothing scheduled now, so any run must come from the boost."""
    hass.states.async_set("sensor.prices", "ok", _price_attributes(cheap=(20, 21)))
    hass.states.async_set(ENTITY, "off")
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
                    "target_minutes": 30,
                    "controlled_entity": ENTITY,
                },
            )
        ],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _device_id(hass: HomeAssistant, subentry_id: str) -> str:
    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, subentry_id), entries[0].entry_id
    )
    assert device is not None
    return device.id


async def test_boost_service_runs_load_for_requested_minutes(hass: HomeAssistant) -> None:
    on = async_mock_service(hass, "homeassistant", "turn_on")
    async_mock_service(hass, "homeassistant", "turn_off")
    entry = await _setup(hass)
    subentry_id = next(iter(entry.subentries))
    coordinator = entry.runtime_data

    await hass.services.async_call(
        DOMAIN,
        SERVICE_BOOST,
        {"device_id": _device_id(hass, subentry_id), "minutes": 90},
        blocking=True,
    )
    await hass.async_block_till_done()

    expected = dt_util.utcnow() + timedelta(minutes=90)
    boost_until = coordinator.runtime[subentry_id].boost_until
    assert boost_until is not None
    assert abs((boost_until - expected).total_seconds()) < 5
    # The plan and the actuator pick it up exactly as they do for the button.
    assert any(c.data.get("entity_id") == ENTITY for c in on)


async def test_boost_service_defaults_to_the_target_runtime(hass: HomeAssistant) -> None:
    async_mock_service(hass, "homeassistant", "turn_on")
    async_mock_service(hass, "homeassistant", "turn_off")
    entry = await _setup(hass)
    subentry_id = next(iter(entry.subentries))
    coordinator = entry.runtime_data

    await hass.services.async_call(
        DOMAIN, SERVICE_BOOST, {"device_id": _device_id(hass, subentry_id)}, blocking=True
    )
    await hass.async_block_till_done()

    expected = dt_util.utcnow() + timedelta(minutes=30)  # the load's target
    boost_until = coordinator.runtime[subentry_id].boost_until
    assert abs((boost_until - expected).total_seconds()) < 5


async def test_boost_service_rejects_a_foreign_device(hass: HomeAssistant) -> None:
    async_mock_service(hass, "homeassistant", "turn_on")
    async_mock_service(hass, "homeassistant", "turn_off")
    await _setup(hass)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, SERVICE_BOOST, {"device_id": "not-a-load-scheduler-device"}, blocking=True
        )


async def test_boost_service_accepts_an_area_target(hass: HomeAssistant) -> None:
    # services.yaml declares a `target:`, so the UI offers areas/floors/labels;
    # the schema used to reject all of them.
    async_mock_service(hass, "homeassistant", "turn_on")
    async_mock_service(hass, "homeassistant", "turn_off")
    entry = await _setup(hass)
    subentry_id = next(iter(entry.subentries))
    area = ar.async_get(hass).async_create("Utility")
    dr.async_get(hass).async_update_device(_device_id(hass, subentry_id), area_id=area.id)

    await hass.services.async_call(
        DOMAIN, SERVICE_BOOST, {"area_id": area.id, "minutes": 20}, blocking=True
    )
    await hass.async_block_till_done()

    boost_until = entry.runtime_data.runtime[subentry_id].boost_until
    expected = dt_util.utcnow() + timedelta(minutes=20)
    assert boost_until is not None
    assert abs((boost_until - expected).total_seconds()) < 5


async def test_boost_service_boosts_a_load_named_twice_once(hass: HomeAssistant) -> None:
    async_mock_service(hass, "homeassistant", "turn_on")
    async_mock_service(hass, "homeassistant", "turn_off")
    entry = await _setup(hass)
    subentry_id = next(iter(entry.subentries))
    coordinator = entry.runtime_data
    calls: list[str] = []
    real_boost = coordinator.async_boost

    async def _spy(sid, minutes):
        calls.append(sid)
        await real_boost(sid, minutes)

    coordinator.async_boost = _spy
    button_id = er.async_get(hass).async_get_entity_id("button", DOMAIN, f"{subentry_id}_boost")
    await hass.services.async_call(
        DOMAIN,
        SERVICE_BOOST,
        {"device_id": _device_id(hass, subentry_id), "entity_id": button_id},
        blocking=True,
    )
    assert calls == [subentry_id]


async def test_boost_service_rejects_a_foreign_entity(hass: HomeAssistant) -> None:
    async_mock_service(hass, "homeassistant", "turn_on")
    async_mock_service(hass, "homeassistant", "turn_off")
    await _setup(hass)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, SERVICE_BOOST, {"entity_id": "input_boolean.not_a_load"}, blocking=True
        )


async def test_adjust_boost_service_adds_and_subtracts_from_existing_end(
    hass: HomeAssistant,
) -> None:
    async_mock_service(hass, "homeassistant", "turn_on")
    async_mock_service(hass, "homeassistant", "turn_off")
    entry = await _setup(hass)
    sid = next(iter(entry.subentries))
    await entry.runtime_data.async_boost(sid, 120)
    start_end = entry.runtime_data.runtime[sid].boost_until
    assert start_end is not None

    plus = await hass.services.async_call(
        DOMAIN,
        SERVICE_ADJUST_BOOST,
        {"device_id": _device_id(hass, sid), "minutes": 60},
        blocking=True,
        return_response=True,
    )
    plus_end = entry.runtime_data.runtime[sid].boost_until
    assert plus_end == start_end + timedelta(minutes=60)
    assert plus["boost_until"][sid] == plus_end.isoformat()
    assert "updated_at" in plus

    minus = await hass.services.async_call(
        DOMAIN,
        SERVICE_ADJUST_BOOST,
        {"device_id": _device_id(hass, sid), "minutes": -60},
        blocking=True,
        return_response=True,
    )
    assert entry.runtime_data.runtime[sid].boost_until == start_end
    assert minus["boost_until"][sid] == start_end.isoformat()


async def test_concurrent_adjustments_accumulate(hass: HomeAssistant) -> None:
    async_mock_service(hass, "homeassistant", "turn_on")
    async_mock_service(hass, "homeassistant", "turn_off")
    entry = await _setup(hass)
    sid = next(iter(entry.subentries))
    await entry.runtime_data.async_boost(sid, 120)
    original = entry.runtime_data.runtime[sid].boost_until
    assert original is not None

    await asyncio.gather(
        entry.runtime_data.async_adjust_boost(sid, 10),
        entry.runtime_data.async_adjust_boost(sid, 20),
    )
    assert entry.runtime_data.runtime[sid].boost_until == original + timedelta(minutes=30)


async def test_adjust_returns_current_none_if_cancelled_during_refresh(
    hass: HomeAssistant,
) -> None:
    async_mock_service(hass, "homeassistant", "turn_on")
    async_mock_service(hass, "homeassistant", "turn_off")
    entry = await _setup(hass)
    coordinator = entry.runtime_data
    sid = next(iter(entry.subentries))
    await coordinator.async_boost(sid, 60)
    real_refresh = coordinator.async_refresh
    refreshing = asyncio.Event()
    continue_refresh = asyncio.Event()

    async def hold_refresh():
        refreshing.set()
        await continue_refresh.wait()
        await real_refresh()

    coordinator.async_refresh = hold_refresh
    task = asyncio.create_task(coordinator.async_adjust_boost(sid, 15))
    await refreshing.wait()
    # Simulate cancellation mutating shared runtime while the adjustment's
    # refresh is still pending. Its response must not resurrect the old end.
    coordinator.runtime[sid].boost_until = None
    continue_refresh.set()
    assert await task is None


@pytest.mark.parametrize("minutes", [0, float("nan"), float("inf"), -float("inf"), 1e308, -1e308])
async def test_adjust_boost_service_rejects_nonfinite_or_zero(
    hass: HomeAssistant, minutes: float
) -> None:
    async_mock_service(hass, "homeassistant", "turn_on")
    async_mock_service(hass, "homeassistant", "turn_off")
    entry = await _setup(hass)
    sid = next(iter(entry.subentries))
    await entry.runtime_data.async_boost(sid, 60)
    with pytest.raises((ServiceValidationError, vol.Invalid)):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_ADJUST_BOOST,
            {"device_id": _device_id(hass, sid), "minutes": minutes},
            blocking=True,
        )


async def test_adjust_boost_rejects_inactive_boost(hass: HomeAssistant) -> None:
    async_mock_service(hass, "homeassistant", "turn_on")
    async_mock_service(hass, "homeassistant", "turn_off")
    entry = await _setup(hass)
    sid = next(iter(entry.subentries))
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_ADJUST_BOOST,
            {"device_id": _device_id(hass, sid), "minutes": 60},
            blocking=True,
        )


async def test_cancel_boost_repeated_and_expired_are_noop(hass: HomeAssistant) -> None:
    async_mock_service(hass, "homeassistant", "turn_on")
    async_mock_service(hass, "homeassistant", "turn_off")
    entry = await _setup(hass)
    sid = next(iter(entry.subentries))
    stops: list[str] = []

    async def manual_stop(load_id: str) -> None:
        stops.append(load_id)

    entry.runtime_data.actuator.async_manual_stop = manual_stop
    # An expired boost must be preserved and must not issue a stop.
    expired = dt_util.utcnow() - timedelta(minutes=1)
    entry.runtime_data.runtime[sid].boost_until = expired
    resp = await hass.services.async_call(
        DOMAIN,
        SERVICE_CANCEL_BOOST,
        {"device_id": _device_id(hass, sid)},
        blocking=True,
        return_response=True,
    )
    assert stops == []
    assert resp["boost_until"][sid] == expired.isoformat()

    await entry.runtime_data.async_boost(sid, 60)
    cancel = await hass.services.async_call(
        DOMAIN,
        SERVICE_CANCEL_BOOST,
        {"device_id": _device_id(hass, sid)},
        blocking=True,
        return_response=True,
    )
    assert stops == [sid]
    assert entry.runtime_data.runtime[sid].boost_until is None
    assert cancel["boost_until"][sid] is None
    assert "updated_at" in cancel
    await hass.services.async_call(
        DOMAIN,
        SERVICE_CANCEL_BOOST,
        {"device_id": _device_id(hass, sid)},
        blocking=True,
    )
    assert stops == [sid]


async def test_adjust_and_cancel_reuse_target_resolver_for_dedup_and_invalid_entity(
    hass: HomeAssistant,
) -> None:
    async_mock_service(hass, "homeassistant", "turn_on")
    async_mock_service(hass, "homeassistant", "turn_off")
    entry = await _setup(hass)
    sid = next(iter(entry.subentries))
    coordinator = entry.runtime_data
    await coordinator.async_boost(sid, 120)
    button_id = er.async_get(hass).async_get_entity_id("button", DOMAIN, f"{sid}_boost")
    called: list[tuple[str, float | None]] = []
    real_adjust = coordinator.async_adjust_boost
    real_stop = coordinator.async_stop_boost

    async def spy_adjust(load_id: str, minutes: float):
        called.append((load_id, minutes))
        return await real_adjust(load_id, minutes)

    async def spy_stop(load_id: str):
        called.append((load_id, None))
        await real_stop(load_id)

    coordinator.async_adjust_boost = spy_adjust
    coordinator.async_stop_boost = spy_stop
    await hass.services.async_call(
        DOMAIN,
        SERVICE_ADJUST_BOOST,
        {"device_id": _device_id(hass, sid), "entity_id": button_id, "minutes": 5},
        blocking=True,
    )
    assert called == [(sid, 5)]
    await hass.services.async_call(
        DOMAIN,
        SERVICE_CANCEL_BOOST,
        {"device_id": _device_id(hass, sid), "entity_id": button_id},
        blocking=True,
    )
    assert called == [(sid, 5), (sid, None)]

    for service, data in (
        (SERVICE_ADJUST_BOOST, {"entity_id": "input_boolean.not_a_load", "minutes": 5}),
        (SERVICE_CANCEL_BOOST, {"entity_id": "input_boolean.not_a_load"}),
    ):
        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(DOMAIN, service, data, blocking=True)


@pytest.mark.parametrize(
    ("initial_minutes", "delta"),
    [(5, -6), (1439, 2), (1440, 1)],
)
async def test_adjust_service_rejects_nonpositive_or_over_24h_end(
    hass: HomeAssistant, initial_minutes: float, delta: float
) -> None:
    async_mock_service(hass, "homeassistant", "turn_on")
    async_mock_service(hass, "homeassistant", "turn_off")
    entry = await _setup(hass)
    sid = next(iter(entry.subentries))
    await entry.runtime_data.async_boost(sid, initial_minutes)
    # Pin the end near now to avoid elapsed test time changing the boundary.
    entry.runtime_data.runtime[sid].boost_until = dt_util.utcnow() + timedelta(
        minutes=initial_minutes
    )
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_ADJUST_BOOST,
            {"device_id": _device_id(hass, sid), "minutes": delta},
            blocking=True,
        )


async def test_adjust_service_rejects_expired_boost(hass: HomeAssistant) -> None:
    async_mock_service(hass, "homeassistant", "turn_on")
    async_mock_service(hass, "homeassistant", "turn_off")
    entry = await _setup(hass)
    sid = next(iter(entry.subentries))
    entry.runtime_data.runtime[sid].boost_until = dt_util.utcnow() - timedelta(seconds=1)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_ADJUST_BOOST,
            {"device_id": _device_id(hass, sid), "minutes": 10},
            blocking=True,
        )


async def test_stop_clears_boost_before_await_so_concurrent_adjustment_fails(
    hass: HomeAssistant,
) -> None:
    async_mock_service(hass, "homeassistant", "turn_on")
    async_mock_service(hass, "homeassistant", "turn_off")
    entry = await _setup(hass)
    coordinator = entry.runtime_data
    sid = next(iter(entry.subentries))
    await coordinator.async_boost(sid, 60)
    stopping = asyncio.Event()
    continue_stop = asyncio.Event()

    async def wait_for_stop(_sid: str) -> None:
        stopping.set()
        await continue_stop.wait()

    coordinator.actuator.async_manual_stop = wait_for_stop
    task = asyncio.create_task(coordinator.async_stop_boost(sid))
    await stopping.wait()
    with pytest.raises(ServiceValidationError):
        await coordinator.async_adjust_boost(sid, 10)
    continue_stop.set()
    await task


async def test_adjusted_boost_is_saved_and_restored_after_reload(
    hass: HomeAssistant, freezer
) -> None:
    async_mock_service(hass, "homeassistant", "turn_on")
    async_mock_service(hass, "homeassistant", "turn_off")
    entry = await _setup(hass)
    sid = next(iter(entry.subentries))
    coordinator = entry.runtime_data
    await coordinator.async_boost(sid, 60)
    await coordinator.async_adjust_boost(sid, 15)
    saved_end = coordinator.runtime[sid].boost_until
    assert saved_end is not None

    freezer.tick(timedelta(seconds=SAVE_DELAY + 1))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.runtime[sid].boost_until == saved_end
