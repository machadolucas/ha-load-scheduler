"""Hub + subentry reconfigure flows and price-source validation."""

from __future__ import annotations

from homeassistant.config_entries import SOURCE_USER, ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.load_scheduler.const import DOMAIN, SUBENTRY_TYPE_LOAD


def _valid_price() -> dict:
    return {
        "raw_today": [
            {
                "start": "2026-01-01T00:00:00+00:00",
                "end": "2026-01-01T01:00:00+00:00",
                "value": 0.1,
            }
        ]
    }


async def _hub(hass: HomeAssistant, **subentries) -> MockConfigEntry:
    hass.states.async_set("sensor.prices", "ok", _valid_price())
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"name": "Hub", "buy_price_entity": "sensor.prices"},
        unique_id="sensor.prices",
        **subentries,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_invalid_price_entity_rejected(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.bad", "1", {"foo": "bar"})
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Hub", "buy_price_entity": "sensor.bad"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"]["base"] == "invalid_price_entity"


async def test_unavailable_price_entity_is_allowed(hass: HomeAssistant) -> None:
    # No state for the entity yet => can't validate => allow (set up later).
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Hub", "buy_price_entity": "sensor.not_yet"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_hub_reconfigure_updates_sources(hass: HomeAssistant) -> None:
    entry = await _hub(hass)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "name": "Hub",
            "buy_price_entity": "sensor.prices",
            "sell_price_entity": "sensor.sell",
        },
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["sell_price_entity"] == "sensor.sell"


async def test_subentry_reconfigure_edits_load(hass: HomeAssistant) -> None:
    entry = await _hub(
        hass,
        subentries_data=[
            ConfigSubentryData(
                subentry_type=SUBENTRY_TYPE_LOAD,
                title="Heater",
                unique_id=None,
                data={
                    "name": "Heater",
                    "mode": "non_sequential",
                    "target_minutes": 30,
                    "runs_per_day": 1,
                },
            )
        ],
    )
    subentry_id = next(iter(entry.subentries))

    result = await entry.start_subentry_reconfigure_flow(hass, subentry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "name": "Heater",
            "mode": "non_sequential",
            "target_minutes": 90,
            "runs_per_day": 1,
        },
    )
    assert result["type"] is FlowResultType.ABORT
    assert entry.subentries[subentry_id].data["target_minutes"] == 90
