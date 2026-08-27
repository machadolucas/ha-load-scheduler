"""The Load Scheduler integration.

A hub config entry holds the shared price/solar sources, the coordinator and the
actuator; one config *subentry* per load (water heater, dishwasher, EV, floor
heating, …) carries that load's schedule and owns its device + entities.
"""

from __future__ import annotations

import hashlib
import logging
import pathlib

import voluptuous as vol
from homeassistant.const import ATTR_DEVICE_ID, ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .actuation import LoadActuator
from .const import (
    ATTR_MINUTES,
    BOOST_MAX_MINUTES,
    BOOST_MIN_MINUTES,
    DEFAULT_BOOST_MINUTES,
    DOMAIN,
    PLATFORMS,
    SERVICE_BOOST,
)
from .coordinator import LoadSchedulerConfigEntry, LoadSchedulerCoordinator

_LOGGER = logging.getLogger(__name__)
_CARD_FILE = "load-scheduler-card.js"
_CARD_URL = f"/{DOMAIN}/{_CARD_FILE}"

_BOOST_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_DEVICE_ID): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional(ATTR_ENTITY_ID): cv.entity_ids,
        vol.Optional(ATTR_MINUTES): vol.All(
            vol.Coerce(float), vol.Range(min=BOOST_MIN_MINUTES, max=BOOST_MAX_MINUTES)
        ),
    }
)


