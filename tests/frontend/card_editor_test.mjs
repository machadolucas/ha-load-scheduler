/*
 * Headless checks for the bundled Lovelace cards' UI editors.
 *
 * The card bundle is plain browser JS with no build step and no test runner, so
 * this stubs just enough DOM (document.createElement, customElements, a host
 * HTMLElement with event plumbing) to load the bundle and drive the editors the
 * way Home Assistant's card-editor dialog does: set hass, set the config, fire
 * `value-changed` off an <ha-form>, and feed every emitted `config-changed`
 * straight back in as the dialog's echo.
 *
 * What it guards is the class of bug that shipped in 0.15.0: an editor field
 * that renders as nothing (an undefined custom element is an inert zero-size
 * box), and the ha-form-specific hazards behind it — a stale `.data` reverting
 * the other fields on the next keystroke, and the echoed config rebuilding the
 * DOM under the cursor.
 *
 * Run directly (`node tests/frontend/card_editor_test.mjs`) or via pytest,
 * which wraps it in tests/test_frontend_card.py.
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const BUNDLE = path.join(
  HERE,
  "..",
  "..",
  "custom_components",
  "load_scheduler",
  "frontend",
  "load-scheduler-card.js",
);

/* ---- minimal DOM ---- */

const registry = new Map();

class El {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.style = { cssText: "" };
    this.dataset = {};
    this._listeners = {};
  }
  appendChild(child) {
    this.children.push(child);
    return child;
  }
  addEventListener(type, fn) {
    (this._listeners[type] = this._listeners[type] || []).push(fn);
  }
  removeEventListener() {}
  dispatchEvent(ev) {
    (this._listeners[ev.type] || []).forEach((fn) => fn(ev));
    return true;
  }
  setAttribute() {}
  getAttribute() {
    return null;
  }
  set innerHTML(v) {
    this._html = v;
    if (v === "") this.children = [];
  }
  get innerHTML() {
    return this._html || "";
  }
  // Test helpers.
  fire(type, detail) {
    this.dispatchEvent({ type, detail, stopPropagation() {} });
  }
  descendants() {
    return this.children.flatMap((c) => [c, ...(c.descendants ? c.descendants() : [])]);
  }
  forms() {
    return this.descendants().filter((e) => e.tagName === "ha-form");
  }
}

class HostElement extends El {
  constructor() {
    super("host");
  }
}

globalThis.HTMLElement = HostElement;
globalThis.document = { createElement: (tag) => new El(tag) };
globalThis.customElements = {
  // ha-form must look defined: the editors fall back to loadCardHelpers()
  // otherwise, which is browser-only.
  get: (name) => (name === "ha-form" ? function HaForm() {} : registry.get(name)),
  define: (name, cls) => registry.set(name, cls),
};
globalThis.window = { customCards: [] };
globalThis.CustomEvent = class CustomEventStub {
  constructor(type, init) {
    this.type = type;
    Object.assign(this, init);
  }
};

new Function(fs.readFileSync(BUNDLE, "utf8"))();

/* ---- fixtures ---- */

const scheduleState = (friendlyName) => ({
  state: "on",
  attributes: {
    friendly_name: friendlyName,
    periods: [],
    config: { mode: "cheapest" },
  },
});

const hass = {
  config: { currency: "EUR" },
  states: {
    "sensor.a_schedule": scheduleState("A Schedule"),
    "sensor.b_schedule": scheduleState("B Schedule"),
    "switch.plug": { state: "off", attributes: { friendly_name: "Plug" } },
  },
};

// Mount an editor the way the dialog does, echoing every emit back at it.
function mountEditor(tagName, config, h = hass) {
  const Cls = registry.get(tagName);
  const el = new Cls();
  if (el.connectedCallback) el.connectedCallback();
  const emitted = [];
  el.addEventListener("config-changed", (ev) => {
    emitted.push(ev.detail.config);
    el.setConfig(ev.detail.config);
  });
  el.hass = h;
  el.setConfig(config);
  return { el, emitted };
}

/* ---- checks ---- */

// Field names anywhere in a schema — the layout nests fields inside `grid`
// wrappers, so a check shouldn't care how they happen to be grouped today.
function fieldNames(schema) {
  return schema.flatMap((s) => (s.schema ? fieldNames(s.schema) : [s.name]));
}
const hasFields = (form, names) => {
  const present = fieldNames(form.schema);
  return names.every((n) => present.includes(n));
};

let failures = 0;
const check = (cond, msg) => {
  console.log(`${cond ? "  ok  " : "  FAIL"} ${msg}`);
  if (!cond) failures += 1;
};

