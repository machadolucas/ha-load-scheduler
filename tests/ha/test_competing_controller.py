"""Competing-controller detection: the repair issue, its decay, and its log."""

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

from custom_components.load_scheduler.competing import (
    SOURCE_SCRIPTED,
    SOURCE_USER,
    ForeignEvent,
)
from custom_components.load_scheduler.const import (
    DOMAIN,
    ISSUE_COMPETING_CONTROLLER,
    SAVE_DELAY,
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
    """A single load scheduled to run *now*, its contactor currently off.

    Running now matters: it makes the integration's own last command a turn-*on*,
    so a foreign off in the same test second can't be mistaken for our own echo.
    The timezone is pinned so the same-time clustering is not at the mercy of a
    DST weekend in the test host's default zone.
    """
    await hass.config.async_set_time_zone("UTC")
    async_mock_service(hass, "homeassistant", "turn_on")
    async_mock_service(hass, "homeassistant", "turn_off")
    hass.states.async_set("sensor.prices", "ok", _price_attributes(cheap=(0, 1)))
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


def _seed(coordinator, subentry_id: str, source: str, days: tuple[int, ...]) -> None:
    """Pre-seed prior-day foreign flips at the current time of day."""
    now = dt_util.utcnow()
    coordinator.foreign_log[subentry_id] = [
        ForeignEvent(
            when=now - timedelta(days=d), turned_on=False, in_active_period=True, source=source
        )
        for d in days
    ]


async def _foreign_flip(hass: HomeAssistant, context: Context) -> None:
    """An on→off flip made by somebody else (the on is ours, the off is theirs)."""
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()
    hass.states.async_set(ENTITY, "off", context=context)
    await hass.async_block_till_done()


def _issue(hass: HomeAssistant, subentry_id: str):
    return ir.async_get(hass).async_get_issue(DOMAIN, f"{ISSUE_COMPETING_CONTROLLER}_{subentry_id}")


async def test_repair_raised_for_recurring_scripted_offs(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    subentry_id = next(iter(entry.subentries))
    coordinator = entry.runtime_data
    _seed(coordinator, subentry_id, SOURCE_SCRIPTED, (1, 2))
    assert _issue(hass, subentry_id) is None

    await _foreign_flip(hass, Context(parent_id="deadbeef"))

    log = coordinator.foreign_log[subentry_id]
    assert len(log) == 3
    assert log[-1].source == SOURCE_SCRIPTED
    assert log[-1].in_active_period is True  # it cut a scheduled run short
    issue = _issue(hass, subentry_id)
    assert issue is not None
    assert issue.translation_placeholders["entity"] == ENTITY
    assert issue.translation_placeholders["count"] == "3"
    assert issue.translation_placeholders["scripted"] == "3"


async def test_own_commands_are_not_logged(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    subentry_id = next(iter(entry.subentries))
    coordinator = entry.runtime_data

    # The integration turned the load on during setup; the switch reporting "on"
    # is the echo of our own command, not a competitor.
    hass.states.async_set(ENTITY, "on")
    await hass.async_block_till_done()

    assert coordinator.foreign_log.get(subentry_id, []) == []
    assert _issue(hass, subentry_id) is None


async def test_user_toggle_does_not_raise(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    subentry_id = next(iter(entry.subentries))
    coordinator = entry.runtime_data
    _seed(coordinator, subentry_id, SOURCE_USER, (1, 2))

    await _foreign_flip(hass, Context(user_id="u1"))

    log = coordinator.foreign_log[subentry_id]
    assert len(log) == 3
    assert log[-1].source == SOURCE_USER
    # Three evenings of a person flipping the switch is a habit, not a rival.
    assert _issue(hass, subentry_id) is None


async def test_issue_clears_on_decay(hass: HomeAssistant, freezer) -> None:
    entry = await _setup(hass)
    subentry_id = next(iter(entry.subentries))
    _seed(entry.runtime_data, subentry_id, SOURCE_SCRIPTED, (1, 2))
    await _foreign_flip(hass, Context(parent_id="deadbeef"))
    assert _issue(hass, subentry_id) is not None

    # The competing automation is disabled; a week later nothing is left to see.
    freezer.move_to(dt_util.utcnow() + timedelta(days=8))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()

    assert entry.runtime_data.foreign_log[subentry_id] == []
    assert _issue(hass, subentry_id) is None


async def test_foreign_log_persists_across_reload(hass: HomeAssistant, freezer) -> None:
    entry = await _setup(hass)
    subentry_id = next(iter(entry.subentries))
    await _foreign_flip(hass, Context(parent_id="deadbeef"))
    before = entry.runtime_data.foreign_log[subentry_id]
    assert len(before) == 1

    # Flush the debounced Store write, then reload the hub. The clock has to
    # really move: a Store that was asked to save twice (here: the run the
    # actuator started, then this foreign change) defers its pending write to
    # the later deadline, which a bare `async_fire_time_changed` can't reach.
    freezer.tick(timedelta(seconds=SAVE_DELAY + 1))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    restored = entry.runtime_data.foreign_log[subentry_id]
    assert [ev.as_dict() for ev in restored] == [ev.as_dict() for ev in before]
