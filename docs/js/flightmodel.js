// Per-ship flight model fitted from flights planned in SFC.
// A line-for-line port of flightmodel.py; see that file for the reasoning.
//
//   hours = overhead + stl / (STL x SF loaded / 1000)
//         + parsecs x jump x e^(beta x (1 - reactor)) + charge x (jumps - 1) x reactor
//   SF    = sfCoef x STL x SF loaded
//   FF    = ffPerParsec x parsecs x reactor
//
// Fallback when that shape doesn't fit a ship ("measured" models): one straight
// line of time against parsecs per slider setting planned on two or more routes.

export const GRID = [0.25, 0.5, 0.75, 1.0];
const BETA_DEFAULT = 1.123, BETA_MAX = 3.0, BETA_STEP = 0.02;
export const MIN_FLIGHTS = 4;
const VARIED = 0.05;
export const AUTO_MAX_ERROR = 0.25; // hours
const MIN_HOURS = 0.5;               // measured lines are never trusted below this

const nearest = (m, stl, re) => m.settings.reduce((best, x) => {
  const d = (x.stl - stl) ** 2 + (x.reactor - re) ** 2;
  const bd = (best.stl - stl) ** 2 + (best.reactor - re) ** 2;
  return d < bd || (d === bd && (x.stl < best.stl || (x.stl === best.stl && x.reactor < best.reactor))) ? x : best;
});
// Closest value to target; ties go to the lower value.
const nearestValue = (vals, target) => vals.reduce((b, v) => {
  const d = Math.abs(v - target), bd = Math.abs(b - target);
  return d < bd || (d === bd && v < b) ? v : b;
});
const mean = (xs) => xs.reduce((a, b) => a + b, 0) / xs.length;

export function modelHours(m, parsecs, jumps, stl, reactor, sfLoaded) {
  if (m.kind === "measured") {
    const x = nearest(m, stl, reactor);
    return Math.max(MIN_HOURS, x.base + x.per_pc * parsecs);
  }
  const burn = stl * sfLoaded / 1000;
  return m.overhead + (burn > 0 ? m.stl / burn : 0)
    + parsecs * m.jump * Math.exp(m.beta * (1 - reactor))
    + m.charge * Math.max(0, jumps - 1) * reactor;
}
export function modelSF(m, stl, sfLoaded) {
  if (m.kind !== "measured") return m.sf_coef * stl * sfLoaded;
  const v = nearestValue(m.settings.map((y) => y.stl), stl);
  return mean(m.settings.filter((y) => y.stl === v).map((y) => y.sf_share)) * sfLoaded;
}
export function modelFF(m, parsecs, reactor) {
  if (m.kind !== "measured") return m.ff_per_parsec * parsecs * reactor;
  const v = nearestValue(m.settings.map((y) => y.reactor), reactor);
  return mean(m.settings.filter((y) => y.reactor === v).map((y) => y.ff_per_pc)) * parsecs;
}

// Ranges of slider values a model covers.
export function modelRanges(m) {
  if (m.kind !== "measured") return { stl: m.stl_range, reactor: m.reactor_range };
  const s = m.settings.map((x) => x.stl), r = m.settings.map((x) => x.reactor);
  return { stl: [Math.min(...s), Math.max(...s)], reactor: [Math.min(...r), Math.max(...r)] };
}

// The nearest settings a model can be trusted at.
export function modelSnap(m, stl, reactor) {
  if (m.kind === "measured") { const x = nearest(m, stl, reactor); return [x.stl, x.reactor]; }
  const clamp = (v, [lo, hi]) => Math.min(hi, Math.max(lo, v));
  return [clamp(stl, m.stl_range), clamp(reactor, m.reactor_range)];
}

// (STL, reactor) pairs to try, inside the measured range only.
export function modelGrid(m) {
  if (m.kind === "measured") return m.settings.map((x) => [x.stl, x.reactor]);
  const values = ([lo, hi]) => {
    const r = (v) => Math.round(v * 1e4) / 1e4;
    const set = new Set(GRID.filter((v) => v >= lo - 1e-9 && v <= hi + 1e-9).map(r));
    set.add(r(lo)); set.add(r(hi));
    return [...set].sort((a, b) => a - b);
  };
  const out = [];
  for (const s of values(m.stl_range)) for (const r of values(m.reactor_range)) out.push([s, r]);
  return out;
}

