"""Ownership of a run: it must survive a reload and a slow relay.

The integration only switches a **coexist** load off if it was the one that
switched it on. That ownership used to live in the actuator's memory only, so it
evaporated on every restart and on any confirmation slower than a few seconds —
and a load nobody owns is a load nobody ever turns off (the author's floor
heating ran for three days). These tests pin both holes shut, without letting
the coexist promise itself regress.
"""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)

from custom_components.load_scheduler.const import (
    DOMAIN,
    ISSUE_UNOWNED_RUN,
    SAVE_DELAY,
    SUBENTRY_TYPE_LOAD,
    UNOWNED_RUN_HOURS,
)

ENTITY = "input_boolean.floor"


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


async def _setup(
    hass: HomeAssistant,
    cheap: tuple[int, ...],
    controlled_state: str,
    *,
    coexist: bool = True,
) -> MockConfigEntry:
    hass.states.async_set("sensor.prices", "ok", _price_attributes(cheap))
    hass.states.async_set(ENTITY, controlled_state)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"name": "Hub", "buy_price_entity": "sensor.prices"},
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
                    "controlled_entity": ENTITY,
                    "coexist": coexist,
                },
            )
        ],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _called_for(calls, entity_id: str = ENTITY) -> bool:
    return any(c.data.get("entity_id") == entity_id for c in calls)


async def _flush_store(hass: HomeAssistant, freezer) -> None:
    """Let the debounced Store write fire, so a restart sees the saved runtime.

    The clock has to really move: a Store asked to save more than once defers
    its pending write to the latest deadline, which firing a mocked time change
    alone never reaches.
    """
    freezer.tick(timedelta(seconds=SAVE_DELAY + 1))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()


async def _flush_and_reload(hass: HomeAssistant, entry: MockConfigEntry, freezer) -> None:
    """Flush the debounced Store write, then reload the hub (a restart, in effect)."""
    await _flush_store(hass, freezer)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()


async def test_our_coexist_run_is_still_ours_after_a_reload(hass: HomeAssistant, freezer) -> None:
    # The live bug: a coexist load the integration switched on, restarted
    # mid-run. Without persisted ownership it refuses to switch the load off at
    # the end of the period — forever.
    async_mock_service(hass, "homeassistant", "turn_on")
    off = async_mock_service(hass, "homeassistant", "turn_off")
    entry = await _setup(hass, cheap=(0, 1), controlled_state="off")
    subentry_id = next(iter(entry.subentries))
    assert entry.runtime_data.runtime[subentry_id].driven is True
    hass.states.async_set(ENTITY, "on")  # the relay confirms our command
    await hass.async_block_till_done()
    await _flush_store(hass, freezer)

    # Down, and back up with the run's period over (the cheap window has moved
    # on) — the shape of the deploy restart that broke the live instance.
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    off.clear()
    hass.states.async_set("sensor.prices", "ok", _price_attributes(cheap=(20, 21)))
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.runtime_data.runtime[subentry_id].driven is False  # switched off
    assert _called_for(off)


async def test_external_coexist_run_survives_a_reload_unowned(hass: HomeAssistant, freezer) -> None:
    # The other half of the promise: a run somebody else started is never cut
    # short, and a reload must not hand its ownership to the integration.
    async_mock_service(hass, "homeassistant", "turn_on")
    off = async_mock_service(hass, "homeassistant", "turn_off")
    entry = await _setup(hass, cheap=(20, 21), controlled_state="on")
    subentry_id = next(iter(entry.subentries))
    assert entry.runtime_data.runtime[subentry_id].driven is False

    await _flush_and_reload(hass, entry, freezer)

    assert entry.runtime_data.runtime[subentry_id].driven is False
    assert not _called_for(off)


