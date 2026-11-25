// "Ship calibration" tab: flights planned in SFC -> fitted flight model.
import * as E from "./engine.js";
import { fitAuto, parseDuration, modelHours, modelSF, modelFF, modelGrid } from "./flightmodel.js";
import * as Ships from "./ships.js";

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const fmtH = (h) => {
  if (!Number.isFinite(h)) return "";
  const m = Math.round(Math.abs(h) * 60);
  return `${h < 0 ? "−" : ""}${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
};
const pct = (x) => `${+(100 * x).toFixed(1)}%`;

let ctx = null; // { getData, getHome, getSfLoaded, onChange }

// Calibration of the selected ship: { rows, model, mode }.
const load = () => Ships.getCalib();
const save = (d) => Ships.setCalib({ ...load(), ...d });

// The selected ship's fitted model, or null. model.sf_loaded is the SF the
// flights were planned with.
export function getModel() { return load().model; }

function options(selected) {
  const data = ctx.getData();
  const exs = data ? data.exchanges : [selected].filter(Boolean);
  return `<option value=""></option>` + exs.map((e) =>
    `<option value="${esc(e)}"${e === selected ? " selected" : ""}>${esc(e)}</option>`).join("");
}

function routeOf(from, to) {
  const data = ctx.getData();
  return data && from && to && from !== to ? data.routes[`${from}>${to}`] || null : null;
}

function readRows() {
  return [...document.querySelectorAll("#calib tbody tr")].map((tr) => {
    const v = (c) => tr.querySelector(`.${c}`).value.trim();
    return { from: v("c-from"), to: v("c-to"), stl: v("c-stl"), reactor: v("c-re"),
             sfLoaded: v("c-load"), time: v("c-time"), sf: v("c-sf"), ff: v("c-ff") };
  });
}

function persistRows() {
  const d = load();
  d.rows = readRows();
  save(d);
  updateComputed();
}

function rowHtml(r = {}) {
  return `<tr>
    <td><select class="c-from" aria-label="From">${options(r.from)}</select></td>
    <td><select class="c-to" aria-label="To">${options(r.to)}</select></td>
    <td class="n"><input class="c-stl" type="number" min="1" max="100" step="1" value="${esc(r.stl ?? "")}" aria-label="STL %"></td>
    <td class="n"><input class="c-re" type="number" min="1" max="100" step="1" value="${esc(r.reactor ?? "")}" aria-label="Reactor %"></td>
    <td class="n"><input class="c-load" type="number" min="1" step="1" value="${esc(r.sfLoaded ?? "")}" aria-label="SF loaded"></td>
    <td class="n"><input class="c-time time" type="text" placeholder="6h 20m" value="${esc(r.time ?? "")}" aria-label="Flight time"></td>
    <td class="n"><input class="c-sf" type="number" min="0" step="1" value="${esc(r.sf ?? "")}" aria-label="SF used"></td>
    <td class="n"><input class="c-ff" type="number" min="0" step="1" value="${esc(r.ff ?? "")}" aria-label="FF used"></td>
    <td class="n c-pc"></td><td class="n c-model"></td>
    <td><button type="button" class="x" aria-label="Remove flight">×</button></td></tr>`;
}

function renderRows(rows) {
  $("#calib tbody").innerHTML = rows.map(rowHtml).join("");
  for (const btn of document.querySelectorAll("#calib .x")) {
    btn.onclick = () => { btn.closest("tr").remove(); persistRows(); };
  }
  updateComputed();
}

// Parsecs per row, and the model's time next to the measured one.
function updateComputed() {
  const model = load().model;
  for (const tr of document.querySelectorAll("#calib tbody tr")) {
    const v = (c) => tr.querySelector(`.${c}`).value.trim();
    const route = routeOf(v("c-from"), v("c-to"));
    tr.querySelector(".c-pc").textContent = route && route.parsecs != null ? route.parsecs.toFixed(1) : "";
    let txt = "";
    if (model && route && route.parsecs != null && v("c-stl") && v("c-re") && v("c-load")) {
      const h = modelHours(model, route.parsecs, route.jumps, v("c-stl") / 100, v("c-re") / 100, Number(v("c-load")));
      txt = fmtH(h);
      try {
        const actual = parseDuration(v("c-time"));
        if (actual) {
          const d = Math.round((h - actual) * 60);
          txt += ` (${d === 0 ? "±" : d > 0 ? "+" : "−"}${Math.abs(d)}m)`;
        }
      } catch { /* shown when fitting */ }
    }
    tr.querySelector(".c-model").textContent = txt;
  }
}

function addSuggested() {
  const data = ctx.getData();
  if (!data) return status("Market data is still loading. Try again in a moment.", true);
  const home = ctx.getHome() || data.exchanges[0];
  const others = data.exchanges
    .map((e) => ({ e, r: routeOf(home, e) }))
    .filter((x) => x.r && x.r.parsecs != null)
    .sort((a, b) => a.r.parsecs - b.r.parsecs);
  if (!others.length) return status(`No routes with known distances from ${home}.`, true);
  const a = others[0];
  const b = others.find((x) => Math.abs(x.r.parsecs - a.r.parsecs) > 5) || others[others.length - 1];
  const load_ = ctx.getSfLoaded() || "";
  const plan = [[a, 50, 50], [a, 25, 50], [a, 100, 50], [a, 50, 25], [a, 50, 100], [b, 50, 50], [b, 100, 100]];
  const rows = readRows().concat(plan.map(([x, stl, re]) => ({ from: home, to: x.e, stl, reactor: re, sfLoaded: load_ })));
  renderRows(rows);
  persistRows();
  status(`Added 7 flights: ${home} → ${a.e} at five settings and ${home} → ${b.e} at two. Plan each one in SFC and fill in the time, SF and FF it shows.`);
}

function status(msg, error = false) {
  const el = $("#calib-status");
  el.textContent = msg;
  el.classList.toggle("error", error);
}

function fit() {
  const rows = readRows();
  const flights = [], problems = [];
  rows.forEach((r, i) => {
    const n = i + 1;
    if (!r.from && !r.to && !r.time) return;
    const route = routeOf(r.from, r.to);
    if (!route || route.parsecs == null) { problems.push(`Row ${n}: pick two different exchanges with a known route.`); return; }
    let hours;
    try { hours = parseDuration(r.time); } catch (e) { problems.push(`Row ${n}: ${e.message}`); return; }
    const num = (x) => (x === "" ? null : Number(x));
    const f = { parsecs: route.parsecs, jumps: route.jumps, hours,
                stl_usage: num(r.stl) / 100, reactor: num(r.reactor) / 100,
                sf_loaded: num(r.sfLoaded), sf: num(r.sf), ff: num(r.ff) };
    if (!hours || !f.stl_usage || !f.reactor || !f.sf_loaded) { problems.push(`Row ${n}: needs the flight time, both sliders and SF loaded.`); return; }
    if (f.stl_usage > 1 || f.reactor > 1) { problems.push(`Row ${n}: sliders go up to 100%.`); return; }
    flights.push(f);
  });
  if (problems.length) return status(problems.join(" "), true);
  let model;
  const mode = $("#calib-mode").value;
  try { model = fitAuto(flights, mode); } catch (e) { return status(e.message, true); }
  model.sf_loaded = Math.max(...flights.map((f) => f.sf_loaded));
  save({ rows, model, mode });
  status(`${model.kind === "measured" ? "Measured settings" : "Formula"} fitted from ${model.flights} flights. Find trades will now pick the slider settings for each trip with ${Ships.current().name}.`);
  renderResult();
  updateComputed();
  ctx.onChange();
}

function renderResult() {
  const model = load().model;
  const out = $("#calib-result");
  if (!model) { out.innerHTML = ""; return; }
  const data = ctx.getData();
  const first = readRows().find((r) => routeOf(r.from, r.to));
  const route = first && routeOf(first.from, first.to);
  let example = "";
  if (route && route.parsecs != null) {
    const rows = modelGrid(model).map(([stl, re]) => {
      const ship = { flightModel: model, stlUsage: stl, reactorUsage: re, sfLoaded: model.sf_loaded };
      const cost = data ? E.fuelCost(ship, data.books, first.from, route) : NaN;
      return `<tr><td class="n">${pct(stl)}</td><td class="n">${pct(re)}</td>
        <td class="n">${fmtH(modelHours(model, route.parsecs, route.jumps, stl, re, model.sf_loaded))}</td>
        <td class="n">${E.fmt(modelSF(model, stl, model.sf_loaded))}</td>
        <td class="n">${E.fmt(modelFF(model, route.parsecs, re))}</td>
        <td class="n">${E.fmt(cost)}</td></tr>`;
    }).join("");
    example = `<h3>What the sliders do on ${esc(first.from)} → ${esc(first.to)}</h3>
      <p class="muted small">With ${E.fmt(model.sf_loaded)} SF loaded and fuel at today's ${esc(first.from)} asks. Find trades weighs these costs against the price risk and lost depth of the longer flights.</p>
      <div class="table-scroll"><table class="data"><thead><tr><th class="n">STL</th><th class="n">Reactor</th>
      <th class="n">Time</th><th class="n">SF</th><th class="n">FF</th><th class="n">Fuel cost</th></tr></thead>
      <tbody>${rows}</tbody></table></div>`;
  }
  const lines = model.kind === "measured" ? `
      <li>Model: <b>measured settings</b>, a straight line of time against parsecs for each setting.</li>
      ${model.settings.map((x) => `<li>STL ${pct(x.stl)} / reactor ${pct(x.reactor)}: ${fmtH(x.base)} + ${(60 * x.per_pc).toFixed(1)} min per parsec, SF ${Math.round(100 * x.sf_share)}% of loaded, ${x.ff_per_pc.toFixed(2)} FF per parsec.</li>`).join("")}
      <li>Settings the tool may choose: only these.</li>` : `
      <li>Model: <b>formula</b>.</li>
      <li>FTL fuel: <b>${model.ff_per_parsec.toFixed(2)}</b> FF per parsec at 100% reactor (game formula 7.2 for a 2,400 reactor).</li>
      <li>STL fuel: <b>${model.sf_coef.toFixed(3)}</b> × STL slider × SF loaded (community research: 0.494 to 0.988).</li>
      <li>Settings the tool may choose: STL ${pct(model.stl_range[0])} to ${pct(model.stl_range[1])}, reactor ${pct(model.reactor_range[0])} to ${pct(model.reactor_range[1])}.</li>`;
  out.innerHTML = `<div class="fit-summary">
    <h3>${esc(Ships.current().name)}'s flight model</h3>
    <ul>
      <li>Typical time error: <b>${Math.round(60 * model.rmse_hours)} min</b> over ${model.flights} flights.</li>
      ${lines}
      ${model.notes.map((n) => `<li>${esc(n)}</li>`).join("")}
    </ul>
    ${model.rmse_hours > 0.5 ? `<div class="warn">The model misses flights by over half an hour on average. Check the Model time column for rows that are far off (often a typo, or a plan with different cargo or SF loaded).${model.kind === "measured" ? "" : " If the rows are right, choose Measured settings and plan each setting on two routes."}</div>` : ""}
    ${example}</div>`;
}

// Re-draw for the selected ship (after a ship switch or new market data).
export function refreshCalib() {
  if (!ctx) return;
  const d = load();
  $("#calib-ship").textContent = Ships.current().name;
  $("#calib-mode").value = d.mode || "auto";
  status("");
  renderRows(d.rows);
  renderResult();
}

export function initCalib(c) {
  ctx = c;
  refreshCalib();
  $("#calib-mode").onchange = () => save({ mode: $("#calib-mode").value });
  const tbody = $("#calib tbody");
  tbody.addEventListener("input", persistRows);
  tbody.addEventListener("change", persistRows);
  $("#calib-add").onclick = () => {
    const rows = readRows();
    const last = rows[rows.length - 1] || { from: ctx.getHome(), sfLoaded: ctx.getSfLoaded() || "" };
    renderRows(rows.concat([{ from: last.from, to: last.to, stl: last.stl, reactor: last.reactor, sfLoaded: last.sfLoaded }]));
    persistRows();
  };
  $("#calib-suggest").onclick = addSuggested;
  $("#calib-fit").onclick = fit;
  $("#calib-clear").onclick = () => {
    const d = load();
    if (!d.model) return status("No model is on.");
    save({ rows: readRows(), model: null });
    status("Model turned off. Your flights are kept, so you can fit it again.");
    renderResult();
    updateComputed();
    ctx.onChange();
  };
}
