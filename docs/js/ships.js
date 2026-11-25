// Ship profiles: each ship keeps its own specs, fuel settings and calibration.
// Stored in this browser; exported and imported in the Python tool's
// ships.json format, so one file works for both.

const KEY = "pua:ships";
const OLD_SETTINGS = "pua:settings", OLD_CALIB = "pua:calib";

// Form fields that belong to a ship rather than to the trader.
export const SHIP_FIELDS = ["weight", "volume", "stlTank", "ftlTank", "hoursPerJump", "overheadHours",
  "maxJumps", "reactor", "sfLoaded", "stlUsage", "stlDeparture", "sfPerTrip", "ffPerParsec"];

export const SHIP_DEFAULTS = {
  weight: 500, volume: 500, stlTank: "", ftlTank: "", hoursPerJump: 1.5, overheadHours: 0,
  maxJumps: "", reactor: 25, sfLoaded: "", stlUsage: 50, stlDeparture: 100, sfPerTrip: "", ffPerParsec: 7.2,
};

const blankCalib = () => ({ rows: [], model: null, mode: "auto" });
const newId = () => `s${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;

function read(key) {
  try { return JSON.parse(localStorage.getItem(key) || "null"); } catch { return null; }
}

function write(store) {
  try { localStorage.setItem(KEY, JSON.stringify(store)); } catch { /* ignore */ }
}

// The store, creating it on first use from the single-ship setup older
// versions kept in the trade settings and the calibration tab.
export function load() {
  const store = read(KEY);
  if (store && store.ships && Object.keys(store.ships).length) {
    if (!store.ships[store.current]) store.current = Object.keys(store.ships)[0];
    return store;
  }
  const old = read(OLD_SETTINGS) || {};
  const ship = { name: "My ship", calib: { ...blankCalib(), ...(read(OLD_CALIB) || {}) } };
  for (const k of SHIP_FIELDS) ship[k] = old[k] ?? SHIP_DEFAULTS[k];
  const id = newId();
  const fresh = { current: id, ships: { [id]: ship } };
  write(fresh);
  return fresh;
}

export const current = () => { const s = load(); return { id: s.current, ...s.ships[s.current] }; };
export const list = () => Object.entries(load().ships).map(([id, s]) => ({ id, name: s.name }))
  .sort((a, b) => a.name.localeCompare(b.name));

export function select(id) {
  const s = load();
  if (s.ships[id]) { s.current = id; write(s); }
}

// Merge fields into the current ship.
export function update(fields) {
  const s = load();
  Object.assign(s.ships[s.current], fields);
  write(s);
}

export function getCalib() { return { ...blankCalib(), ...(current().calib || {}) }; }
export function setCalib(calib) { update({ calib }); }

function uniqueName(s, name) {
  const taken = new Set(Object.values(s.ships).map((x) => x.name));
  if (!taken.has(name)) return name;
  let n = 2;
  while (taken.has(`${name} (${n})`)) n++;
  return `${name} (${n})`;
}

export function create(name, copyOf = null) {
  const s = load();
  const base = copyOf ? JSON.parse(JSON.stringify(s.ships[copyOf])) : { ...SHIP_DEFAULTS, calib: blankCalib() };
  base.name = uniqueName(s, name.trim() || "New ship");
  const id = newId();
  s.ships[id] = base;
  s.current = id;
  write(s);
  return id;
}

export function rename(name) {
  const s = load();
  const clean = name.trim();
  if (!clean || clean === s.ships[s.current].name) return;
  s.ships[s.current].name = uniqueName(s, clean);
  write(s);
}

export function remove() {
  const s = load();
  if (Object.keys(s.ships).length < 2) throw new Error("You need at least one ship.");
  delete s.ships[s.current];
  s.current = Object.keys(s.ships)[0];
  write(s);
}

// ---------------------------------------------------------------------------
// ships.json format (shared with the Python tool)
// ---------------------------------------------------------------------------
const numOrNull = (v) => (v === "" || v == null || !Number.isFinite(Number(v)) ? null : Number(v));
const frac = (v) => (numOrNull(v) == null ? null : Number(v) / 100);

function toPython(ship) {
  const out = {
    cargo_weight: numOrNull(ship.weight), cargo_volume: numOrNull(ship.volume),
    stl_tank: numOrNull(ship.stlTank), ftl_tank: numOrNull(ship.ftlTank),
    sf_loaded: numOrNull(ship.sfLoaded), stl_usage: frac(ship.stlUsage),
    reactor_usage: frac(ship.reactor), stl_departure_share: frac(ship.stlDeparture),
    hours_per_distance: numOrNull(ship.hoursPerJump), overhead_hours: numOrNull(ship.overheadHours),
    max_distance: numOrNull(ship.maxJumps), sf_per_trip: numOrNull(ship.sfPerTrip),
    ff_per_parsec: numOrNull(ship.ffPerParsec),
  };
  const calib = ship.calib || {};
  if (calib.model) out.flight_model = calib.model;
  if (calib.rows && calib.rows.length) out.flights = calib.rows;
  if (calib.mode && calib.mode !== "auto") out.model_mode = calib.mode;
  for (const k of Object.keys(out)) if (out[k] == null) delete out[k];
  return out;
}

function fromPython(name, p) {
  const pct = (v) => (numOrNull(v) == null ? "" : +(Number(v) <= 1 ? 100 * v : v).toFixed(4));
  const val = (v, d) => (numOrNull(v) == null ? d : Number(v));
  const ship = {
    name,
    weight: val(p.cargo_weight, SHIP_DEFAULTS.weight), volume: val(p.cargo_volume, SHIP_DEFAULTS.volume),
    stlTank: val(p.stl_tank, ""), ftlTank: val(p.ftl_tank, ""), sfLoaded: val(p.sf_loaded, ""),
    stlUsage: pct(p.stl_usage) || SHIP_DEFAULTS.stlUsage, reactor: pct(p.reactor_usage) || SHIP_DEFAULTS.reactor,
    stlDeparture: pct(p.stl_departure_share) || SHIP_DEFAULTS.stlDeparture,
    hoursPerJump: val(p.hours_per_distance, SHIP_DEFAULTS.hoursPerJump),
    overheadHours: val(p.overhead_hours, SHIP_DEFAULTS.overheadHours), maxJumps: val(p.max_distance, ""),
    sfPerTrip: val(p.sf_per_trip, ""), ffPerParsec: val(p.ff_per_parsec, SHIP_DEFAULTS.ffPerParsec),
    calib: { rows: Array.isArray(p.flights) ? p.flights : [], model: null, mode: p.model_mode || "auto" },
  };
  const m = p.flight_model;
  if (m && typeof m === "object" && (m.kind === "measured" ? Array.isArray(m.settings) : Number.isFinite(m.overhead))) {
    ship.calib.model = { ...m, kind: m.kind || "formula", notes: m.notes || [],
                         sf_loaded: m.sf_loaded || numOrNull(p.sf_loaded) || 0 };
  }
  return ship;
}

// One ship, or all of them, as a ships.json document.
export function exportJson(all = false) {
  const s = load();
  const ids = all ? Object.keys(s.ships) : [s.current];
  const ships = {};
  for (const id of ids) ships[s.ships[id].name] = toPython(s.ships[id]);
  return JSON.stringify({ ships }, null, 2);
}

// Adds every ship in a ships.json document. Returns the names added.
export function importJson(text) {
  let doc;
  try { doc = JSON.parse(text); } catch { throw new Error("That file isn't valid JSON."); }
  const ships = doc && typeof doc.ships === "object" && doc.ships ? doc.ships : null;
  if (!ships || !Object.keys(ships).length) throw new Error('No ships found. Expected a ships.json file with a "ships" section.');
  const s = load();
  const added = [];
  for (const [name, p] of Object.entries(ships)) {
    if (!p || typeof p !== "object") continue;
    const ship = fromPython(uniqueName(s, String(name).slice(0, 60) || "Imported ship"), p);
    const id = newId();
    s.ships[id] = ship;
    s.current = id;
    added.push(ship.name);
  }
  if (!added.length) throw new Error("No usable ships in that file.");
  write(s);
  return added;
}
