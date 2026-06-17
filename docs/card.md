# Dashboard cards

Load Scheduler bundles two Lovelace cards (in one JS file): a compact
**upcoming-runs** card and a **diagnostic** card. The integration registers them
as a frontend resource automatically on setup (no manual resource entry needed),
so after a restart you can add either from the card picker. Both are configurable
from the dashboard UI (a visual editor) as well as YAML, and both auto-discover
the integration's `…_schedule` sensors when you omit `entities`.

## Compact card (`custom:load-scheduler-card`)

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

`entities` are the per-load **`…_schedule`** sensors (one per load device). Omit
`entities` and the card shows every Load Scheduler load it can find.

## Diagnostic card (`custom:load-scheduler-diagnostic-card`)

A denser, always-expanded panel per load that explains **why** a schedule looks
the way it does — useful for tuning a load or debugging. Each panel shows:

- **Targets** — the run-time math: target → done today → remaining → the
  min-service floor (cap-exempt) → the price cap → what got scheduled. This is
  the dynamic-remaining calculation made visible (a load that already ran enough
  today shows a smaller remaining/scheduled time).
- **Configuration** — the load's *type* and rules: mode (cheapest / block /
  info), priority, whether it takes solar (and whether it competed for it this
  tick), the search window or multi-day horizon, runs/day, draw, top-up, the
  low-temp safety floor, and the wired entities (controlled / feedback / temp /
  delivered).
- **Schedule** — each upcoming period with its clock times, duration, source
  (☀ / ⚡), and per-period €/kWh, plus the total and a rough run cost.
- **Controls** (optional) — inline **Boost** (run now, toggles off again),
  **Enable/disable**, and a **target** stepper, reusing the load's own
  button/switch/number entities.

```yaml
type: custom:load-scheduler-diagnostic-card
title: Loads — diagnostics   # optional
entities:                    # optional (auto-discovered if omitted)
  - sensor.water_heater_schedule
  - sensor.dishwasher_schedule
compact: false               # collapse to tap-to-expand rows
show_targets: true           # each section can be toggled off
show_config: true
show_costs: true
show_controls: true
```

The currency symbol follows your Home Assistant configuration. Costs are derived
from the per-period effective price; the run-cost estimate needs the load's
**draw (kW)** to be set.

## UI configuration

Both cards have a visual editor: when you add one from the card picker (or click
**Edit** on it), you get a form to set the title, pick the schedule sensors
(filtered to this integration), and — for the diagnostic card — toggle the
sections and compact mode. YAML still works exactly as above.

## Sizing

In a **Sections** dashboard both cards are resizable: they declare grid options
(`getGridOptions`) so you can drag them narrower than the full section width and
the height auto-fits. The compact card goes down to a quarter; the denser
diagnostic card stops at half a section so its key/value columns stay readable.
The layout is responsive — names ellipsise and columns stay aligned at small
widths.

## Run history

The bespoke history view is a planned addition. In the meantime, Home
Assistant's built-in **History graph** card over the
`binary_sensor.<load>_running` entities gives a clean timeline of past runs —
those binary sensors are recorded automatically.

## Notes

- Both cards are plain JavaScript (no build step) bundled in one file, served
  from the integration at `/load_scheduler/load-scheduler-card.js`.
- The integration injects that URL with a `?v=<content-hash>` cache-buster, so an
  updated card is picked up automatically (the hash changes with the file). If
  you still see a stale card on a device after an update, hard-refresh once — or,
  in the Companion app, **Settings → Companion App → Troubleshooting → Reset
  frontend cache**. Also check **Settings → Dashboards → Resources** and remove
  any old manual entry for this card: a duplicate resource can load a different
  version on different devices.
- If you run Lovelace in YAML (storage-less) mode and resources aren't
  auto-registered, add the URL as a `module` resource manually.
