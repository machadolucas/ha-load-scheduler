# Load Scheduler

[![hacs](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A Home Assistant custom integration that schedules flexible electrical loads —
water heater, dishwasher, EV charger, floor heating — into the **cheapest** (and
**greenest**) times, using a day-ahead price forecast and, optionally, a
sell-price + solar-production forecast.

It replaces the usual pile of template sensors, `calendar`-bus automations and
ad-hoc solar-divert automations with **one configurable integration**: a hub
that holds the shared price/solar sources, and one entry per load that you set
up through a guided wizard.

> **Status: beta.** Install as a HACS custom repository (below) — not yet in the
> HACS default store. Backed by an extensive test suite.

## Why

15-minute spot pricing makes "run this when it's cheap" surprisingly fiddly:
overnight windows cross midnight, some loads can pause/resume while others need
one contiguous block, and solar changes the calculus entirely (sometimes it's
better to heat at night and *sell* the daytime sun). Doing that in YAML/Jinja is
fragile and untestable. This integration moves the logic into tested Python and
exposes simple knobs.

## Features

- **Two scheduling modes**
  - *Non-sequential* — the N cheapest slots, scattered then merged (e.g. a water
    heater / thermal store that can pause and resume freely).
  - *Sequential* — one or more **contiguous** blocks of a fixed length, with
    support for **multiple non-colliding runs per day** (e.g. a washing machine
    twice) and a minimum separation between them.
- **Runtime *or* energy targets** — schedule by minutes of run time, or by kWh
  to deliver at the load's power draw (EV charging). **Dynamic remaining** shrinks
  the remaining target by how much the load already ran today — measured
  automatically from the feedback element since local midnight (no extra sensor),
  or from an explicit "delivered today" sensor if you prefer. So a load that
  already ran enough — e.g. on solar, or via a manual/comfort run — is scheduled
  for less or skipped.
- **Informational mode** — compute and display the cheapest time without
  actuating anything (e.g. a non-connected dishwasher you start by hand).
- **Solar arbitration** — value each slot at its *effective cost*: the buy price
  when importing, or the foregone **sell** price when running on predicted solar
  excess; allocate predicted excess across competing loads by priority; and
  divert live surplus in real time.
- **Minimum-service guarantee** — a per-load floor (minimum daily delivery / max
  time without running) that overrides the price cap so a load never starves
  (the tank never runs cold; a wet-room floor still dries out).
- **Multi-day horizon & forecast deferral** — give a load an N-hour horizon
  instead of a daily window and it can defer an expensive day to a cheaper next
  day once tomorrow's real prices are known. Beyond that real horizon, an
  optional **predictor price-forecast sensor** (e.g. a wind + temperature +
  solar estimate of the day after tomorrow) lets it bet that the *following*
  24 h will be cheaper — applied with a confidence margin so it only defers when
  the forecast clearly wins, and always bounded by the minimum-service floor.
- **Absolute price cap/floor**, **min-run / min-off** dwell, **manual boost**
  (a toggle — press again to cancel), and a **temporary disable** switch.
- **Coexist (top-up) mode** — a load can run *alongside* your existing control:
  the scheduler only ever switches it **on** (cheap hours / solar / safety floor)
  and only switches **off** runs it started itself, never an external one. It
  observes and credits manual/automation runs as delivered. Lets you add
  cheap/green energy on top of (e.g.) floor-heating comfort automations without
  the two fighting. The `running` sensor reflects the real switch state.
- **Restart-safe** — the plan and actuation state survive Home Assistant
  restarts; on boot the integration reconciles each load to the state it *should*
  be in (so a load whose run ended during downtime is correctly switched off).
- **DST-correct** for 23h/25h days.
- **Predictor-friendly** — the target is a writable `number` entity, so an
  external model can push a predicted value; per-run events and the optional
  "delivered today" sensor give it signals to learn from.
- **Compact dashboard card** (bundled) showing upcoming runs (run history via
  Home Assistant's built-in History card for now).
- **Backs up with Home Assistant** — all config + state lives in the config
  entry and `.storage/`.

## Installation

### HACS (custom repository)

1. HACS → ⋮ → *Custom repositories* → add `https://github.com/machadolucas/ha-load-scheduler`, category **Integration**.
2. Install **Load Scheduler**, then restart Home Assistant.
3. *Settings → Devices & Services → Add Integration → Load Scheduler.*

### Manual

Copy `custom_components/load_scheduler` into your Home Assistant
`config/custom_components/` directory and restart.

## Configuration

1. **Add the hub** and select your **buy-price forecast** sensor (optionally a
   sell-price and a solar-production forecast).
2. **Add a load** (from the integration's page) and follow the wizard: mode,
   target, search window/deadline, optional actuation target, solar options,
   minimum-service, and failsafe.

Each load exposes a `binary_sensor` (running), a merged `schedule` sensor (next
start + the upcoming periods), a `number` (target), an `enabled` switch and a
`boost` button; the hub exposes one shared `calendar`.

## Planned

- A bespoke run-history view in the card (today the built-in History card over
  the `…_running` binary sensors covers it).
- HACS default-store submission (brands entry + screenshots).

See [`CLAUDE.md`](CLAUDE.md) and [`docs/`](docs/) for architecture and design
notes.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements_test.txt
.venv/bin/pytest            # run the test suite
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

The scheduling engine ([`engine.py`](custom_components/load_scheduler/engine.py))
has **no Home Assistant dependency** and is tested in isolation.

## License

MIT © Lucas Machado