def _card_version(path: pathlib.Path) -> str:
    """A short content hash of the card file, for cache-busting its URL."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:8]


async def _async_register_resource(hass: HomeAssistant, url: str) -> bool:
    """Add/refresh the card in the Lovelace resource registry (storage mode).

    Preferred over ``add_extra_js_url``: the resource registry is fetched by the
    frontend at runtime over WebSocket, so the card survives a stale app shell —
    a CDN edge or the service worker serving cached index HTML that omits an
    injected ``<script>`` (the failure mode where the browser never even requests
    the card). This is how HACS registers its cards. Returns ``True`` when
    handled, ``False`` when the registry isn't usable (YAML resource mode, or
    Lovelace not ready) so the caller can fall back to ``add_extra_js_url``.

    Idempotent across restarts: it matches on the URL path and updates the version
    in place, so the resource list never accumulates duplicates.
    """
    try:
        from homeassistant.components.lovelace.const import LOVELACE_DATA
        from homeassistant.components.lovelace.resources import ResourceStorageCollection
    except ImportError:
        return False
    data = hass.data.get(LOVELACE_DATA)
    if data is None:
        return False
    resources = data.resources
    if not isinstance(resources, ResourceStorageCollection):
        return False  # YAML resource mode — can't be edited programmatically
    if not resources.loaded:
        await resources.async_load()
        resources.loaded = True
    base = url.split("?", 1)[0]
    existing = [
        item
        for item in resources.async_items()
        if str(item.get("url", "")).split("?", 1)[0] == base
    ]
    if existing:
        keep, *dupes = existing
        if keep.get("url") != url:
            await resources.async_update_item(keep["id"], {"url": url})
        for dupe in dupes:  # collapse duplicates from older registrations
            await resources.async_delete_item(dupe["id"])
    else:
        await resources.async_create_item({"res_type": "module", "url": url})
    return True


async def _async_register_frontend(hass: HomeAssistant) -> None:
    """Register the bundled Lovelace card (best-effort).

    The card is added to the Lovelace **resource registry** (like HACS) rather
    than injected into the index HTML via ``add_extra_js_url``: an injected
    ``<script>`` is dropped whenever a cached app shell — a CDN edge or the
    frontend service worker serving stale index HTML — is used, which makes the
    card vanish ("Custom element doesn't exist") until a hard refresh. The
    resource registry is fetched by the frontend at runtime, so it is immune;
    ``add_extra_js_url`` remains a fallback for YAML resource mode.

    The file is served with long-lived cache headers (``cache_headers=True``) and
    the URL carries a ``?v=<content-hash>`` query so a changed card refetches
    while unchanged files stay cached. The static route matches on path only, so
    the query is ignored server-side; the bare path is what's registered.

    Skipped silently when the frontend/http components aren't available (e.g. in
    the test harness); the integration works without the card.
    """
    if hass.data.get(f"{DOMAIN}_card"):
        return
    try:
        from homeassistant.components.http import StaticPathConfig

        path = pathlib.Path(__file__).parent / "frontend" / _CARD_FILE
        # Register the static route *first*: serving the card must never depend on
        # the cache-buster below. Computing the hash reads the file, and any
        # failure there must not leave the card unregistered (a silent 404).
        await hass.http.async_register_static_paths([StaticPathConfig(_CARD_URL, str(path), True)])
        url = _CARD_URL
        try:
            version = await hass.async_add_executor_job(_card_version, path)
            url = f"{_CARD_URL}?v={version}"
        except Exception as err:  # noqa: BLE001 - cache-bust is best-effort
            _LOGGER.debug("Load Scheduler card cache-buster skipped: %s", err)
        # Prefer the resource registry; fall back to extra-JS for YAML mode.
        if not await _async_register_resource(hass, url):
            from homeassistant.components.frontend import add_extra_js_url

            add_extra_js_url(hass, url)
        hass.data[f"{DOMAIN}_card"] = True
    except Exception as err:  # noqa: BLE001 - best-effort; core may be partial
        _LOGGER.debug("Load Scheduler card not registered: %s", err)


def _resolve_load(hass: HomeAssistant, device_id: str) -> tuple[LoadSchedulerCoordinator, str]:
    """Map a targeted device to its coordinator + subentry id.

    One load subentry owns exactly one device, identified as
    ``(DOMAIN, subentry_id)`` (see ``entity.LoadSchedulerEntity``), so the
    device *is* the load. The owning hub is found through the device's config
    entries rather than assumed, since several hubs may coexist.
    """
    device = dr.async_get(hass).async_get(device_id)
    subentry_id = (
        next((ident[1] for ident in device.identifiers if ident[0] == DOMAIN), None)
        if device is not None
        else None
    )
    if subentry_id is not None:
        for entry_id in device.config_entries:
            entry = hass.config_entries.async_get_entry(entry_id)
            if entry is None or entry.domain != DOMAIN or subentry_id not in entry.subentries:
                continue
            coordinator = getattr(entry, "runtime_data", None)
            if coordinator is not None:
                return coordinator, subentry_id
    raise ServiceValidationError(
        f"Device {device_id} is not a Load Scheduler load (or its hub is not loaded)"
    )


def _targeted_devices(hass: HomeAssistant, call: ServiceCall) -> list[str]:
    """The device ids a call targets, folding entity targets onto their device."""
    device_ids = set(call.data.get(ATTR_DEVICE_ID, []))
    registry = er.async_get(hass)
    for entity_id in call.data.get(ATTR_ENTITY_ID, []):
        entry = registry.async_get(entity_id)
        if entry is None or entry.device_id is None:
            raise ServiceValidationError(f"{entity_id} does not belong to a Load Scheduler load")
        device_ids.add(entry.device_id)
    if not device_ids:
        raise ServiceValidationError("No Load Scheduler load was targeted")
    return sorted(device_ids)


async def _async_boost_service(call: ServiceCall) -> None:
    """Handle ``load_scheduler.boost``."""
    minutes = call.data.get(ATTR_MINUTES)
    # Resolve every target before acting, so a call naming one bad device does
    # not boost half the others first.
    targets = [
        _resolve_load(call.hass, device_id) for device_id in _targeted_devices(call.hass, call)
    ]
    for coordinator, subentry_id in targets:
        run = minutes
        if run is None:
            # Same default as the boost button: the load's own target runtime.
            rt = coordinator.runtime[subentry_id]
            run = rt.target_minutes if rt.target_minutes > 0 else DEFAULT_BOOST_MINUTES
        await coordinator.async_boost(subentry_id, run)


@callback
def _async_register_services(hass: HomeAssistant) -> None:
    """Register the domain-level services once, on the first hub set up.

    Like the bundled card, these are shared resources rather than per-entry
    ones: the handler resolves the hub from the targeted device, and a second
    hub must not re-register (or, on unload, tear down) what the first owns.
    """
    if hass.services.has_service(DOMAIN, SERVICE_BOOST):
        return
    hass.services.async_register(DOMAIN, SERVICE_BOOST, _async_boost_service, schema=_BOOST_SCHEMA)


async def async_setup_entry(hass: HomeAssistant, entry: LoadSchedulerConfigEntry) -> bool:
    """Set up Load Scheduler from the hub config entry."""
    coordinator = LoadSchedulerCoordinator(hass, entry)
    await coordinator.async_load_runtime()
    await coordinator.async_refresh_baseline()
    coordinator.async_setup_listeners()
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await _async_register_frontend(hass)
    _async_register_services(hass)

    # Actuation: drive controlled entities + reconcile on startup (catch-up).
    actuator = LoadActuator(hass, coordinator)
    coordinator.actuator = actuator  # let entities signal an explicit stop
    await actuator.async_start()
    entry.async_on_unload(actuator.async_shutdown)
    entry.async_on_unload(coordinator.async_add_listener(actuator.async_handle_update))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: LoadSchedulerConfigEntry) -> bool:
    """Unload the hub config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload_entry(hass: HomeAssistant, entry: LoadSchedulerConfigEntry) -> None:
    """Reload on options/subentry changes (picks up added/removed loads)."""
    await hass.config_entries.async_reload(entry.entry_id)
