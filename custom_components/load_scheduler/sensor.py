"""`sensor.<load>_schedule` — next run time + the upcoming periods.

A single merged sensor (per the "avoid entity bloat" goal): its state is the
next run's start (a timestamp), and its attributes carry the full upcoming
schedule, the active period, cost and status — everything a dashboard/card
needs in one entity.
"""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from .const import SUBENTRY_TYPE_LOAD
from .coordinator import LoadSchedulerConfigEntry
from .entity import LoadSchedulerEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LoadSchedulerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the schedule sensor for each load subentry."""
    coordinator = entry.runtime_data
    for subentry_id, subentry in entry.subentries.items():
        if subentry.subentry_type != SUBENTRY_TYPE_LOAD:
            continue
        async_add_entities(
            [LoadScheduleSensor(coordinator, subentry_id, subentry)],
            config_subentry_id=subentry_id,
        )


class LoadScheduleSensor(LoadSchedulerEntity, SensorEntity):
    """State = next run start; attributes = the full upcoming plan."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator, subentry_id, subentry) -> None:
        super().__init__(coordinator, subentry_id, subentry, "schedule")
        self._unsub_state = None

    @property
    def native_value(self) -> datetime | None:
        plan = self._plan
        if not plan:
            return None
        nxt = plan.next_period(dt_util.utcnow())
        return nxt.start if nxt else None

    @property
    def extra_state_attributes(self) -> dict:
        plan = self._plan
        if not plan:
            return {}
        now = dt_util.utcnow()
        active = plan.active_period(now)
        controlled_on, heating = self._actual_state()
        return {
            "periods": [
                {
                    "start": dt_util.as_local(p.start).isoformat(),
                    "end": dt_util.as_local(p.end).isoformat(),
                    "source": str(p.source),
                    "avg_cost": round(p.avg_cost, 5),
                }
                for p in plan.periods
            ],
            "running": active is not None,
            # Actual controlled-entity / element state, for the card's dot:
            # active = the switch is on; heating = the element is actually drawing
            # (power ≥ idle threshold). None when there's no such entity to read.
            "active": controlled_on,
            "heating": heating,
            "current_period_end": dt_util.as_local(active.end).isoformat() if active else None,
            "target_minutes": plan.target_minutes,
            "enabled": plan.enabled,
            "status": plan.error or ("disabled" if not plan.enabled else "ok"),
        }

    def _actual_state(self) -> tuple[bool | None, bool | None]:
        """(controlled_on, heating) read live from the load's entities."""
        cfg = self.coordinator.load_config(self._subentry_id)
        controlled_on: bool | None = None
        heating: bool | None = None
        if cfg.controlled_entity:
            st = self.coordinator.hass.states.get(cfg.controlled_entity)
            controlled_on = st is not None and st.state == "on"
        if cfg.feedback_entity:
            fb = self.coordinator.hass.states.get(cfg.feedback_entity)
            if fb is not None and fb.state not in ("unknown", "unavailable"):
                try:
                    heating = float(fb.state) >= cfg.feedback_idle_w
                except (TypeError, ValueError):
                    heating = fb.state in ("on", "heating")
        return controlled_on, heating

    @callback
    def _state_changed(self, _event) -> None:
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        cfg = self.coordinator.load_config(self._subentry_id)
        watch = [e for e in (cfg.controlled_entity, cfg.feedback_entity) if e]
        if watch:
            self._unsub_state = async_track_state_change_event(
                self.hass, watch, self._state_changed
            )

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None
