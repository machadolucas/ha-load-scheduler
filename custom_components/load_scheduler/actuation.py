"""Drive each load's controlled entity, combining the plan with live overrides.

The desired on/off for a load at any instant is resolved by precedence:

1. **Manual override** — if the controlled entity was changed out of band, the
   integration backs off (returns "don't touch"). A manual **off** stops the
   current run: it cancels any active boost and suppresses the rest of the active
   period (not just a short grace), so a load you switch off does not pop back
   on. A manual **on** is left alone and its run is credited as delivered.
2. **Low-temp safety floor** — for a load with a temperature sensor configured,
   force heat when it drops below the threshold (Finland winters), regardless of
   price.
3. **Scheduled plan** — the coordinator's periods (cheap/solar/min-service/boost).
4. **Real-time solar divert** — when there's live export surplus and selling
   isn't worth it, surplus is dispatched to the highest-priority eligible loads.
5. Otherwise **off**.

**Coexist (top-up) loads** never have step 5 force them off: the integration
only switches such a load *off* if it was the one that switched it *on*. This
lets it add cheap/green energy on top of an external control (e.g. floor-heating
comfort automations) without ever fighting it — external on-runs are observed
and credited, never cut short.

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
    CONF_PREDICTED_NET_ENERGY_ENTITY,
    CONF_SELL_THRESHOLD,
    DEFAULT_NET_EXPORT_THRESHOLD,
    DEFAULT_SELL_THRESHOLD,
    DIVERT_MIN_DWELL_S,
    DIVERT_SATISFIED_BACKOFF_S,
    DIVERT_SETTLE_S,
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
        self._predicted_net_entity: str | None = data.get(CONF_PREDICTED_NET_ENERGY_ENTITY)
        self._net_export_threshold: float = float(
            data.get(CONF_NET_EXPORT_THRESHOLD, DEFAULT_NET_EXPORT_THRESHOLD)
        )
        self._live_sell_entity: str | None = data.get(CONF_LIVE_SELL_ENTITY)
        self._sell_threshold: float = float(data.get(CONF_SELL_THRESHOLD, DEFAULT_SELL_THRESHOLD))

        self._diverted: set[str] = set()
        self._last_divert_change: datetime | None = None
        # Loads parked out of the divert pool because they're satisfied (full),
        # keyed by subentry_id → the UTC time the back-off expires.
        self._satisfied_until: dict[str, datetime] = {}
        self._override_until: dict[str, datetime] = {}
        self._last_command: dict[str, tuple[bool, datetime]] = {}
        # Loads the integration currently holds ON (a run it started). Used so a
        # coexist load is only ever switched off by the integration if it was the
        # one that switched it on.
        self._driven: set[str] = set()
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
        if self._predicted_net_entity:
            watched.add(self._predicted_net_entity)
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
        """Detect a manual (foreign) change to a controlled entity and react."""
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
            if ours:
                return
            grace_until = now + timedelta(seconds=MANUAL_OVERRIDE_GRACE_S)
            if is_on:
                # Manual ON: don't immediately undo it; the run is credited via
                # the measured delivered sensor. It's not a run we started.
                self._override_until[sid] = grace_until
                self._driven.discard(sid)
            else:
                # Manual OFF: stop the current run. Suppress the rest of the
                # active period (not just the short grace) and cancel any boost,
                # so the load does not pop back on.
                plan = (self._coordinator.data or {}).get(sid)
                active = plan.active_period(now) if plan else None
                self._override_until[sid] = max(active.end, grace_until) if active else grace_until
                self._driven.discard(sid)
                rt = self._coordinator.runtime.get(sid)
                if rt is not None and rt.boost_until and now < rt.boost_until:
                    self._coordinator.config_entry.async_create_task(
                        self._hass, self._coordinator.async_cancel_boost(sid), "ls_cancel_boost"
                    )
            _LOGGER.debug(
                "Manual override (%s) on %s; backing off", "on" if is_on else "off", entity_id
            )
            return

    # ── real-time divert ─────────────────────────────────────────────────────

    def _eligible_for_divert(self, sid: str, cfg: LoadConfig) -> bool:
        if cfg.is_informational or not cfg.controlled_entity or not cfg.allow_solar:
            return False
        if not self._coordinator.runtime[sid].enabled:
            return False
        if self._override_active(sid):
            return False
        backoff = self._satisfied_until.get(sid)
        if backoff is not None and dt_util.utcnow() < backoff:
            return False
        return not self._is_satisfied(cfg)

    def _is_satisfied(self, cfg: LoadConfig) -> bool:
        """A load running but with its element idle (e.g. a full hot-water tank).

        Only judged after the load has been on for ``DIVERT_SETTLE_S`` — long
        enough for the element to actually start drawing (and the power sensor to
        report it). Without the settle window a load that was *just* switched on
        reads as idle and gets switched straight back off, flickering the relay
        every divert tick.
        """
        if not cfg.feedback_entity:
            return False
        controlled = self._hass.states.get(cfg.controlled_entity)
        if controlled is None or controlled.state != "on":
            return False
        if (dt_util.utcnow() - controlled.last_changed).total_seconds() < DIVERT_SETTLE_S:
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

        # Interval-aware gate: only *start* a load when the predicted end-of-interval
        # net is also exporting, so we don't begin a run we won't still be exporting
        # for by the close of the 15-min metering interval.
        predicted_ok = True
        if self._predicted_net_entity:
            pred = _as_float(self._hass.states.get(self._predicted_net_entity))
            predicted_ok = pred is not None and pred < -self._net_export_threshold

        now = dt_util.utcnow()
        # Drop any diverted loads that are no longer eligible (disabled, satisfied…).
        # A load found satisfied (full tank, element idle) is parked for a back-off
        # so the surplus is reallocated to a lower-priority load instead of pulsing
        # this one on/off every dwell.
        kept: set[str] = set()
        for sid in self._diverted:
            cfg = self._coordinator.load_config(sid)
            if self._is_satisfied(cfg):
                self._satisfied_until[sid] = now + timedelta(seconds=DIVERT_SATISFIED_BACKOFF_S)
            if self._eligible_for_divert(sid, cfg):
                kept.add(sid)
        self._diverted = kept

        if (
            self._last_divert_change is not None
            and (now - self._last_divert_change).total_seconds() < DIVERT_MIN_DWELL_S
        ):
            return  # anti-thrash dwell

        exporting = net < -self._net_export_threshold
        importing = net > self._net_export_threshold

        def priority(sid: str) -> int:
            return self._coordinator.load_config(sid).priority

        if exporting and sell_ok and predicted_ok:
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

    @callback
    def note_manual_stop(self, sid: str) -> None:
        """Back off after an explicit user stop (e.g. cancelling a boost).

        Sets the same grace as a manual off and drops the load from the driven /
        diverted sets, so the real-time divert or the plan don't immediately
        re-grab a load the user just stopped (notably on a solar-exporting summer
        night). After the grace, normal scheduling/divert resumes.
        """
        self._override_until[sid] = dt_util.utcnow() + timedelta(seconds=MANUAL_OVERRIDE_GRACE_S)
        self._driven.discard(sid)
        self._diverted.discard(sid)

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
        if not desired_on and cfg.coexist and sid not in self._driven:
            # Coexist (top-up): never switch off a run we didn't start.
            return
        self._last_command[sid] = (desired_on, dt_util.utcnow())
        if desired_on:
            self._driven.add(sid)
        else:
            self._driven.discard(sid)
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
