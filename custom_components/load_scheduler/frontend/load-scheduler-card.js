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
// Inline bolt glyph for the boost pill (same no-icon-font reasoning).
const BOLT_SVG =
  '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">' +
  '<path d="M13 2 4.5 13.2H11l-1 8.8 8.5-11.2H12z"/></svg>';
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
  // Round once at the display granularity, then split — rounding the
  // remainder separately can produce "19h60" or "2d24h".
  const mins = Math.round(secs / 60);
  if (mins < 60) return `in ${mins}m`;
  if (secs < 86400) {
    const h = Math.floor(mins / 60);
    const m = mins % 60;
    return m ? `in ${h}h${String(m).padStart(2, "0")}` : `in ${h}h`;
  }
  const hours = Math.round(secs / 3600);
  const d = Math.floor(hours / 24);
  const h = hours % 24;
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

// Minutes left on an active boost, read from the schedule sensor's
// `boost_until` (a local ISO timestamp). 0 when there is no boost or it has
// already lapsed — the coordinator clears the attribute on its next tick, so
// don't trust a stale one.
function boostRemaining(attrs) {
  const until = attrs && attrs.boost_until;
  if (!until) return 0;
  const ms = new Date(until) - Date.now();
  return ms > 0 ? ms / 60000 : 0;
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
  /* Each tile is a real <ha-card>, so it inherits the active theme's card
     background / border / radius / backdrop-filter. We add only the click
     affordance, inner padding, and a primary border when hovered/selected. */
  .tile { cursor: pointer; }
  .tile:hover { --ha-card-border-color: var(--primary-color); }
  .tile.selected { --ha-card-border-color: var(--primary-color);
          box-shadow: 0 0 0 1px var(--primary-color); }
  .tile.missing { color: var(--error-color); cursor: default; }
  .tile .ti { padding: 5px 9px 7px; }
  .tile.missing .ti { font-size: 0.78em; }
  /* Status row: name (+ toggle). */
  .tile .top { display: flex; align-items: center; gap: 8px; }
  .tile .name { flex: 1 1 auto; min-width: 0; font-weight: 600; font-size: 0.9em;
          white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  /* Round, finger-sized on/off button (touch target ~36px). The button doubles
     as the status indicator: its fill is the status colour (heating/idle/on),
     transparent when off — so there's no separate dot. */
  .toggle { flex: 0 0 auto; cursor: pointer; user-select: none; width: 36px; height: 36px;
          border-radius: 50%; display: inline-flex; align-items: center; justify-content: center;
          border: 1.5px solid var(--divider-color, rgba(127,127,127,0.5));
          background: transparent; color: var(--secondary-text-color);
          transition: background 0.15s, border-color 0.15s, transform 0.1s; }
  .toggle svg { width: 18px; height: 18px; }
  .toggle.heating { background: #ff9800; border-color: #ff9800; color: #fff;
          animation: ls-glow 1.5s ease-in-out infinite; }
  .toggle.idle { background: #ffe082; border-color: #ffe082; color: #333; }
  .toggle.on { background: var(--success-color, #4caf50);
          border-color: var(--success-color, #4caf50); color: #fff; }
  .toggle:hover { border-color: var(--primary-color); }
  .toggle:active { transform: scale(0.92); }
  .tile .line { display: flex; justify-content: space-between; gap: 8px; font-size: 0.76em;
          line-height: 1.32; min-width: 0; }
  .tile .line .lk { color: var(--secondary-text-color); }
  .tile .line .lv { font-variant-numeric: tabular-nums; white-space: nowrap; min-width: 0; }
  .tile .line .lv.wrap { white-space: normal; overflow-wrap: anywhere; text-align: right; }
  .tile .muted { color: var(--secondary-text-color); }
  /* Optional tank-charge bar, sits between the name/toggle row and the body.
     Thin (~5px) and rounded; the fill's colour is set inline via --tank-color
     (JS computes a red→green gradient, saturated when near-empty). The fill's
     leading edge fades out over ~12px because the charge is only an estimate. */
  .tank { display: flex; align-items: center; gap: 6px; margin: 4px 0 2px; min-width: 0; }
  .tank .bar { position: relative; flex: 1 1 auto; min-width: 0; height: 5px; border-radius: 2.5px;
          background: color-mix(in srgb, var(--secondary-text-color) 18%, transparent);
          overflow: hidden; }
  .tank .fill { position: relative; height: 100%; border-radius: 2.5px;
          background: linear-gradient(90deg, var(--tank-color) calc(100% - 12px), transparent 100%); }
  /* Discreet slow shimmer while the tank is actively charging. */
  .tank .fill.charging::after { content: ""; position: absolute; inset: 0;
          background: repeating-linear-gradient(90deg, transparent 0, transparent 12px,
            rgba(255,255,255,0.12) 12px, rgba(255,255,255,0.12) 24px);
          background-size: 24px 100%; animation: ls-tank-shimmer 3s linear infinite; }
  .tank .pct { flex: 0 0 auto; font-size: 0.68em; font-variant-numeric: tabular-nums;
          color: var(--secondary-text-color); }
  @keyframes ls-tank-shimmer {
    0% { background-position: 0 0; }
    100% { background-position: 24px 0; }
  }
  @media (prefers-reduced-motion: reduce) {
    .tank .fill.charging::after { animation: none; }
  }
  /* The expanded schedule is its own <ha-card> too. */
  .detail { display: block; margin: 8px 0 0; font-variant-numeric: tabular-nums; }
  .detail .detail-body { padding: 6px 10px; }
  .detail-head { display: flex; align-items: center; justify-content: space-between; gap: 8px;
          margin-bottom: 3px; }
  .detail-name { font-weight: 600; font-size: 0.85em; cursor: pointer; }
  .detail-name:hover { text-decoration: underline; }
  .detail .close { cursor: pointer; color: var(--secondary-text-color); padding: 2px 6px;
          font-size: 1em; user-select: none; }
  .detail .prow { font-size: 0.78em; line-height: 1.5; }
  .detail .prow.tot { color: var(--secondary-text-color); margin-top: 3px; }
  .detail .prow.muted { color: var(--secondary-text-color); }
  /* The schedule list and the boost control share a row so the panel gains no
     height; flex-wrap (not a viewport media query — the card's width is set by
     its dashboard column, not the window) drops the control below when narrow. */
  .detail-cols { display: flex; flex-wrap: wrap; align-items: flex-start; gap: 4px 10px; }
  /* The floor stops the schedule rows being squeezed into two lines each; once
     the control no longer fits beside them it wraps to its own row instead. */
  .detail-main { flex: 1 1 55%; min-width: min(100%, 210px); }
  .detail-side { flex: 0 0 auto; margin-left: auto; display: flex; flex-direction: column;
          align-items: flex-end; gap: 2px; }
  .bbtn { display: inline-flex; align-items: center; gap: 5px; cursor: pointer;
          border: 1px solid var(--divider-color, rgba(127,127,127,0.4));
          background: var(--card-background-color); color: var(--primary-text-color);
          border-radius: 13px; padding: 3px 11px; font-size: 0.78em; font-weight: 500;
          user-select: none; white-space: nowrap; }
  .bbtn:hover { background: var(--secondary-background-color); }
  .bbtn svg { width: 12px; height: 12px; flex: 0 0 auto; }
  /* Boosting reuses the heating orange, so the pill matches the timeline. */
  .bbtn.active { background: #ff9800; border-color: #ff9800; color: #fff; }
  .bcap { font-size: 0.66em; color: var(--secondary-text-color); white-space: nowrap;
          padding-right: 2px; }
  /* 24h activity timeline (history-graph style), colours matching the statuses. */
  .tlwrap { margin-top: 7px; position: relative; }
  .tl { display: flex; height: 14px; border-radius: 7px; overflow: hidden;
          background: var(--divider-color, rgba(127,127,127,0.25)); cursor: pointer; }
  .tl .seg { display: block; min-width: 0; }
  .tltip { position: absolute; bottom: 22px; transform: translateX(-50%);
          background: var(--secondary-background-color, #333);
          color: var(--primary-text-color); border-radius: 6px; padding: 3px 8px;
          font-size: 0.72em; line-height: 1.35; white-space: nowrap; pointer-events: none;
          border: 1px solid var(--divider-color, rgba(127,127,127,0.4));
          box-shadow: 0 2px 8px rgba(0,0,0,0.3); z-index: 3; }
  .tltip[hidden] { display: none; }
  .tltip .tipst { display: block; font-weight: 600; text-transform: capitalize; }
  .tltip .tipdot { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
          margin-right: 5px; vertical-align: middle; background: var(--disabled-text-color); }
  .tltip .tipdot.heating { background: #ff9800; }
  .tltip .tipdot.idle { background: #ffe082; }
  .tltip .tipdot.on { background: var(--success-color, #4caf50); }
  .tl .seg.heating { background: #ff9800; }
  .tl .seg.idle { background: #ffe082; }
  .tl .seg.on { background: var(--success-color, #4caf50); }
  .tl .seg.off { background: transparent; }
  .tlcap { display: flex; justify-content: space-between; font-size: 0.66em;
          color: var(--secondary-text-color); margin-top: 2px; }
  .tlload { font-size: 0.78em; color: var(--secondary-text-color); margin-top: 7px; }
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
    const selected = this._selected === entityId;
    let toggle = "";
    if (toggleable) {
      toggle =
        `<span class="toggle ${isOn ? "on" : "off"}" data-action="toggle" ` +
        `data-entity="${entityId}" data-on="${isOn}" role="button" ` +
        `title="${isOn ? "on" : "off"} · tap to turn ${isOn ? "off" : "on"}" ` +
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
    return `<ha-card class="tile basic${
      selected ? " selected" : ""
    }" data-tile="${entityId}"><div class="ti">
      <div class="top">
        <span class="name">${name}</span>
        ${toggle}
      </div>
      ${body}
    </div></ha-card>`;
  }

  // Optional thin charge bar for an external tank sensor (0–100 %). Renders
  // nothing (zero height) unless `item.tank_charge` names an entity that exists
  // with a finite numeric state. `a` is the schedule sensor's attributes — its
  // `heating` flag drives the charging shimmer.
  _tankBar(item, a) {
    if (!item.tank_charge) return "";
    const ts = this._hass.states[item.tank_charge];
    if (!ts) return "";
    const num = Number(ts.state);
    if (!isFinite(num)) return "";
    const pct = Math.max(0, Math.min(100, num));
    // Continuous red→green hue; saturation high (≈90%) near empty, muted
    // (≈35%) toward full, so a full tank reads quiet and a near-empty one alarms.
    const hue = pct * 1.2;
    const sat = 90 - pct * 0.55;
    const color = `hsl(${hue.toFixed(0)}, ${sat.toFixed(0)}%, 48%)`;
    const ta = ts.attributes || {};
    const charging = a.heating === true;
    let tip = "Estimated tank charge";
    if (typeof ta.showers_left === "number" && isFinite(ta.showers_left)) {
      tip += ` · ~${Math.round(ta.showers_left)} showers left`;
    }
    if (ta.calibrated === false) tip += " (calibrating…)";
    return `<div class="tank" data-action="more-info" data-entity="${item.tank_charge}" title="${tip}">
      <div class="bar"><div class="fill${charging ? " charging" : ""}" style="width:${pct.toFixed(
        0,
      )}%; --tank-color:${color}"></div></div>
      <span class="pct">${Math.round(pct)}%</span>
    </div>`;
  }

  _tile(item) {
    const entityId = item.entity;
    const st = this._hass.states[entityId];
    if (!st)
      return `<ha-card class="tile missing"><div class="ti">${
        item.name || entityId
      } (unavailable)</div></ha-card>`;
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
        `<span class="toggle ${dc}" data-action="toggle" ` +
        `data-entity="${controlled}" data-on="${on}" role="button" ` +
        `title="${DOT_LABEL[dc]} · tap to turn ${on ? "off" : "on"}" ` +
        `aria-label="${on ? "Turn off" : "Turn on"}">${POWER_SVG}</span>`;
    }

    let body;
    if (informational) {
      body =
        st.state && st.state !== "unknown" && st.state !== "unavailable"
          ? `<div class="line"><span class="lk">Next</span><span class="lv wrap">${fmtClock(
              st.state,
            )} · ${fmtRelative(st.state)}</span></div>`
          : `<div class="line"><span class="lv wrap muted">no run scheduled</span></div>`;
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

    return `<ha-card class="tile${
      selected ? " selected" : ""
    }" data-tile="${entityId}"><div class="ti">
      <div class="top">
        <span class="name">${name}</span>
        ${toggle}
      </div>
      ${this._tankBar(item, a)}
      ${body}
    </div></ha-card>`;
  }

  // The single shared detail panel rendered below the grid for the selected tile:
  // the upcoming schedule (scheduler loads) plus a 24h activity timeline.
  _detail() {
    if (!this._selected) return "";
    const st = this._hass.states[this._selected];
    if (!st) return "";
    const a = st.attributes || {};
    const c = a.config || {};
    const item = this._entities().find((e) => e.entity === this._selected);
    const name =
      (item && item.name) || (a.friendly_name || this._selected).replace(/\s*schedule$/i, "");
    const isScheduler = Array.isArray(a.periods) && a.config && a.config.mode;
    // Clicking the title opens the controlled switch's more-info (or the
    // schedule sensor itself for informational loads with nothing to control).
    const moreEntity = c.controlled_entity || this._selected;
    let main = "";
    if (isScheduler) {
      const sym = currencySymbol(this._hass);
      const ps = a.periods || [];
      main += ps.length
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
      if (tot.length) main += `<div class="prow tot">${tot.join(" · ")}</div>`;
    }
    // The boost control fills the empty space beside the schedule list rather
    // than adding a row, so the panel keeps its height.
    const side = this._boostHtml(item, a);
    const cols = side
      ? `<div class="detail-cols"><div class="detail-main">${main}</div>${side}</div>`
      : main;
    return `<ha-card class="detail"><div class="detail-body">
      <div class="detail-head">
        <span class="detail-name" data-action="more-info" data-entity="${moreEntity}">${name} — ${
          isScheduler ? "schedule" : "activity"
        }</span>
        <span class="close" data-close="1">✕</span>
      </div>${cols}${this._timelineHtml()}</div></ha-card>`;
  }

  // How long a boost from this card should run: the per-entity override, else
  // the card-wide default, else what the integration itself would pick (the
  // load's target runtime, or DEFAULT_BOOST_MINUTES when that is zero). The
  // last case is left unset in the service call so the backend keeps deciding;
  // `effective` exists only so the caption can show a concrete number.
  _boostMinutes(item, a) {
    const pick = (v) => {
      const n = parseFloat(v);
      return n > 0 ? n : null;
    };
    const configured = pick(item && item.boost_minutes) || pick(this._config.boost_minutes);
    const target = parseFloat(a.target_minutes);
    return { configured, effective: configured || (target > 0 ? target : 60) };
  }

  // The combined boost status + action pill, with the duration underneath it.
  _boostHtml(item, a) {
    const ctl = loadControls(this._hass, this._selected);
    if (!ctl.boost) return ""; // informational load: nothing to boost
    const left = boostRemaining(a);
    const { configured, effective } = this._boostMinutes(item, a);
    const label = left ? `${fmtDuration(left)} left` : "Boost";
    const cap = left ? `until ${fmtClock(a.boost_until)}` : fmtDuration(effective);
    return `<div class="detail-side">
      <span class="bbtn${left ? " active" : ""}" data-action="boost"
        data-entity="${this._selected}" data-cancel="${ctl.boost}"
        data-minutes="${configured || ""}" data-on="${left ? "true" : "false"}"
        title="${left ? "Cancel the boost" : `Run now for ${fmtDuration(effective)}`}"
        >${BOLT_SVG}${label}</span>
      <span class="bcap">${cap}</span>
    </div>`;
  }

  _historyHours() {
    const h = Number(this._config.history_hours);
    return h > 0 ? Math.min(h, 168) : 24;
  }

  // The on/off (and, for a heater, heating/idle) timeline bar for the selection.
  _timelineHtml() {
    const hours = this._historyHours();
    const span = fmtDuration(hours * 60);
    const h = this._historyData;
    if (h && h.id === this._selected && h.none) return ""; // nothing to chart
    if (!h || h.id !== this._selected) {
      return `<div class="tlload">Loading last ${span} of activity…</div>`;
    }
    if (h.error) return `<div class="tlload">Activity history unavailable.</div>`;
    if (!h.segments || !h.segments.length) {
      return `<div class="tlload">No activity in the last ${span}.</div>`;
    }
    const segs = h.segments
      .map((s) => {
        const dur = Math.max(0, s.end - s.start);
        const range = `${fmtClock(new Date(s.start).toISOString())} – ${fmtClock(
          new Date(s.end).toISOString(),
        )}`;
        return `<span class="seg ${s.status}" style="flex:${dur}" data-status="${s.status}" data-range="${range}"></span>`;
      })
      .join("");
    // The tooltip is positioned/filled on hover or tap (see _showTip).
    return `<div class="tlwrap"><div class="tl">${segs}</div>
      <div class="tltip" hidden></div>
      <div class="tlcap"><span>${span} ago</span><span>now</span></div></div>`;
  }

  // Which real entities back the timeline, and how to read their status.
  _historyEntities(selectedId) {
    const st = this._hass.states[selectedId];
    if (!st) return null;
    const a = st.attributes || {};
    const c = a.config || {};
    if (Array.isArray(a.periods) && c.mode) {
      if (!c.controlled_entity) return null; // informational: nothing to chart
      return {
        controlled: c.controlled_entity,
        feedback: c.feedback_entity || null,
        idleW: Number(c.feedback_idle_w) || 0,
        mode: "scheduler",
      };
    }
    return { controlled: selectedId, feedback: null, idleW: 0, mode: "basic" };
  }

  async _loadHistory(selectedId) {
    const info = this._historyEntities(selectedId);
    if (!info) {
      this._historyData = { id: selectedId, none: true };
      return;
    }
    const hours = this._historyHours();
    const end = new Date();
    const start = new Date(end.getTime() - hours * 3600000);
    const ids = info.feedback ? [info.controlled, info.feedback] : [info.controlled];
    let data;
    try {
      const res = await this._hass.callWS({
        type: "history/history_during_period",
        start_time: start.toISOString(),
        end_time: end.toISOString(),
        entity_ids: ids,
        minimal_response: true,
        no_attributes: true,
        significant_changes_only: true,
      });
      if (this._selected !== selectedId) return; // selection changed mid-fetch
      const ctrl = this._normSeries(res[info.controlled]);
      const fb = info.feedback ? this._normSeries(res[info.feedback]) : null;
      data = {
        id: selectedId,
        segments: this._buildSegments(start.getTime(), end.getTime(), ctrl, fb, info),
      };
    } catch (err) {
      data = { id: selectedId, error: true };
    }
    if (this._selected !== selectedId) return;
    this._historyData = data;
    this._sig = null; // force a one-off rebuild to show the timeline
    this._render();
  }

  // Compressed history rows → time-ordered [{t(ms), state}].
  _normSeries(arr) {
    if (!Array.isArray(arr)) return [];
    const out = [];
    for (const e of arr) {
      const state = e.s != null ? e.s : e.state;
      if (state == null) continue;
      let t;
      if (e.lc != null) t = e.lc * 1000;
      else if (e.lu != null) t = e.lu * 1000;
      else if (e.last_changed) t = Date.parse(e.last_changed);
      else if (e.last_updated) t = Date.parse(e.last_updated);
      else continue;
      out.push({ t, state });
    }
    out.sort((x, y) => x.t - y.t);
    return out;
  }

  // Merge the controlled (on/off) and feedback (power, or on/off for a
  // binary_sensor) step-functions into off / idle / heating (or plain on/off
  // for a basic switch) segments. A dead feedback sample (unavailable/unknown/
  // not-yet-sampled) degrades to the no-feedback assumption below — heating —
  // rather than painting the whole bar idle.
  _buildSegments(start, end, ctrl, fb, info) {
    const times = new Set([start]);
    for (const e of ctrl) if (e.t > start && e.t < end) times.add(e.t);
    if (fb) for (const e of fb) if (e.t > start && e.t < end) times.add(e.t);
    const sorted = [...times].sort((a, b) => a - b);
    sorted.push(end);
    const valAt = (series, t) => {
      let v = null;
      for (const e of series) {
        if (e.t <= t) v = e.state;
        else break;
      }
      return v;
    };
    const segs = [];
    for (let i = 0; i < sorted.length - 1; i++) {
      const t0 = sorted[i];
      const t1 = sorted[i + 1];
      if (t1 <= t0) continue;
      const on = valAt(ctrl, t0) === "on";
      let status;
      if (!on) status = "off";
      else if (info.mode === "basic") status = "on";
      else if (fb) {
        const v = valAt(fb, t0);
        const p = parseFloat(v);
        if (!isNaN(p)) status = p >= info.idleW ? "heating" : "idle";
        else if (v === "on" || v === "heating") status = "heating";
        else if (v === "off") status = "idle";
        // Dead feedback (unavailable/unknown/no sample yet) degrades to the
        // no-feedback assumption — switch on ⇒ heating — never all-idle.
        else status = "heating";
      } else status = "heating";
      const last = segs[segs.length - 1];
      if (last && last.status === status) last.end = t1;
      else segs.push({ start: t0, end: t1, status });
    }
    return segs;
  }

  // (Re)start the auto-collapse countdown. Also called from the panel's own
  // controls, so interacting with one doesn't leave the user a second away
  // from the panel vanishing under them.
  _armCollapse() {
    if (this._timer) clearTimeout(this._timer);
    this._timer = setTimeout(() => {
      this._selected = null;
      this._timer = null;
      this._render();
    }, 60000);
  }

  // Only (re)arm the auto-collapse timer when the selection actually changes —
  // `_render` runs on every (frequent) hass update and must not reset it.
  _select(id) {
    this._selected = id;
    this._historyData = null; // show "loading" until the fetch returns
    this._tipPinned = false;
    if (this._timer) {
      clearTimeout(this._timer);
      this._timer = null;
    }
    if (id) {
      this._loadHistory(id); // async; re-renders the timeline when ready
      this._armCollapse();
    }
    this._render();
  }

  _onClick(e) {
    // Tap on a timeline segment → pin its tooltip (touch-friendly). Tap anywhere
    // else first dismisses a pinned tooltip.
    const seg = e.target.closest(".seg");
    if (seg) {
      this._tipPinned = true;
      this._showTip(seg, null);
      return;
    }
    if (this._tipPinned) {
      this._tipPinned = false;
      this._hideTip();
    }
    const action = e.target.closest("[data-action]");
    if (action) {
      e.stopPropagation(); // never open the detail panel from a control
      const entity = action.dataset.entity;
      if (action.dataset.action === "more-info") {
        if (entity) {
          this.dispatchEvent(
            new CustomEvent("hass-more-info", {
              detail: { entityId: entity },
              bubbles: true,
              composed: true,
            }),
          );
        }
        return;
      }
      if (action.dataset.action === "boost") {
        this._armCollapse();
        if (this._hass && entity) {
          if (action.dataset.on === "true") {
            // Only the button cancels: it also flags the manual stop so the
            // plan/divert don't re-grab the load. The service has no cancel.
            this._hass.callService("button", "press", { entity_id: action.dataset.cancel });
          } else {
            // The service resolves entity → device → load, so the schedule
            // sensor is a valid target. No `minutes` = the backend's own
            // default (the load's target runtime).
            const mins = parseFloat(action.dataset.minutes);
            const data = { entity_id: entity };
            if (mins > 0) data.minutes = mins;
            this._hass.callService("load_scheduler", "boost", data);
          }
        }
        return;
      }
      const on = action.dataset.on === "true";
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

  // Fill + position the activity-timeline tooltip over a segment. ``clientX``
  // follows the cursor on hover; null centres it on the segment (tap).
  _showTip(seg, clientX) {
    if (!this._root) return;
    const wrap = this._root.querySelector(".tlwrap");
    const tip = wrap && wrap.querySelector(".tltip");
    if (!tip) return;
    const status = seg.dataset.status;
    tip.innerHTML =
      `<span class="tipst"><span class="tipdot ${status}"></span>${status}</span>` +
      `${seg.dataset.range}`;
    const wr = wrap.getBoundingClientRect();
    const sr = seg.getBoundingClientRect();
    const x = (clientX != null ? clientX : sr.left + sr.width / 2) - wr.left;
    tip.hidden = false;
    const half = tip.offsetWidth / 2;
    tip.style.left = `${Math.max(half + 2, Math.min(x, wr.width - half - 2))}px`;
  }

  _hideTip() {
    const tip = this._root && this._root.querySelector(".tltip");
    if (tip) tip.hidden = true;
  }

  _onPointerMove(e) {
    if (e.pointerType === "touch") return; // touch uses tap-to-pin
    const tip = this._root && this._root.querySelector(".tltip");
    if (!tip) return;
    const seg = e.target.closest && e.target.closest(".seg");
    if (seg) this._showTip(seg, e.clientX);
    else if (!this._tipPinned) tip.hidden = true;
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
          a.boost_until,
          c.controlled_entity,
          c.mode,
          periods.map((p) => `${p.start}-${p.end}`).join(","),
        ].join("|"),
      );
      if (it.tank_charge) {
        const ts = this._hass.states[it.tank_charge];
        if (ts) {
          const ta = ts.attributes || {};
          parts.push(
            `tank:${Math.round(Number(ts.state))}|${ta.calibrated}|${Math.round(
              Number(ta.showers_left),
            )}`,
          );
        } else {
          parts.push("tank:missing");
        }
      }
    }
    return parts.join("§");
  }

  _render() {
    if (!this._hass || !this._config) return;
    if (!this._root) {
      // No outer ha-card: a plain, transparent container, so each tile is its
      // own themed <ha-card> with no surrounding border.
      this._root = document.createElement("div");
      this.appendChild(this._root);
      this._root.addEventListener("click", (e) => this._onClick(e));
      this._root.addEventListener("pointermove", (e) => this._onPointerMove(e));
      this._root.addEventListener("pointerleave", () => {
        if (!this._tipPinned) this._hideTip();
      });
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
    this._root.innerHTML = `<style>${CARD_CSS}</style>${title}${grid}${this._detail()}`;
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

  // Normalised, ordered list of {entity, name?} — same contract as the compact
  // card, so a config written for one card opens in the other.
  _entities() {
    const list = Array.isArray(this._config.entities) ? this._config.entities : null;
    const raw = list && list.length ? list : discoverScheduleEntities(this._hass);
    return raw
      .map((e) => (typeof e === "string" ? { entity: e } : e))
      .filter((e) => e && e.entity);
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
      const left = boostRemaining(a);
      const mins = parseFloat(this._opt("boost_minutes", 0));
      parts.push(
        `<span class="btn${left ? " active" : ""}" data-action="boost" data-entity="${
          entityId
        }" data-cancel="${ctl.boost}" data-minutes="${mins > 0 ? mins : ""}" data-on="${
          left ? "true" : "false"
        }">${left ? `Boosting · ${fmtDuration(left)} left` : "Boost"}</span>`,
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

  _panel(item, sym, first) {
    const entityId = item.entity;
    const st = this._hass.states[entityId];
    if (!st) {
      const label = item.name || entityId;
      return `<div class="panel missing${first ? " first" : ""}">${label} (unavailable)</div>`;
    }
    const a = st.attributes || {};
    const c = a.config || {};
    const name = item.name || (a.friendly_name || entityId).replace(/\s*schedule$/i, "");
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
      if (el.dataset.on === "true") {
        // Cancelling goes through the button: it also flags the manual stop.
        this._hass.callService("button", "press", { entity_id: el.dataset.cancel });
      } else {
        const mins = parseFloat(el.dataset.minutes);
        const data = { entity_id: entity };
        if (mins > 0) data.minutes = mins;
        this._hass.callService("load_scheduler", "boost", data);
      }
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
 * UI editors (everything goes through HA's <ha-form>)
 * ------------------------------------------------------------------ */

// Every field below is an <ha-form> selector, never a bare <ha-textfield>: that
// element is only defined if something else on the page happened to load its
// chunk, and an *undefined* custom element renders as an inert zero-size box —
// which is how the title / hours / boost / name fields silently disappeared from
// this editor while the <ha-entity-picker> next to them rendered fine. <ha-form>
// is pulled in by the card-editor dialog itself, so it is always there.

const COMPACT_SCHEMA = [
  { name: "title", selector: { text: {} } },
  {
    name: "history_hours",
    selector: { number: { min: 1, max: 168, step: 1, mode: "box", unit_of_measurement: "h" } },
  },
  {
    name: "boost_minutes",
    selector: { number: { min: 5, max: 1440, step: 5, mode: "box", unit_of_measurement: "min" } },
  },
];

// `name: ""` makes <ha-form> splice the grid's value straight into the row
// object, so the grid is pure layout and a row stays flat {name, tank_charge,
// boost_minutes} — exactly what the entities list serialises.
const COMPACT_ROW_SCHEMA = [
  {
    name: "",
    type: "grid",
    column_min_width: "140px",
    schema: [
      { name: "name", selector: { text: {} } },
      { name: "tank_charge", selector: { entity: { filter: { domain: "sensor" } } } },
      {
        name: "boost_minutes",
        selector: {
          number: { min: 5, max: 1440, step: 5, mode: "box", unit_of_measurement: "min" },
        },
      },
    ],
  },
];

const DIAG_ROW_SCHEMA = [{ name: "name", selector: { text: {} } }];

// The compact card also tiles plain switches/lights, so its picker stays
// unfiltered; the diagnostic card needs our rationale attributes, so it filters.
const ANY_ENTITY_SELECTOR = { entity: {} };
const SCHEDULE_ENTITY_SELECTOR = {
  entity: { filter: { integration: "load_scheduler", domain: "sensor" } },
};

// Belt and braces for the failure above: if <ha-form> somehow still isn't
// defined when an editor mounts, touching a built-in card's editor loads the
// chunk that defines it. Memoised and fully guarded — on failure we render anyway.
let _editorElementsLoaded = null;
function ensureEditorElements() {
  if (customElements.get("ha-form")) return Promise.resolve();
  if (!_editorElementsLoaded) {
    _editorElementsLoaded = (async () => {
      const helpers = await window.loadCardHelpers();
      const card = await helpers.createCardElement({ type: "entities", entities: [] });
      const cls = card && card.constructor;
      if (cls && cls.getConfigElement) await cls.getConfigElement();
    })().catch(() => {});
  }
  return _editorElementsLoaded;
}

// Shared editor: a top-level <ha-form> plus (optionally) an ordered list of
// entity rows, each its own small <ha-form>. Subclasses only supply schemas.
class LoadSchedulerCardEditorBase extends HTMLElement {
  setConfig(config) {
    this._config = config || {};
    const json = JSON.stringify(this._config);
    // The dialog echoes every config we emit back at us, and ha-form's text and
    // number selectors fire on *every keystroke* — so an echo can arrive a
    // keystroke stale. Matching only the last emit would miss it and rebuild the
    // DOM under the cursor, hence a short ring of what we sent.
    if (this._sent && this._sent.indexOf(json) !== -1) return;
    if (!this._built) {
      if (this._hass) this._build();
      return;
    }
    // A genuinely external edit (the YAML tab). Rebuild — but never mid-word.
    if (this._focusWithin) {
      this._pendingRebuild = true;
      return;
    }
    this._build();
  }

  connectedCallback() {
    if (this._focusBound) return;
    this._focusBound = true;
    // focusin/focusout compose out of ha-form's shadow roots, so the host can
    // tell whether a rebuild would steal the caret.
    this.addEventListener("focusin", () => {
      this._focusWithin = true;
    });
    this.addEventListener("focusout", () => {
      this._focusWithin = false;
      if (this._pendingRebuild) {
        this._pendingRebuild = false;
        this._build();
      }
    });
  }

  set hass(hass) {
    this._hass = hass;
    // Only .hass here — .data is owned by _emit, which keeps it in step with
    // what was typed.
    (this._forms || []).forEach((f) => (f.hass = hass));
    if (!this._built && this._config) this._build();
  }

  /* ---- subclass hooks ---- */
  _schema() {
    return [];
  }
  _labels() {
    return {};
  }
  _rowSchema() {
    return null; // null → this card has no per-entity list
  }
  _rowLabels() {
    return {};
  }
  _rowHelper() {
    return "";
  }
  _rowKeys() {
    return [];
  }
  _addSelector() {
    return ANY_ENTITY_SELECTOR;
  }
  _hint() {
    return "";
  }

  /* ---- shared implementation ---- */

  _makeForm(schema, data, onChange, computeLabel, computeHelper) {
    const form = document.createElement("ha-form");
    form.hass = this._hass;
    form.schema = schema;
    form.data = data;
    if (computeLabel) form.computeLabel = computeLabel;
    if (computeHelper) form.computeHelper = computeHelper;
    form.addEventListener("value-changed", (ev) => {
      ev.stopPropagation();
      onChange(ev.detail.value, form);
    });
    (this._forms = this._forms || []).push(form);
    return form;
  }

  // ha-form hands back `undefined` for a cleared field; keep those (and the
  // empty strings) out of the saved config so an untouched card stays a minimal
  // YAML block. `false` and `0` are real values and must survive.
  _clean(obj) {
    const out = {};
    Object.keys(obj).forEach((k) => {
      const v = obj[k];
      if (v === undefined || v === null || v === "") return;
      out[k] = v;
    });
    return out;
  }

  // Working list of {entity, ...rowKeys}; from config, else the auto-discovered
  // set so the user can reorder/rename the defaults straight away.
  _syncWorking() {
    const list = Array.isArray(this._config.entities) ? this._config.entities : null;
    const raw = list && list.length ? list : discoverScheduleEntities(this._hass);
    const keys = this._rowKeys();
    this._working = raw
      .map((e) => (typeof e === "string" ? { entity: e } : e))
      .filter((e) => e && e.entity)
      .map((e) => {
        const item = { entity: e.entity };
        keys.forEach((k) => {
          if (e[k] !== undefined) item[k] = e[k];
        });
        return item;
      });
  }

  _serialiseEntities() {
    return (this._working || []).map((e) => {
      // An entry with no extras collapses back to a bare entity id, so a
      // hand-written YAML list survives a trip through the editor unchanged.
      const obj = { entity: e.entity };
      this._rowKeys().forEach((k) => {
        const v = e[k];
        if (typeof v === "number") {
          if (v > 0) obj[k] = v;
        } else if (v != null && String(v).trim()) {
          obj[k] = String(v).trim();
        }
      });
      return Object.keys(obj).length === 1 ? e.entity : obj;
    });
  }

  // The row data a row form displays: only the row keys, never `entity`.
  _rowData(item) {
    const data = {};
    this._rowKeys().forEach((k) => {
      if (item[k] !== undefined) data[k] = item[k];
    });
    return data;
  }

  _emit(patch) {
    const next = this._clean({ ...this._config, ...(patch || {}) });
    if (this._rowSchema()) {
      const entities = this._serialiseEntities();
      if (entities.length) next.entities = entities;
      else delete next.entities;
    }
    this._config = next;
    // Keep the top form's .data in step: <ha-form> is a controlled component, so
    // a stale .data would revert the *other* fields on the next keystroke.
    // Re-assigning the value a field already shows doesn't move its caret.
    if (this._topForm) this._topForm.data = next;
    this._sent = (this._sent || []).concat(JSON.stringify(next)).slice(-16);
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
    const [item] = this._working.splice(i, 1);
    this._working.splice(j, 0, item);
    this._emit();
    this._build();
  }

  _miniButton(glyph, title, disabled, onClick) {
    const b = document.createElement("button");
    b.textContent = glyph;
    b.title = title;
    b.setAttribute("aria-label", title);
    b.disabled = !!disabled;
    b.style.cssText =
      "flex:0 0 auto;width:28px;height:28px;line-height:1;border:none;border-radius:6px;" +
      "background:var(--secondary-background-color);color:var(--primary-text-color);" +
      `cursor:${disabled ? "default" : "pointer"};opacity:${disabled ? 0.4 : 1};`;
    b.addEventListener("click", onClick);
    return b;
  }

  _rowEl(item, i) {
    const st = this._hass.states[item.entity];
    const friendly = (st && st.attributes && st.attributes.friendly_name) || item.entity;

    const box = document.createElement("div");
    box.style.cssText =
      "border:1px solid var(--divider-color);border-radius:8px;padding:6px 8px;" +
      "display:flex;flex-direction:column;gap:4px;";

    const head = document.createElement("div");
    head.style.cssText = "display:flex;align-items:center;gap:6px;";
    head.appendChild(this._miniButton("↑", "Move up", i === 0, () => this._move(i, -1)));
    head.appendChild(
      this._miniButton("↓", "Move down", i === this._working.length - 1, () => this._move(i, 1)),
    );
    const info = document.createElement("div");
    info.style.cssText = "flex:1 1 auto;min-width:0;overflow:hidden;";
    info.innerHTML =
      `<div style="font-size:0.86em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${friendly}</div>` +
      `<div style="font-size:0.72em;color:var(--secondary-text-color);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${item.entity}</div>`;
    head.appendChild(info);
    head.appendChild(
      this._miniButton("✕", "Remove", false, () => {
        this._working.splice(i, 1);
        this._emit();
        this._build();
      }),
    );
    box.appendChild(head);

    box.appendChild(
      this._makeForm(
        this._rowSchema(),
        this._rowData(item),
        (v, form) => {
          // Mutate the working row and re-hand it to the form; never rebuild the
          // DOM here — that is what would drop the caret mid-word.
          Object.assign(this._working[i], v);
          this._emit();
          form.data = this._rowData(this._working[i]);
        },
        (s) => this._rowLabels()[s.name] || s.name,
        (s) => this._rowHelper(item, s, friendly),
      ),
    );
    return box;
  }

  _build() {
    if (!this._hass || !this._config) return;
    if (!customElements.get("ha-form") && !this._elementsTried) {
      // The editor chunk hasn't loaded yet; build again once it has (one retry,
      // so a failure degrades to rendering whatever is available).
      this._elementsTried = true;
      ensureEditorElements().then(() => this._build());
      return;
    }
    this._syncWorking();
    this.innerHTML = "";
    this._forms = [];
    this._built = true;

    const wrap = document.createElement("div");
    wrap.style.cssText = "display:flex;flex-direction:column;gap:10px;padding:4px 0;";

    const schema = this._schema();
    if (schema.length) {
      this._topForm = this._makeForm(
        schema,
        this._config,
        (v) => this._emit(v),
        (s) => this._labels()[s.name] || s.name,
      );
      wrap.appendChild(this._topForm);
    }

    if (this._rowSchema()) {
      const hint = document.createElement("div");
      hint.textContent = this._hint();
      hint.style.cssText = "font-size:0.82em;color:var(--secondary-text-color);";
      wrap.appendChild(hint);

      this._working.forEach((item, i) => wrap.appendChild(this._rowEl(item, i)));

      wrap.appendChild(
        this._makeForm(
          [{ name: "entity", selector: this._addSelector() }],
          {},
          (v) => {
            const id = v && v.entity;
            if (!id) return;
            this._working.push({ entity: id });
            this._emit();
            this._build(); // structural change — nothing is focused mid-gesture
          },
          () => "Add entity",
        ),
      );
    }

    this.appendChild(wrap);
  }
}

class LoadSchedulerCardEditor extends LoadSchedulerCardEditorBase {
  _schema() {
    return COMPACT_SCHEMA;
  }
  _labels() {
    return {
      title: "Title (optional)",
      history_hours: "Activity timeline hours",
      boost_minutes: "Boost duration (default for all loads)",
    };
  }
  _rowSchema() {
    return COMPACT_ROW_SCHEMA;
  }
  _rowLabels() {
    return { name: "Name", tank_charge: "Tank charge sensor", boost_minutes: "Boost" };
  }
  _rowHelper(item, schema, friendly) {
    if (schema.name === "name") return friendly;
    if (schema.name === "boost_minutes") return "card default";
    return "";
  }
  _rowKeys() {
    return ["name", "tank_charge", "boost_minutes"];
  }
  _hint() {
    return (
      "Entities — reorder with the arrows, set an optional display name, " +
      "tank-charge sensor and boost duration:"
    );
  }
}

class LoadSchedulerDiagnosticCardEditor extends LoadSchedulerCardEditorBase {
  // Seed the display toggles to their defaults so the form mirrors what the
  // card actually shows (the card defaults every show_* to on, compact to off).
  setConfig(config) {
    super.setConfig({
      compact: false,
      show_rationale: true,
      show_targets: true,
      show_config: true,
      show_costs: true,
      show_controls: true,
      ...config,
    });
  }

  _schema() {
    return [
      { name: "title", selector: { text: {} } },
      { name: "compact", selector: { boolean: {} } },
      { name: "show_rationale", selector: { boolean: {} } },
      { name: "show_targets", selector: { boolean: {} } },
      { name: "show_config", selector: { boolean: {} } },
      { name: "show_costs", selector: { boolean: {} } },
      { name: "show_controls", selector: { boolean: {} } },
      {
        name: "boost_minutes",
        selector: {
          number: { min: 5, max: 1440, step: 5, mode: "box", unit_of_measurement: "min" },
        },
      },
    ];
  }

  _labels() {
    return {
      title: "Title (optional)",
      compact: "Compact (collapse to rows)",
      show_rationale: "Show plain-English rationale",
      show_targets: "Show targets math",
      show_config: "Show configuration",
      show_costs: "Show schedule & cost",
      show_controls: "Show controls",
      boost_minutes: "Boost duration (blank = the load's target runtime)",
    };
  }

  _rowSchema() {
    return DIAG_ROW_SCHEMA;
  }
  _rowLabels() {
    return { name: "Name" };
  }
  _rowHelper(item, schema, friendly) {
    return schema.name === "name" ? friendly : "";
  }
  _rowKeys() {
    return ["name"];
  }
  _addSelector() {
    return SCHEDULE_ENTITY_SELECTOR;
  }
  _hint() {
    return "Loads — reorder with the arrows, set an optional display name:";
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
