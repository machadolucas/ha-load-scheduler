/*
 * Load Scheduler cards — two dependency-free Lovelace cards in one bundle.
 *
 * 1. `custom:load-scheduler-card` — a responsive grid of load "tiles": each tile
 *    shows a status dot (with on/idle/off label), the load name, and either its
 *    target + time-run-today and an on/off button (actionable loads) or just the
 *    next run + countdown (informational loads, e.g. a dishwasher). Tapping a
 *    tile opens one shared full-width schedule panel below the grid. Any
 *    non-scheduler entity (a plain switch/light/input_boolean) gets a basic tile
 *    instead — name, on/off dot + toggle, and how long it's been on — so this
 *    card can replace a regular entities/glance card too.
 *
 * 2. `custom:load-scheduler-diagnostic-card` — a denser, always-expanded panel
 *    per load showing the *rationale*: the targets math (target → done today →
 *    remaining → min-service floor → cap), the load's configuration/type, the
 *    planned periods with per-period cost, and (optionally) inline controls
 *    (boost / enable / target). A `compact` toggle collapses it to summary rows.
 *
 * Both cards are configurable from the dashboard UI (a card editor) as well as
 * YAML, and both auto-discover the integration's `…_schedule` sensors when
 * `entities` is omitted.
 *
 * Dot colour: orange = actually heating (element drawing power), light yellow =
 * powered but idle (on, element satisfied), grey = off.
 */

const SOURCE_ICON = { solar: "☀", grid: "⚡", mixed: "☀⚡" };
// The dot's tiny label maps the dot colour to a word: actually drawing power →
// "on", powered-but-satisfied → "idle", off → "off".
const DOT_LABEL = { heating: "on", idle: "idle", off: "off" };
// Inline power glyph for the round on/off toggle (no icon-font dependency).
const POWER_SVG =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" ' +
  'stroke-linecap="round" aria-hidden="true"><path d="M12 3.5v8"/>' +
  '<path d="M7 6.6a7 7 0 1 0 10 0"/></svg>';
const MODE_LABEL = {
  non_sequential: "cheapest",
  sequential: "block",
  informational: "info",
};
const CURRENCY_SYMBOL = {
  EUR: "€", USD: "$", GBP: "£", JPY: "¥", AUD: "$", CAD: "$",
  SEK: "kr", NOK: "kr", DKK: "kr", CHF: "Fr", PLN: "zł",
};

function fmtDuration(minutes) {
  const m = Math.round(minutes || 0);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  const rem = m % 60;
  return rem ? `${h}h${String(rem).padStart(2, "0")}` : `${h}h`;
}

function fmtRelative(iso) {
  if (!iso) return "idle";
  const secs = (new Date(iso) - new Date()) / 1000;
  if (secs < 60) return "now";
  if (secs < 3600) return `in ${Math.round(secs / 60)}m`;
  if (secs < 86400) {
    const h = Math.floor(secs / 3600);
    const m = Math.round((secs % 3600) / 60);
    return m ? `in ${h}h${String(m).padStart(2, "0")}` : `in ${h}h`;
  }
  const d = Math.floor(secs / 86400);
  const h = Math.round((secs % 86400) / 3600);
  return h ? `in ${d}d${h}h` : `in ${d}d`;
}

function fmtClock(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const sameDay = d.toDateString() === new Date().toDateString();
  return sameDay ? time : `${d.toLocaleDateString([], { weekday: "short" })} ${time}`;
}

// Dot class shared by both cards: orange heating, yellow idle, grey off.
function dotClass(a) {
  if (a.heating === true) return "heating";
  if (a.active === true) return a.heating === false ? "idle" : "heating";
  return "off";
}

function currencySymbol(hass) {
  const c = hass && hass.config && hass.config.currency;
  return CURRENCY_SYMBOL[c] || (c ? `${c} ` : "");
}

// All the integration's per-load `…_schedule` sensors, for the optional default
// when `entities` is omitted (and for the editor's stub config).
function discoverScheduleEntities(hass) {
  if (!hass || !hass.entities) return [];
  return Object.keys(hass.entities)
    .filter((id) => {
      const e = hass.entities[id];
      if (!e || e.platform !== "load_scheduler" || !id.startsWith("sensor.")) return false;
      if (e.translation_key) return e.translation_key === "schedule";
      const st = hass.states[id];
      return !!(st && st.attributes && Array.isArray(st.attributes.periods));
    })
    .sort();
}

// The sibling control entities (switch/button/number) of a load, found via the
// shared device of its schedule sensor. One of each per load device.
function loadControls(hass, scheduleEntityId) {
  const out = { enabled: null, boost: null, target: null };
  const reg = hass && hass.entities && hass.entities[scheduleEntityId];
  const deviceId = reg && reg.device_id;
  if (!deviceId) return out;
  for (const e of Object.values(hass.entities)) {
    if (e.device_id !== deviceId) continue;
    const dom = e.entity_id.split(".")[0];
    if (dom === "switch") out.enabled = e.entity_id;
    else if (dom === "button") out.boost = e.entity_id;
    else if (dom === "number") out.target = e.entity_id;
  }
  return out;
}

function define(name, cls) {
  if (!customElements.get(name)) customElements.define(name, cls);
}

function registerCard(card) {
  window.customCards = window.customCards || [];
  if (!window.customCards.some((c) => c.type === card.type)) {
    window.customCards.push(card);
  }
}

/* ------------------------------------------------------------------ *
 * Card 1: the compact "next run" card
 * ------------------------------------------------------------------ */

