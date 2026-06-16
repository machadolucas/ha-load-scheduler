# CLAUDE.md — Load Scheduler

Working notes for AI agents and future-me. Read this before changing code.

**Status (beta):** all features below are implemented and covered by tests (run
them — see Dev workflow). Tests run under a Python **3.13** environment because
Home Assistant doesn't support 3.14 yet. The CI workflow files exist locally but
are git-ignored (the push token lacks `workflow` scope). See
`docs/architecture.md` for the module map and data flow.

## What this is

A Home Assistant custom integration (`load_scheduler`) that schedules flexible
loads (water heater, dishwasher, EV, floor heating) into the cheapest/greenest
times from a price forecast. It replaces a pile of template-sensor +
calendar-bus + solar-divert automations on the author's home server (the
`macserver` repo). Design rationale lives in the approved plan at
`~/.claude/plans/structured-beaming-pixel.md` (the *why*); this file is the *how*.

## Architecture

- **Hub config entry** — shared resources + coordination: price forecast (buy +
  optional sell), solar forecast(s) + consumption baseline, the coordinator
  (per-load planning + priority solar allocation), the actuator (incl. live
  divert), one shared `calendar`, persistence, diagnostics, repairs, and the
  bundled-card registration.
- **One config *subentry* per load** — its parameters + its own device and
  entities. The `ConfigSubentry` API is relatively new; the `homeassistant`
  floor in `hacs.json` tracks it.

### Modules (`custom_components/load_scheduler/`)

| File | Role | HA? |
|---|---|---|
| `engine.py` | **Pure** scheduling: modes, effective cost, min-service, cap, min-run/off, merge | no |
| `price_source.py` | Normalise price entities → UTC slots (buy + optional sell) | no |
| `solar_source.py` | Parse PV forecasts (Solcast etc.) → per-slot energy | no |
| `windows.py` | DST-safe window + next-time resolution | no |
| `baseline.py` | Hour-of-day consumption profile from samples | no |
| `models.py` | Subentry config → `LoadConfig` → `LoadParams` (target conversion, dynamic remaining) | no |
| `coordinator.py` | Read sources, allocate solar by priority, run engine per load, statistics baseline, repairs, failsafe | yes |
| `actuation.py` | Resolve desired state + drive controlled entities, real-time divert, restart catch-up | yes |
| `persistence.py` | `Store` for runtime (target / enabled / boost) | yes |
| `config_flow.py` | Hub flow + per-load subentry wizard; both reconfigurable | yes |
| `binary_sensor`/`sensor`/`number`/`switch`/`button`/`calendar` | Entities | yes |
| `diagnostics.py`, `repairs` (strings) | Support | yes |
| `frontend/load-scheduler-card.js` | Bundled vanilla-JS Lovelace card | — |

## The engine contract (read before touching `engine.py`)

- Everything is **timezone-aware**; the engine **never calls `now()`** — pass it.
  It operates in **UTC** (`price_source` normalises slots to UTC), so all
  arithmetic is DST-free; `windows` anchors to local wall-clock.
- Durations are **minutes** (floats); the final run is trimmed to the exact
  minute. kWh targets are converted to minutes at the `number` entity, so the
  engine never sees kWh.
- `effective_cost(slot)` = `buy` when importing, `sell` (foregone) on solar
  excess, blended when partial.
- `min_service_minutes` is a cap-exempt floor: `target = max(target,
  min_service)`, and the price `cap` only filters discretionary minutes above
  the floor. Dynamic remaining subtracts delivered-today from both.

## Control & safety model (do not regress)

There is no policy enum — load *types* are expressed through config:

- **Top-up** (LVV, EV): `target > 0`, `allow_solar`; an optional
  `feedback_entity` marks the load "satisfied" (running but element idle, e.g. a
  full tank) so it isn't given more solar.
- **Comfort-shed / secondary heat** (floor heating; heat pumps are primary):
  `target = 0` + a `min_service` daily dry-out + a `temp_entity`/`temp_min`
  **low-temp safety floor** + `allow_solar`. It then only runs on solar divert,
  the dry-out minimum, or when the room is too cold — i.e. off when expensive.

Actuator precedence per tick (`actuation.py`): **manual override** (a
foreign-context change backs off for a grace period) → **low-temp safety floor**
→ **scheduled plan** (cheap/solar/min-service/boost) → **real-time divert** (live
export surplus, sell-gated, allocated by priority, min-dwell anti-thrash) →
**off**. Floor-heating shed overlaps the existing `price_hold_multi_level`
system — don't let two controllers drive the same switch.

## Dev workflow

```bash
# HA-side tests need Python 3.13 (HA doesn't support 3.14):
uv venv --python 3.13 .venv313
uv pip install --python .venv313/bin/python -r requirements_test.txt ruff
.venv313/bin/python -m pytest
.venv313/bin/ruff check . && .venv313/bin/ruff format --check .
```

- The pure tests (`test_engine`/`price_source`/`windows`/`baseline`) load their
  module via `importlib`, so they run with only `pytest`. **Keep those modules
  HA-free.** The `tests/ha/` tests use `pytest-homeassistant-custom-component`.
- Prefer `ha_reload`-friendly changes; after editing run the full suite.

## Conventions

- Comment the *why*, not the *what*; match the density in `engine.py`.
- New scheduling behaviour goes in `engine.py` (or another pure module) as a
  tested pure function first, then gets wired into the coordinator.
- Capture real price/solar payloads as fixtures (`diagnostics.py` doubles as a
  fixture source).
- Don't commit secrets; config lives in the config entry and runtime state in
  `.storage/` (both in HA backups).

## Branding / icon

`brands/` holds the icon: `icon.svg` (editable source) + `icon.png` (256) +
`icon@2x.png` (512), rendered with `rsvg-convert`. Full-bleed app tile: blue
energy gradient, white price bars with the cheapest in green, and an amber
lightning bolt ("run the load on the cheap slot"). The sibling `load_need_predictor`
integration's icon is derived from this one (bolt → forecast line). Re-render
after editing the SVG:

```bash
cd brands && rsvg-convert -w 512 -h 512 icon.svg -o icon@2x.png \
                        && rsvg-convert -w 256 -h 256 icon.svg -o icon.png
```

**TODO — make it show in HA + HACS (not done yet):** HA/HACS load integration
icons only from the `home-assistant/brands` repo (no repo-local or manifest
override). Open a PR there adding `custom_integrations/load_scheduler/icon.png`
+ `icon@2x.png` (the files in `brands/`). Keep it full-bleed so brands' trim
check passes. After merge, an HA restart may be needed to clear the brand cache.
