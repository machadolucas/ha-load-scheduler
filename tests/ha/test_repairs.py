"""Repair issue raised when the price source is unusable."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.load_scheduler.const import (
    DOMAIN,
    ISSUE_PRICE_UNAVAILABLE,
    SUBENTRY_TYPE_LOAD,
)


def _entry(hass: HomeAssistant) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={"name": "Hub", "buy_price_entity": "sensor.prices"},
        unique_id="sensor.prices",
        subentries_data=[
            ConfigSubentryData(
                subentry_type=SUBENTRY_TYPE_LOAD,
                title="Heater",
                unique_id=None,
                data={"name": "Heater", "mode": "non_sequential", "target_minutes": 30},
            )
        ],
    )


async def test_issue_raised_without_price_data(hass: HomeAssistant) -> None:
    entry = _entry(hass)  # no sensor.prices state
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_PRICE_UNAVAILABLE)


async def test_issue_absent_with_price_data(hass: HomeAssistant) -> None:
    base = dt_util.now().replace(second=0, microsecond=0)
    hass.states.async_set(
        "sensor.prices",
        "ok",
        {
            "data_today": [
                {
                    "start": (base + timedelta(minutes=15 * i)).isoformat(),
                    "end": (base + timedelta(minutes=15 * (i + 1))).isoformat(),
                    "buy": 0.1,
                }
                for i in range(8)
            ],
            "data_tomorrow": [],
        },
    )
    entry = _entry(hass)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_PRICE_UNAVAILABLE) is None
