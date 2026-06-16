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
> The Load Scheduler already exposes the hooks a predictor needs:
>
> - **Output:** write the predicted runtime to the load's target. Three equivalent
>   ways — set the integration's `number.<load>_target`, publish my own sensor and
>   point the load's *target source* at it, or call the
>   `load_scheduler.set_parameter` service (supports an expiry). I can also drive
>   the **minimum-service** thresholds the same way (e.g. lower them when the
>   house is empty for a week).
> - **Training signal:** the integration fires `load_scheduler_run_started` /
>   `load_scheduler_run_ended` events and tracks **delivered-today** (measured
>   from the actual-heating feedback, not switch-on time). For the LVV the real
>   delivery is visible via `binary_sensor.leddetector_water_heater` /
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
| Push a predicted target | `number.<load>_target`, external target-source entity, or `load_scheduler.set_parameter` |
| Push minimum-service | same pluggable-source mechanism |
| Observe actual runs | events `load_scheduler_run_started` / `_ended` |
| Observe delivery | `delivered-today` (from actual-heating feedback) |

## Notes captured this session

- Summer: the LVV target is often set to 0 (run on solar), so the predictor
  should respect/produce a **minimum-service** floor rather than only a target.
- The scheduler already does buy-vs-sell solar arbitration; the predictor only
  needs to answer *how much*, not *when*.