const CARD_CSS = `
  .title { font-weight: 600; font-size: 0.92em; padding: 0 2px 7px; }
  .hint { color: var(--secondary-text-color); padding: 4px 2px; font-size: 0.82em; }
  /* No outer padding (the container is transparent); align-items: stretch so all
     tiles in a row share the row's height and look uniform. */
  .grid { display: grid; gap: 8px; padding: 0; align-items: stretch;
          grid-template-columns: repeat(auto-fill, minmax(138px, 1fr)); }
  .tile { border: 1px solid var(--divider-color, rgba(127,127,127,0.25)); border-radius: 10px;
          padding: 4px 8px 6px; cursor: pointer; background: var(--card-background-color);
          transition: border-color 0.15s, box-shadow 0.15s; }
  .tile:hover { border-color: var(--primary-color); }
  .tile.selected { border-color: var(--primary-color);
          box-shadow: 0 0 0 1px var(--primary-color) inset; }
  .tile.missing { color: var(--error-color); cursor: default; font-size: 0.78em; }
  /* Status row: dot + name (+ toggle). The 36px toggle sets the row height when
     present; tiles without one stay short. */
  .tile .top { display: flex; align-items: center; gap: 8px; }
  .tile .dot { width: 13px; height: 13px; border-radius: 50%;
          background: var(--disabled-text-color); flex: 0 0 auto; }
  .tile .dot.heating { background: #ff9800; animation: ls-glow 1.5s ease-in-out infinite; }
  .tile .dot.idle { background: #ffe082; }
  .tile .dot.on { background: var(--success-color, #4caf50); }
  .tile .name { flex: 1 1 auto; min-width: 0; font-weight: 600; font-size: 0.9em;
          white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  /* Round, finger-sized on/off button (touch target ~36px). */
  .toggle { flex: 0 0 auto; cursor: pointer; user-select: none; width: 36px; height: 36px;
          border-radius: 50%; display: inline-flex; align-items: center; justify-content: center;
          border: 1.5px solid var(--divider-color, rgba(127,127,127,0.5));
          background: var(--card-background-color); color: var(--secondary-text-color);
          transition: background 0.15s, border-color 0.15s, transform 0.1s; }
  .toggle svg { width: 18px; height: 18px; }
  .toggle.on { background: var(--primary-color); border-color: var(--primary-color);
          color: var(--text-primary-color, #fff); }
  .toggle:hover { border-color: var(--primary-color); }
  .toggle:active { transform: scale(0.92); }
  .tile .line { display: flex; justify-content: space-between; gap: 8px; font-size: 0.76em;
          line-height: 1.32; }
  .tile .line .lk { color: var(--secondary-text-color); }
  .tile .line .lv { font-variant-numeric: tabular-nums; white-space: nowrap; }
  .tile .muted { color: var(--secondary-text-color); }
  .detail { margin: 8px 0 0; border: 1px solid var(--divider-color, rgba(127,127,127,0.25));
          border-radius: 10px; padding: 6px 10px; background: var(--card-background-color);
          font-variant-numeric: tabular-nums; }
  .detail-head { display: flex; align-items: center; justify-content: space-between; gap: 8px;
          margin-bottom: 3px; }
  .detail-name { font-weight: 600; font-size: 0.85em; }
  .detail .close { cursor: pointer; color: var(--secondary-text-color); padding: 2px 6px;
          font-size: 1em; user-select: none; }
  .detail .prow { font-size: 0.78em; line-height: 1.5; }
  .detail .prow.tot { color: var(--secondary-text-color); margin-top: 3px; }
  .detail .prow.muted { color: var(--secondary-text-color); }
  @keyframes ls-glow {
    0%, 100% { box-shadow: 0 0 0 0 rgba(255,152,0,0.5); }
    50% { box-shadow: 0 0 8px 3px rgba(255,152,0,0.6); }
  }
`;

