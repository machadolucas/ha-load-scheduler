/*
 * Load Scheduler card — a very compact, dependency-free Lovelace card.
 *
 * One tight row per load: a state dot · name · "next in …" · duration · a
 * grid/solar badge · an expand chevron. Tap a row to expand its periods.
 *
 * Dot colour: orange = actually heating (element drawing power), light yellow =
 * powered but idle (on, element satisfied), grey = off.
 *
 * Config:
 *   type: custom:load-scheduler-card
 *   title: Loads            # optional
 *   entities:               # the per-load `…_schedule` sensors
 *     - sensor.water_heater_lvv_schedule
 *     - sensor.dishwasher_schedule
 */

const SOURCE_ICON = { solar: "☀", grid: "⚡", mixed: "☀⚡" };

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

class LoadSchedulerCard extends HTMLElement {
  setConfig(config) {
    if (!config || !Array.isArray(config.entities) || !config.entities.length) {
      throw new Error("Define `entities` (the per-load schedule sensors).");
    }
    this._config = config;
    this._expanded = new Set();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  getCardSize() {
    return 1 + (this._config?.entities?.length || 1);
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

    // Dot: orange = heating, light yellow = on-but-idle, grey = off.
    let dotCls = "off";
    if (a.heating === true) dotCls = "heating";
    else if (a.active === true) dotCls = a.heating === false ? "idle" : "heating";

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
        <span class="dot ${dotCls}"></span>
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
    this._card.innerHTML = `
      <style>
        .title { font-weight: 600; padding: 8px 12px 2px; }
        .row { display: flex; flex-wrap: nowrap; align-items: center; gap: 8px;
               padding: 3px 12px; cursor: pointer; font-size: 0.95em; line-height: 1.25; }
        .row .name { flex: 1 1 auto; font-weight: 500; white-space: nowrap;
               overflow: hidden; text-overflow: ellipsis; }
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
      ${this._config.entities.map((e) => this._row(e)).join("")}`;
  }
}

customElements.define("load-scheduler-card", LoadSchedulerCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: "load-scheduler-card",
  name: "Load Scheduler Card",
  description: "Compact upcoming-runs view for Load Scheduler loads.",
});
