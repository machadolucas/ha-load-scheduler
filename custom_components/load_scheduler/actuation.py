"""Drive each load's controlled entity, combining the plan with live overrides.

The desired on/off for a load at any instant is resolved by precedence:

1. **Manual override** — if the controlled entity was changed out of band, the
   integration backs off for a grace period (returns "don't touch").
2. **Low-temp safety floor** — for a load with a temperature sensor configured,
   force heat when it drops below the threshold (Finland winters), regardless of
   price.
3. **Scheduled plan** — the coordinator's periods (cheap/solar/min-service/boost).
4. **Real-time solar divert** — when there's live export surplus and selling
   isn't worth it, surplus is dispatched to the highest-priority eligible loads.
5. Otherwise **off**.

This also gives restart catch-up: ``async_start`` reconciles once on setup.

Anti-thrash: divert decisions hold for a minimum dwell time; the divert set is
filled/drained one load at a time as live net energy swings, mirroring (and
superseding) the per-load ``Solar - Auto …`` automations but coordinated by
priority. A load whose actual-heating feedback shows it is already satisfied
(running but idle, e.g. a full tank) is not given more solar.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from homeassistant.const import SERVICE_TURN_OFF, SERVICE_TURN_ON
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_point_in_time,
    async_track_state_change_event,
)
from homeassistant.util import dt as dt_util

from .const import (
    CONF_LIVE_SELL_ENTITY,
    CONF_NET_ENERGY_ENTITY,
    CONF_NET_EXPORT_THRESHOLD,
    CONF_SELL_THRESHOLD,
    DEFAULT_NET_EXPORT_THRESHOLD,
    DEFAULT_SELL_THRESHOLD,
    DIVERT_MIN_DWELL_S,
    EVENT_RUN_ENDED,
    EVENT_RUN_STARTED,
    MANUAL_OVERRIDE_GRACE_S,
)
from .coordinator import LoadSchedulerCoordinator
from .models import LoadConfig

_LOGGER = logging.getLogger(__name__)
_COMMAND_WINDOW_S = 5  # a controlled-entity change within this of our command is "ours"


def _as_float(state) -> float | None:
    if state is None:
        return None
    try:
        return float(state.state)
    except (TypeError, ValueError):
        return None


class LoadActuator:
    """Resolves and applies each load's controlled-entity state."""

    def __init__(self, hass: HomeAssistant, coordinator: LoadSchedulerCoordinator) -> None:
        self._hass = hass
        self._coordinator = coordinator
        data = coordinator.config_entry.data
        self._net_entity: str | None = data.get(CONF_NET_ENERGY_ENTITY)
        self._net_export_threshold: float = float(
            data.get(CONF_NET_EXPORT_THRESHOLD, DEFAULT_NET_EXPORT_THRESHOLD)
        )
        self._live_sell_entity: str | None = data.get(CONF_LIVE_SELL_ENTITY)
        self._sell_threshold: float = float(data.get(CONF_SELL_THRESHOLD, DEFAULT_SELL_THRESHOLD))

        self._diverted: set[str] = set()
        self._last_divert_change: datetime | None = None
        self._override_until: dict[str, datetime] = {}
        self._last_command: dict[str, tuple[bool, datetime]] = {}
        self._unsub_boundary = None
        self._unsubs: list = []

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def async_start(self) -> None:
        """Initial reconcile (restart catch-up) + register live listeners."""
        watched = self._watched_entities()
        if watched:
            self._unsubs.append(
                async_track_state_change_event(self._hass, watched, self._async_on_event)
            )
        self._update_divert()
        await self._reconcile()
        self._schedule_next_boundary()

    @callback
    def async_handle_update(self) -> None:
        """Coordinator-listener callback: the plan may have changed."""
        self._schedule_next_boundary()
        self._evaluate("plan update")

    @callback
    def async_shutdown(self) -> None:
        if self._unsub_boundary is not None:
            self._unsub_boundary()
            self._unsub_boundary = None
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()

    def _watched_entities(self) -> list[str]:
        watched: set[str] = set()
        if self._net_entity:
            watched.add(self._net_entity)
        if self._live_sell_entity:
            watched.add(self._live_sell_entity)
        for sid in self._coordinator.config_entry.subentries:
            cfg = self._coordinator.load_config(sid)
            for entity in (cfg.controlled_entity, cfg.temp_entity, cfg.feedback_entity):
                if entity:
                    watched.add(entity)
        return sorted(watched)

    # ── event handling ───────────────────────────────────────────────────────

    @callback
    def _async_on_event(self, event: Event) -> None:
        entity_id = event.data.get("entity_id")
        self._note_controlled_change(entity_id, event)
        self._evaluate("source change")

    @callback
    def _evaluate(self, _reason: str) -> None:
        self._update_divert()
        self._coordinator.config_entry.async_create_task(
            self._hass, self._reconcile(), "ls_reconcile"
        )

    def _note_controlled_change(self, entity_id, event: Event) -> None:
        """Detect a manual (foreign) change to a controlled entity."""
        now = dt_util.utcnow()
        for sid in self._coordinator.config_entry.subentries:
            cfg = self._coordinator.load_config(sid)
            if cfg.controlled_entity != entity_id:
                continue
            new = event.data.get("new_state")
            if new is None:
                return
            is_on = new.state == "on"
            cmd = self._last_command.get(sid)
            ours = (
                cmd is not None
                and cmd[0] == is_on
                and (now - cmd[1]).total_seconds() < _COMMAND_WINDOW_S
            )
            if not ours:
                self._override_until[sid] = now + timedelta(seconds=MANUAL_OVERRIDE_GRACE_S)
                _LOGGER.debug("Manual override on %s; backing off", entity_id)
            return

    # ── real-time divert ─────────────────────────────────────────────────────

    def _eligible_for_divert(self, sid: str, cfg: LoadConfig) -> bool:
        if cfg.is_informational or not cfg.controlled_entity or not cfg.allow_solar:
            return False
        if not self._coordinator.runtime[sid].enabled:
            return False
        if self._override_active(sid):
            return False
        return not self._is_satisfied(cfg)

    def _is_satisfied(self, cfg: LoadConfig) -> bool:
        """A load running but with its element idle (e.g. a full hot-water tank)."""
        if not cfg.feedback_entity:
            return False
        controlled = self._hass.states.get(cfg.controlled_entity)
        if controlled is None or controlled.state != "on":
            return False
        fb = self._hass.states.get(cfg.feedback_entity)
        if fb is None:
            return False
        power = _as_float(fb)
        if power is not None:
            return power < cfg.feedback_idle_w
        return fb.state in ("off", "idle", "unavailable")

    @callback
    def _update_divert(self) -> None:
        """Fill/drain the diverted set as live export surplus swings."""
        if not self._net_entity:
            return
        net = _as_float(self._hass.states.get(self._net_entity))
        if net is None:
            return

        sell_ok = True
        if self._live_sell_entity:
            sell = _as_float(self._hass.states.get(self._live_sell_entity))
            sell_ok = sell is not None and sell < self._sell_threshold

        # Drop any diverted loads that are no longer eligible (disabled, satisfied…).
        self._diverted = {
            sid
            for sid in self._diverted
            if self._eligible_for_divert(sid, self._coordinator.load_config(sid))
        }

        now = dt_util.utcnow()
        if (
            self._last_divert_change is not None
            and (now - self._last_divert_change).total_seconds() < DIVERT_MIN_DWELL_S
        ):
            return  # anti-thrash dwell

        exporting = net < -self._net_export_threshold
        importing = net > self._net_export_threshold

        def priority(sid: str) -> int:
            return self._coordinator.load_config(sid).priority

        if exporting and sell_ok:
            candidates = [
                sid
                for sid in self._coordinator.config_entry.subentries
                if sid not in self._diverted
                and self._eligible_for_divert(sid, self._coordinator.load_config(sid))
            ]
            if candidates:
                self._diverted.add(max(candidates, key=priority))
                self._last_divert_change = now
        elif importing or not sell_ok:
            if self._diverted:
                self._diverted.discard(min(self._diverted, key=priority))
                self._last_divert_change = now

    # ── desired state + actuation ────────────────────────────────────────────

    def _override_active(self, sid: str) -> bool:
        until = self._override_until.get(sid)
        return until is not None and dt_util.utcnow() < until

    def _desired_on(self, sid: str, cfg: LoadConfig) -> bool | None:
        """Resolve the desired controlled-entity state, or None to not touch."""
        if self._override_active(sid):
            return None
        plan = (self._coordinator.data or {}).get(sid)
        if plan is None or plan.error:
            return None
        if cfg.is_informational or not cfg.controlled_entity:
            return None
        # Low-temp safety floor (overrides everything below).
        if cfg.temp_entity:
            temp = _as_float(self._hass.states.get(cfg.temp_entity))
            if temp is not None and temp < cfg.temp_min:
                return True
        if plan.active_period(dt_util.utcnow()) is not None:
            return True
        return sid in self._diverted

    async def _reconcile(self) -> None:
        for sid in self._coordinator.config_entry.subentries:
            cfg = self._coordinator.load_config(sid)
            desired = self._desired_on(sid, cfg)
            if desired is None:
                continue
            await self._apply(sid, cfg, desired)

    async def _apply(self, sid: str, cfg: LoadConfig, desired_on: bool) -> None:
        entity_id = cfg.controlled_entity
        state = self._hass.states.get(entity_id)
        is_on = state is not None and state.state == "on"
        if desired_on == is_on:
            return
        self._last_command[sid] = (desired_on, dt_util.utcnow())
        await self._hass.services.async_call(
            "homeassistant",
            SERVICE_TURN_ON if desired_on else SERVICE_TURN_OFF,
            {"entity_id": entity_id},
            blocking=False,
        )
        self._hass.bus.async_fire(
            EVENT_RUN_STARTED if desired_on else EVENT_RUN_ENDED,
            {"subentry_id": sid, "name": cfg.name, "entity_id": entity_id},
        )

    # ── boundary scheduling ──────────────────────────────────────────────────

    def _boundaries_after(self, now: datetime) -> list[datetime]:
        return [
            t
            for plan in (self._coordinator.data or {}).values()
            for p in plan.periods
            for t in (p.start, p.end)
            if t > now
        ]

    @callback
    def _schedule_next_boundary(self) -> None:
        if self._unsub_boundary is not None:
            self._unsub_boundary()
            self._unsub_boundary = None
        bounds = self._boundaries_after(dt_util.utcnow())
        if bounds:
            self._unsub_boundary = async_track_point_in_time(
                self._hass, self._boundary_fired, min(bounds)
            )

    @callback
    def _boundary_fired(self, _now) -> None:
        self._unsub_boundary = None
        self._evaluate("boundary")
        self._schedule_next_boundary()