class LoadSchedulerCard extends HTMLElement {
  // `entities` is optional now (auto-discovered when omitted) so the UI editor
  // can start from an empty config without throwing.
  setConfig(config) {
    this._config = config || {};
    this._selected = null; // entity id whose schedule the shared panel shows
    this._timer = null; // auto-collapse handle for the detail panel
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  disconnectedCallback() {
    if (this._timer) {
      clearTimeout(this._timer);
      this._timer = null;
    }
  }

  // Normalised, ordered list of {entity, name?}. Accepts both the bare-string
  // and the {entity, name} object form in config; order is the render order.
  _entities() {
    const list = Array.isArray(this._config.entities) ? this._config.entities : null;
    const raw = list && list.length ? list : discoverScheduleEntities(this._hass);
    return raw
      .map((e) => (typeof e === "string" ? { entity: e } : e))
      .filter((e) => e && e.entity);
  }

  getCardSize() {
    return 2 + Math.ceil((this._entities().length || 1) / 2);
  }

  // Sections (grid) view: resizable; tiles want a little width to read well.
  getGridOptions() {
    return { columns: 12, min_columns: 4, rows: "auto" };
  }

  static getConfigElement() {
    return document.createElement("load-scheduler-card-editor");
  }

  static getStubConfig(hass) {
    return { entities: discoverScheduleEntities(hass) };
  }

  // The displayed target — read the sibling `number` so it honours the load's
  // configured unit (minutes shown as a duration, kWh shown as kWh).
  _targetText(entityId, a) {
    const ctl = loadControls(this._hass, entityId);
    if (ctl.target) {
      const ts = this._hass.states[ctl.target];
      if (ts && ts.state != null && ts.state !== "unknown" && ts.state !== "unavailable") {
        const unit = ts.attributes.unit_of_measurement || "";
        const num = parseFloat(ts.state);
        if (unit === "min" || unit === "minutes") return num > 0 ? fmtDuration(num) : "—";
        return `${ts.state}${unit ? " " + unit : ""}`;
      }
    }
    const t = a.target_minutes || 0;
    return t > 0 ? fmtDuration(t) : "—";
  }

  // A plain switch/light/input_boolean tile: name, on/off dot + toggle, and how
  // long it's been on. Used for any entity that isn't a scheduler schedule sensor
  // so this card can replace a regular entities/glance card too.
  _basicTile(item, st) {
    const entityId = item.entity;
    const a = st.attributes || {};
    const name = item.name || a.friendly_name || entityId;
    const state = st.state;
    const isOn = state === "on";
    const toggleable = isOn || state === "off";
    let toggle = "";
    if (toggleable) {
      toggle =
        `<span class="toggle ${isOn ? "on" : "off"}" data-action="toggle" ` +
        `data-entity="${entityId}" data-on="${isOn}" ` +
        `title="${isOn ? "Turn off" : "Turn on"}" role="button" ` +
        `aria-label="${isOn ? "Turn off" : "Turn on"}">${POWER_SVG}</span>`;
    }
    let body = "";
    if (isOn && st.last_changed) {
      const mins = (Date.now() - new Date(st.last_changed)) / 60000;
      body = `<div class="line"><span class="lk">On for</span><span class="lv">${fmtDuration(
        mins,
      )}</span></div>`;
    } else if (!toggleable) {
      // Not an on/off entity (e.g. unavailable, or a plain sensor): show state.
      body = `<div class="line"><span class="lv muted">${state}</span></div>`;
    }
    return `<div class="tile basic">
      <div class="top">
        <span class="dot ${toggleable && isOn ? "on" : "off"}" title="${
          toggleable ? (isOn ? "on" : "off") : state
        }"></span>
        <span class="name">${name}</span>
        ${toggle}
      </div>
      ${body}
    </div>`;
  }

  _tile(item) {
    const entityId = item.entity;
    const st = this._hass.states[entityId];
    if (!st) return `<div class="tile missing">${item.name || entityId} (unavailable)</div>`;
    const a = st.attributes || {};
    // Anything that isn't one of our schedule sensors → a basic switch tile.
    if (!(Array.isArray(a.periods) && a.config && a.config.mode)) {
      return this._basicTile(item, st);
    }
    const c = a.config || {};
    const name = item.name || (a.friendly_name || entityId).replace(/\s*schedule$/i, "");
    const controlled = c.controlled_entity;
    const informational = c.mode === "informational" || !controlled;
    const dc = dotClass(a);
    const selected = this._selected === entityId;

    let toggle = "";
    if (!informational) {
      const on = a.active === true;
      toggle =
        `<span class="toggle ${on ? "on" : "off"}" data-action="toggle" ` +
        `data-entity="${controlled}" data-on="${on}" ` +
        `title="${on ? "Turn off" : "Turn on"}" role="button" ` +
        `aria-label="${on ? "Turn off" : "Turn on"}">${POWER_SVG}</span>`;
    }

    let body;
    if (informational) {
      body =
        st.state && st.state !== "unknown" && st.state !== "unavailable"
          ? `<div class="line"><span class="lk">Next</span><span class="lv">${fmtClock(
              st.state,
            )} · ${fmtRelative(st.state)}</span></div>`
          : `<div class="line"><span class="lv muted">no run scheduled</span></div>`;
    } else {
      body =
        `<div class="line"><span class="lk">Target</span><span class="lv">${this._targetText(
          entityId,
          a,
        )}</span></div>` +
        `<div class="line"><span class="lk">Today</span><span class="lv">${fmtDuration(
          a.delivered_minutes || 0,
        )}</span></div>`;
    }

    return `<div class="tile${selected ? " selected" : ""}" data-tile="${entityId}">
      <div class="top">
        <span class="dot ${dc}" title="${DOT_LABEL[dc]}"></span>
        <span class="name">${name}</span>
        ${toggle}
      </div>
      ${body}
    </div>`;
  }

  // The single shared detail panel rendered below the grid for the selected tile.
  _detail() {
    if (!this._selected) return "";
    const st = this._hass.states[this._selected];
    if (!st) return "";
    const a = st.attributes || {};
    const item = this._entities().find((e) => e.entity === this._selected);
    const name =
      (item && item.name) || (a.friendly_name || this._selected).replace(/\s*schedule$/i, "");
    const sym = currencySymbol(this._hass);
    const ps = a.periods || [];
    const rows = ps.length
      ? ps
          .map((p) => {
            const mins = (new Date(p.end) - new Date(p.start)) / 60000;
            return `<div class="prow">${fmtClock(p.start)} → ${fmtClock(p.end)} · ${fmtDuration(
              mins,
            )}</div>`;
          })
          .join("")
      : `<div class="prow muted">No runs scheduled.</div>`;
    const tot = [];
    if (a.scheduled_minutes) tot.push(`${fmtDuration(a.scheduled_minutes)} total`);
    if (a.est_cost) tot.push(`est ${sym}${a.est_cost.toFixed(2)}`);
    const totLine = tot.length ? `<div class="prow tot">${tot.join(" · ")}</div>` : "";
    return `<div class="detail">
      <div class="detail-head">
        <span class="detail-name">${name} — schedule</span>
        <span class="close" data-close="1">✕</span>
      </div>${rows}${totLine}</div>`;
  }

  // Only (re)arm the auto-collapse timer when the selection actually changes —
  // `_render` runs on every (frequent) hass update and must not reset it.
  _select(id) {
    this._selected = id;
    if (this._timer) {
      clearTimeout(this._timer);
      this._timer = null;
    }
    if (id) {
      this._timer = setTimeout(() => {
        this._selected = null;
        this._timer = null;
        this._render();
      }, 60000);
    }
    this._render();
  }

  _onClick(e) {
    const toggle = e.target.closest("[data-action]");
    if (toggle) {
      e.stopPropagation(); // never open the detail panel from the on/off button
      const entity = toggle.dataset.entity;
      const on = toggle.dataset.on === "true";
      if (this._hass && entity) {
        this._hass.callService("homeassistant", on ? "turn_off" : "turn_on", {
          entity_id: entity,
        });
      }
      return;
    }
    if (e.target.closest("[data-close]")) {
      this._select(null);
      return;
    }
    const tile = e.target.closest("[data-tile]");
    if (tile && tile.dataset.tile) {
      const id = tile.dataset.tile;
      this._select(this._selected === id ? null : id);
    }
  }

  // A compact string of everything the output depends on. HA fires `set hass`
  // on every unrelated state change; rebuilding innerHTML each time destroys the
  // DOM mid-hover/click (flicker + missed clicks). We rebuild only when this
  // changes — plus a 1-minute bucket so relative times still tick.
  _signature() {
    const parts = [
      JSON.stringify(this._config),
      this._selected || "",
      Math.floor(Date.now() / 60000),
    ];
    for (const it of this._entities()) {
      const st = this._hass.states[it.entity];
      if (!st) {
        parts.push(`${it.entity}:missing`);
        continue;
      }
      const a = st.attributes || {};
      const c = a.config || {};
      const periods = a.periods || [];
      parts.push(
        [
          it.entity,
          it.name || "",
          st.state,
          a.active,
          a.heating,
          a.running,
          a.friendly_name,
          a.target_minutes,
          a.delivered_minutes,
          a.scheduled_minutes,
          a.est_cost,
          c.controlled_entity,
          c.mode,
          periods.map((p) => `${p.start}-${p.end}`).join(","),
        ].join("|"),
      );
    }
    return parts.join("§");
  }

  _render() {
    if (!this._hass || !this._config) return;
    if (!this._card) {
      this._card = document.createElement("ha-card");
      // The container is invisible — each tile is its own little card.
      this._card.style.setProperty("--ha-card-background", "transparent");
      this._card.style.setProperty("--ha-card-box-shadow", "none");
      this._card.style.setProperty("--ha-card-border-width", "0");
      this.appendChild(this._card);
      this._card.addEventListener("click", (e) => this._onClick(e));
    }
    const sig = this._signature();
    if (sig === this._sig) return; // nothing the card shows has changed
    this._sig = sig;
    const title = this._config.title
      ? `<div class="title">${this._config.title}</div>`
      : "";
    const entities = this._entities();
    const grid = entities.length
      ? `<div class="grid">${entities.map((e) => this._tile(e)).join("")}</div>`
      : `<div class="hint">No Load Scheduler schedule sensors found — pick them in the card editor.</div>`;
    this._card.innerHTML = `<style>${CARD_CSS}</style>${title}${grid}${this._detail()}`;
  }
}

/* ------------------------------------------------------------------ *
 * Card 2: the diagnostic card (rationale + optional controls)
 * ------------------------------------------------------------------ */

function whenText(st, a) {
  if (a.status && a.status !== "ok" && a.status !== "disabled") return a.status;
  if (a.enabled === false) return "disabled";
  if (a.boost_until) return "boosting";
  if (a.running) return "now";
  if (st.state && st.state !== "unknown" && st.state !== "unavailable") {
    return fmtRelative(st.state);
  }
  return "idle";
}

function kvGrid(pairs) {
  const cells = pairs
    .filter(([, v]) => v !== null && v !== undefined && v !== "")
    .map(([k, v]) => `<span class="k">${k}</span><span class="v">${v}</span>`)
    .join("");
  return `<div class="kv">${cells}</div>`;
}

function fmtConfigWindow(c) {
  if (c.horizon_hours) return `next ${c.horizon_hours}h`;
  const e = c.earliest ? c.earliest.slice(0, 5) : null;
  const d = c.deadline ? c.deadline.slice(0, 5) : null;
  if (e || d) return `${e || "—"}–${d || "—"}`;
  return "anytime";
}

function targetsHtml(a, sym) {
  const c = a.config || {};
  const pairs = [
    ["Target", fmtDuration(a.target_minutes)],
    ["Done today", fmtDuration(a.delivered_minutes)],
    ["Remaining", fmtDuration(a.remaining_minutes)],
  ];
  if (c.min_service_minutes) {
    pairs.push([
      "Min-service",
      `${fmtDuration(c.min_service_minutes)} (${fmtDuration(a.min_service_remaining)} left)`,
    ]);
  }
  if (c.cap != null) pairs.push(["Price cap", `${sym}${c.cap}/kWh`]);
  pairs.push(["Scheduled", fmtDuration(a.scheduled_minutes)]);
  return kvGrid(pairs);
}

function configHtml(a) {
  const c = a.config || {};
  const solar = c.allow_solar ? (a.solar_enabled ? "yes · active" : "yes") : "no";
  const pairs = [
    ["Mode", MODE_LABEL[c.mode] || c.mode],
    ["Priority", c.priority],
    ["Solar", solar],
    ["Window", fmtConfigWindow(c)],
    ["Runs/day", c.runs_per_day],
  ];
  if (c.draw_kw) pairs.push(["Draw", `${c.draw_kw} kW`]);
  if (c.coexist) pairs.push(["Top-up", "never forced off"]);
  if (c.temp_entity) pairs.push(["Temp floor", `≥ ${c.temp_min}°`]);
  const wires = [
    c.controlled_entity && `controls ${c.controlled_entity}`,
    c.feedback_entity && `feedback ${c.feedback_entity}`,
    c.temp_entity && `temp ${c.temp_entity}`,
    c.delivered_entity && `delivered ${c.delivered_entity}`,
  ].filter(Boolean);
  const wiring = wires.length ? `<div class="wiring">${wires.join(" · ")}</div>` : "";
  return kvGrid(pairs) + wiring;
}

function periodsHtml(a, sym) {
  const ps = a.periods || [];
  if (!ps.length) return `<div class="periods"><span class="k">No runs scheduled</span></div>`;
  const rows = ps
    .map((p) => {
      const mins = (new Date(p.end) - new Date(p.start)) / 60000;
      const cost = p.avg_cost ? ` · ${sym}${p.avg_cost.toFixed(3)}/kWh` : "";
      return `<div>${fmtClock(p.start)} → ${fmtClock(p.end)} · ${fmtDuration(mins)} ${
        SOURCE_ICON[p.source] || ""
      }${cost}</div>`;
    })
    .join("");
  const tot = [];
  if (a.scheduled_minutes) tot.push(`${fmtDuration(a.scheduled_minutes)} total`);
  if (a.est_cost) tot.push(`est ${sym}${a.est_cost.toFixed(2)}`);
  const totLine = tot.length ? `<div class="tot">${tot.join(" · ")}</div>` : "";
  return `<div class="periods">${rows}${totLine}</div>`;
}

/* ---- Natural-language rationale --------------------------------------------- *
 * Turn the coordinator's structured `rationale` attribute (+ the targets/config
 * attrs) into a plain-English explanation of what the load is doing and why.
 * The facts come from the backend (rationale.py); the wording lives here.       */

function priceText(sym, v) {
  return `${sym}${Number(v).toFixed(3)}/kWh`;
}

function rationaleText(a, sym) {
  const r = a.rationale;
  const c = a.config || {};
  if (!r) return "No scheduling rationale available yet.";

  if (r.boost && a.boost_until) {
    return `Running now because you pressed Boost — until ${fmtClock(a.boost_until)}.`;
  }
  if (r.skip_reason === "disabled") {
    return "Scheduling is turned off for this load (its enable switch is off).";
  }
  if (r.skip_reason === "no_price_data") {
    return (a.periods || []).length
      ? "No price forecast is available, so it falls back to the fixed failsafe time."
      : "Waiting for a price forecast — none is available right now, so nothing is scheduled.";
  }
  if (r.skip_reason) return skipSentence(r, a, c, sym);
  return scheduledSentence(r, a, c, sym);
}

function skipSentence(r, a, c, sym) {
  if (r.skip_reason === "already_satisfied") {
    const noTarget = (a.target_minutes || 0) <= 0 && !c.min_service_minutes;
    if (noTarget) {
      const triggers = [];
      if (c.allow_solar) triggers.push("solar surplus");
      if (c.temp_entity) triggers.push(`the room dropping below ${c.temp_min}°`);
      const t = triggers.length ? triggers.join(" or ") : "a manual boost";
      return `No daily target — it only runs on ${t}. Neither applies right now, so it stays off.`;
    }
    const done = fmtDuration(a.delivered_minutes || 0);
    return c.min_service_minutes
      ? `Done for today — its daily minimum is already covered (${done} run).`
      : `Done for today — the target is already met (${done} run).`;
  }
  if (r.skip_reason === "no_slots_in_window") {
    return "Nothing scheduled: no price slots fall inside this load's time window yet.";
  }
  if (r.skip_reason === "all_above_cap") {
    const cap = r.cap != null ? `${sym}${r.cap}/kWh` : "your";
    const cheapest =
      r.cheapest_cost != null ? ` (cheapest is ${priceText(sym, r.cheapest_cost)})` : "";
    return `Nothing scheduled: every slot in the window is above your ${cap} price cap${cheapest}. It will wait for cheaper prices.`;
  }
  if (r.skip_reason === "no_contiguous_block") {
    return "Nothing scheduled: no cheap-enough continuous block long enough fits in the window.";
  }
  return "Nothing scheduled right now.";
}

function scheduledSentence(r, a, c, sym) {
  const informational = c.mode === "informational" || !c.controlled_entity;
  const mins = r.scheduled_minutes || 0;
  const parts = [];

  if (informational) {
    const first = (a.periods || [])[0];
    let s = `Cheapest ${fmtDuration(mins)} block starts ${first ? fmtClock(first.start) : "—"}`;
    if (first) {
      const rel = fmtRelative(first.start);
      if (rel && rel !== "idle") s += ` (${rel})`;
    }
    parts.push(s + ".", "Display only — it isn't switched automatically.");
    return parts.join(" ");
  }

  const target = a.target_minutes || 0;
  const done = a.delivered_minutes || 0;
  if (target > 0) {
    let s = `Needs ${fmtDuration(target)} today`;
    if (done > 0) {
      s += `; ${fmtDuration(done)} already ran, so ${fmtDuration(a.remaining_minutes || 0)} left`;
    }
    parts.push(s + ".");
  } else if (c.min_service_minutes) {
    parts.push(`Running its ${fmtDuration(c.min_service_minutes)} daily minimum.`);
  }

  let s =
    c.mode === "sequential"
      ? `Booked the cheapest continuous ${fmtDuration(mins)}`
      : `Booked the cheapest ${fmtDuration(mins)}`;
  if (r.cap != null) s += ` at or below your ${sym}${r.cap}/kWh cap`;
  if (r.cheapest_cost != null) s += ` (cheapest ${priceText(sym, r.cheapest_cost)})`;
  if (a.est_cost) s += ` — about ${sym}${a.est_cost.toFixed(2)}`;
  parts.push(s + ".");

  if (r.solar_enabled) {
    if (r.solar_minutes > 0) {
      parts.push(`Solar surplus covers ${fmtDuration(r.solar_minutes)} of it.`);
    } else if (r.solar_excess_kwh > 0.05) {
      parts.push("Some solar surplus is forecast, but cheaper grid slots won out.");
    } else {
      parts.push("No solar surplus is forecast in this window.");
    }
  }

  return parts.join(" ");
}

const DIAG_CSS = `
  .title { font-weight: 600; font-size: 0.92em; padding: 8px 10px 1px; }
  .hint { color: var(--secondary-text-color); padding: 8px 10px; font-size: 0.82em; }
  .panel { padding: 6px 10px; border-top: 1px solid var(--divider-color, rgba(127,127,127,0.2)); }
  .panel.first { border-top: none; }
  .panel.missing { color: var(--error-color); font-size: 0.82em; }
  .head { display: flex; flex-wrap: nowrap; align-items: center; gap: 7px; }
  .head .name { flex: 1 1 auto; min-width: 0; font-weight: 600; font-size: 0.92em;
         white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .head .when { color: var(--secondary-text-color); white-space: nowrap; font-size: 0.8em; }
  .badge-mode { font-size: 0.6em; text-transform: uppercase; letter-spacing: 0.04em;
         padding: 1px 6px; border-radius: 8px; background: var(--secondary-background-color);
         color: var(--secondary-text-color); white-space: nowrap; flex: 0 0 auto; }
  .dot { width: 11px; height: 11px; border-radius: 50%;
         background: var(--disabled-text-color); flex: 0 0 auto; }
  .dot.heating { background: #ff9800; }
  .dot.idle { background: #ffe082; }
  .sec { margin-top: 6px; }
  .sec .lbl { font-size: 0.6em; text-transform: uppercase; letter-spacing: 0.05em;
         color: var(--secondary-text-color); margin-bottom: 2px; }
  .kv { display: grid; grid-template-columns: auto 1fr; gap: 0 10px;
         font-size: 0.8em; line-height: 1.4; }
  .kv .k { color: var(--secondary-text-color); white-space: nowrap; }
  .kv .v { font-variant-numeric: tabular-nums; }
  .wiring { font-size: 0.72em; color: var(--secondary-text-color); margin-top: 3px;
         word-break: break-all; }
  .periods { font-size: 0.78em; line-height: 1.45; font-variant-numeric: tabular-nums; }
  .periods .tot { color: var(--secondary-text-color); margin-top: 2px; }
  .periods .k { color: var(--secondary-text-color); }
  .controls { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; align-items: center; }
  .btn { cursor: pointer; border: 1px solid var(--divider-color, rgba(127,127,127,0.4));
         background: var(--card-background-color); color: var(--primary-text-color);
         border-radius: 14px; padding: 4px 12px; font-size: 0.8em; user-select: none;
         white-space: nowrap; }
  .btn:hover { background: var(--secondary-background-color); }
  .btn.on { background: var(--primary-color); color: var(--text-primary-color, #fff);
         border-color: var(--primary-color); }
  .btn.active { background: #ff9800; border-color: #ff9800; color: #fff; }
  .stepper { display: inline-flex; align-items: center; gap: 4px;
         border: 1px solid var(--divider-color, rgba(127,127,127,0.4)); border-radius: 14px;
         padding: 1px 4px; }
  .stepper .sbtn { cursor: pointer; width: 22px; text-align: center; user-select: none;
         font-weight: 600; font-size: 1.05em; }
  .stepper .sval { font-variant-numeric: tabular-nums; min-width: 4em; text-align: center;
         font-size: 0.8em; }
  .row { display: flex; flex-wrap: nowrap; align-items: center; gap: 7px;
         cursor: pointer; font-size: 0.9em; line-height: 1.3; }
  .row .name { flex: 1 1 auto; min-width: 0; font-weight: 500;
         white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .row .when { color: var(--secondary-text-color); white-space: nowrap; font-size: 0.88em; }
  .row .chev { color: var(--secondary-text-color); width: 0.8em; transition: transform 0.15s; }
  .row.expanded .chev { transform: rotate(90deg); }
  .rationale { font-size: 0.86em; line-height: 1.45; margin: 5px 0 2px; }
  .details-toggle { margin-top: 7px; color: var(--secondary-text-color);
         font-size: 0.64em; text-transform: uppercase; letter-spacing: 0.05em; }
  .details-toggle .lbl { flex: 1 1 auto; }
  .cbody { margin-top: 4px; }
`;

class LoadSchedulerDiagnosticCard extends HTMLElement {
  setConfig(config) {
    this._config = config || {};
    this._expanded = new Set();
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  _opt(key, dflt) {
    const v = this._config[key];
    return v === undefined ? dflt : v;
  }

  _entities() {
    const list = Array.isArray(this._config.entities) ? this._config.entities : null;
    return list && list.length ? list : discoverScheduleEntities(this._hass);
  }

  getCardSize() {
    return 1 + this._entities().length * (this._opt("compact", false) ? 1 : 3);
  }

  // Denser than the compact card → don't let it shrink below half a section.
  getGridOptions() {
    return { columns: 12, min_columns: 6, rows: "auto" };
  }

  static getConfigElement() {
    return document.createElement("load-scheduler-diagnostic-card-editor");
  }

  static getStubConfig(hass) {
    return { entities: discoverScheduleEntities(hass) };
  }

  _sections(entityId, a, sym) {
    let html = "";
    if (this._opt("show_targets", true)) {
      html += `<div class="sec"><div class="lbl">Targets</div>${targetsHtml(a, sym)}</div>`;
    }
    if (this._opt("show_config", true)) {
      html += `<div class="sec"><div class="lbl">Configuration</div>${configHtml(a)}</div>`;
    }
    if (this._opt("show_costs", true)) {
      html += `<div class="sec"><div class="lbl">Schedule</div>${periodsHtml(a, sym)}</div>`;
    }
    if (this._opt("show_controls", true)) {
      html += this._controlsHtml(entityId, a);
    }
    return html;
  }

  _controlsHtml(entityId, a) {
    const ctl = loadControls(this._hass, entityId);
    const parts = [];
    if (ctl.boost) {
      const on = !!a.boost_until;
      parts.push(
        `<span class="btn${on ? " active" : ""}" data-action="boost" data-entity="${ctl.boost}">${
          on ? "Boosting" : "Boost"
        }</span>`,
      );
    }
    if (ctl.enabled) {
      const on = a.enabled !== false;
      parts.push(
        `<span class="btn${on ? " on" : ""}" data-action="enable" data-entity="${ctl.enabled}" data-on="${on}">${
          on ? "Enabled" : "Disabled"
        }</span>`,
      );
    }
    if (ctl.target) {
      const st = this._hass.states[ctl.target];
      const unit = st && st.attributes.unit_of_measurement ? st.attributes.unit_of_measurement : "";
      const val = st ? `${st.state}${unit}` : "—";
      parts.push(
        `<span class="stepper">` +
          `<span class="sbtn" data-action="target" data-entity="${ctl.target}" data-delta="-1">−</span>` +
          `<span class="sval">${val}</span>` +
          `<span class="sbtn" data-action="target" data-entity="${ctl.target}" data-delta="1">+</span>` +
          `</span>`,
      );
    }
    return parts.length ? `<div class="controls">${parts.join("")}</div>` : "";
  }

  _panel(entityId, sym, first) {
    const st = this._hass.states[entityId];
    if (!st) {
      return `<div class="panel missing${first ? " first" : ""}">${entityId} (unavailable)</div>`;
    }
    const a = st.attributes || {};
    const c = a.config || {};
    const name = (a.friendly_name || entityId).replace(/\s*schedule$/i, "");
    const mode = MODE_LABEL[c.mode] || c.mode || "";
    const compact = this._opt("compact", false);
    const expanded = this._expanded.has(entityId);
    const narrative = this._opt("show_rationale", true)
      ? `<div class="rationale">${rationaleText(a, sym)}</div>`
      : "";

    if (compact) {
      // One tappable row; expanding reveals the narrative + structured detail.
      const body = expanded
        ? `<div class="cbody">${narrative}${this._sections(entityId, a, sym)}</div>`
        : "";
      return `<div class="panel${first ? " first" : ""}">
        <div class="row${expanded ? " expanded" : ""}" data-entity="${entityId}">
          <span class="dot ${dotClass(a)}"></span>
          <span class="name">${name}</span>
          <span class="badge-mode">${mode}</span>
          <span class="when">${whenText(st, a)}</span>
          <span class="chev">›</span>
        </div>${body}</div>`;
    }

    // Narrative leads; the structured sections sit behind a "Details" toggle.
    const head = `<div class="head">
        <span class="dot ${dotClass(a)}"></span>
        <span class="name">${name}</span>
        <span class="badge-mode">${mode}</span>
        <span class="when">${whenText(st, a)}</span>
      </div>`;
    const toggle = `<div class="row details-toggle${expanded ? " expanded" : ""}" data-entity="${entityId}">
        <span class="lbl">Details</span><span class="chev">›</span>
      </div>`;
    const details = expanded ? this._sections(entityId, a, sym) : "";
    return `<div class="panel${first ? " first" : ""}">${head}${narrative}${toggle}${details}</div>`;
  }

  _handleAction(el) {
    const action = el.dataset.action;
    const entity = el.dataset.entity;
    if (!this._hass || !entity) return;
    if (action === "boost") {
      this._hass.callService("button", "press", { entity_id: entity });
    } else if (action === "enable") {
      const turnOn = el.dataset.on !== "true";
      this._hass.callService("switch", turnOn ? "turn_on" : "turn_off", { entity_id: entity });
    } else if (action === "target") {
      const st = this._hass.states[entity];
      if (!st) return;
      const cur = parseFloat(st.state) || 0;
      const step = parseFloat(st.attributes.step) || 1;
      const delta = parseFloat(el.dataset.delta) || 0;
      const min = st.attributes.min != null ? parseFloat(st.attributes.min) : -Infinity;
      const max = st.attributes.max != null ? parseFloat(st.attributes.max) : Infinity;
      let next = cur + delta * step;
      next = Math.min(max, Math.max(min, next));
      // Round to the step's precision to avoid float dust (e.g. 0.5 steps).
      const decimals = (String(step).split(".")[1] || "").length;
      next = Number(next.toFixed(decimals));
      this._hass.callService("number", "set_value", { entity_id: entity, value: next });
    }
  }

  _onClick(e) {
    const act = e.target.closest("[data-action]");
    if (act) {
      e.stopPropagation();
      this._handleAction(act);
      return;
    }
    const row = e.target.closest(".row");
    if (row && row.dataset.entity) {
      const id = row.dataset.entity;
      this._expanded.has(id) ? this._expanded.delete(id) : this._expanded.add(id);
      this._render();
    }
  }

  _render() {
    if (!this._hass || !this._config) return;
    if (!this._card) {
      this._card = document.createElement("ha-card");
      this.appendChild(this._card);
      this._card.addEventListener("click", (e) => this._onClick(e));
    }
    const sym = currencySymbol(this._hass);
    const entities = this._entities();
    const title = this._config.title ? `<div class="title">${this._config.title}</div>` : "";
    const body = entities.length
      ? entities.map((e, i) => this._panel(e, sym, i === 0)).join("")
      : `<div class="hint">No Load Scheduler schedule sensors found — pick them in the card editor.</div>`;
    this._card.innerHTML = `<style>${DIAG_CSS}</style>${title}${body}`;
  }
}

/* ------------------------------------------------------------------ *
 * UI editors (shared base on top of HA's <ha-form>)
 * ------------------------------------------------------------------ */

// Diagnostic card: only our schedule sensors (it needs the rationale/config).
const ENTITIES_SELECTOR = {
  entity: { multiple: true, filter: { integration: "load_scheduler", domain: "sensor" } },
};

class LoadSchedulerCardEditorBase extends HTMLElement {
  setConfig(config) {
    this._config = { ...config };
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  // Subclasses override these:
  _schema() {
    return [];
  }
  _labels() {
    return {};
  }

  _render() {
    if (!this._hass || !this._config) return;
    if (!this._form) {
      this._form = document.createElement("ha-form");
      this.appendChild(this._form);
      this._form.computeLabel = (s) => this._labels()[s.name] || s.name;
      // Translate <ha-form>'s internal `value-changed` into the editor↔dialog
      // `config-changed` contract; guard equal values to avoid editor loops.
      this._form.addEventListener("value-changed", (ev) => {
        ev.stopPropagation();
        const next = ev.detail.value;
        if (JSON.stringify(next) === JSON.stringify(this._config)) return;
        this._config = next;
        this.dispatchEvent(
          new CustomEvent("config-changed", {
            detail: { config: next },
            bubbles: true,
            composed: true,
          }),
        );
      });
    }
    this._form.hass = this._hass;
    this._form.schema = this._schema();
    this._form.data = this._config;
  }
}

// Custom editor for the compact card: a reorderable list of entities, each with
// an optional display-name override, plus an "add entity" picker. (ha-form can't
// express an ordered list of {entity, name} objects, hence the bespoke UI.)
class LoadSchedulerCardEditor extends HTMLElement {
  setConfig(config) {
    this._config = config || {};
    // Skip the rebuild triggered by our own emit (keeps a name field's focus).
    if (this._hass && JSON.stringify(this._config) !== this._lastEmitted) {
      this._build();
    }
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (this._addPicker) this._addPicker.hass = hass;
    if (first && this._config) this._build();
  }

