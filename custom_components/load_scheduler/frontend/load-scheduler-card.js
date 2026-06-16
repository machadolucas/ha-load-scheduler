/*
 * Load Scheduler card — a very compact, dependency-free Lovelace card.
 *
 * One tight row per load: name · next run (relative) · duration · avg price ·
 * a small grid/solar badge. Tap a row to expand its upcoming periods.
 *
 * Config:
 *   type: custom:load-scheduler-card
 *   title: Loads            # optional
 *   entities:               # the per-load `schedule` sensors
 *     - sensor.water_heater_schedule
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

function fmtWhen(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  const now = new Date();
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const sameDay = d.toDateString() === now.toDateString();
  const tomorrow = new Date(now);
  tomorrow.setDate(now.getDate() + 1);
  if (sameDay) return `today ${time}`;
  if (d.toDateString() === tomorrow.toDateString()) return `tomorrow ${time}`;
  return `${d.toLocaleDateString([], { weekday: "short" })} ${time}`;
}

function fmtPrice(eurPerKwh) {
  if (eurPerKwh == null) return "";
  return `${(eurPerKwh * 100).toFixed(1)}c`;
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
    const name = a.friendly_name || entityId;
    const running = a.running;
    const totalMin = periods.reduce(
      (s, p) => s + (new Date(p.end) - new Date(p.start)) / 60000,
      0,
    );
    const sources = new Set(periods.map((p) => p.source));
    const src = sources.size > 1 ? "mixed" : [...sources][0] || "grid";
    const avg =
      periods.length &&
      periods.reduce((s, p) => s + (p.avg_cost || 0), 0) / periods.length;

    let when;
    if (a.status && a.status !== "ok") when = a.status;
    else if (running) when = `now → ${fmtWhen(a.current_period_end)}`;
    else if (st.state && st.state !== "unknown") when = fmtWhen(st.state);
    else when = "idle";

    const expanded = this._expanded.has(entityId);
    const detail =
      expanded && periods.length
        ? `<div class="detail">${periods
            .map(
              (p) =>
                `<div>${fmtWhen(p.start)} → ${fmtWhen(p.end)} · ${fmtPrice(
                  p.avg_cost,
                )} ${SOURCE_ICON[p.source] || ""}</div>`,
            )
            .join("")}</div>`
        : "";

    return `
      <div class="row ${running ? "on" : ""}" data-entity="${entityId}">
        <span class="dot"></span>
        <span class="name">${name}</span>
        <span class="when">${when}</span>
        <span class="dur">${periods.length ? fmtDuration(totalMin) : ""}</span>
        <span class="price">${periods.length ? fmtPrice(avg) : ""}</span>
        <span class="badge">${periods.length ? SOURCE_ICON[src] || "" : ""}</span>
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
        this._expanded.has(id)
          ? this._expanded.delete(id)
          : this._expanded.add(id);
        this._render();
      });
    }
    const title = this._config.title
      ? `<div class="title">${this._config.title}</div>`
      : "";
    this._card.innerHTML = `
      <style>
        .title { font-weight: 600; padding: 8px 12px 0; }
        .row { display: flex; align-items: center; gap: 8px; padding: 6px 12px;
               cursor: pointer; font-size: 0.95em; }
        .row .name { flex: 1 1 auto; font-weight: 500; }
        .row .when { color: var(--secondary-text-color); }
        .row .dur, .row .price { color: var(--secondary-text-color);
               font-variant-numeric: tabular-nums; }
        .row .dot { width: 8px; height: 8px; border-radius: 50%;
               background: var(--disabled-text-color); flex: 0 0 auto; }
        .row.on .dot { background: var(--success-color, #4caf50); }
        .row.missing { color: var(--error-color); }
        .detail { padding: 0 12px 6px 28px; color: var(--secondary-text-color);
               font-size: 0.85em; }
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
