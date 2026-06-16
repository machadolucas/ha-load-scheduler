# CLAUDE.md — Load Scheduler

Working notes for AI agents and future-me. Read this before changing code.

**Status (beta):** M0–M7 are complete and tested (engine, price/solar/window
normalization, coordinator, all entities + calendar, actuation with restart
catch-up, persistence, reconfigure, validation, solar forecast + allocation +
real-time divert, statistics baseline, repairs, failsafe, bundled card). 75
tests pass under `.venv313` (HA needs py3.13, not 3.14). Still planned: kWh/EV
target mode + dynamic remaining; a bespoke run-history card view. CI workflow
files are present locally but git-ignored (the push token lacks `workflow`
scope). See `docs/architecture.md` for the module map.

## What this is

A Home Assistant custom integration (`load_scheduler`) that schedules flexible
loads into the cheapest/greenest times from a price forecast. It originated as a
replacement for a pile of template-sensor + calendar-bus + solar-divert
automations on the author's home server (the `macserver` repo). The full design
rationale and the decisions behind it live in the approved plan at
`~/.claude/plans/structured-beaming-pixel.md` — consult it for the *why*; this
file is the *how*.

## Architecture (target)

- **Hub config entry** — shared resources + global coordination: price
  forecast (buy + optional sell), solar forecast, the cross-load **allocator**
  and real-time **divert controller**, one shared `calendar`, persistence,
  diagnostics, repairs, and the frontend card registration.
- **One config *subentry* per load** — its parameters + its own device and
  entities. (Subentry API is relatively new; the `homeassistant` floor in
  `hacs.json` must track it.)

### Module map (`custom_components/load_scheduler/`)

| File | Role | Status |
|---|---|---|
| `engine.py` | **Pure** scheduling algorithms. No HA imports. The testable core. | ✅ M1 |
| `const.py` | Domain + config keys. | ✅ |
| `__init__.py` | Hub setup/unload; startup reconciliation (M2). | stub |
| `config_flow.py` | Hub flow now; per-load `ConfigSubentryFlow` + reconfigure (M2/M3). | hub only |
| `coordinator.py` | PriceCoordinator, SolarCoordinator, Allocator, DivertController. | M2+ |
| `price_source.py` | `PriceAdaptor`/`Raw`-style normalisation (buy+sell → 15-min slots). | M3 |
| `persistence.py` | `Store` wrapper (plans, actuation state, delivered-today). | M2 |
| `actuation.py` | Drive controlled entities + reconciliation + events. | M2 |
| `binary_sensor / sensor / number / switch / button / calendar` | Entities. | M2+ |
| `diagnostics.py`, `repairs.py` | Redacted dump (= test fixtures) + issue registry. | M3 |
| `frontend/` | Bundled Lovelace card (TS → `dist/`). | M7 |

## The engine contract (read before touching `engine.py`)

- Everything is **timezone-aware**; the engine **never calls `now()`** — pass it.
- Do time math off **actual slot boundaries** from the price source (correct UTC
  offset already), never `naive + timedelta(hours=n)`. This is what makes DST
  (23h/25h days) correct.
- Durations are **minutes** (floats); the final run is trimmed to the exact
  minute.
- `effective_cost(slot)` = `buy` when importing, `sell` (foregone) when on solar
  excess, blended when partial.
- `min_service_minutes` is a cap-exempt floor: `target = max(target, min_service)`,
  and the price `cap` only filters the discretionary minutes above the floor.

## Control & safety model (do not regress)

Per-load **control policy**:
- **top-up** (LVV, EV): additive — only ever turned ON for cheap/solar; ends a
  run early when an **actual-heating feedback** (e.g. LVV LED detector / power)
  shows the element went idle.
- **comfort-shed** (floor heating; heat pumps are primary): may be kept **OFF**
  when expensive, with escapes — minimum dry-out, thermostat satisfied
  (power < ~50 W), and a **low-temp safety floor** (force heat below ~18 °C).

Actuator precedence per tick: safety floor → manual override (back off on a
foreign-context change) → boost → minimum-service → comfort-shed → solar divert
→ scheduled run → off. Anti-thrash via min-on/min-off dwell + hysteresis.
Floor-heating shed must coordinate with the existing `price_hold_multi_level`
system (don't drive the same switch from two places).

## Dev workflow

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements_test.txt   # pulls HA for HA-side tests
.venv/bin/pytest                                  # engine tests need only pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

- The engine tests load `engine.py` via `importlib` (so they never import the
  package `__init__`, hence no HA needed). Keep `engine.py` HA-free.
- HA-side tests (from M2) use `pytest-homeassistant-custom-component`; enable
  `asyncio_mode = "auto"` in `pyproject.toml` then.
- CI: `.github/workflows/validate.yaml` (hassfest + HACS) and `test.yaml` (ruff +
  pytest + coverage). Raise `--cov-fail-under=90` once HA-side tests exist.

## Conventions

- Comment the *why*, not the *what*; match the density already in `engine.py`.
- New scheduling behaviour goes in `engine.py` as a pure function **with tests
  first**, then gets wired into the coordinator.
- Capture real price/solar payloads as test fixtures (the `diagnostics.py` dump
  is designed to double as a fixture, per the `nordpool_planner` pattern).
- Don't commit secrets; the integration stores config in the config entry and
  state in `.storage/` (both covered by HA backups).