  // Working list of {entity, name}; from config, else the auto-discovered set so
  // the user can reorder/rename the defaults straight away.
  _syncWorking() {
    const list = Array.isArray(this._config.entities) ? this._config.entities : null;
    const raw = list && list.length ? list : discoverScheduleEntities(this._hass);
    this._working = raw.map((e) =>
      typeof e === "string" ? { entity: e } : { entity: e.entity, name: e.name },
    );
  }

  _emit() {
    const entities = this._working
      .filter((e) => e && e.entity)
      .map((e) =>
        e.name && String(e.name).trim() ? { entity: e.entity, name: String(e.name).trim() } : e.entity,
      );
    const next = { ...this._config };
    if (entities.length) next.entities = entities;
    else delete next.entities;
    this._config = next;
    this._lastEmitted = JSON.stringify(next);
    this.dispatchEvent(
      new CustomEvent("config-changed", {
        detail: { config: next },
        bubbles: true,
        composed: true,
      }),
    );
  }

  _move(i, delta) {
    const j = i + delta;
    if (j < 0 || j >= this._working.length) return;
    const a = this._working;
    [a[i], a[j]] = [a[j], a[i]];
    this._emit();
    this._build();
  }

  _miniButton(glyph, label, disabled, onClick) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = glyph;
    b.title = label;
    b.setAttribute("aria-label", label);
    b.disabled = !!disabled;
    b.style.cssText =
      "flex:0 0 auto;cursor:pointer;width:30px;height:34px;border-radius:8px;font-size:1em;" +
      "border:1px solid var(--divider-color, rgba(127,127,127,0.5));" +
      "background:var(--card-background-color);color:var(--primary-text-color);";
    if (disabled) b.style.opacity = "0.4";
    if (!disabled) b.addEventListener("click", onClick);
    return b;
  }

