# Dashboard card

Load Scheduler bundles a compact Lovelace card. The integration registers it as
a frontend resource automatically on setup (no manual resource entry needed),
so after a restart you can add it to any dashboard.

It is intentionally tiny — one row per load:

```
🟠 Water heater   now    1h      ☀   ›
🟡 Dishwasher     in 5h  3h      ⚡  ›
⚪ Floor (shower) in 2h  9h59    ☀  ›
```

- the **dot** reflects the load's actual state: **orange** = heating (the element
  is drawing power), **light yellow** = powered but idle (on, element satisfied),
  **grey** = off;
- the time is **`now`** when the scheduled run is current, otherwise **`in 5h`** —
  the relative countdown to the next run (handy for manually-started loads like a
  dishwasher), or `idle` when nothing is scheduled;
- duration is the total scheduled run time, rendered mixed (`2h45`);
- the badge shows whether the run is **solar** (☀), **grid** (⚡) or mixed (☀⚡);
- the **›** chevron marks the row as expandable.

Tap a row to expand its individual upcoming periods (with their clock times,
price and source).

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

## Sizing

In a **Sections** dashboard the card is resizable: it declares grid options
(`getGridOptions`) so you can drag it **narrower than the full section width**
(down to a quarter) and its height auto-fits the number of loads. The rows are
responsive — the load name ellipsises and the columns stay aligned at small
widths.

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
