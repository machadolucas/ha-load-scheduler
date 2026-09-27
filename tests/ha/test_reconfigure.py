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


async def test_hub_reconfigure_clearing_an_optional_source_removes_it(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set("sensor.prices", "ok", _valid_price())
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "name": "Hub",
            "buy_price_entity": "sensor.prices",
            "sell_price_entity": "sensor.sell",
        },
        unique_id="sensor.prices",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Hub", "buy_price_entity": "sensor.prices"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert "sell_price_entity" not in entry.data
    assert entry.data["name"] == "Hub"


async def test_hub_reconfigure_moves_the_unique_id_with_the_buy_sensor(
    hass: HomeAssistant,
) -> None:
    entry = await _hub(hass)
    hass.states.async_set("sensor.prices2", "ok", _valid_price())
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Hub", "buy_price_entity": "sensor.prices2"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert entry.unique_id == "sensor.prices2"


async def test_hub_reconfigure_rejects_another_hubs_buy_sensor(hass: HomeAssistant) -> None:
    entry = await _hub(hass)
    MockConfigEntry(
        domain=DOMAIN,
        data={"name": "Other", "buy_price_entity": "sensor.other"},
        unique_id="sensor.other",
    ).add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Hub", "buy_price_entity": "sensor.other"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"]["base"] == "already_configured"
    assert entry.unique_id == "sensor.prices"


def _load(name: str, **extra) -> dict:
    return {
        "name": name,
        "mode": "non_sequential",
        "target_minutes": 30,
        "runs_per_day": 1,
        **extra,
    }


async def test_subentry_rejects_a_switch_another_load_controls(hass: HomeAssistant) -> None:
    entry = await _hub(
        hass,
        subentries_data=[
            ConfigSubentryData(
                subentry_type=SUBENTRY_TYPE_LOAD,
                title="Heater",
                unique_id=None,
                data=_load("Heater", controlled_entity="switch.shared"),
            )
        ],
    )
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_LOAD), context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], _load("Floor", controlled_entity="switch.shared")
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"]["controlled_entity"] == "controlled_entity_in_use"

    # Reconfiguring the owner itself keeps its own switch.
    subentry_id = next(iter(entry.subentries))
    result = await entry.start_subentry_reconfigure_flow(hass, subentry_id)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], _load("Heater", controlled_entity="switch.shared", target_minutes=45)
    )
    assert result["type"] is FlowResultType.ABORT
    assert entry.subentries[subentry_id].data["target_minutes"] == 45


async def test_subentry_kwh_target_requires_a_draw(hass: HomeAssistant) -> None:
    entry = await _hub(hass)
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_LOAD), context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], _load("EV", target_type="kwh")
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"]["draw_kw"] == "draw_required_for_kwh"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], _load("EV", target_type="kwh", draw_kw=11)
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
