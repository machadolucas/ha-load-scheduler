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
function mountEditor(tagName, config) {
  const Cls = registry.get(tagName);
  const el = new Cls();
  if (el.connectedCallback) el.connectedCallback();
  const emitted = [];
  el.addEventListener("config-changed", (ev) => {
    emitted.push(ev.detail.config);
    el.setConfig(ev.detail.config);
  });
  el.hass = hass;
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

console.log(failures ? `\n${failures} failure(s)` : "\nall checks passed");
process.exit(failures ? 1 : 0);
