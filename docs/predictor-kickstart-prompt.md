# Kickstart prompt — load-need predictor

This file is a **ready-to-paste prompt** for a future session that builds a
predictor for how much a load actually needs (it is *not* part of the Load
Scheduler integration). The Load Scheduler is built so a predictor can plug in
without touching its internals — see "Integration points" below. Paste the
block, fill the brackets, and go.

---

## Prompt

> I want to build a predictor that estimates how much my **water heater** (and,
> later, other loads) needs to run, and feed that into my **Load Scheduler**
> Home Assistant integration. Today I set the runtime by hunch. It really
> depends on:
>
> - how many people are home (my wife or I travelling; guests over — there's a
>   **guest-visit boolean** / guests calendar),
> - recent **hot-water consumption**,
> - **outdoor temperature** and **incoming (mains) water temperature**,
> - season / day of week.
>
> The Load Scheduler exposes these hooks:
>
> - **Output (push the prediction):** write the load's target with
>   `number.set_value` on `number.<load>_target` — minutes for a runtime load, or
>   kWh for an energy load. That is the supported hook today (a dedicated service
>   or an external target-source binding may be added later). The per-load
>   **minimum-service** floor is currently a config value (set via the load's
>   reconfigure), not a live entity.
> - **Dynamic remaining:** the scheduler subtracts an optional per-load
>   "delivered today" sensor from the target, so the predictor can instead (or
>   also) influence *how much remains*. Point it at e.g.
>   `sensor.lvv_heating_during_last_24h`.
> - **Training signals:** the integration fires `load_scheduler_run_started` /
>   `load_scheduler_run_ended` events, and `binary_sensor.<load>_running` is
>   recorded. Actual LVV heating is visible via
>   `binary_sensor.leddetector_water_heater` /
>   `sensor.leddetector_water_heater_power`.
> - **Beyond-horizon price forecast:** the hub has an optional
>   `forecast_price_entity` setting. Point it at a sensor I publish that
>   estimates electricity prices *past* the real Nord Pool day-ahead horizon
>   (the day after tomorrow), in the same per-slot attribute shape as Nord Pool
>   (`data_today` / `raw_today` list of `{start, end, buy}`). The scheduler uses
>   those slots only for times beyond the real prices and adds a configurable
>   confidence margin, so a load with a multi-day `horizon_hours` can defer an
>   expensive day to a forecast-cheaper one.
>
> Relevant existing entities in my HA:
>
> - Water heater: `switch.shellypro1_30c6f78b0f24_switch_0` (contactor),
>   `binary_sensor.leddetector_water_heater`, `sensor.leddetector_water_heater_power`,
>   `sensor.lvv_heating_during_last_24h`.
> - Climate/weather: `sensor.average_temperature_inside`,
>   `sensor.outdoor_sensor_temperature`, `sensor.tampere_temperature`, RuuviTags.
>   Incoming-water temperature: [ FILL IN sensor id if available, else note it
>   needs adding ].
> - Occupancy/presence: `person.lucas_machado`, `person.marja_helena_sivonen`,
>   the guest-visit boolean (`input_boolean.guest_visit_active`) / guests calendar.
> - Solar: Solcast forecast sensors; `sensor.net_energy_current_15_min`.
>
> Help me design and build the predictor:
>
> 1. Decide where it runs (a small companion HA custom integration, a Node-RED /
>    AppDaemon app, or an external service writing back via the HA API).
> 2. Choose an approach — start with a simple, explainable baseline (e.g.
>    consumption + temperature regression, or a lookup table by occupancy ×
>    season) before anything heavier.
> 3. Use Home Assistant **long-term statistics** for history (raw high-frequency
>    history isn't retained here to keep the DB small).
> 4. Log predictions vs. actual delivered-today so the model can be evaluated and
>    improved over time.
>
> Keep it incremental and testable, and don't over-engineer the first version.

---

## Integration points (reference)

| Need | Hook |
|---|---|
| Push a predicted target | write `number.<load>_target` (`number.set_value`) — minutes, or kWh in energy mode |
| Influence remaining | the load's optional "delivered today" sensor is subtracted from the target |
| Observe actual runs | events `load_scheduler_run_started` / `_ended`; recorded `binary_sensor.<load>_running` |
| Minimum-service floor | config (per-load reconfigure) — not yet a live entity |
| Beyond-horizon price forecast | publish a Nord-Pool-shaped sensor; set it as the hub's `forecast_price_entity` (+ confidence margin). Slots are used only past the real horizon; a load needs `horizon_hours` to act on it |

## Two distinct prediction tasks

The predictor really has **two** jobs, and they feed different hooks:

1. **How much** a load needs (the target / minimum-service floor) — the original
   task above.
2. **Beyond-horizon price forecast** (new) — estimate prices for the day after
   tomorrow so the scheduler can bet "skip this expensive 24 h; the next 24 h
   will be cheaper." Feeds `forecast_price_entity`.

### Beyond-horizon price forecast — context & findings

Nord Pool only gives real prices through tomorrow. The bet on the *following*
24 h must come from forecasts that reach further out — and three do, ~72 h:

- **Finland wind production forecast** — `sensor.finland_wind_forecast_average_fmi`
  (REST/template sensor; attributes hold a 72 h series). More wind ⇒ lower
  prices.
- **Outdoor temperature forecast** (weather integration / FMI). Colder ⇒ higher
  prices, and the effect strengthens in deep cold.
- **Solar production forecast** — Solcast (already wired into the scheduler).

Correlation analysis over 356 days (incl. a full winter) of this house's data:

| Predictor → day-ahead price | All days | Cold (< −5 °C) | Warm |
|---|---|---|---|
| Finland wind forecast | r ≈ −0.23 | r ≈ −0.49 | weak |
| Outdoor temperature | r ≈ −0.45 | — | r ≈ −0.09 |
| Joint (wind + temp) | R² ≈ 0.37 | stronger | weaker |

Mean price on cold days ≈ **11.4 c/kWh** vs warm ≈ **4.2 c/kWh**. So:

- Weight **temperature** more heavily the colder it gets (Finnish winters reach
  −30 °C; the correlation rises with cold). Wind matters most in the cold band.
- The output is a *price* estimate, not a need estimate. It can be coarse
  (e.g. a per-day or per-block average expanded into slots) — the scheduler only
  needs it to rank a future window against the known one, and the confidence
  **margin** absorbs imprecision.

### Why predict price/opportunity, not consumption

Daily delivered LVV energy (`sensor.leddetector_water_heater_energy` daily
change) is **~7.3–7.8 kWh mean, CV ~41–48 %, lag-1 autocorrelation ~0.07** —
i.e. day-to-day demand is nearly **unpredictable from yesterday**. So don't try
to forecast tomorrow's hot-water *need*; instead:

- Treat the tank as a **buffer**. Capacity ≈ a full reheat of 7–8 h at 3000 W;
  hot water comfortably lasts ~2 days, runs out around the 3rd.
- The **minimum-service floor** is the safety net that guarantees the tank never
  starves regardless of any bet.
- The predictor's job is to spot **cheap/green opportunity windows** (the price
  forecast) so the scheduler can shift discretionary heating into them — the bet
  pays off because price is far more forecastable (R² ≈ 0.37 from weather) than
  demand (autocorr ≈ 0.07).

## Notes captured this session

- Summer: the LVV target is often set to 0 (run on solar), so the predictor
  should respect/produce a **minimum-service** floor rather than only a target.
- The scheduler already does buy-vs-sell solar arbitration; the predictor only
  needs to answer *how much* and *roughly how expensive the day after tomorrow
  will be* — never *when within the known horizon* (the engine handles that).
