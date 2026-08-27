# Architecture

## Shape

- **Hub config entry** — the shared resources and coordination: price + solar
  sources, the `LoadSchedulerCoordinator`, the `LoadActuator`, the single shared
  `calendar`, persistence, repairs and the bundled card.
- **One config *subentry* per load** — its parameters and its own device +
  entities (`binary_sensor` running, merged `sensor` schedule, `number` target,
  `switch` enabled, `button` boost).

## Data flow

```
price entity ─┐
sell entity ──┤ price_source.normalize → UTC slots ─┐
solar entity ─┘ solar_source + baseline → excess ───┤
                                                     ▼
                              coordinator: per-load LoadParams
                                       │  (priority allocation of excess)
                                       ▼
                              engine.compute_plan (pure) → periods
                                       │
                  ┌────────────────────┼─────────────────────┐
                  ▼                    ▼                     ▼
            binary_sensor /       calendar (hub)        actuator
            schedule sensor                          (plan + live divert
                                                      + safety + override)
                                                            ▼
                                                     controlled entity
```

## Modules

| Module | Responsibility | HA? |
|---|---|---|
| `engine.py` | Pure scheduling: non-seq / seq(multi) / effective-cost / min-service / cap / min-run-off / merge | no |
| `price_source.py` | Normalize heterogeneous price entities → UTC slots (buy+sell) | no |
| `solar_source.py` | Parse PV forecasts (Solcast etc.) → per-slot energy | no |
| `windows.py` | DST-safe window + next-time resolution | no |
| `baseline.py` | Hour-of-day consumption profile from samples | no |
| `models.py` | Subentry config → `LoadConfig` → `LoadParams` | no |
| `rationale.py` | Pure decision facts (skip reason, cap-qualifying slots, solar coverage) for the diagnostic card's plain-English narration | no |
| `divert.py` | Pure real-time divert decision: predicted interval-close net, load-aware engage/shed, priority-preserving | no |
| `competing.py` | Pure competing-controller verdict: burst / same-local-time recurrence over a decaying 7-day log of foreign flips | no |
| `coordinator.py` | Read sources, allocate solar by priority, run engine per load, repairs, statistics baseline | yes |
| `actuation.py` | Resolve desired state (override → safety → plan → divert), drive controlled entities, restart catch-up | yes |
| `persistence.py` | `Store` for runtime (target/enabled/boost) | yes |
| `config_flow.py` | Hub flow + per-load subentry wizard (+ reconfigure) | yes |
| `binary_sensor/sensor/number/switch/button/calendar` | Entities | yes |
| `diagnostics.py`, `repairs.py`(strings) | Support | yes |
| `frontend/` | Two bundled Lovelace cards (compact + diagnostic) + `ha-form` editors | — |

## Key contracts

- **The engine runs in UTC** and never calls `now()`. `price_source` normalizes
  every slot to UTC so all engine arithmetic is DST-free; `windows` anchors to
  local wall-clock and the coordinator passes an explicit `now`.
- **Durations are minutes**; the final run is trimmed to the exact minute.
- **Candidate slots are clipped to the window.** A slot straddling `now` or the
  deadline is admitted (so a run already under way stays eligible) but only its
  in-window part is budgeted and scheduled — otherwise the plan spends target
  minutes on runtime that has already elapsed, or runs past the deadline. Its
  `excess_kwh` is scaled by the same fraction, which leaves the solar blend in
  `effective_cost` unchanged.
- **Contiguous runs are scanned in minutes, not slot counts.** The forecast mixes
  resolutions (quarter-hourly day-ahead, hourly predictor slots beyond it), so
  `_best_block` walks forward accumulating real minutes and weights each slot's
  cost by the minutes taken from it. A slot-count block size or an unweighted
  cost sum compares an hour against a quarter-hour as if they were equal.
- **`min_run_minutes` shapes selection, not clean-up.** With it set, a
  non-sequential load buys whole runs (`_plan_runs`) instead of scattered slots:
  one `min_run` at a time, the last absorbing the remainder so the target is
  still exact. Picking the cheapest slots and *then* deleting sub-`min_run`
  fragments threw those minutes away and left the load short.