  _row(item, i) {
    const st = this._hass.states[item.entity];
    const friendly = (st && st.attributes && st.attributes.friendly_name) || item.entity;
    const row = document.createElement("div");
    row.style.cssText = "display:flex;align-items:center;gap:6px;";

    row.appendChild(this._miniButton("↑", "Move up", i === 0, () => this._move(i, -1)));
    row.appendChild(
      this._miniButton("↓", "Move down", i === this._working.length - 1, () => this._move(i, 1)),
    );

    const info = document.createElement("div");
    info.style.cssText = "flex:1 1 38%;min-width:0;overflow:hidden;";
    info.innerHTML =
      `<div style="font-size:0.86em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${friendly}</div>` +
      `<div style="font-size:0.72em;color:var(--secondary-text-color);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${item.entity}</div>`;
    row.appendChild(info);

    const name = document.createElement("ha-textfield");
    name.label = "Name";
    name.value = item.name || "";
    name.placeholder = friendly;
    name.style.cssText = "flex:1 1 42%;min-width:0;";
    name.addEventListener("change", () => {
      this._working[i].name = name.value;
      this._emit(); // no rebuild → the field keeps focus/value
    });
    row.appendChild(name);

    row.appendChild(
      this._miniButton("✕", "Remove", false, () => {
        this._working.splice(i, 1);
        this._emit();
        this._build();
      }),
    );
    return row;
  }

