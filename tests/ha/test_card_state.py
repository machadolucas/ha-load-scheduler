"""Schedule sensor exposes actual on/heating state for the card's dot."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.load_scheduler.const import DOMAIN, SUBENTRY_TYPE_LOAD


def _prices() -> dict:
    base = dt_util.now().replace(second=0, microsecond=0)
    return {
        "data_today": [
            {
                "start": (base + timedelta(minutes=15 * i)).isoformat(),
                "end": (base + timedelta(minutes=15 * (i + 1))).isoformat(),
                "buy": 0.10,
            }
            for i in range(24)
        ],
        "data_tomorrow": [],
    }


async def test_schedule_sensor_active_and_heating(hass: HomeAssistant) -> None:
    async_mock_service(hass, "homeassistant", "turn_on")
    async_mock_service(hass, "homeassistant", "turn_off")
    hass.states.async_set("sensor.prices", "ok", _prices())
    hass.states.async_set("switch.heater", "off")
    hass.states.async_set("sensor.heater_power", "0", {"unit_of_measurement": "W"})
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
                    "target_minutes": 60,
                    "controlled_entity": "switch.heater",
                    "feedback_entity": "sensor.heater_power",
                    "feedback_idle_w": 50,
                },
            )
        ],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    subentry_id = next(iter(entry.subentries))
    sid = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{subentry_id}_schedule")

    # Off (switch off, element drawing nothing).
    attrs = hass.states.get(sid).attributes
    assert attrs["active"] is False and attrs["heating"] is False

    # On but element idle (power below threshold) => active, not heating.
    hass.states.async_set("switch.heater", "on")
    hass.states.async_set("sensor.heater_power", "20", {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    attrs = hass.states.get(sid).attributes
    assert attrs["active"] is True and attrs["heating"] is False

    # Element drawing power => heating.
    hass.states.async_set("sensor.heater_power", "1500", {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    assert hass.states.get(sid).attributes["heating"] is True


async def test_schedule_sensor_rationale_attributes(hass: HomeAssistant) -> None:
    """The diagnostic card's data: targets math + a static config summary."""
    async_mock_service(hass, "homeassistant", "turn_on")
    async_mock_service(hass, "homeassistant", "turn_off")
    hass.states.async_set("sensor.prices", "ok", _prices())
    hass.states.async_set("switch.heater", "off")
    hass.states.async_set("sensor.heater_power", "0", {"unit_of_measurement": "W"})
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
                    "target_minutes": 60,
                    "controlled_entity": "switch.heater",
                    "feedback_entity": "sensor.heater_power",
                    "feedback_idle_w": 50,
                },
            )
        ],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    subentry_id = next(iter(entry.subentries))
    sid = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{subentry_id}_schedule")
    attrs = hass.states.get(sid).attributes

    # Rationale: nothing delivered yet, so the full target remains and is scheduled.
    assert attrs["delivered_minutes"] == 0
    assert attrs["remaining_minutes"] == 60
    assert attrs["min_service_remaining"] == 0
    assert attrs["scheduled_minutes"] == 60
    assert attrs["est_cost"] == 0  # no draw_kw configured
    assert attrs["solar_enabled"] is False  # hub has no solar source
    assert attrs["boost_until"] is None

    # Structured rationale the diagnostic card narrates.
    rat = attrs["rationale"]
    assert rat is not None
    assert rat["skip_reason"] is None  # the full target was scheduled
    assert rat["mode"] == "non_sequential"
    assert rat["scheduled_minutes"] == 60
    assert rat["cap"] is None
    assert rat["solar_enabled"] is False
    assert rat["boost"] is False
    assert rat["cheapest_cost"] == 0.1
    assert rat["candidate_count"] > 0

    # Static config summary (the load's type + its wiring).
    cfg = attrs["config"]
    assert cfg["mode"] == "non_sequential"
    assert cfg["priority"] == 0
    assert cfg["runs_per_day"] == 1
    assert cfg["target_type"] == "runtime"
    assert cfg["allow_solar"] is True
    assert cfg["cap"] is None
    assert cfg["controlled_entity"] == "switch.heater"
    assert cfg["feedback_entity"] == "sensor.heater_power"

    # Delivered-today shrinks the remaining target (dynamic remaining). Pin the
    # measurement timestamp so the throttled recorder refresh doesn't overwrite.
    coordinator = entry.runtime_data
    coordinator._delivered_today[subentry_id] = 20.0
    coordinator._delivered_at = dt_util.utcnow()
    await coordinator.async_request_refresh()
    await hass.async_block_till_done()
    attrs = hass.states.get(sid).attributes
    assert attrs["delivered_minutes"] == 20
    assert attrs["remaining_minutes"] == 40


async def test_rationale_reports_all_above_cap(hass: HomeAssistant) -> None:
    """A cap below every price leaves nothing scheduled, explained as such."""
    async_mock_service(hass, "homeassistant", "turn_on")
    async_mock_service(hass, "homeassistant", "turn_off")
    hass.states.async_set("sensor.prices", "ok", _prices())  # all slots @ 0.10
    hass.states.async_set("switch.heater", "off")
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
                    "target_minutes": 60,
                    "min_service_minutes": 0,
                    "price_cap": 0.05,  # below every 0.10 slot
                    "controlled_entity": "switch.heater",
                },
            )
        ],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    subentry_id = next(iter(entry.subentries))
    sid = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{subentry_id}_schedule")
    rat = hass.states.get(sid).attributes["rationale"]
    assert rat["skip_reason"] == "all_above_cap"
    assert rat["cap"] == 0.05
    assert rat["cap_qualifying_count"] == 0
    assert rat["scheduled_minutes"] == 0


def test_schedule_sensor_excludes_bulky_attrs_from_recorder() -> None:
    """The big, slow-changing attributes stay out of the recorder."""
    from custom_components.load_scheduler.sensor import LoadScheduleSensor

    assert {"periods", "config", "rationale"} <= LoadScheduleSensor._unrecorded_attributes
