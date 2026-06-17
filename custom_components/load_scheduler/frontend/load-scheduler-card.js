/*
 * Load Scheduler cards — two dependency-free Lovelace cards in one bundle.
 *
 * 1. `custom:load-scheduler-card` — a very compact "next run" view: one tight
 *    row per load (state dot · name · "next in …" · duration · grid/solar badge ·
 *    expand chevron). Tap a row to expand its periods.
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

class LoadSchedulerCard extends HTMLElement {
  // `entities` is optional now (auto-discovered when omitted) so the UI editor
  // can start from an empty config without throwing.
  setConfig(config) {
    this._config = config || {};
    this._expanded = new Set();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  _entities() {
    const list = Array.isArray(this._config.entities) ? this._config.entities : null;
    return list && list.length ? list : discoverScheduleEntities(this._hass);
  }

  getCardSize() {
    return 1 + (this._entities().length || 1);
  }

  // Sections (grid) view: resizable, and narrower than a full section if wanted.
  getGridOptions() {
    return { columns: 12, min_columns: 3, rows: "auto" };
  }

  static getConfigElement() {
    return document.createElement("load-scheduler-card-editor");
  }

  static getStubConfig(hass) {
    return { entities: discoverScheduleEntities(hass) };
  }

  _row(entityId) {
    const st = this._hass.states[entityId];
    if (!st) return `<div class="row missing">${entityId} (unavailable)</div>`;
    const a = st.attributes || {};
    const periods = a.periods || [];
    const name = (a.friendly_name || entityId).replace(/\s*schedule$/i, "");
    const totalMin = periods.reduce(
      (s, p) => s + (new Date(p.end) - new Date(p.start)) / 60000,
      0,
    );
    const sources = new Set(periods.map((p) => p.source));
    const src = sources.size > 1 ? "mixed" : [...sources][0] || "grid";

    let when;
    if (a.status && a.status !== "ok") when = a.status;
    else if (a.running) when = "now";
    else if (st.state && st.state !== "unknown") when = fmtRelative(st.state);
    else when = "idle";

    const expanded = this._expanded.has(entityId);
    const detail =
      expanded && periods.length
        ? `<div class="detail">${periods
            .map(
              (p) =>
                `<div>${fmtClock(p.start)} → ${fmtClock(p.end)} ${
                  SOURCE_ICON[p.source] || ""
                }</div>`,
            )
            .join("")}</div>`
        : "";

    return `
      <div class="row${expanded ? " expanded" : ""}" data-entity="${entityId}">
        <span class="dot ${dotClass(a)}"></span>
        <span class="name">${name}</span>
        <span class="when">${when}</span>
        <span class="dur">${periods.length ? fmtDuration(totalMin) : ""}</span>
        <span class="badge">${periods.length ? SOURCE_ICON[src] || "" : ""}</span>
        <span class="chev">${periods.length ? "›" : ""}</span>
      </div>${detail}`;
  }

  _render() {
    if (!this._hass || !this._config) return;
    if (!this._card) {
      this._card = document.createElement("ha-card");
      this.appendChild(this._card);
      this._card.addEventListener("click", (e) => {
        const row = e.target.closest(".row");
        if (!row || !row.dataset.entity) return;
        const id = row.dataset.entity;
        this._expanded.has(id) ? this._expanded.delete(id) : this._expanded.add(id);
        this._render();
      });
    }
    const title = this._config.title
      ? `<div class="title">${this._config.title}</div>`
      : "";
    const entities = this._entities();
    const rows = entities.length
      ? entities.map((e) => this._row(e)).join("")
      : `<div class="hint">No Load Scheduler schedule sensors found — pick them in the card editor.</div>`;
    this._card.innerHTML = `
      <style>
        .title { font-weight: 600; padding: 8px 12px 2px; }
        .hint { color: var(--secondary-text-color); padding: 8px 12px; font-size: 0.9em; }
        .row { display: flex; flex-wrap: nowrap; align-items: center; gap: 8px;
               padding: 3px 12px; cursor: pointer; font-size: 0.95em; line-height: 1.25; }
        .row .name { flex: 1 1 auto; min-width: 0; font-weight: 500;
               white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .row .when { color: var(--secondary-text-color); white-space: nowrap; }
        .row .dur { color: var(--secondary-text-color); white-space: nowrap;
               font-variant-numeric: tabular-nums; min-width: 2.5em; text-align: right; }
        .row .badge { white-space: nowrap; text-align: right; flex: 0 0 auto; }
        .row .chev { color: var(--secondary-text-color); width: 0.8em;
               transition: transform 0.15s; }
        .row.expanded .chev { transform: rotate(90deg); }
        .row .dot { width: 9px; height: 9px; border-radius: 50%;
               background: var(--disabled-text-color); flex: 0 0 auto; }
        .row .dot.heating { background: #ff9800; }
        .row .dot.idle { background: #ffe082; }
        .row.missing { color: var(--error-color); padding: 6px 12px; }
        .detail { padding: 0 12px 4px 29px; color: var(--secondary-text-color);
               font-size: 0.85em; line-height: 1.4; font-variant-numeric: tabular-nums; }
      </style>
      ${title}
      ${rows}`;
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

const DIAG_CSS = `
  .title { font-weight: 600; padding: 10px 12px 2px; }
  .hint { color: var(--secondary-text-color); padding: 10px 12px; font-size: 0.9em; }
  .panel { padding: 8px 12px; border-top: 1px solid var(--divider-color, rgba(127,127,127,0.2)); }
  .panel.first { border-top: none; }
  .panel.missing { color: var(--error-color); }
  .head { display: flex; flex-wrap: nowrap; align-items: center; gap: 8px; }
  .head .name { flex: 1 1 auto; min-width: 0; font-weight: 600;
         white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .head .when { color: var(--secondary-text-color); white-space: nowrap; font-size: 0.9em; }
  .badge-mode { font-size: 0.68em; text-transform: uppercase; letter-spacing: 0.04em;
         padding: 1px 6px; border-radius: 8px; background: var(--secondary-background-color);
         color: var(--secondary-text-color); white-space: nowrap; flex: 0 0 auto; }
  .dot { width: 9px; height: 9px; border-radius: 50%;
         background: var(--disabled-text-color); flex: 0 0 auto; }
  .dot.heating { background: #ff9800; }
  .dot.idle { background: #ffe082; }
  .sec { margin-top: 7px; }
  .sec .lbl { font-size: 0.68em; text-transform: uppercase; letter-spacing: 0.05em;
         color: var(--secondary-text-color); margin-bottom: 2px; }
  .kv { display: grid; grid-template-columns: auto 1fr; gap: 0 10px;
         font-size: 0.88em; line-height: 1.45; }
  .kv .k { color: var(--secondary-text-color); white-space: nowrap; }
  .kv .v { font-variant-numeric: tabular-nums; }
  .wiring { font-size: 0.78em; color: var(--secondary-text-color); margin-top: 3px;
         word-break: break-all; }
  .periods { font-size: 0.85em; line-height: 1.5; font-variant-numeric: tabular-nums; }
  .periods .tot { color: var(--secondary-text-color); margin-top: 2px; }
  .periods .k { color: var(--secondary-text-color); }
  .controls { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 9px; align-items: center; }
  .btn { cursor: pointer; border: 1px solid var(--divider-color, rgba(127,127,127,0.4));
         background: var(--card-background-color); color: var(--primary-text-color);
         border-radius: 14px; padding: 3px 11px; font-size: 0.85em; user-select: none;
         white-space: nowrap; }
  .btn:hover { background: var(--secondary-background-color); }
  .btn.on { background: var(--primary-color); color: var(--text-primary-color, #fff);
         border-color: var(--primary-color); }
  .btn.active { background: #ff9800; border-color: #ff9800; color: #fff; }
  .stepper { display: inline-flex; align-items: center; gap: 4px;
         border: 1px solid var(--divider-color, rgba(127,127,127,0.4)); border-radius: 14px;
         padding: 1px 4px; }
  .stepper .sbtn { cursor: pointer; width: 20px; text-align: center; user-select: none;
         font-weight: 600; font-size: 1.05em; }
  .stepper .sval { font-variant-numeric: tabular-nums; min-width: 4em; text-align: center;
         font-size: 0.85em; }
  .row { display: flex; flex-wrap: nowrap; align-items: center; gap: 8px;
         cursor: pointer; font-size: 0.95em; line-height: 1.3; }
  .row .name { flex: 1 1 auto; min-width: 0; font-weight: 500;
         white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .row .when { color: var(--secondary-text-color); white-space: nowrap; font-size: 0.9em; }
  .row .chev { color: var(--secondary-text-color); width: 0.8em; transition: transform 0.15s; }
  .row.expanded .chev { transform: rotate(90deg); }
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

    if (compact) {
      const expanded = this._expanded.has(entityId);
      const body = expanded
        ? `<div style="margin-top:4px">${this._sections(entityId, a, sym)}</div>`
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

    const head = `<div class="head">
        <span class="dot ${dotClass(a)}"></span>
        <span class="name">${name}</span>
        <span class="badge-mode">${mode}</span>
        <span class="when">${whenText(st, a)}</span>
      </div>`;
    return `<div class="panel${first ? " first" : ""}">${head}${this._sections(entityId, a, sym)}</div>`;
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

class LoadSchedulerCardEditor extends LoadSchedulerCardEditorBase {
  _schema() {
    return [
      { name: "title", selector: { text: {} } },
      { name: "entities", selector: ENTITIES_SELECTOR },
    ];
  }
  _labels() {
    return { title: "Title (optional)", entities: "Schedule sensors (auto if empty)" };
  }
}

class LoadSchedulerDiagnosticCardEditor extends LoadSchedulerCardEditorBase {
  // Seed the display toggles to their defaults so the form mirrors what the
  // card actually shows (the card defaults every show_* to on, compact to off).
  setConfig(config) {
    this._config = {
      compact: false,
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