  _build() {
    if (!this._hass || !this._config) return;
    this._syncWorking();
    this.innerHTML = "";
    const wrap = document.createElement("div");
    wrap.style.cssText = "display:flex;flex-direction:column;gap:10px;padding:4px 0;";

    const title = document.createElement("ha-textfield");
    title.label = "Title (optional)";
    title.value = this._config.title || "";
    title.style.width = "100%";
    title.addEventListener("change", () => {
      const v = title.value.trim();
      const next = { ...this._config };
      if (v) next.title = v;
      else delete next.title;
      this._config = next;
      this._lastEmitted = JSON.stringify(next);
      this.dispatchEvent(
        new CustomEvent("config-changed", {
          detail: { config: next },
          bubbles: true,
          composed: true,
        }),
      );
    });
    wrap.appendChild(title);

    const hint = document.createElement("div");
    hint.textContent = "Entities — reorder with the arrows, set an optional display name:";
    hint.style.cssText = "font-size:0.82em;color:var(--secondary-text-color);";
    wrap.appendChild(hint);

    this._working.forEach((item, i) => wrap.appendChild(this._row(item, i)));

    const add = document.createElement("ha-entity-picker");
    add.hass = this._hass;
    add.label = "Add entity";
    add.allowCustomEntity = false;
    add.addEventListener("value-changed", (ev) => {
      const id = ev.detail && ev.detail.value;
      if (!id) return;
      this._working.push({ entity: id });
      this._emit();
      this._build();
    });
    this._addPicker = add;
    wrap.appendChild(add);

    this.appendChild(wrap);
  }
}

class LoadSchedulerDiagnosticCardEditor extends LoadSchedulerCardEditorBase {
  // Seed the display toggles to their defaults so the form mirrors what the
  // card actually shows (the card defaults every show_* to on, compact to off).
  setConfig(config) {
    this._config = {
      compact: false,
      show_rationale: true,
      show_targets: true,
      show_config: true,
      show_costs: true,
      show_controls: true,
      ...config,
    };
    this._render();
  }

