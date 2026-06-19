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
- **Real-time divert** uses the accumulated current-interval net-energy sensor
  (negative = export): add the highest-priority eligible load when exporting past
  the threshold and the live sell price is below its gate; shed the lowest-priority
  when importing. An optional **predicted end-of-interval net** sensor gates the
  *turn-on* (both live and predicted must show export) — the interval-aware
  "don't start a run we won't still be exporting for" debounce for 15-min net
  metering. A fixed dwell prevents thrash; an explicit stop (manual off / boost
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
- **Schedule rationale** — the per-load `LoadPlan` also captures the planning
  math the coordinator would otherwise discard (`delivered_minutes`,
  `remaining_minutes`, `min_service_remaining`, `boost_until`, `solar_enabled`,
  `scheduled_minutes`, `est_cost`); `sensor.<load>_schedule` surfaces these plus a
  flat static `config` summary for the diagnostic card. The bulky `periods` and
  `config` attributes are excluded from the recorder (`_unrecorded_attributes`).
- **Multi-day horizon** — a load with `horizon_hours` searches `now → now+N h`
  instead of a daily window, so the engine can defer an expensive day to a
  cheaper next one. The coordinator's `_price_slots` appends an optional
  predictor **forecast entity**'s slots for times *beyond* the real day-ahead
  horizon (filtered to `start > last real slot`), adding a confidence margin to
  their buy price so a forecast window only wins when it's cheaper by more than
  the margin. Minimum-service still bounds how long a load may be deferred.

See [scheduling-algorithms](../custom_components/load_scheduler/engine.py) (the
engine docstrings) and the per-module docstrings for details.