async def test_late_confirmation_is_not_a_manual_override(hass: HomeAssistant, freezer) -> None:
    # A cloud/Zigbee relay confirming long after any fixed command window: still
    # our own command, so no override and no loss of ownership.
    async_mock_service(hass, "homeassistant", "turn_on")
    async_mock_service(hass, "homeassistant", "turn_off")
    entry = await _setup(hass, cheap=(0, 1), controlled_state="off")
    subentry_id = next(iter(entry.subentries))
    coordinator = entry.runtime_data
    assert coordinator.runtime[subentry_id].driven is True

    freezer.tick(timedelta(seconds=90))
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()

    assert coordinator.runtime[subentry_id].driven is True
    assert coordinator.actuator._override_active(subentry_id) is False
    assert coordinator.foreign_log.get(subentry_id, []) == []


async def test_manual_on_after_our_command_landed_is_an_override(hass: HomeAssistant) -> None:
    # Once our command has been observed it is no longer pending, so a later
    # flip back to the same state is a person, not our echo.
    async_mock_service(hass, "homeassistant", "turn_on")
    async_mock_service(hass, "homeassistant", "turn_off")
    entry = await _setup(hass, cheap=(0, 1), controlled_state="off")
    subentry_id = next(iter(entry.subentries))
    coordinator = entry.runtime_data

    hass.states.async_set(ENTITY, "on")  # our own command confirming
    await hass.async_block_till_done()
    assert coordinator.foreign_log.get(subentry_id, []) == []

    hass.states.async_set(ENTITY, "off", context=Context(user_id="u1"))
    await hass.async_block_till_done()
    hass.states.async_set(ENTITY, "on", context=Context(user_id="u1"))
    await hass.async_block_till_done()

    assert [ev.turned_on for ev in coordinator.foreign_log[subentry_id]] == [False, True]
    assert coordinator.runtime[subentry_id].driven is False
    assert coordinator.actuator._override_active(subentry_id) is True


async def test_attribute_only_change_is_not_a_manual_off(hass: HomeAssistant) -> None:
    # A state_changed event that doesn't move the switch (attributes only) must
    # not read as a manual off: that would disown the run and suppress the period.
    async_mock_service(hass, "homeassistant", "turn_on")
    async_mock_service(hass, "homeassistant", "turn_off")
    entry = await _setup(hass, cheap=(0, 1), controlled_state="off")
    subentry_id = next(iter(entry.subentries))
    coordinator = entry.runtime_data
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()

    hass.states.async_set(ENTITY, "on", {"friendly_name": "Floor"})
    await hass.async_block_till_done()

    assert coordinator.runtime[subentry_id].driven is True
    assert coordinator.actuator._override_active(subentry_id) is False


def _issue(hass: HomeAssistant, subentry_id: str):
    return ir.async_get(hass).async_get_issue(DOMAIN, f"{ISSUE_UNOWNED_RUN}_{subentry_id}")


async def test_unowned_run_issue_raised_and_cleared(hass: HomeAssistant, freezer) -> None:
    # A coexist load on for longer than the threshold, outside every period,
    # with nobody owning it: nothing will ever switch it off, so say so.
    async_mock_service(hass, "homeassistant", "turn_on")
    async_mock_service(hass, "homeassistant", "turn_off")
    started = dt_util.utcnow()
    freezer.move_to(started - timedelta(hours=UNOWNED_RUN_HOURS + 1))
    hass.states.async_set(ENTITY, "on")  # somebody else started it, long ago
    freezer.move_to(started)

    entry = await _setup(hass, cheap=(20, 21), controlled_state="on")
    subentry_id = next(iter(entry.subentries))
    coordinator = entry.runtime_data
    # The first refresh runs before the actuator exists; the periodic tick is
    # what evaluates the issue.
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    issue = _issue(hass, subentry_id)
    assert issue is not None
    assert issue.translation_placeholders["entity"] == ENTITY

    hass.states.async_set(ENTITY, "off")
    await hass.async_block_till_done()
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert _issue(hass, subentry_id) is None
