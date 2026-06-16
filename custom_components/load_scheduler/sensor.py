"""`sensor.<load>_schedule` — next run time + the upcoming periods.

A single merged sensor (per the "avoid entity bloat" goal): its state is the
next run's start (a timestamp), and its attributes carry the full upcoming
schedule, the active period, cost and status — everything a dashboard/card
needs in one entity.
"""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
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
            "current_period_end": dt_util.as_local(active.end).isoformat() if active else None,
            "target_minutes": plan.target_minutes,
            "enabled": plan.enabled,
            "status": plan.error or ("disabled" if not plan.enabled else "ok"),
        }
