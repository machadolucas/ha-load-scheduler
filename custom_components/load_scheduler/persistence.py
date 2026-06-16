"""Store-backed persistence for per-load runtime state.

The user-adjustable values that must survive restarts — the target runtime and
the enabled flag — live in a ``Store`` under ``.storage/`` (so they are part of
Home Assistant backups). The coordinator treats this as the source of truth,
ahead of any entity RestoreState.
"""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

from .const import DOMAIN, SAVE_DELAY, STORAGE_VERSION


class RuntimeStore:
    """Thin wrapper over ``Store`` keyed per hub config entry."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store[dict] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}")

    async def async_load(self) -> dict:
        """Return the saved ``{subentry_id: {...}}`` mapping (empty if none)."""
        return await self._store.async_load() or {}

    @callback
    def async_schedule_save(self, snapshot: Callable[[], dict]) -> None:
        """Debounced save; ``snapshot`` is called when the write actually fires."""
        self._store.async_delay_save(snapshot, SAVE_DELAY)