function nnls(cols, y, iters = 3000) {
  const k = cols.length;
  const dot = (a, b) => a.reduce((t, x, i) => t + x * b[i], 0);
  const g = cols.map((ci) => cols.map((cj) => dot(ci, cj)));
  const h = cols.map((c) => dot(c, y));
  const w = new Array(k).fill(0);
  for (let it = 0; it < iters; it++) {
    let moved = 0;
    for (let j = 0; j < k; j++) {
      if (g[j][j] <= 0) continue;
      let grad = -h[j];
      for (let i = 0; i < k; i++) grad += g[j][i] * w[i];
      const nw = Math.max(0, w[j] - grad / g[j][j]);
      moved = Math.max(moved, Math.abs(nw - w[j]));
      w[j] = nw;
    }
    if (moved < 1e-12) break;
  }
  return w;
}

function throughOrigin(xs, ys) {
  const sxx = xs.reduce((t, x) => t + x * x, 0);
  return sxx > 0 ? xs.reduce((t, x, i) => t + x * ys[i], 0) / sxx : null;
}

const complete = (flights) => flights.filter((f) => f.hours && f.parsecs && f.stl_usage && f.reactor && f.sf_loaded);

// flights: [{parsecs, jumps, stl_usage, reactor, sf_loaded, hours, sf?, ff?}]
// Throws an Error with a readable reason if the data can't support a fit.
export function fitModel(flights) {
  const rows = complete(flights);
  if (rows.length < MIN_FLIGHTS) {
    throw new Error(`Need at least ${MIN_FLIGHTS} complete flights (time, both sliders and SF loaded); have ${rows.length}.`);
  }
  const stlVals = rows.map((f) => f.stl_usage), reVals = rows.map((f) => f.reactor);
  const span = (xs) => Math.max(...xs) - Math.min(...xs);
  const notes = [];
  if (span(stlVals) < VARIED) notes.push(`STL slider was not varied, so the tool will keep it at ${+(100 * stlVals[0]).toFixed(2)}%.`);
  const reVaried = span(reVals) >= VARIED;
  if (!reVaried) notes.push(`Reactor slider was not varied, so the tool will keep it at ${+(100 * reVals[0]).toFixed(2)}%.`);
  if (new Set(rows.map((f) => f.parsecs.toFixed(1))).size < 2) {
    notes.push("All flights are the same route, so times for other routes are a guess. Add a route of a different length.");
  }

  const y = rows.map((f) => f.hours);
  const burn = rows.map((f) => f.stl_usage * f.sf_loaded / 1000);
  const betas = reVaried
    ? Array.from({ length: Math.floor(BETA_MAX / BETA_STEP) + 1 }, (_, i) => Math.round(i * BETA_STEP * 1e4) / 1e4)
    : [BETA_DEFAULT];
  let best = null;
  for (const beta of betas) {
    const cols = [rows.map(() => 1), burn.map((b) => 1 / b),
      rows.map((f) => f.parsecs * Math.exp(beta * (1 - f.reactor))),
      rows.map((f) => Math.max(0, f.jumps - 1) * f.reactor)];
    const w = nnls(cols, y);
    let sse = 0;
    for (let i = 0; i < rows.length; i++) {
      let p = 0;
      for (let j = 0; j < 4; j++) p += w[j] * cols[j][i];
      sse += (p - y[i]) ** 2;
    }
    if (!best || sse < best.sse - 1e-12) best = { sse, beta, w };
  }
  const ffRows = rows.filter((f) => f.ff != null);
  let ffPc = throughOrigin(ffRows.map((f) => f.parsecs * f.reactor), ffRows.map((f) => f.ff));
  const sfRows = rows.filter((f) => f.sf != null);
  let sfCoef = throughOrigin(sfRows.map((f) => f.stl_usage * f.sf_loaded), sfRows.map((f) => f.sf));
  if (ffPc == null) { ffPc = 7.2; notes.push("No FF figures, so the game constant 7.2 FF per parsec is used."); }
  if (sfCoef == null) { sfCoef = 0.494 * 2; notes.push("No SF figures, so the community STL estimate is used."); }
  const [o, st, jp, ch] = best.w;
  return {
    kind: "formula", overhead: o, stl: st, jump: jp, beta: best.beta, charge: ch,
    ff_per_parsec: ffPc, sf_coef: sfCoef,
    stl_range: [Math.min(...stlVals), Math.max(...stlVals)],
    reactor_range: [Math.min(...reVals), Math.max(...reVals)],
    flights: rows.length, rmse_hours: Math.sqrt(best.sse / rows.length), notes,
  };
}

