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
| `coordinator.py` | Read sources, allocate solar by priority, run engine per load, repairs, statistics baseline | yes |
| `actuation.py` | Resolve desired state (override → safety → plan → divert), drive controlled entities, restart catch-up | yes |
| `persistence.py` | `Store` for runtime (target/enabled/boost) | yes |
| `config_flow.py` | Hub flow + per-load subentry wizard (+ reconfigure) | yes |
| `binary_sensor/sensor/number/switch/button/calendar` | Entities | yes |
| `diagnostics.py`, `repairs.py`(strings) | Support | yes |
| `frontend/` | Bundled Lovelace card | — |

## Key contracts

- **The engine runs in UTC** and never calls `now()`. `price_source` normalizes
  every slot to UTC so all engine arithmetic is DST-free; `windows` anchors to
  local wall-clock and the coordinator passes an explicit `now`.
- **Durations are minutes**; the final run is trimmed to the exact minute.
- **Runtime state** (target / enabled / boost) lives in the `Store` (source of
  truth, in backups); entities are views/setters over it.
- **Actuation precedence** (per tick): manual override → low-temp safety floor →
  scheduled plan (incl. boost / min-service) → real-time divert → off.
- **Solar excess** = forecast PV − baseline; allocated to loads highest-priority
  first against a shared residual so no kWh is double-counted.
- **Multi-day horizon** — a load with `horizon_hours` searches `now → now+N h`
  instead of a daily window, so the engine can defer an expensive day to a
  cheaper next one. The coordinator's `_price_slots` appends an optional
  predictor **forecast entity**'s slots for times *beyond* the real day-ahead
  horizon (filtered to `start > last real slot`), adding a confidence margin to
  their buy price so a forecast window only wins when it's cheaper by more than
  the margin. Minimum-service still bounds how long a load may be deferred.

See [scheduling-algorithms](../custom_components/load_scheduler/engine.py) (the
engine docstrings) and the per-module docstrings for details.
