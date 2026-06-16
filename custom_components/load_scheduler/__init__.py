"""The Load Scheduler integration.

A hub config entry holds the shared price/solar sources; one config *subentry*
per load (water heater, dishwasher, EV, floor heating, …) carries that load's
schedule. See the architecture notes in ``CLAUDE.md`` and ``docs/``.

M0 scope: the hub entry loads and stores its config. The coordinator, entity
platforms, persistence, actuation and the scheduling engine wiring arrive in
M2+. The pure :mod:`.engine` already implements the scheduling algorithms.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, PLATFORMS

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Load Scheduler from a config entry (the hub)."""
    hass.data.setdefault(DOMAIN, {})
    # Placeholder for the per-hub runtime object (coordinator) added in M2.
    hass.data[DOMAIN][entry.entry_id] = {}

    if PLATFORMS:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = True
    if PLATFORMS:
        unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the config entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)