// Straight lines of time against parsecs, one per slider setting planned on
// at least two routes of different length.
export function fitMeasured(flights) {
  const rows = complete(flights);
  const r4 = (v) => Math.round(v * 1e4) / 1e4;
  const groups = new Map();
  for (const f of rows) {
    const k = `${r4(f.stl_usage)}|${r4(f.reactor)}`;
    if (!groups.has(k)) groups.set(k, { stl: r4(f.stl_usage), reactor: r4(f.reactor), fs: [] });
    groups.get(k).fs.push(f);
  }
  const ordered = [...groups.values()].sort((a, b) => a.stl - b.stl || a.reactor - b.reactor);
  const settings = [], used = [], skipped = [];
  for (const { stl, reactor, fs } of ordered) {
    const pcs = fs.map((f) => f.parsecs);
    if (Math.max(...pcs) - Math.min(...pcs) < 1) { skipped.push(`${+(100 * stl).toFixed(2)}/${+(100 * reactor).toFixed(2)}`); continue; }
    const n = fs.length;
    const mx = pcs.reduce((a, b) => a + b, 0) / n, my = fs.reduce((a, f) => a + f.hours, 0) / n;
    let sxx = 0, sxy = 0;
    fs.forEach((f, i) => { sxx += (pcs[i] - mx) ** 2; sxy += (pcs[i] - mx) * (f.hours - my); });
    const perPc = sxy / sxx, base = my - perPc * mx;
    const sfFs = fs.filter((f) => f.sf != null), ffFs = fs.filter((f) => f.ff != null);
    const sfShare = sfFs.length ? sfFs.reduce((a, f) => a + f.sf / f.sf_loaded, 0) / sfFs.length : 0.494 * 2 * stl;
    const ffPc = ffFs.length ? throughOrigin(ffFs.map((f) => f.parsecs), ffFs.map((f) => f.ff)) : 7.2 * reactor;
    settings.push({ stl, reactor, base, per_pc: perPc, sf_share: sfShare, ff_per_pc: ffPc,
                    pc_min: Math.min(...pcs), pc_max: Math.max(...pcs) });
    for (const f of fs) used.push([f, base + perPc * f.parsecs]);
  }
  if (!settings.length) throw new Error("Measured settings need each slider setting planned on at least two routes of different length; none were.");
  const lo = Math.max(...settings.map((x) => x.pc_min)), hi = Math.min(...settings.map((x) => x.pc_max));
  const notes = [`Times are measured between ${lo.toFixed(0)} and ${hi.toFixed(0)} parsecs; shorter or longer routes are estimated by extending the lines. Plan a short and a long route to cover more.`];
  if (skipped.length) notes.push(`Planned on one route only, so not used: ${skipped.join(", ")} (STL/reactor %).`);
  const sse = used.reduce((a, [f, p]) => a + (p - f.hours) ** 2, 0);
  return { kind: "measured", settings, flights: used.length, rmse_hours: Math.sqrt(sse / used.length), notes };
}

// mode: "formula", "measured", or "auto" (formula unless it fits badly).
export function fitAuto(flights, mode = "auto") {
  if (mode === "measured") return fitMeasured(flights);
  const formula = fitModel(flights);
  if (mode === "formula" || formula.rmse_hours <= AUTO_MAX_ERROR) return formula;
  const miss = Math.round(60 * formula.rmse_hours);
  let measured;
  try { measured = fitMeasured(flights); } catch (e) {
    formula.notes.push(`The formula misses by ${miss} min on average. ${e.message} Plan your settings on a second route to use them instead.`);
    return formula;
  }
  measured.notes.push(`The formula missed by ${miss} min on average, so the tool uses your measured settings instead.`);
  return measured;
}

// Hours from "6h 20m 57s", "1d 2h", "6:20", "6:20:57" or "6.35".
export function parseDuration(text) {
  const s = String(text ?? "").trim().toLowerCase();
  if (!s) return null;
  const bad = () => new Error(`Can't read the time "${text}". Use e.g. 6h 20m or 6:20.`);
  if (s.includes(":")) {
    const p = s.split(":").map(Number);
    if (p.some((x) => !Number.isFinite(x))) throw bad();
    while (p.length < 3) p.push(0);
    return p[0] + p[1] / 60 + p[2] / 3600;
  }
  const units = { d: 24, h: 1, m: 1 / 60, s: 1 / 3600 };
  let total = 0, num = "", seen = false;
  for (const ch of s.replace(/\s+/g, "")) {
    if (/[0-9.]/.test(ch)) num += ch;
    else if (ch in units && num) { total += parseFloat(num) * units[ch]; num = ""; seen = true; }
    else throw bad();
  }
  if (num) { if (seen) throw bad(); total += parseFloat(num); }
  return total;
}
