"""`button.<load>_boost` — run the load now for its target duration.

A boost overrides both the price plan and the enable switch (see the
coordinator), so it is a true manual "run now". It expires automatically and
survives a restart (persisted via the Store).
"""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DEFAULT_BOOST_MINUTES, SUBENTRY_TYPE_LOAD
from .coordinator import LoadSchedulerConfigEntry
from .entity import LoadSchedulerEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LoadSchedulerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the boost button for each load subentry."""
    coordinator = entry.runtime_data
    for subentry_id, subentry in entry.subentries.items():
        if subentry.subentry_type != SUBENTRY_TYPE_LOAD:
            continue
        async_add_entities(
            [LoadBoostButton(coordinator, subentry_id, subentry)],
            config_subentry_id=subentry_id,
        )


class LoadBoostButton(LoadSchedulerEntity, ButtonEntity):
    """Forces the load to run now for its target duration (or a default)."""

    def __init__(self, coordinator, subentry_id, subentry) -> None:
        super().__init__(coordinator, subentry_id, subentry, "boost")

    async def async_press(self) -> None:
        target = self.coordinator.runtime[self._subentry_id].target_minutes
        minutes = target if target > 0 else DEFAULT_BOOST_MINUTES
        await self.coordinator.async_boost(self._subentry_id, minutes)
