"""Pure decision logic for the real-time solar divert (no Home Assistant imports).

The divert controller dispatches a live export surplus to flexible loads. With
**15-minute net metering** what the meter bills is ``import − export`` summed
over each interval, so the goal is simple: by the time the interval closes, the
net should be in *export* (or at worst zero), never import.

This module reduces that goal to one pure function, :func:`decide_divert`, so it
can be unit-tested without HA (like ``engine``/``rationale``). It works off the
**predicted** end-of-interval net (accumulated-so-far + live power extrapolated
over the minutes left), not the after-the-fact accumulated net, so it acts
*before* an import happens rather than mopping it up afterwards.

Two ideas keep it from leaking a trickle of import every interval:

* **Load-aware engagement.** A candidate is only switched on if its *own*
  projected consumption for the rest of the interval still leaves the interval
  closing in export. Engaging by priority alone — the old behaviour — would turn
  on a load too big for the remaining surplus and tip the interval into import.
* **Hysteresis.** Engaging needs the projection to stay below ``-engage_buffer``;
  shedding triggers once it reaches ``+shed_margin``. The gap between them is a
  hold band, so the set doesn't flip-flop around break-even (which would also
  double relay cycling).

At most one load is added or removed per call (mirroring the actuator's
one-at-a-time, dwell-gated behaviour). Priority is preserved: the
highest-priority eligible load is engaged first and shed last.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DivertCandidate:
    """An eligible, not-yet-diverted load the controller may switch on."""

    sid: str
    priority: int
    # kWh this load would draw if it ran for the rest of the metering interval.
    projected_energy: float


@dataclass(frozen=True)
class DivertDecision:
    """At most one action this tick: add ``sid``, remove ``sid``, or neither."""

    add: str | None = None
    remove: str | None = None


def decide_divert(
    *,
    predicted_net: float,
    diverted: list[tuple[str, int]],
    candidates: list[DivertCandidate],
    engage_buffer: float,
    shed_margin: float,
    sell_ok: bool,
    can_engage: bool,
    can_shed: bool,
) -> DivertDecision:
    """Pick the next divert action from the predicted interval-close net.

    ``predicted_net`` and the buffers are kWh; ``predicted_net < 0`` means the
    interval is projected to close in export. ``diverted`` is the currently-on
    set as ``(sid, priority)``. ``sell_ok`` is False when the live sell price is
    high enough that exporting beats self-consuming. ``can_engage``/``can_shed``
    are the (asymmetric) anti-thrash dwell gates.
    """
    # Shed when selling is worth more than self-consuming, or when the interval
    # is projected to close in net import. Drop the lowest-priority load first.
    if (not sell_ok or predicted_net >= shed_margin) and diverted and can_shed:
        return DivertDecision(remove=min(diverted, key=lambda d: d[1])[0])
    # Otherwise add the highest-priority candidate whose own projected draw still
    # leaves the interval closing in export by at least ``engage_buffer``.
    if sell_ok and can_engage and candidates:
        best = max(candidates, key=lambda c: c.priority)
        if predicted_net + best.projected_energy <= -engage_buffer:
            return DivertDecision(add=best.sid)
    return DivertDecision()
