# Dashboard card

Load Scheduler bundles a compact Lovelace card. The integration registers it as
a frontend resource automatically on setup (no manual resource entry needed),
so after a restart you can add it to any dashboard.

It is intentionally tiny — one row per load:

```
● Water heater   tonight 02:15   2h45   5.2c   ☀
  Dishwasher     tomorrow 13:00  3h     6.1c   ⚡
```

- the dot is filled while the load is **running**;
- the time is the **next run** (or `now → …` while running);
- duration is rendered mixed (`2h45`);
- the price is the average effective price for the plan;
- the badge shows whether the run is **solar** (☀), **grid** (⚡) or mixed.

Tap a row to expand its individual upcoming periods.

## Usage

Add a Manual card (or the card picker → "Load Scheduler Card"):

```yaml
type: custom:load-scheduler-card
title: Loads          # optional
entities:
  - sensor.water_heater_schedule
  - sensor.dishwasher_schedule
```

`entities` are the per-load **`…_schedule`** sensors (one per load device).

## Run history

The bespoke history view is a planned addition. In the meantime, Home
Assistant's built-in **History graph** card over the
`binary_sensor.<load>_running` entities gives a clean timeline of past runs —
those binary sensors are recorded automatically.

## Notes

- The card is plain JavaScript (no build step) served from the integration at
  `/load_scheduler/load-scheduler-card.js`.
- If you run Lovelace in YAML (storage-less) mode and resources aren't
  auto-registered, add that URL as a `module` resource manually.