- **Runtime state** (target / enabled / boost) lives in the `Store` (source of
  truth, in backups); entities are views/setters over it.
- **Actuation precedence** (per tick): manual override → low-temp safety floor →
  scheduled plan (incl. boost / min-service) → real-time divert → off. A manual
  **off** stops the current run (cancels any boost, suppresses the rest of the
  active period); a manual **on** is left alone and credited via the measured
  delivered sensor. Boost is a toggle (press again to cancel).
- **Coexist (top-up) loads** (`coexist`): the integration only ever switches the
  load *on*, and only switches *off* a run it started itself — it never turns off
  an externally-started run (a comfort automation, a manual flip). Lets it add
  cheap/green energy on top of existing control without fighting it. The
  `running` binary sensor reflects the **actual controlled-entity state**, not the
  plan, so an override shows the truth.
- **Solar excess** = forecast PV − baseline; allocated to loads highest-priority
  first against a shared residual so no kWh is double-counted.
- **Real-time divert** (pure decision in `divert.py`). With a **predicted
  end-of-interval net** sensor configured (accumulated-so-far + live power
  extrapolated over the minutes left), it drives both engage and shed off that
  projection so it acts *before* an import happens — the right signal for 15-min
  net metering. Engagement is **load-aware**: the highest-priority eligible load
  is added only if its own projected draw for the rest of the interval still
  leaves the interval closing in export (by `net_export_threshold`), so it never
  turns on a load too big for the remaining surplus; shed drops the
  lowest-priority load once the interval is projected to import. Dwell is
  **asymmetric** — slow to engage (`DIVERT_ENGAGE_DWELL_S`), quick to shed
  (`DIVERT_SHED_DWELL_S`) — and the gap between the engage/shed thresholds is a
  hysteresis hold band; relay protection comes from that predictive accuracy, not
  a long dwell. Without a predicted sensor it falls back to a reactive deadband on
  the accumulated current-interval net (negative = export): add when exporting
  past the threshold and the live sell price is below its gate, shed when
  importing. An explicit stop (manual off / boost
  cancel) backs off so divert can't immediately re-grab the load. A diverted load
  that is on but idle (element satisfied, e.g. a full tank) is **left powered**,
  not switched off: it draws nothing, so the live export still flows to the other
  loads, and it resumes drawing on its own thermostat (shed last, as the highest
  priority). Cycling it off/on would only flicker the relay for no gain.
- **Delivered today** (dynamic remaining) — subtracted from the target and the
  min-service floor. With no `delivered_entity` the coordinator measures it from
  the recorder: the feedback element's (or controlled entity's) on-time since
  local midnight (`async_refresh_delivered`, throttled ~2 min). It counts heating
  regardless of who started it and resets daily, so no external sensor is needed.
  Because that reset is the accounting boundary, `LoadParams.min_service_by`
  (the next local midnight) holds the **min-service floor** to the day it
  protects: guaranteed minutes prefer slots finishing before it, falling back to
  the rest of the window only rather than going unmet.
- **Schedule rationale** — the per-load `LoadPlan` also captures the planning
  math the coordinator would otherwise discard (`delivered_minutes`,
  `remaining_minutes`, `min_service_remaining`, `boost_until`, `solar_enabled`,
  `scheduled_minutes`, `est_cost`); `sensor.<load>_schedule` surfaces these plus a
  flat static `config` summary for the diagnostic card. The bulky `periods` and
  `config` attributes are excluded from the recorder (`_unrecorded_attributes`).
- **Multi-day horizon** — a load with `horizon_hours` searches `now → now+N h`,
  **intersected** with its `earliest`/`deadline` window when either is set, so the
  engine can defer an expensive day to a cheaper next one without escaping the
  daily window the wizard collected. The coordinator's `_price_slots` appends an optional
  predictor **forecast entity**'s slots for times *beyond* the real day-ahead
  horizon (filtered to `start > last real slot`), adding a confidence margin to
  their buy price so a forecast window only wins when it's cheaper by more than
  the margin. Minimum-service still bounds how long a load may be deferred.

See [scheduling-algorithms](../custom_components/load_scheduler/engine.py) (the
engine docstrings) and the per-module docstrings for details.
