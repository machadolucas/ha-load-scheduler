"""A single hub calendar showing every load's scheduled run periods.

Replaces the old shared ``calendar.electricity`` bus: it is purely a
human-visible view (one event per scheduled period, summary = load name),
auto-regenerated from the plan and **not** used for actuation.
"""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DEFAULT_NAME, DOMAIN
from .coordinator import LoadSchedulerConfigEntry, LoadSchedulerCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LoadSchedulerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the single hub calendar (attached to the hub device)."""
    async_add_entities([LoadSchedulerCalendar(entry.runtime_data, entry.entry_id)])


class LoadSchedulerCalendar(CoordinatorEntity[LoadSchedulerCoordinator], CalendarEntity):
    """Aggregates all loads' periods into one calendar."""

    _attr_has_entity_name = True
    _attr_translation_key = "schedule"

    def __init__(self, coordinator: LoadSchedulerCoordinator, entry_id: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_calendar"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name=DEFAULT_NAME,
            manufacturer="Load Scheduler",
            entry_type=DeviceEntryType.SERVICE,
        )

    def _events(self) -> list[CalendarEvent]:
        out: list[CalendarEvent] = []
        for subentry_id, plan in (self.coordinator.data or {}).items():
            name = self.coordinator.load_config(subentry_id).name
            out.extend(CalendarEvent(start=p.start, end=p.end, summary=name) for p in plan.periods)
        return out

    @property
    def event(self) -> CalendarEvent | None:
        now = dt_util.utcnow()
        events = self._events()
        current = [e for e in events if e.start <= now < e.end]
        if current:
            return min(current, key=lambda e: e.end)
        future = [e for e in events if e.start >= now]
        return min(future, key=lambda e: e.start) if future else None

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        return [e for e in self._events() if e.end > start_date and e.start < end_date]
