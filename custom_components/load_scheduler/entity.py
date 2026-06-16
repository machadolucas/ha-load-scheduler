"""Shared base for Load Scheduler per-load entities."""

from __future__ import annotations

from homeassistant.config_entries import ConfigSubentry
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import LoadPlan, LoadSchedulerCoordinator


class LoadSchedulerEntity(CoordinatorEntity[LoadSchedulerCoordinator]):
    """Base entity for one load (one subentry = one device)."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: LoadSchedulerCoordinator,
        subentry_id: str,
        subentry: ConfigSubentry,
        key: str,
    ) -> None:
        super().__init__(coordinator)
        self._subentry_id = subentry_id
        self._attr_unique_id = f"{subentry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, subentry_id)},
            name=subentry.title,
            manufacturer="Load Scheduler",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def _plan(self) -> LoadPlan | None:
        """This load's current plan (may be None before the first refresh)."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get(self._subentry_id)