console.log("compact card editor");
{
  const { el, emitted } = mountEditor("load-scheduler-card-editor", {
    type: "custom:load-scheduler-card",
    entities: [
      { entity: "sensor.a_schedule", name: "A", tank_charge: "sensor.tank" },
      "sensor.b_schedule",
      "switch.plug",
    ],
    grid_options: { columns: "full" },
  });
  const forms = el.forms();

  check(forms.length === 5, `renders 5 ha-forms (top + 3 rows + add), got ${forms.length}`);
  check(
    hasFields(forms[0], ["title", "history_hours", "boost_minutes"]),
    "top form exposes title, history_hours and boost_minutes",
  );
  check(
    hasFields(forms[1], ["name", "tank_charge", "boost_minutes"]),
    "each row exposes name, tank_charge and boost_minutes",
  );

  forms[0].fire("value-changed", { value: { ...forms[0].data, title: "Loads", history_hours: 48 } });
  check(emitted.at(-1).title === "Loads", "title is emitted");
  check(emitted.at(-1).history_hours === 48, "history_hours is emitted");
  check(emitted.at(-1).grid_options.columns === "full", "unrelated config keys are preserved");
  check(forms[0].data.title === "Loads", "the form's .data is kept in step with the emit");

  // The regression this guards: a stale .data would revert `title` here.
  forms[0].fire("value-changed", { value: { ...forms[0].data, boost_minutes: 90 } });
  check(
    emitted.at(-1).title === "Loads" && emitted.at(-1).boost_minutes === 90,
    "a second edit does not revert the first field",
  );

  forms[2].fire("value-changed", { value: { name: "Bee", boost_minutes: 30 } });
  const c = emitted.at(-1);
  check(
    c.entities[1].name === "Bee" && c.entities[1].boost_minutes === 30,
    "a bare-string row gains name + boost as an object",
  );
  check(c.entities[2] === "switch.plug", "an untouched row stays a bare entity id");
  check(c.entities[0].tank_charge === "sensor.tank", "an existing tank_charge is preserved");

  forms[2].fire("value-changed", { value: { name: undefined, boost_minutes: undefined } });
  check(
    emitted.at(-1).entities[1] === "sensor.b_schedule",
    "clearing every override collapses the row back to a bare entity id",
  );
}

console.log("diagnostic card editor");
{
  const { el, emitted } = mountEditor("load-scheduler-diagnostic-card-editor", {
    type: "custom:load-scheduler-diagnostic-card",
    compact: true,
    entities: ["sensor.a_schedule", "sensor.b_schedule"],
  });
  const forms = el.forms();

  check(forms.length === 4, `renders 4 ha-forms (top + 2 rows + add), got ${forms.length}`);
  check(
    !fieldNames(forms[0].schema).includes("entities"),
    "entities is off the top form (the rows replace it)",
  );
  check(
    hasFields(forms[0], ["title", "boost_minutes", "compact", "show_rationale", "show_costs"]),
    "top form still exposes every card-wide option after the regrouping",
  );
  check(
    forms[0].data.compact === true && forms[0].data.show_costs === true,
    "stored values win over the seeded show_* defaults",
  );

  forms[1].fire("value-changed", { value: { name: "Alpha" } });
  check(
    emitted.at(-1).entities[0].name === "Alpha" && emitted.at(-1).entities[1] === "sensor.b_schedule",
    "a per-entity name override is emitted in object form",
  );

  forms[0].fire("value-changed", { value: { ...forms[0].data, show_costs: false } });
  check(emitted.at(-1).show_costs === false, "a false toggle survives the config cleaner");
}

console.log("diagnostic card rendering");
{
  const Card = registry.get("load-scheduler-diagnostic-card");
  const card = new Card();
  card.setConfig({
    type: "custom:load-scheduler-diagnostic-card",
    entities: [{ entity: "sensor.a_schedule", name: "Alpha" }, "sensor.b_schedule"],
  });
  card.hass = hass;
  const html = card.children[0].innerHTML;
  check(html.includes("Alpha"), "the {entity, name} object form renders its name override");
  check(html.includes("B"), "a bare-string entry alongside it still renders");
}

