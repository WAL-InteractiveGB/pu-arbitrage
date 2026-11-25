// UI for the pu-arbitrage web app.
import * as E from "./engine.js";
import { Fio, History, DEFAULT_API } from "./fio.js";
import * as Log from "./tradelog.js";
import { initCalib, getModel, refreshCalib } from "./calib.js";
import * as Ships from "./ships.js";
import { modelSnap } from "./flightmodel.js";

const HIGHS_BASE = "https://cdn.jsdelivr.net/npm/highs@1.15.4/build/";
const SETTINGS_KEY = "pua:settings";
// Trader settings. Ship settings (cargo, tanks, fuel) live in ship profiles.
const DEFAULTS = {
  home: "", capital: 500000, gateways: [],
  flatFee: "", mode: "both", feeRate: 1, riskAversion: 1, roundTrip: false, useHistory: true, pickFuel: true,
};
const FIELDS = ["capital", "flatFee", "mode", "feeRate", "riskAversion", "roundTrip", "useHistory", "pickFuel"];

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const fmt = E.fmt;
const f2 = (x) => (Number.isFinite(x) ? x.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : "n/a");
const signed = (x) => `<span class="${x >= 0 ? "pos" : "neg"}">${fmt(x)}</span>`;

// The API can be pointed elsewhere for testing: ?api=http://localhost:8000/snap
const apiParam = new URLSearchParams(location.search).get("api");
const fio = new Fio(apiParam && /^https?:\/\//.test(apiParam) ? apiParam : DEFAULT_API);

const state = { data: null, results: [], selected: -1, solver: null, loadedAt: null, lastRun: null };
let solverPromise;

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------
function loadSettings() {
  try { return { ...DEFAULTS, ...JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}") }; }
  catch { return { ...DEFAULTS }; }
}

function saveSettings() {
  const f = readForm();
  const trader = { home: f.home, gateways: f.gateways };
  for (const k of FIELDS) trader[k] = f[k];
  try { localStorage.setItem(SETTINGS_KEY, JSON.stringify(trader)); } catch { /* ignore */ }
  const ship = {};
  for (const k of Ships.SHIP_FIELDS) ship[k] = f[k];
  Ships.update(ship);
}

function fillForm(s) {
  for (const k of FIELDS) {
    const el = $(`#${k}`);
    if (el.type === "checkbox") el.checked = !!s[k]; else el.value = s[k];
  }
  fillShip();
  $("#gw-rows").innerHTML = "";
  for (const g of s.gateways || []) addGatewayRow(g);
}

// Ship fields and the picker, from the current ship profile.
function fillShip() {
  const ship = Ships.current();
  for (const k of Ships.SHIP_FIELDS) $(`#${k}`).value = ship[k] ?? Ships.SHIP_DEFAULTS[k];
  $("#ship-select").innerHTML = Ships.list().map((x) =>
    `<option value="${esc(x.id)}"${x.id === ship.id ? " selected" : ""}>${esc(x.name)}</option>`).join("");
  $("#ship-delete").disabled = Ships.list().length < 2;
}

function shipChanged() {
  fillShip();
  refreshCalib();
  updateModelNote();
  state.results = [];
  $("#results-wrap").hidden = true;
  $("#detail").hidden = true;
  $("#empty").hidden = false;
}

function initShips() {
  $("#ship-select").onchange = (e) => { saveSettings(); Ships.select(e.target.value); shipChanged(); };
  $("#ship-new").onclick = () => {
    const name = prompt("Name for the new ship:", "New ship");
    if (name == null) return;
    saveSettings();
    Ships.create(name);
    shipChanged();
  };
  $("#ship-dup").onclick = () => {
    const cur = Ships.current();
    const name = prompt("Name for the copy (it keeps the calibration):", `${cur.name} copy`);
    if (name == null) return;
    saveSettings();
    Ships.create(name, cur.id);
    shipChanged();
  };
  $("#ship-rename").onclick = () => {
    const name = prompt("New name:", Ships.current().name);
    if (name == null) return;
    Ships.rename(name);
    shipChanged();
  };
  $("#ship-delete").onclick = () => {
    const cur = Ships.current();
    if (!confirm(`Delete ${cur.name} and its calibration from this browser? This can't be undone. Export it first if you want to keep a copy.`)) return;
    try { Ships.remove(); } catch (err) { alert(err.message); return; }
    shipChanged();
  };
  $("#ship-export").onclick = () => {
    saveSettings();
    const name = Ships.current().name.replace(/[^\w.-]+/g, "_");
    download(`ships_${name}.json`, Ships.exportJson(), "application/json");
  };
  $("#ship-import").onchange = async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    try {
      saveSettings();
      const added = Ships.importJson(await file.text());
      shipChanged();
      status(`Imported ${added.join(", ")}.`);
    } catch (err) {
      alert(`Could not import: ${err.message}`);
    } finally {
      e.target.value = "";
    }
  };
}

function readForm() {
  const out = {};
  for (const k of [...FIELDS, ...Ships.SHIP_FIELDS]) {
    const el = $(`#${k}`);
    out[k] = el.type === "checkbox" ? el.checked : el.value;
  }
  out.home = $("#home").value || loadSettings().home;
  out.gateways = [...document.querySelectorAll(".gw-row")].map((r) => ({
    from: r.querySelector(".gw-from").value, to: r.querySelector(".gw-to").value,
    fee: r.querySelector(".gw-fee").value,
  }));
  return out;
}

function exchangeOptions(selected) {
  const exs = state.data ? state.data.exchanges : [selected].filter(Boolean);
  return exs.map((e) => `<option value="${esc(e)}"${e === selected ? " selected" : ""}>${esc(e)}</option>`).join("");
}

function addGatewayRow(g = {}) {
  const row = document.createElement("div");
  row.className = "gw-row";
  row.innerHTML = `
    <select class="gw-from" aria-label="From">${exchangeOptions(g.from)}</select>
    <select class="gw-to" aria-label="To">${exchangeOptions(g.to)}</select>
    <input class="gw-fee" type="number" min="0" step="100" placeholder="Fee" aria-label="Fee" value="${esc(g.fee ?? "")}">
    <button type="button" class="x" aria-label="Remove">×</button>`;
  row.querySelector(".x").onclick = () => { row.remove(); saveSettings(); };
  $("#gw-rows").append(row);
}

function refreshExchangeSelects() {
  const s = loadSettings();
  const home = state.data.exchanges.includes(s.home) ? s.home : state.data.exchanges[0];
  $("#home").innerHTML = state.data.exchanges.map((e) =>
    `<option value="${esc(e)}"${e === home ? " selected" : ""}>${esc(e)} · ${esc(state.data.currency[e] || "?")}</option>`).join("");
  $("#gw-rows").innerHTML = "";
  for (const g of s.gateways || []) addGatewayRow(g);
  refreshCalib();
}

// Shows whether a fitted flight model is in use on the Find trades form.
function updateModelNote() {
  const model = getModel();
  const note = $("#model-note");
  note.hidden = !model;
  $("#pickFuel-wrap").hidden = !model;
  $("#calib-pill").hidden = !model;
  for (const id of ["hoursPerJump", "overheadHours"]) $(`#${id}`).disabled = !!model;
  if (!model) return;
  note.innerHTML = `Using your <b>ship calibration</b> (${model.flights} flights, typical error
    ${Math.round(60 * model.rmse_hours)} min). Flight times come from it, so hours per jump is ignored.
    The sliders below are the starting point.`;
}

// ---------------------------------------------------------------------------
// Status
// ---------------------------------------------------------------------------
function status(msg, frac = null, error = false) {
  const el = $("#status");
  el.textContent = msg;
  el.classList.toggle("error", error);
  const p = document.querySelector(".progress");
  p.hidden = frac === null;
  if (frac !== null) $("#bar").style.width = `${Math.round(100 * frac)}%`;
}

function updateAge() {
  if (!state.loadedAt) return;
  const mins = Math.round((Date.now() - state.loadedAt) / 60e3);
  $("#data-age").textContent = `Market data from ${mins < 1 ? "just now" : `${mins} min ago`}`;
}

// ---------------------------------------------------------------------------
// Data loading
// ---------------------------------------------------------------------------
async function loadData(force = false) {
  $("#run").disabled = true;
  try {
    if (force) fio.mem.delete("/exchange/full");
    const mk = await fio.loadMarket((m) => status(m));
    const books = new E.BookStore(mk.full);
    const exchanges = books.exchanges;
    const currency = {};
    for (const e of exchanges) {
      currency[e] = books.currency[e] || mk.currency[e] || E.DEFAULT_CURRENCY[e] || null;
    }
    const { routes, failed } = await fio.loadRoutes(exchanges, mk.system, mk.pcLen, (m) => status(m));
    state.data = { books, exchanges, currency, region: mk.region, routes, failed, materials: mk.materials };
    state.loadedAt = Date.now();
    refreshExchangeSelects();
    updateAge();
    status(`Ready: ${exchanges.length} exchanges, ${Object.keys(routes).length / 2} routes.`
           + (failed.length ? ` Could not map ${failed.join(", ")}.` : ""));
    $("#run").disabled = false;
  } catch (err) {
    console.error(err);
    status(`Could not load market data from FIO (${err.message}). Check your connection and press Refresh.`, null, true);
  }
}

// ---------------------------------------------------------------------------
// Running a scan
// ---------------------------------------------------------------------------
function num(v, name, { min = -Infinity, optional = false } = {}) {
  if (v === "" || v == null) {
    if (optional) return null;
    throw new Error(`${name} is required.`);
  }
  const n = Number(v);
  if (!Number.isFinite(n) || n < min) throw new Error(`${name} must be a number${min > -Infinity ? ` of at least ${min}` : ""}.`);
  return n;
}

async function run() {
  if (!state.data) return;
  const f = readForm();
  saveSettings();
  let ship, s, borders;
  const warnings = [];
  try {
    const model = getModel();
    ship = {
      stlTank: num(f.stlTank, "STL tank", { min: 1, optional: true }),
      ftlTank: num(f.ftlTank, "FTL tank", { min: 1, optional: true }),
      weight: num(f.weight, "Cargo weight", { min: 1 }), volume: num(f.volume, "Cargo volume", { min: 1 }),
      hoursPerJump: model ? 0 : num(f.hoursPerJump, "Hours per jump", { min: 0 }),
      flightModel: model,
      overheadHours: num(f.overheadHours, "Extra hours", { min: 0, optional: true }) || 0,
      maxJumps: num(f.maxJumps, "Max jumps", { min: 1, optional: true }) ?? Infinity,
      reactorUsage: num(f.reactor, "Reactor usage", { min: 1 }) / 100,
      sfLoaded: num(f.sfLoaded, "SF loaded", { min: 0, optional: true }),
      stlUsage: (num(f.stlUsage, "STL fuel usage", { min: 1, optional: true }) ?? 100 * E.C.DEFAULT_STL_USAGE) / 100,
      stlDepartureShare: (num(f.stlDeparture, "Departure burn", { min: 0, optional: true }) ?? 100) / 100,
      sfPerTrip: num(f.sfPerTrip, "Measured SF per flight", { min: 0, optional: true }),
      ffPerParsec: num(f.ffPerParsec, "FF per parsec", { min: 0, optional: true }) ?? E.C.FF_PER_PARSEC,
    };
    if (ship.reactorUsage > 1) throw new Error("Reactor usage can't be above 100%.");
    if (model && !ship.sfLoaded) ship.sfLoaded = model.sf_loaded;
    if (ship.stlTank && ship.sfLoaded > ship.stlTank) throw new Error(`SF loaded (${ship.sfLoaded}) is more than the STL tank holds (${ship.stlTank}).`);
    if (model) [ship.stlUsage, ship.reactorUsage] = modelSnap(model, ship.stlUsage, ship.reactorUsage);
    if (ship.stlUsage > 1) throw new Error("STL fuel usage can't be above 100%.");
    if (ship.stlDepartureShare > 1) throw new Error("Departure burn can't be above 100% of the approach.");
    s = { capital: num(f.capital, "Cash", { min: 1 }), roundTrip: f.roundTrip,
          riskAversion: num(f.riskAversion, "Risk aversion", { min: 0 }),
          feeRate: num(f.feeRate, "Exchange fee", { min: 0 }) / 100, mode: f.mode,
          pickFuel: !!model && f.pickFuel };
    const routes = {};
    for (const g of f.gateways) {
      if (g.fee === "" || g.from === g.to) continue;
      routes[`${g.from}>${g.to}`] = { entry_fee: num(g.fee, `Gateway fee ${g.from}>${g.to}`, { min: 0 }) };
    }
    const flat = num(f.flatFee, "Flat border fee", { min: 0, optional: true });
    borders = new E.BorderModel(state.data.exchanges, state.data.region,
                                { routes, default: flat ? { entry_fee: flat } : {} });
  } catch (err) {
    status(err.message, null, true);
    return;
  }
  if (ship.sfPerTrip == null && !ship.sfLoaded) warnings.push("SF loaded isn't set, so STL fuel isn't counted. Enter how much SF you take off with for a fuller cost.");

  $("#run").disabled = true;
  try {
    state.solver = await solverPromise;
  } catch {
    state.solver = null;
  }
  if (!state.solver) warnings.push("The optimizer (HiGHS) didn't load, so cargo is chosen with a simpler method and round trips through other currencies are skipped. Reload the page to try again.");
  $("#warnings").innerHTML = warnings.map((w) => `<div class="warn">${esc(w)}</div>`).join("");

  const history = new History(fio, f.useHistory, (m) => status(`${state.statusPrefix || ""} · ${m}`));
  const market = { ...state.data, history, borders, solver: state.solver };
  const home = f.home;
  const started = performance.now();
  try {
    const results = await E.scan(home, ship, s, market, (m, frac) => { state.statusPrefix = m; status(m, frac); });
    state.results = results;
    state.lastRun = { home, ship, s, currency: state.data.currency[home] };
    const secs = ((performance.now() - started) / 1000).toFixed(1);
    status(results.length ? `Found ${results.length} profitable trips from ${home} in ${secs}s.`
                          : `No profitable trips from ${home} with these settings.`);
    renderResults();
  } catch (err) {
    console.error(err);
    status(`Something went wrong: ${err.message}`, null, true);
  } finally {
    $("#run").disabled = false;
    updateAge();
  }
}

// ---------------------------------------------------------------------------
// Results
// ---------------------------------------------------------------------------
function renderResults() {
  const { results } = state;
  const ship = state.lastRun.ship;
  $("#empty").hidden = results.length > 0;
  $("#results-wrap").hidden = !results.length;
  $("#results tbody").innerHTML = results.map((r, i) => `
    <tr data-i="${i}" tabindex="0">
      <td>${i + 1}</td><td>${esc(r.label.replaceAll(">", " → "))}</td>
      <td><span class="tag">${esc(r.kind)}</span></td>
      <td class="n">${sliders(r.ship || ship)}</td>
      <td class="n">${r.hours.toFixed(1)}</td><td class="n">${fmt(r.capitalUsed)}</td>
      <td class="n">${Math.round(100 * r.weightUsed / ship.weight)}% / ${Math.round(100 * r.volumeUsed / ship.volume)}%</td>
      <td class="n">${fmt(r.fuelHome)}</td><td class="n">${fmt(r.borderFees)}</td>
      <td class="n pos">${fmt(r.netProfit)}</td><td class="n">${fmt(r.risk)}</td>
      <td class="n">${signed(r.score)}</td>
    </tr>`).join("");
  for (const tr of document.querySelectorAll("#results tbody tr")) {
    const open = () => showDetail(Number(tr.dataset.i));
    tr.onclick = open;
    tr.onkeydown = (e) => { if (e.key === "Enter") open(); };
  }
  if (results.length) showDetail(0, false);
  else $("#detail").hidden = true;
}

const sliders = (sh) => `${Math.round(100 * sh.stlUsage)}% / ${Math.round(100 * sh.reactorUsage)}%`;

function itemTable(items, cin, cout) {
  return `<div class="table-scroll"><table class="data"><thead><tr>
    <th>Item</th><th class="n">Units</th><th class="n">Avg buy</th><th class="n">Avg sell</th>
    <th class="n">Spent ${esc(cin)}</th><th class="n">Get ${esc(cout)}</th></tr></thead><tbody>
    ${items.map((i) => `<tr><td>${esc(i.ticker)}</td><td class="n">${fmt(i.units)}</td>
      <td class="n">${f2(i.buy_vwap)}</td><td class="n">${f2(i.sell_vwap)}</td>
      <td class="n">${fmt(i.spent ?? i.cost)}</td><td class="n">${fmt(i.received ?? i.sales)}</td></tr>`).join("")}
    </tbody></table></div>`;
}

function crossingLine(c) {
  const where = c.fromRegion !== c.toRegion ? `${c.fromRegion} → ${c.toRegion}` : `within ${c.fromRegion}`;
  return `${esc(c.origin)} → ${esc(c.dest)} (${esc(where)}): ${esc(E.describeCrossing(c))}`;
}

function steps(r) {
  const buy = (items) => items.map((i) => `${fmt(i.units)} ${esc(i.ticker)} (avg ${f2(i.buy_vwap)})`).join(", ");
  const sh = r.ship || state.lastRun.ship;
  const plan = `In SFC, set the STL slider to <b>${Math.round(100 * sh.stlUsage)}%</b> and the reactor to <b>${Math.round(100 * sh.reactorUsage)}%</b>`
    + (sh.flightModel ? ` (about ${r.hours.toFixed(1)} h in all).` : ".");
  if (r.kind === "one-way") {
    return `<ol>
      <li>At <b>${esc(r.origin)}</b>, buy fuel, then buy ${buy(r.items)}.</li>
      <li>${plan} Fly to <b>${esc(r.dest)}</b>${r.crossings[0].entry_fee ? ` (gateway fee ${fmt(r.crossings[0].entry_fee)})` : ""}.</li>
      <li>Sell everything there.${state.lastRun.s.roundTrip ? ` Fly home empty.` : ""}</li></ol>`;
  }
  return `<ol>
    <li>At <b>${esc(r.origin)}</b>, buy fuel, then buy ${buy(r.leg1)}.</li>
    <li>${plan} Fly to <b>${esc(r.mid)}</b> and sell all of it.</li>
    <li>At <b>${esc(r.mid)}</b>, buy fuel, then buy ${buy(r.leg2)} with the proceeds.</li>
    <li>Fly to <b>${esc(r.final)}</b> at the same settings and sell everything.</li></ol>`;
}

function showDetail(i, scroll = true) {
  state.selected = i;
  for (const tr of document.querySelectorAll("#results tbody tr")) tr.classList.toggle("sel", Number(tr.dataset.i) === i);
  const r = state.results[i];
  const cur = state.data.currency;
  const home = cur[r.origin] || "home";
  let body;
  if (r.kind === "one-way") {
    body = `
      <div class="breakdown">sales <b>${fmt(r.sales)}</b> − purchases <b>${fmt(r.purchases)}</b> − fuel <b>${fmt(r.fuel)}</b>
        − gateway/border fees <b>${fmt(r.borderFees)}</b> = <b class="pos">${fmt(r.netProfit)} ${esc(home)}</b></div>
      ${r.crossings.map((c) => `<div class="small muted">${crossingLine(c)}</div>`).join("")}
      <h3>What to do</h3>${steps(r)}
      <h3>Cargo</h3>${itemTable(r.items, home, home)}`;
  } else {
    const away = cur[r.mid] || "mid";
    body = `
      <div class="breakdown">${esc(home)}: <b>${fmt(r.rev2)}</b> back − <b>${fmt(r.cost1)}</b> out − fuel <b>${fmt(r.fuel1)}</b>
        − fees <b>${fmt(r.homeBorderFees)}</b>${r.midConvertible ? ` + <b>${fmt(r.leftover)}</b> unspent at ${esc(r.mid)}` : ""}
        = <b class="pos">${fmt(r.netProfit)} ${esc(home)}</b><br>
        Paid at ${esc(r.mid)} in ${esc(away)}: fuel <b>${fmt(r.fuel2)}</b>, fees <b>${fmt(r.cross1.entry_fee + r.cross2.exit_fee)}</b>
        ${r.midConvertible ? "" : `<br>${esc(away)} left over that can't be brought home: <b>${fmt(r.leftover)}</b> (counted as zero)`}</div>
      ${r.crossings.map((c) => `<div class="small muted">${crossingLine(c)}</div>`).join("")}
      <h3>What to do</h3>${steps(r)}
      <h3>Leg 1: ${esc(r.origin)} → ${esc(r.mid)}</h3>${itemTable(r.leg1, home, away)}
      <h3>Leg 2: ${esc(r.mid)} → ${esc(r.final)}</h3>${itemTable(r.leg2, away, home)}`;
  }
  const d = $("#detail");
  d.hidden = false;
  d.innerHTML = `
    <h2>#${i + 1} ${esc(r.label.replaceAll(">", " → "))} <span class="tag">${esc(r.kind)}</span></h2>
    <div class="small muted">${r.hours.toFixed(1)} hours · risk (1 sd) ${fmt(r.risk)} · score ${fmt(r.score)}/hr</div>
    ${body}
    <div class="buttons"><button class="primary" id="log-this">Log this trade</button>
    <span id="log-msg" class="muted small"></span></div>`;
  $("#log-this").onclick = () => {
    const data = Log.load();
    const lr = state.lastRun;
    const t = Log.record(data, r, { home: lr.home, currency: lr.currency, capital: lr.s.capital,
                                    ship: null, settings: lr.s });
    Log.save(data);
    $("#log-msg").textContent = `Logged as trade #${t.id}. Close it in the Trade log tab when you've sold everything.`;
    renderLog();
  };
  if (scroll) d.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

// ---------------------------------------------------------------------------
// Trade log tab
// ---------------------------------------------------------------------------
function renderLog() {
  const data = Log.load();
  const open = data.trades.filter((t) => t.status === "open").length;
  const pill = $("#open-count");
  pill.hidden = !open;
  pill.textContent = open;
  const s = Log.stats(data.trades);
  $("#log-summary").innerHTML = s ? Log.summaryLines(s).map((l) => `<li>${esc(l)}</li>`).join("") : "";
  $("#log-empty").hidden = data.trades.length > 0;
  $("#log tbody").innerHTML = data.trades.slice().reverse().map((t) => {
    const r = Log.row(t);
    const action = t.status === "open"
      ? `<form class="closeform" data-id="${t.id}">
           <input name="profit" type="number" step="1" placeholder="Actual profit" required aria-label="Actual profit">
           <input name="hours" type="number" step="0.5" min="0" placeholder="Hours" aria-label="Actual hours">
           <button class="ghost" type="submit">Close</button>
           <button class="x" type="button" data-abandon="${t.id}" title="Not taken">Abandon</button>
         </form>`
      : `<button class="x" type="button" data-delete="${t.id}" title="Delete from log">Delete</button>`;
    return `<tr><td>${r.id}</td><td>${esc(r.date)}</td><td>${esc(r.route)}</td><td>${esc(r.kind)}</td>
      <td>${esc(r.cur)}</td><td class="n">${esc(r.pred)}</td><td class="n">${esc(r.risk)}</td>
      <td class="n">${esc(r.actual)}</td><td class="n">${esc(r.error)}</td><td class="n">${esc(r.z)}</td>
      <td class="n">${esc(r.hours)}</td><td>${action}</td></tr>`;
  }).join("");
  for (const form of document.querySelectorAll(".closeform")) {
    form.onsubmit = (e) => {
      e.preventDefault();
      const d = Log.load();
      const fd = new FormData(form);
      const hours = fd.get("hours");
      Log.close(d, Number(form.dataset.id), { profit: Number(fd.get("profit")),
                                              hours: hours === "" ? null : Number(hours) });
      Log.save(d);
      renderLog();
    };
  }
  for (const b of document.querySelectorAll("[data-abandon]")) {
    b.onclick = () => { const d = Log.load(); Log.abandon(d, Number(b.dataset.abandon)); Log.save(d); renderLog(); };
  }
  for (const b of document.querySelectorAll("[data-delete]")) {
    b.onclick = () => {
      if (!confirm(`Delete trade #${b.dataset.delete} from the log? This can't be undone.`)) return;
      const d = Log.load(); Log.remove(d, Number(b.dataset.delete)); Log.save(d); renderLog();
    };
  }
}

function download(name, text, type) {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = Object.assign(document.createElement("a"), { href: url, download: name });
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// ---------------------------------------------------------------------------
// Start-up
// ---------------------------------------------------------------------------
function setTab(name) {
  for (const b of document.querySelectorAll(".tabs button")) b.setAttribute("aria-selected", b.dataset.tab === name);
  for (const s of document.querySelectorAll(".tab")) s.hidden = s.id !== `tab-${name}`;
  if (name === "log") renderLog();
}

function init() {
  for (const b of document.querySelectorAll(".tabs button")) b.onclick = () => setTab(b.dataset.tab);
  fillForm(loadSettings());
  initShips();
  initCalib({
    getData: () => state.data,
    getHome: () => $("#home").value || loadSettings().home,
    getSfLoaded: () => $("#sfLoaded").value,
    onChange: updateModelNote,
  });
  updateModelNote();
  $("#setup").addEventListener("input", saveSettings);
  $("#setup").addEventListener("change", saveSettings);
  $("#setup").addEventListener("submit", (e) => { e.preventDefault(); run(); });
  $("#gw-add").onclick = () => { addGatewayRow(); saveSettings(); };
  $("#run").onclick = run;
  $("#refresh").onclick = () => loadData(true);
  $("#export-json").onclick = () => download("trade_log.json", JSON.stringify(Log.load(), null, 2), "application/json");
  $("#export-md").onclick = () => download("track_record.md", Log.markdown(Log.load()), "text/markdown");
  $("#import-json").onchange = async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    try {
      const data = Log.importJson(await file.text());
      if (Log.load().trades.length && !confirm("Replace the trade log in this browser with the imported one?")) return;
      Log.save(data);
      renderLog();
    } catch (err) {
      alert(`Could not import: ${err.message}`);
    } finally {
      e.target.value = "";
    }
  };
  setInterval(updateAge, 60e3);

  solverPromise = typeof window.Module === "function"
    ? window.Module({ locateFile: (f) => HIGHS_BASE + f })
    : Promise.reject(new Error("HiGHS script not loaded"));
  solverPromise.catch(() => {});
  renderLog();
  loadData();
}

init();
