"""`switch.<load>_enabled` — temporarily enable/disable a load's scheduling."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import SUBENTRY_TYPE_LOAD
from .coordinator import LoadSchedulerConfigEntry
from .entity import LoadSchedulerEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LoadSchedulerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the enabled switch for each load subentry."""
    coordinator = entry.runtime_data
    for subentry_id, subentry in entry.subentries.items():
        if subentry.subentry_type != SUBENTRY_TYPE_LOAD:
            continue
        async_add_entities(
            [LoadEnabledSwitch(coordinator, subentry_id, subentry)],
            config_subentry_id=subentry_id,
        )


class LoadEnabledSwitch(LoadSchedulerEntity, SwitchEntity):
    """When off, the load is not scheduled (its plan is empty).

    State lives in the coordinator's runtime (persisted to the Store), so it is
    restored across restarts without RestoreEntity.
    """

    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(self, coordinator, subentry_id, subentry) -> None:
        super().__init__(coordinator, subentry_id, subentry, "enabled")

    @property
    def is_on(self) -> bool:
        return self.coordinator.runtime[self._subentry_id].enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_enabled(self._subentry_id, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_enabled(self._subentry_id, False)