  _schema() {
    return [
      { name: "title", selector: { text: {} } },
      { name: "entities", selector: ENTITIES_SELECTOR },
      { name: "compact", selector: { boolean: {} } },
      { name: "show_rationale", selector: { boolean: {} } },
      { name: "show_targets", selector: { boolean: {} } },
      { name: "show_config", selector: { boolean: {} } },
      { name: "show_costs", selector: { boolean: {} } },
      { name: "show_controls", selector: { boolean: {} } },
    ];
  }
  _labels() {
    return {
      title: "Title (optional)",
      entities: "Schedule sensors (auto if empty)",
      compact: "Compact (collapse to rows)",
      show_rationale: "Show plain-English rationale",
      show_targets: "Show targets math",
      show_config: "Show configuration",
      show_costs: "Show schedule & cost",
      show_controls: "Show controls",
    };
  }
}

define("load-scheduler-card", LoadSchedulerCard);
define("load-scheduler-diagnostic-card", LoadSchedulerDiagnosticCard);
define("load-scheduler-card-editor", LoadSchedulerCardEditor);
define("load-scheduler-diagnostic-card-editor", LoadSchedulerDiagnosticCardEditor);

registerCard({
  type: "load-scheduler-card",
  name: "Load Scheduler Card",
  description: "Compact upcoming-runs view for Load Scheduler loads.",
});
registerCard({
  type: "load-scheduler-diagnostic-card",
  name: "Load Scheduler Diagnostic Card",
  description: "Per-load schedule rationale: targets, config, costs and controls.",
});
