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
priority. A diverted load that is on but idle (its element satisfied, e.g. a full
tank) is left powered, not switched off: it draws nothing, so the live export
still flows to the other loads, and it resumes drawing on its own thermostat
(shed last, as the highest priority). Cycling it off/on would only flicker the
relay for no benefit.
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
    DIVERT_ENGAGE_DWELL_S,
    DIVERT_SHED_DWELL_S,
    DIVERT_SHED_MARGIN,
    EVENT_RUN_ENDED,
    EVENT_RUN_STARTED,
    MANUAL_OVERRIDE_GRACE_S,
)
from .coordinator import LoadSchedulerCoordinator
from .divert import DivertCandidate, decide_divert
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
        return not self._override_active(sid)

    @callback
    def _update_divert(self) -> None:
        """Fill/drain the diverted set as live export surplus swings.

        With a predicted end-of-interval net sensor configured, engage and shed
        decisions are driven off that projection (load-aware — see
        :func:`divert.decide_divert`); otherwise fall back to a reactive deadband
        on the live accumulated net.
        """
        if not self._net_entity:
            return
        net = _as_float(self._hass.states.get(self._net_entity))
        if net is None:
            return

        sell_ok = True
        if self._live_sell_entity:
            sell = _as_float(self._hass.states.get(self._live_sell_entity))
            sell_ok = sell is not None and sell < self._sell_threshold

        # Drop any diverted loads that are no longer eligible (disabled, manual
        # override). A load that is on but idle (e.g. a full tank) is deliberately
        # left powered: it draws nothing, the live export still flows to the other
        # loads, and it resumes drawing on its own thermostat — switching it off
        # and on would just flicker the relay for no gain.
        self._diverted = {
            sid
            for sid in self._diverted
            if self._eligible_for_divert(sid, self._coordinator.load_config(sid))
        }

        now = dt_util.utcnow()
        if self._predicted_net_entity:
            self._update_divert_predicted(now, sell_ok)
        else:
            self._update_divert_reactive(now, net, sell_ok)

    def _update_divert_predicted(self, now: datetime, sell_ok: bool) -> None:
        """Engage/shed off the predicted interval-close net (load-aware)."""
        predicted_net = _as_float(self._hass.states.get(self._predicted_net_entity))
        if predicted_net is None:
            return

        elapsed = (
            None
            if self._last_divert_change is None
            else (now - self._last_divert_change).total_seconds()
        )
        can_engage = elapsed is None or elapsed >= DIVERT_ENGAGE_DWELL_S
        can_shed = elapsed is None or elapsed >= DIVERT_SHED_DWELL_S

        # Energy a not-yet-running load would draw over the rest of the metering
        # interval — what decides whether it "fits" the projected export. 15
        # divides every real UTC offset, so the boundary is correct in any tz.
        minutes_left = 15 - (now.minute % 15) - now.second / 60.0

        candidates: list[DivertCandidate] = []
        for sid in self._coordinator.config_entry.subentries:
            if sid in self._diverted:
                continue
            cfg = self._coordinator.load_config(sid)
            if not self._eligible_for_divert(sid, cfg):
                continue
            candidates.append(
                DivertCandidate(
                    sid=sid,
                    priority=cfg.priority,
                    projected_energy=(cfg.draw_kw or 0.0) * minutes_left / 60.0,
                )
            )
        diverted = [(sid, self._coordinator.load_config(sid).priority) for sid in self._diverted]

        decision = decide_divert(
            predicted_net=predicted_net,
            diverted=diverted,
            candidates=candidates,
            engage_buffer=self._net_export_threshold,
            shed_margin=DIVERT_SHED_MARGIN,
            sell_ok=sell_ok,
            can_engage=can_engage,
            can_shed=can_shed,
        )
        if decision.add is not None:
            self._diverted.add(decision.add)
            self._last_divert_change = now
        elif decision.remove is not None:
            self._diverted.discard(decision.remove)
            self._last_divert_change = now

    def _update_divert_reactive(self, now: datetime, net: float, sell_ok: bool) -> None:
        """Fallback with no predicted-net sensor: react to the live accumulated net."""
        if (
            self._last_divert_change is not None
            and (now - self._last_divert_change).total_seconds() < DIVERT_ENGAGE_DWELL_S
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