console.log("markup escaping");
{
  const EVIL = `<img src=x onerror="alert(1)">`;
  const ESCAPED = "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;";
  const evilHass = {
    config: { currency: "EUR" },
    states: {
      "sensor.evil_schedule": {
        state: "unknown",
        attributes: {
          friendly_name: EVIL,
          periods: [],
          status: `<script>s()</script>`,
          config: { mode: "non_sequential", controlled_entity: "switch.x", priority: `<i>p</i>` },
        },
      },
      "switch.evil": { state: `<script>s()</script>`, attributes: { friendly_name: EVIL } },
    },
  };
  const clean = (html) => !/<img|<script|<i>/.test(html);

  const Compact = registry.get("load-scheduler-card");
  const cc = new Compact();
  cc.setConfig({
    title: `"><script>t()</script>`,
    entities: ["sensor.evil_schedule", "switch.evil", { entity: "sensor.gone", name: EVIL }],
  });
  cc.hass = evilHass;
  const ch = cc.children[0].innerHTML;
  check(clean(ch), "compact card: no raw markup from names, states or the title");
  check(ch.includes(ESCAPED), "compact card: a malicious friendly_name renders as escaped text");
  check(ch.includes("&lt;script&gt;s()&lt;/script&gt;"), "compact card: a basic tile's raw state is escaped");

  const Diag = registry.get("load-scheduler-diagnostic-card");
  const dc = new Diag();
  dc.setConfig({
    title: `"><script>t()</script>`,
    entities: ["sensor.evil_schedule", { entity: "sensor.gone", name: EVIL }],
  });
  dc.hass = evilHass;
  dc._expanded.add("sensor.evil_schedule"); // show the Configuration section too
  dc._sig = null;
  dc._render();
  const dh = dc.children[0].innerHTML;
  check(clean(dh), "diagnostic card: no raw markup from names, status, config or the title");
  check(dh.includes(ESCAPED), "diagnostic card: a malicious friendly_name renders as escaped text");

  const { el } = mountEditor(
    "load-scheduler-card-editor",
    { entities: ["sensor.evil_schedule"] },
    evilHass,
  );
  const infos = el.descendants().map((d) => d.innerHTML).filter((h) => h.includes("evil_schedule"));
  check(
    infos.length > 0 && infos.every(clean) && infos.some((h) => h.includes(ESCAPED)),
    "editor: the row header escapes the entity's friendly_name",
  );
}

// A load device with its sibling controls, for the signature checks.
function deviceHass(target) {
  return {
    config: { currency: "EUR" },
    entities: {
      "sensor.a_schedule": {
        entity_id: "sensor.a_schedule",
        device_id: "d1",
        platform: "load_scheduler",
        translation_key: "schedule",
      },
      "switch.a_enabled": { entity_id: "switch.a_enabled", device_id: "d1" },
      "button.a_boost": { entity_id: "button.a_boost", device_id: "d1" },
      "number.a_target": { entity_id: "number.a_target", device_id: "d1" },
    },
    states: {
      "sensor.a_schedule": {
        state: "unknown",
        attributes: {
          friendly_name: "A Schedule",
          periods: [],
          target_minutes: 60,
          config: { mode: "non_sequential", controlled_entity: "switch.heater" },
        },
      },
      "number.a_target": { state: String(target), attributes: { unit_of_measurement: "min" } },
      "switch.a_enabled": { state: "on", attributes: {} },
      "button.a_boost": { state: "unknown", attributes: {} },
    },
  };
}
const withState = (h, id, st) => ({ ...h, states: { ...h.states, [id]: st } });

console.log("diagnostic card render gate");
{
  const Diag = registry.get("load-scheduler-diagnostic-card");
  const card = new Diag();
  card.setConfig({ entities: ["sensor.a_schedule"] }); // show_controls defaults on
  const h1 = deviceHass(60);
  card.hass = h1;
  const root = card.children[0];
  // Expand so the controls (and the target stepper) are in the output.
  card._expanded.add("sensor.a_schedule");
  card._render();
  check(root.innerHTML.includes("60min"), "the target stepper shows the number's value + unit");

  root.innerHTML = "SENTINEL";
  const h2 = withState(h1, "sensor.unrelated", { state: "1", attributes: {} });
  card.hass = h2;
  check(root.innerHTML === "SENTINEL", "an unrelated state change does not rebuild the card");

  const h3 = withState(h2, "number.a_target", {
    state: "90",
    attributes: { unit_of_measurement: "min" },
  });
  card.hass = h3;
  check(root.innerHTML.includes("90min"), "a target number change re-renders");

  root.innerHTML = "SENTINEL";
  const sched = h3.states["sensor.a_schedule"];
  card.hass = withState(h3, "sensor.a_schedule", {
    ...sched,
    attributes: { ...sched.attributes, friendly_name: "Renamed Schedule" },
  });
  check(root.innerHTML.includes("Renamed"), "a schedule sensor change re-renders");

  root.innerHTML = "SENTINEL";
  card._onClick({
    target: { closest: (sel) => (sel === ".row" ? { dataset: { entity: "sensor.a_schedule" } } : null) },
    stopPropagation() {},
  });
  check(root.innerHTML !== "SENTINEL", "toggling a panel's details re-renders");
}

