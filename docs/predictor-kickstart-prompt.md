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

## Notes captured this session

- Summer: the LVV target is often set to 0 (run on solar), so the predictor
  should respect/produce a **minimum-service** floor rather than only a target.
- The scheduler already does buy-vs-sell solar arbitration; the predictor only
  needs to answer *how much*, not *when*.
