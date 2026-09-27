"""A load added while the hub is still setting up must not fail the setup.

Reproduces 2026-09-27: two load subentries added back to back. The first add
reloaded the hub; the second landed while that reload's first refresh was
awaiting the recorder (``_maybe_refresh_delivered``). That is after the refresh
seeds runtime for the loads it knows, but before its per-load loop iterates the
live ``config_entry.subentries``. The loop then hit the new load with no runtime
(``KeyError``) and put the whole hub into setup-retry, so every load went
unscheduled until the retry.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState, ConfigSubentry, ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.load_scheduler.const import DOMAIN, SUBENTRY_TYPE_LOAD
from custom_components.load_scheduler.coordinator import LoadSchedulerCoordinator


def _price_attributes() -> dict:
    base = dt_util.now().replace(second=0, microsecond=0) - timedelta(minutes=15)
    today = [
        {
            "start": (base + timedelta(minutes=15 * i)).isoformat(),
            "end": (base + timedelta(minutes=15 * (i + 1))).isoformat(),
            "buy": 0.01 if i in (10, 11) else 0.20,
            "sell": 0.005,
        }
        for i in range(24)
    ]
    return {"data_today": today, "data_tomorrow": []}


def _load(name: str) -> dict:
    return {"name": name, "mode": "non_sequential", "target_minutes": 30}


async def test_load_added_mid_setup_does_not_fail_setup(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.prices", "ok", _price_attributes())
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"name": "Hub", "buy_price_entity": "sensor.prices"},
        unique_id="sensor.prices",
        subentries_data=[
            ConfigSubentryData(
                subentry_type=SUBENTRY_TYPE_LOAD, title="First", unique_id=None, data=_load("First")
            )
        ],
    )
    entry.add_to_hass(hass)

    added: list[str] = []
    original = LoadSchedulerCoordinator._maybe_refresh_delivered

    async def add_load_mid_refresh(self, now_utc):
        # Runs inside the first refresh, after runtime was seeded and before the
        # per-load loop: the window the second subentry landed in.
        if not added:
            sub = ConfigSubentry(
                data=_load("Second"),
                subentry_type=SUBENTRY_TYPE_LOAD,
                title="Second",
                unique_id=None,
            )
            hass.config_entries.async_add_subentry(entry, sub)
            added.append(sub.subentry_id)
        await original(self, now_utc)

    with patch.object(LoadSchedulerCoordinator, "_maybe_refresh_delivered", add_load_mid_refresh):
        # Not asserting the return value: setup schedules its follow-up reload
        # before returning, so the entry may already be mid-reload by then.
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    new_sid = added[0]
    # The mid-setup load got a default runtime rather than failing the refresh...
    assert entry.runtime_data.runtime_for(new_sid).enabled is True
    # ...and the follow-up reload created its entities.
    reg = er.async_get(hass)
    sched_id = reg.async_get_entity_id("sensor", DOMAIN, f"{new_sid}_schedule")
    assert sched_id is not None
    sched = hass.states.get(sched_id)
    assert sched is not None and sched.attributes["status"] == "ok"