console.log("compact card render gate");
{
  const Compact = registry.get("load-scheduler-card");
  const card = new Compact();
  card.setConfig({}); // auto-discovery
  const h1 = deviceHass(60);
  card.hass = h1;
  const root = card.children[0];
  check(root.innerHTML.includes('data-tile="sensor.a_schedule"'), "auto-discovery finds the schedule sensor");
  check(root.innerHTML.includes(">1h<"), "the tile shows the target from the sibling number");

  root.innerHTML = "SENTINEL";
  card.hass = withState(h1, "sensor.unrelated", { state: "1", attributes: {} });
  check(root.innerHTML === "SENTINEL", "an unrelated state change does not rebuild the tiles");

  card.hass = withState(h1, "number.a_target", {
    state: "90",
    attributes: { unit_of_measurement: "min" },
  });
  check(root.innerHTML.includes(">1h30<"), "a target number change shows immediately");
}

console.log("activity timeline");
{
  // The pre-cursor implementation: rescan each series from the start per
  // boundary. The cursor sweep must produce identical segments.
  function referenceSegments(start, end, ctrl, fb, info) {
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
        else status = "heating";
      } else status = "heating";
      const last = segs[segs.length - 1];
      if (last && last.status === status) last.end = t1;
      else segs.push({ start: t0, end: t1, status });
    }
    return segs;
  }

  let seed = 12345;
  const rand = () => ((seed = (seed * 1103515245 + 12345) % 2147483648) / 2147483648);
  const pick = (xs) => xs[Math.floor(rand() * xs.length)];
  // Coarse timestamps so equal-time samples (and samples before start/at end)
  // occur; sorted the same way _normSeries does (stable).
  const series = (n, values) =>
    Array.from({ length: n }, () => ({ t: Math.floor(rand() * 40) * 25, state: pick(values) })).sort(
      (x, y) => x.t - y.t,
    );

  const Compact = registry.get("load-scheduler-card");
  const card = new Compact();
  let same = true;
  for (let trial = 0; trial < 200; trial++) {
    const ctrl = series(1 + Math.floor(rand() * 30), ["on", "off", "unavailable"]);
    const fb =
      trial % 3 === 0
        ? null
        : series(Math.floor(rand() * 60), ["0", "12", "49.9", "50", "1500", "unavailable", "unknown", "on", "off"]);
    const info = { idleW: 50, mode: trial % 5 === 0 ? "basic" : "scheduler" };
    const got = card._buildSegments(100, 900, ctrl, fb, info);
    const want = referenceSegments(100, 900, ctrl, fb, info);
    if (JSON.stringify(got) !== JSON.stringify(want)) {
      same = false;
      console.log("    mismatch", JSON.stringify({ ctrl, fb, info, got, want }));
      break;
    }
  }
  check(same, "the cursor sweep matches the rescanning reference on 200 random series");

  const ctrl = [{ t: 0, state: "on" }];
  const kw = [{ t: 0, state: "1.5" }];
  const statusFor = (unit) =>
    card._buildSegments(100, 200, ctrl, kw, { idleW: 50, mode: "scheduler", feedbackUnit: unit })[0]
      .status;
  check(statusFor("kW") === "heating", "a 1.5 kW feedback reading counts as heating");
  check(statusFor("W") === "idle", "a 1.5 W feedback reading is idle");
  check(statusFor(undefined) === "idle", "a unit-less feedback reading is taken as watts");
  const tiny = (unit) =>
    card._buildSegments(100, 200, ctrl, [{ t: 0, state: "0.2" }], {
      idleW: 50,
      mode: "scheduler",
      feedbackUnit: unit,
    })[0].status;
  check(
    tiny("mW") === "idle" && tiny("MW") === "heating",
    "mW and MW are not confused (case-sensitive units)",
  );
}

console.log("diagnostic narration");
{
  const Diag = registry.get("load-scheduler-diagnostic-card");
  const card = new Diag();
  card.setConfig({ entities: ["sensor.m_schedule"] });
  card.hass = {
    config: { currency: "EUR" },
    states: {
      "sensor.m_schedule": {
        state: "unknown",
        attributes: {
          friendly_name: "M Schedule",
          periods: [],
          target_minutes: 60,
          remaining_minutes: 10,
          rationale: { skip_reason: "below_min_run" },
          config: { mode: "non_sequential", controlled_entity: "switch.m", min_run_minutes: 30 },
        },
      },
    },
  };
  const html = card.children[0].innerHTML;
  check(
    html.includes("shorter than this load's minimum run of 30m") && html.includes("only 10m"),
    "below_min_run is narrated in plain English",
  );
  check(!html.includes("Nothing scheduled right now."), "below_min_run doesn't fall through to the generic line");
}

console.log(failures ? `\n${failures} failure(s)` : "\nall checks passed");
process.exit(failures ? 1 : 0);
