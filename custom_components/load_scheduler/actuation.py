"""Drive each load's controlled entity, combining the plan with live overrides.

The desired on/off for a load at any instant is resolved by precedence:

1. **Manual override** — if the controlled entity was changed out of band, the
   integration backs off (returns "don't touch"). A manual **off** stops the
   current run: it cancels any active boost and suppresses the rest of the active
   period (not just a short grace), so a load you switch off does not pop back
   on. A manual **on** is left alone and its run is credited as delivered.
2. **Low-temp safety floor** — for a load with a temperature sensor configured,
   force heat when it drops below the threshold (Finland winters), regardless of
   price — and regardless of whether there is a plan at all: a dead price feed
   must not leave a cold room unheated. Released with hysteresis
   (``TEMP_FLOOR_HYSTERESIS``) so a sensor hovering at the threshold doesn't
   flip the relay on every sample.
3. **Scheduled plan** — the coordinator's periods (cheap/solar/min-service/boost).
4. **Real-time solar divert** — when there's live export surplus and selling
   isn't worth it, surplus is dispatched to the highest-priority eligible loads.
5. Otherwise **off**.

**Coexist (top-up) loads** never have step 5 force them off: the integration
only switches such a load *off* if it was the one that switched it *on*. This
lets it add cheap/green energy on top of an external control (e.g. floor-heating
comfort automations) without ever fighting it — external on-runs are observed
and credited, never cut short. That ownership is persisted with the rest of the
runtime (``LoadRuntime.driven``), so a run in progress across a restart stays
ours and still gets switched off at the end of its period.

An *off* command keeps that ownership until the off is actually observed: a
``turn_off`` that fails (``blocking=False`` swallows the error) must still be
retried, and a coexist load disowned at command time never would be.

Only a real ``on`` ↔ ``off`` transition is somebody driving the load. An entity
coming back from ``unavailable``/``unknown`` (every Z2M/MQTT switch after an HA
restart) is reality being reported, not a command: it never raises an override
or cancels a boost, and it only ends our ownership if it comes back ``off``.
While the entity is unavailable nothing is sent to it at all.

This also gives restart catch-up: ``async_start`` reconciles once on setup.

Anti-thrash: divert decisions hold for a minimum dwell time; the divert set is
filled/drained one load at a time as live net energy swings, mirroring (and
superseding) the per-load ``Solar - Auto …`` automations but coordinated by
priority. A diverted load that is on but idle (its element satisfied, e.g. a full
tank) is left powered, not switched off: it draws nothing, so the live export
still flows to the other loads, and it resumes drawing on its own thermostat
(shed last, as the highest priority). Cycling it off/on would only flicker the
relay for no benefit. Divert also honours each load's ``min_run_minutes`` (a
diverted load isn't shed before it has run that long) and ``min_off_minutes``
(a load that just stopped isn't re-engaged), so a short dwell can't
short-cycle a compressor.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from homeassistant.const import SERVICE_TURN_OFF, SERVICE_TURN_ON
from homeassistant.core import Context, Event, HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_point_in_time,
    async_track_state_change_event,
)
from homeassistant.util import dt as dt_util

from .competing import SOURCE_SCRIPTED, SOURCE_UNKNOWN, SOURCE_USER, ForeignEvent
from .const import (
    COMMAND_PENDING_S,
    COMMAND_RESEND_S,
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
    TEMP_FLOOR_HYSTERESIS,
)
from .coordinator import LoadSchedulerCoordinator
from .divert import DivertCandidate, decide_divert
from .models import LoadConfig

_LOGGER = logging.getLogger(__name__)


# The only controlled-entity states that say anything about the load. Anything
# else (unavailable, unknown, a missing entity) is "we don't know".
_KNOWN = ("on", "off")


def _as_float(state) -> float | None:
    if state is None:
        return None
    try:
        return float(state.state)
    except (TypeError, ValueError):
        return None


def _change_source(context: Context | None) -> str:
    """Classify who made a change from its event context.

    A ``user_id`` means a person acted in the UI; a bare ``parent_id`` means the
    change was spawned by something else — an automation, script or scene.
    *Which* one cannot be recovered from an integration: there is no public
    context → entity lookup, so the repair issue names the pattern and leaves
    finding the culprit to the automation traces.
    """
    if context is None:
        return SOURCE_UNKNOWN
    if context.user_id:
        return SOURCE_USER
    if context.parent_id:
        return SOURCE_SCRIPTED
    return SOURCE_UNKNOWN


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
        # Whether the sensor governing divert was readable last time; only used
        # to log the drop to unavailable once rather than on every sample.
        self._divert_sensor_ok: bool = True
        self._override_until: dict[str, datetime] = {}
        # Commands we sent whose effect on the controlled entity we haven't seen
        # yet: subentry_id → (commanded state, when). See `_claim_pending`.
        self._pending_command: dict[str, tuple[bool, datetime]] = {}
        # When each controlled entity's current on-run started (UTC). Only the
        # unowned-run repair needs it; a run that predates this actuator falls
        # back to the state machine's stamp (see `_run_on_since`).
        self._on_since: dict[str, datetime] = {}
        # When each controlled entity last went off (UTC) — divert's min-off gate.
        self._off_since: dict[str, datetime] = {}
        # Loads whose low-temp safety floor is currently engaged: the hysteresis
        # latch (engage below temp_min, release at temp_min + hysteresis).
        self._floor_active: set[str] = set()
        # Explicit stop requests (boost cancel) whose off hasn't been observed
        # yet: subentry_id → when requested. While set, the load is held off and
        # the off retried, so a lost turn_off can't leave a cancelled run going.
        self._stop_requested: dict[str, datetime] = {}
        # Re-entrancy guard: reconcile awaits service calls, so two ticks could
        # otherwise interleave and both act on the same stale state.
        self._reconciling = False
        self._reconcile_again = False
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
        self._sync_driven_with_reality()
        self._update_divert()
        await self._reconcile()
        self._schedule_next_boundary()

    def _sync_driven_with_reality(self) -> None:
        """Drop ownership claims restored from the Store that reality contradicts.

        The persisted flag means "the run going on right now is one we started",
        so it only stays true while the load is actually on: if it is off, that
        run has ended (someone switched it off, or HA was down past its end) and
        the claim is void. The reverse — a run started externally while HA was
        down — is indistinguishable from our own surviving run, so ownership is
        kept there; it self-corrects on the first foreign change to the entity.

        Only an explicit ``off`` voids the claim. A Z2M/MQTT switch restores as
        ``unavailable`` after every restart, and dropping ownership on that would
        strand every coexist run in progress (on, and nothing ever switching it
        off) — the very failure persisted ownership exists to prevent. If it
        later reports ``off``, the recovery path clears the claim then.
        """
        for sid in self._coordinator.config_entry.subentries:
            if not self._is_driven(sid):
                continue
            entity_id = self._coordinator.load_config(sid).controlled_entity
            state = self._hass.states.get(entity_id) if entity_id else None
            if state is not None and state.state == "off":
                self._coordinator.note_driven(sid, False)

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
            # Not the feedback entity: nothing here reads it (an idle diverted
            # load is deliberately left powered), and a power sensor sampling
            # every few seconds would spawn a reconcile per sample.
            for entity in (cfg.controlled_entity, cfg.temp_entity):
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

    def _is_driven(self, sid: str) -> bool:
        """Whether the integration currently holds this load's run ON."""
        rt = self._coordinator.runtime.get(sid)
        return rt is not None and rt.driven

    def _claim_pending(self, sid: str, is_on: bool, now: datetime) -> bool:
        """Whether this change is the confirmation of our own last command.

        Attribution is by *pendingness*, not by a clock window: a command stays
        pending until the controlled entity actually moves, and the first move
        matching what we commanded is our own echo however late it lands. The
        old fixed 5-second window mis-read a Shelly/Zigbee/cloud relay (or a
        busy event loop) confirming a second too late as a **manual on**, which
        set an override *and dropped ownership* — so a run we had started became
        unownable and nothing would ever switch it off again.

        The entry is dropped as soon as the entity moves, either way, so a later
        genuine manual flip to the same state is correctly read as foreign. The
        expiry only bounds a command that is never confirmed at all.
        """
        pending = self._pending_command.pop(sid, None)
        if pending is None:
            return False
        commanded, sent = pending
        if (now - sent).total_seconds() > COMMAND_PENDING_S:
            return False
        # Moved the other way: our command was overridden or never landed. It is
        # no longer pending either way (already popped), and this change is not
        # ours.
        return commanded == is_on

    def _note_controlled_change(self, entity_id, event: Event) -> None:
        """Detect a manual (foreign) change to a controlled entity and react."""
        now = dt_util.utcnow()
        for sid in self._coordinator.config_entry.subentries:
            cfg = self._coordinator.load_config(sid)
            if cfg.controlled_entity != entity_id:
                continue
            new = event.data.get("new_state")
            old = event.data.get("old_state")
            # A drop to unavailable/unknown is nobody driving the load: it must
            # not be read as a manual *off* (that would suppress the rest of the
            # period and disown a run we started, for a flaky relay nobody
            # touched). Nothing is known until it reports again.
            if new is None or new.state not in _KNOWN:
                return
            old_state = old.state if old is not None else None
            # Attribute-only change: the switch didn't move.
            if old_state == new.state:
                return
            is_on = new.state == "on"
            if old_state not in _KNOWN:
                # Recovery (restart, relay back online): an off here is not an
                # observed *stop* — it may have been off for hours — so it must
                # not stamp `_off_since`, or min_off / separation would hold the
                # first run back for nothing.
                if is_on:
                    self._note_on_since(sid, True, now)
                else:
                    self._on_since.pop(sid, None)
                    self._settle_stop(sid, now)
                self._note_recovery(sid, is_on, now)
                return
            self._note_on_since(sid, is_on, now)
            if not is_on:
                self._settle_stop(sid, now)
            if self._claim_pending(sid, is_on, now):
                if not is_on:
                    # Our own off, confirmed: only now is the run over. Ownership
                    # was kept through the command so a lost turn_off could still
                    # be retried (see `_apply`).
                    self._coordinator.note_driven(sid, False)
                return
            plan = (self._coordinator.data or {}).get(sid)
            active = plan.active_period(now) if plan else None
            # Log it for competing-controller detection. Only genuine on↔off
            # flips get this far — restart and connectivity churn ("unknown" →
            # "off") took the recovery path above and would drown out the real
            # pattern.
            self._coordinator.note_foreign_change(
                sid,
                ForeignEvent(
                    when=now,
                    turned_on=is_on,
                    in_active_period=active is not None,
                    source=_change_source(event.context),
                ),
            )
            grace_until = now + timedelta(seconds=MANUAL_OVERRIDE_GRACE_S)
            if is_on:
                # Manual ON: don't immediately undo it; the run is credited via
                # the measured delivered sensor. It's not a run we started.
                self._override_until[sid] = grace_until
                self._coordinator.note_driven(sid, False)
                # A person wants it on: that supersedes an earlier stop request.
                self._stop_requested.pop(sid, None)
            else:
                # Manual OFF: stop the current run. Suppress the rest of the
                # active period (not just the short grace) and cancel any boost,
                # so the load does not pop back on.
                self._override_until[sid] = max(active.end, grace_until) if active else grace_until
                self._coordinator.note_driven(sid, False)
                rt = self._coordinator.runtime.get(sid)
                if rt is not None and rt.boost_until and now < rt.boost_until:
                    self._coordinator.config_entry.async_create_task(
                        self._hass, self._coordinator.async_cancel_boost(sid), "ls_cancel_boost"
                    )
            # Wake up when the back-off ends so precedence is re-evaluated then,
            # not at whatever tick happens to come next.
            self._schedule_next_boundary()
            _LOGGER.debug(
                "Manual override (%s) on %s; backing off", "on" if is_on else "off", entity_id
            )
            return

    def _settle_stop(self, sid: str, now: datetime) -> None:
        """An off was observed: a pending stop request is done.

        Only now does the user's stop turn into the normal back-off, measured
        from when the load actually went off.
        """
        if self._stop_requested.pop(sid, None) is None:
            return
        grace = now + timedelta(seconds=MANUAL_OVERRIDE_GRACE_S)
        current = self._override_until.get(sid)
        self._override_until[sid] = max(current, grace) if current else grace
        self._schedule_next_boundary()

    def _note_recovery(self, sid: str, is_on: bool, now: datetime) -> None:
        """The entity reported again after being unavailable/unknown (or appeared).

        That is a Z2M/MQTT switch restoring after a restart or a relay
        reconnecting — reality being reported, not anybody driving the load — so
        it never raises an override or cancels a boost. Any command still pending
        is settled by it either way. Coming back *off* means whatever run we held
        is over; coming back *on* keeps ownership, so a coexist run we started
        before a restart is still ours to switch off.
        """
        self._claim_pending(sid, is_on, now)
        if not is_on:
            self._coordinator.note_driven(sid, False)

    # ── run ownership / duration facts ───────────────────────────────────────

    @callback
    def _note_on_since(self, sid: str, is_on: bool, now: datetime) -> None:
        """Remember when the current on-run (or off-gap) started, whoever caused it.

        ``setdefault``: an on → unavailable → on flap is still the same run, so
        min-run is measured from its real start.
        """
        if is_on:
            self._on_since.setdefault(sid, now)
            self._off_since.pop(sid, None)
        else:
            self._on_since.pop(sid, None)
            self._off_since.setdefault(sid, now)

    def _run_on_since(self, sid: str, entity_id: str) -> datetime | None:
        """When the load's current on-run started (UTC), or None if it's off."""
        state = self._hass.states.get(entity_id)
        if state is None or state.state != "on":
            return None
        # A run that was already going when this actuator started has no tracked
        # stamp; fall back to the state machine's. HA re-stamps `last_changed`
        # on restart, so the elapsed time (and any threshold measured from it)
        # restarts with HA — the tracked stamp takes over from the next real
        # transition, and a run stuck on will trip the threshold on the next day
        # anyway.
        return self._on_since.get(sid) or state.last_changed

    def _run_off_since(self, sid: str, entity_id: str) -> datetime | None:
        """When the load last went off (UTC), or None if it isn't off."""
        state = self._hass.states.get(entity_id)
        if state is None or state.state != "off":
            return None
        # Same fallback as `_run_on_since`: after a restart the gap is measured
        # from the restart, which errs on the side of protecting the compressor.
        return self._off_since.get(sid) or state.last_changed

    @callback
    def unowned_on_since(self, sid: str) -> datetime | None:
        """When the load's current *unowned* on-run started, else None.

        Unowned = the controlled entity is on but the integration did not start
        it, so nothing the integration does will ever switch it off. The
        coordinator pairs this with the plan to decide whether that is a run in
        progress or a load nobody is going to stop (see
        ``_update_unowned_run_issue``).
        """
        cfg = self._coordinator.load_config(sid)
        if not cfg.controlled_entity or self._is_driven(sid):
            return None
        return self._run_on_since(sid, cfg.controlled_entity)

    # ── real-time divert ─────────────────────────────────────────────────────

    def _eligible_for_divert(self, sid: str, cfg: LoadConfig) -> bool:
        if cfg.is_informational or not cfg.controlled_entity or not cfg.allow_solar:
            return False
        if not self._coordinator.runtime_for(sid).enabled:
            return False
        return not self._override_active(sid)

    def _controlled_is_on(self, cfg: LoadConfig) -> bool:
        """Is the load's controlled entity already on (plan, floor, coexist run)?

        Such a load's draw is already in the live/predicted net, so offering it
        as a divert candidate would count that draw twice — and, with its full
        projected draw, a big on-by-plan load at high priority would block every
        lower-priority load from engaging.
        """
        state = self._hass.states.get(cfg.controlled_entity)
        return state is not None and state.state == "on"

    def _divert_can_start(self, sid: str, cfg: LoadConfig, now: datetime) -> bool:
        """Min-off: a load that stopped less than ``min_off_minutes`` ago waits.

        The divert dwell is hub-wide and short; without this a compressor load
        shed a minute ago would be re-engaged on the next export blip.
        """
        if not cfg.min_off_minutes:
            return True
        since = self._run_off_since(sid, cfg.controlled_entity)
        return since is None or (now - since).total_seconds() >= cfg.min_off_minutes * 60

    def _divert_can_shed(self, sid: str, now: datetime) -> bool:
        """Min-run: a diverted load isn't shed before it has run ``min_run_minutes``.

        A diverted load that isn't on yet (command in flight) has no run to
        protect, so it can always be dropped.
        """
        cfg = self._coordinator.load_config(sid)
        if not cfg.min_run_minutes or not cfg.controlled_entity:
            return True
        since = self._run_on_since(sid, cfg.controlled_entity)
        return since is None or (now - since).total_seconds() >= cfg.min_run_minutes * 60

    def _divert_candidates(self, now: datetime) -> list[str]:
        """Loads divert may engage: eligible, not already on, past their min-off."""
        out = []
        for sid in self._coordinator.config_entry.subentries:
            if sid in self._diverted:
                continue
            cfg = self._coordinator.load_config(sid)
            if (
                self._eligible_for_divert(sid, cfg)
                and not self._controlled_is_on(cfg)
                and self._divert_can_start(sid, cfg, now)
            ):
                out.append(sid)
        return out

    @callback
    def _update_divert(self) -> None:
        """Fill/drain the diverted set as live export surplus swings.

        With a predicted end-of-interval net sensor configured, engage and shed
        decisions are driven off that projection alone (load-aware — see
        :func:`divert.decide_divert`); otherwise fall back to a reactive deadband
        on the live accumulated net.
        """
        # Drop any diverted loads that are no longer eligible (disabled, manual
        # override) *first*, whatever the sensors say: returning early on an
        # unavailable net sensor used to leave a load the user just disabled
        # diverted — and on. A load that is on but idle (e.g. a full tank) is
        # deliberately left powered: it draws nothing, the live export still
        # flows to the other loads, and it resumes drawing on its own thermostat
        # — switching it off and on would just flicker the relay for no gain.
        self._diverted = {
            sid
            for sid in self._diverted
            if sid in self._coordinator.config_entry.subentries
            and self._eligible_for_divert(sid, self._coordinator.load_config(sid))
        }

        governing = self._predicted_net_entity or self._net_entity
        if not governing:
            return
        now = dt_util.utcnow()
        value = _as_float(self._hass.states.get(governing))
        if value is None:
            # No reading means no evidence of a surplus: stop holding loads on
            # for divert and let the plan/precedence decide (coexist ownership
            # still applies — `_apply` never cuts a run we didn't start). Loads
            # still inside their min-run are dropped once they've served it.
            if self._divert_sensor_ok:
                _LOGGER.debug("Divert sensor %s unavailable; releasing diverted loads", governing)
            self._divert_sensor_ok = False
            released = {sid for sid in self._diverted if self._divert_can_shed(sid, now)}
            if released:
                self._diverted -= released
                self._last_divert_change = now
            return
        self._divert_sensor_ok = True

        sell_ok = True
        if self._live_sell_entity:
            sell = _as_float(self._hass.states.get(self._live_sell_entity))
            sell_ok = sell is not None and sell < self._sell_threshold

        if self._predicted_net_entity:
            self._update_divert_predicted(now, value, sell_ok)
        else:
            self._update_divert_reactive(now, value, sell_ok)

    def _update_divert_predicted(self, now: datetime, predicted_net: float, sell_ok: bool) -> None:
        """Engage/shed off the predicted interval-close net (load-aware)."""
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

        candidates = [
            DivertCandidate(
                sid=sid,
                priority=(cfg := self._coordinator.load_config(sid)).priority,
                projected_energy=(cfg.draw_kw or 0.0) * minutes_left / 60.0,
            )
            for sid in self._divert_candidates(now)
        ]
        # Only loads past their min-run are offered for shedding; a protected
        # one simply isn't a shed option yet (the next-lowest priority goes).
        diverted = [
            (sid, self._coordinator.load_config(sid).priority)
            for sid in self._diverted
            if self._divert_can_shed(sid, now)
        ]

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
            candidates = self._divert_candidates(now)
            if candidates:
                self._diverted.add(max(candidates, key=priority))
                self._last_divert_change = now
        elif importing or not sell_ok:
            sheddable = [sid for sid in self._diverted if self._divert_can_shed(sid, now)]
            if sheddable:
                self._diverted.discard(min(sheddable, key=priority))
                self._last_divert_change = now

    # ── desired state + actuation ────────────────────────────────────────────

    def _override_active(self, sid: str) -> bool:
        until = self._override_until.get(sid)
        return until is not None and dt_util.utcnow() < until

    async def async_manual_stop(self, sid: str) -> None:
        """Stop a load now on an explicit user request (e.g. cancelling a boost).

        Sends the off through the normal command path — so its echo is
        attributed to us and ownership is only dropped once the off is observed
        — and sets the same grace as a manual off, so the real-time divert or
        the plan don't immediately re-grab a load the user just stopped (notably
        on a solar-exporting summer night). Backing off *without* switching off,
        as this used to, left a non-coexist load running through the grace and a
        coexist one running forever.

        The grace alone would also stop the off being *retried* (an override
        means "don't touch"), so a lost turn_off would leave the cancelled run
        going. A stop request therefore outranks the override until the off is
        observed (or the request expires with `COMMAND_PENDING_S`): the load is
        held off and the off re-sent at the normal resend pace; the grace then
        restarts from the observed off.

        A coexist run somebody else started is still left alone (`_apply`'s
        ownership guard, and no stop request for it). After the grace, normal
        scheduling/divert resumes.
        """
        cfg = self._coordinator.load_config(sid)
        self._diverted.discard(sid)
        now = dt_util.utcnow()
        if not cfg.is_informational and cfg.controlled_entity:
            state = self._hass.states.get(cfg.controlled_entity)
            if state is not None and state.state == "off":
                self._coordinator.note_driven(sid, False)  # nothing running to own
            else:
                if self._is_driven(sid) or not cfg.coexist:
                    self._stop_requested[sid] = now
                await self._apply(sid, cfg, False)
        self._override_until[sid] = now + timedelta(seconds=MANUAL_OVERRIDE_GRACE_S)
        self._schedule_next_boundary()

    def _stop_holds_off(self, sid: str, cfg: LoadConfig, now: datetime) -> bool:
        """Whether an unconfirmed stop request still holds this load off."""
        requested = self._stop_requested.get(sid)
        if requested is None:
            return False
        if (now - requested).total_seconds() > COMMAND_PENDING_S or (
            cfg.coexist and not self._is_driven(sid)
        ):
            # Expired (a relay that never confirms mustn't be held forever), or
            # the run is no longer ours to switch off.
            self._stop_requested.pop(sid, None)
            return False
        return True

    def _temp_floor(self, sid: str, cfg: LoadConfig) -> bool:
        """Whether the low-temp safety floor holds the load on (with hysteresis).

        An unreadable sensor releases the floor, as before: forcing heat
        regardless of price needs evidence the room is cold.
        """
        temp = _as_float(self._hass.states.get(cfg.temp_entity)) if cfg.temp_entity else None
        if temp is not None and (
            temp < cfg.temp_min
            or (sid in self._floor_active and temp < cfg.temp_min + TEMP_FLOOR_HYSTERESIS)
        ):
            self._floor_active.add(sid)
            return True
        self._floor_active.discard(sid)
        return False

    def _desired_on(self, sid: str, cfg: LoadConfig) -> bool | None:
        """Resolve the desired controlled-entity state, or None to not touch."""
        if cfg.is_informational or not cfg.controlled_entity:
            return None
        # An explicit stop outranks its own back-off until the off is seen.
        if self._stop_holds_off(sid, cfg, dt_util.utcnow()):
            return False
        if self._override_active(sid):
            return None
        # Low-temp safety floor (overrides everything below) — checked before
        # the plan, so a dead price feed (no plan, or a plan error) and a cold
        # room still means heat.
        if self._temp_floor(sid, cfg):
            return True
        plan = (self._coordinator.data or {}).get(sid)
        if plan is None or plan.error:
            # No usable plan (a failsafe run is a plan without an error) and the
            # floor isn't holding it: nothing gives us a reason to keep a run we
            # own going, so switch it off (retried until observed — ownership
            # clears then). Returning None left a released floor's heat on for as
            # long as the price feed stayed dead — and a restart mid-run lost any
            # in-memory memory of the floor, so this keys off persisted ownership
            # alone. An unreadable temp sensor releases the floor the same way.
            # A run we don't own is still left alone.
            return False if self._is_driven(sid) else None
        if plan.active_period(dt_util.utcnow()) is not None:
            return True
        return sid in self._diverted and self._coordinator.runtime_for(sid).enabled

    async def _reconcile(self) -> None:
        if self._reconciling:
            # A pass is mid-await; have it run once more when done rather than
            # interleaving a second pass over the same (stale) states.
            self._reconcile_again = True
            return
        self._reconciling = True
        try:
            again = True
            while again:
                self._reconcile_again = False
                for sid in list(self._coordinator.config_entry.subentries):
                    if sid not in self._coordinator.config_entry.subentries:
                        continue  # removed while an earlier load was awaited
                    cfg = self._coordinator.load_config(sid)
                    desired = self._desired_on(sid, cfg)
                    if desired is None:
                        continue
                    await self._apply(sid, cfg, desired)
                again = self._reconcile_again
        finally:
            self._reconciling = False

    async def _apply(self, sid: str, cfg: LoadConfig, desired_on: bool) -> None:
        entity_id = cfg.controlled_entity
        state = self._hass.states.get(entity_id)
        if state is None or state.state not in _KNOWN:
            # An unavailable relay (or a Z2M switch still restoring after a
            # restart) is not "off". Treating it as off re-sent turn_on — and
            # re-fired RUN_STARTED — on every watched state change, for a device
            # that can't hear it. Act once it reports again.
            return
        is_on = state.state == "on"
        if desired_on == is_on:
            return
        if not desired_on and cfg.coexist and not self._is_driven(sid):
            # Coexist (top-up): never switch off a run we didn't start.
            return
        now = dt_util.utcnow()
        pending = self._pending_command.get(sid)
        if (
            pending is not None
            and pending[0] == desired_on
            and (now - pending[1]).total_seconds() < COMMAND_RESEND_S
        ):
            return  # the same command is still in flight; give the relay time
        self._pending_command[sid] = (desired_on, now)
        if desired_on:
            self._coordinator.note_driven(sid, True)
        # An off keeps ownership until the off is observed (the echo in
        # `_note_controlled_change`): blocking=False swallows a failed
        # turn_off, and a coexist load disowned at command time would then
        # never be retried — it'd stay on forever.
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
        if sid in self._stop_requested:
            self._schedule_next_boundary()  # wake for the retry, not the next tick

    # ── diagnostics ──────────────────────────────────────────────────────────

    @callback
    def diagnostics(self, sid: str) -> dict:
        """The actuator's live view of one load, for the diagnostics dump."""
        pending = self._pending_command.get(sid)
        on_since = self._on_since.get(sid)
        off_since = self._off_since.get(sid)
        until = self._override_until.get(sid)
        return {
            "diverted": sid in self._diverted,
            "override_until": until.isoformat() if until else None,
            "override_active": self._override_active(sid),
            "pending_command": (
                None if pending is None else {"on": pending[0], "sent": pending[1].isoformat()}
            ),
            "on_since": on_since.isoformat() if on_since else None,
            "off_since": off_since.isoformat() if off_since else None,
            "floor_active": sid in self._floor_active,
            "stop_requested": (
                self._stop_requested[sid].isoformat() if sid in self._stop_requested else None
            ),
        }

    # ── boundary scheduling ──────────────────────────────────────────────────

    def _boundaries_after(self, now: datetime) -> list[datetime]:
        """Every instant the desired state can change without an event.

        Period edges, plus each manual-override expiry: when a back-off ends the
        plan/divert may want the load again (or, for a run we own, off), and
        waiting for the next coordinator tick would add up to five minutes.
        Expiry only re-evaluates precedence — an external coexist run is never
        reclaimed by it (`_apply` won't switch off what we didn't start).
        """
        bounds = [
            t
            for plan in (self._coordinator.data or {}).values()
            for p in plan.periods
            for t in (p.start, p.end)
            if t > now
        ]
        bounds.extend(t for t in self._override_until.values() if t > now)
        # An unconfirmed stop: wake to retry the off once the resend interval
        # has passed, and when the request expires.
        for sid, requested in self._stop_requested.items():
            if (pending := self._pending_command.get(sid)) is not None:
                bounds.append(pending[1] + timedelta(seconds=COMMAND_RESEND_S))
            bounds.append(requested + timedelta(seconds=COMMAND_PENDING_S))
        return [t for t in bounds if t > now]

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
