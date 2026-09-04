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
  **grey** = off; the `feedback_entity` can be a numeric power sensor or a plain
  on/off (binary_sensor), and if it goes unavailable the dot and the expanded
  24h activity timeline both fall back to the controlled switch's state (heating
  while on) instead of reading idle/off;
- the time is **`now`** when the scheduled run is current, otherwise **`in 5h`** —
  the relative countdown to the next run (handy for manually-started loads like a
  dishwasher), or `idle` when nothing is scheduled;
- duration is the total scheduled run time, rendered mixed (`2h45`);
- the badge shows whether the run is **solar** (☀), **grid** (⚡) or mixed (☀⚡);
- the **›** chevron marks the row as expandable.

Tap a row to expand its individual upcoming periods (with their clock times,
price and source). Inside that expanded detail panel, clicking the **title**
opens the more-info dialog of the load's controlled switch — or, for
informational loads with no controlled entity, the schedule sensor itself.

Beside the period list — in the space it leaves free, so the panel gains no
height — sits the load's **boost pill**, with the duration printed small
underneath it:

- **⚡ Boost** (outlined) — tap to run the load now for that duration,
  overriding both the price plan and the enable switch. The caption shows how
  long that will be.
- **⚡ 42m left** (orange, matching the "heating" colour elsewhere) — a boost is
  running; the caption shows when it ends, and tapping cancels it (the same
  explicit stop as the load's own Boost button, so the plan and solar divert
  don't immediately re-grab the load).

The duration comes from `boost_minutes` — per entity, falling back to the
card-wide value, falling back to what the integration itself would pick (the
load's target runtime, or 60 minutes when that is zero). Loads with nothing to
control (informational) get no pill.

If an entity sets `tank_charge` (below), a thin progress bar appears under the
tile's title:

- continuous red→green fill — saturated red when low, muted green when full;
- a soft, faded leading edge, since the value is an estimate rather than a
  measurement;
- a subtle shimmer while the load's schedule sensor reports `heating: true`;
- the integer percentage, printed at the end of the bar.

Clicking the bar opens the **`tank_charge`** sensor's more-info dialog (not the
tile's own). The tooltip shows estimated showers left, plus a calibrating note
when the sensor reports `calibrated: false`.

## Usage

Add a Manual card (or the card picker → "Load Scheduler Card"):

```yaml
type: custom:load-scheduler-card
title: Loads          # optional
boost_minutes: 60     # optional, default duration of the card's boost pill
history_hours: 24     # optional, span of the activity timeline (default 24, max 168)
entities:
  - entity: sensor.water_heater_schedule
    name: Water heater              # optional, overrides the friendly name
    tank_charge: sensor.lvv_water_heater_tank_charge   # optional
    boost_minutes: 90               # optional, overrides the card-wide value
  - sensor.dishwasher_schedule
```

`entities` are the per-load **`…_schedule`** sensors (one per load device),
either as plain entity IDs or as objects with `entity` (required), `name`
(optional display override), `tank_charge` (optional) and `boost_minutes`
(optional). `tank_charge`
points at an external 0–100 percentage sensor — e.g.
`sensor.lvv_water_heater_tank_charge` from the load-need-predictor
integration — and enables the tank-charge progress bar described above. Omit
`entities` and the card shows every Load Scheduler load it can find (without
a tank-charge bar, since auto-discovery has no way to know which percentage
sensor belongs to which load).

`history_hours` sets how far back the detail panel's activity timeline looks —
24 hours by default, clamped to 168 (a week).

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
- **Controls** (optional) — inline **Boost** (run now for `boost_minutes`, or
  the load's target runtime when unset; while boosting it shows the time left
  and tapping cancels), **Enable/disable**, and a **target** stepper, reusing
  the load's own button/switch/number entities.

```yaml
type: custom:load-scheduler-diagnostic-card
title: Loads — diagnostics   # optional
entities:                    # optional (auto-discovered if omitted)
  - entity: sensor.water_heater_schedule
    name: Water heater       # optional display override
  - sensor.dishwasher_schedule
compact: false               # collapse to tap-to-expand rows
show_rationale: true         # the plain-English paragraph
show_targets: true           # each section can be toggled off
show_config: true
show_costs: true
show_controls: true
boost_minutes: 60            # optional; blank = the load's target runtime
```

The currency symbol follows your Home Assistant configuration. Costs are derived
from the per-period effective price; the run-cost estimate needs the load's
**draw (kW)** to be set.

## UI configuration

**Every** option above is settable from the visual editor — you never need the
YAML tab. Add a card from the picker (or click **Edit** on one) and you get a
form for the card-wide options — laid out in a grid so it stays a couple of rows
rather than one tall column — then one card per entity: reorder with the arrows,
remove with ✕, add with the picker at the bottom, and set that entity's display
name (plus, on the compact card, its tank-charge sensor and boost duration). YAML still works exactly as above, and an entity with no per-entity
overrides stays a plain entity ID when the editor writes the config back.

Everything the editor renders goes through Home Assistant's `ha-form`
selectors. That is deliberate: a bare `<ha-textfield>` is only defined if
something else on the page happened to load its chunk, and an undefined custom
element renders as an invisible zero-size box — which is how these fields used
to disappear from the editor (fixed in 0.16.0).

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
- The integration registers that file in the **Lovelace resource registry** (the
  same mechanism HACS uses), with a `?v=<content-hash>` cache-buster so an updated
  card is picked up automatically. The registry is fetched by the frontend at
  runtime, so the card survives a stale cached app shell (a CDN edge or the
  service worker serving old index HTML) — the case where `add_extra_js_url`
  would silently drop the card and you'd see "Custom element doesn't exist". You
  can see the entry under **Settings → Dashboards → Resources**.
- If you run Lovelace in **YAML resource mode** (the registry can't be edited
  programmatically), the integration falls back to injecting the script directly;
  add the URL as a `module` resource manually if needed.
