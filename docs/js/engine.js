// Calculation engine for the pu-arbitrage web app.
// A direct port of arbitrage.py and borders.py: same models, same numbers.
// No DOM access here, so the engine can be tested on its own.

import { modelHours, modelSF, modelFF, modelGrid } from "./flightmodel.js";

export const C = {
  FEE_RATE: 0.01,            // exchange fee per trade (placeholder)
  UNLIMITED: 1e9,            // quantity for orders with no stated size
  // Depth left on arrival = exp(-TAKER_SHARE x daily units traded x days in
  // transit / units on the book), floored at MIN_DEPTH_SHARE.
  TAKER_SHARE: 0.5,          // share of traded volume assumed to hit the side you need
  MIN_DEPTH_SHARE: 0.1,
  DEST_DEPTH_SHARE: 0.5,     // fallback when FIO gives no volume for an item
  MAX_CANDIDATES: 40,
  MAX_CANDIDATES_CYCLE: 25,
  HISTORY_DAYS: 30,
  MIN_HISTORY_POINTS: 10,
  DEFAULT_DAILY_VOL: 0.06,
  REVERSION: 0.25,
  MAX_HAIRCUT: 0.5,
  SOLVER_SECONDS: 20,
  FF_PER_PARSEC: 7.2,        // FF per parsec at 100% reactor usage (community-derived)
  DEFAULT_REACTOR_USAGE: 0.25,
  // STL: approach burns STL_BURN x STL slider x SF loaded; departure the same
  // at most (less if the gate is close). Community research (prun-toolset).
  STL_BURN: 0.494,
  DEFAULT_STL_USAGE: 0.5,
  STL_DEPARTURE_SHARE: 1,
  REFINE_TOP: 5,             // best trips whose slider settings get optimised
  RERUN_SETTINGS: 2,         // settings per trip re-run in full after screening
};

// Built-in currency map, used only when FIO does not say (same as currencies.py).
export const DEFAULT_CURRENCY = {
  AI1: "AIC", CI1: "CIS", CI2: "CIS", IC1: "ICA", NC1: "NCC", NC2: "NCC",
};

export function sameCurrency(cur, a, b) {
  return a === b || (cur[a] != null && cur[a] === cur[b]);
}

// ---------------------------------------------------------------------------
// Statistics helpers (population standard deviation, as Python's pstdev)
// ---------------------------------------------------------------------------
const mean = (xs) => xs.reduce((s, x) => s + x, 0) / xs.length;
function pstdev(xs) {
  const m = mean(xs);
  return Math.sqrt(xs.reduce((s, x) => s + (x - m) ** 2, 0) / xs.length);
}
function median(xs) {
  const s = [...xs].sort((a, b) => a - b);
  const n = s.length;
  return n % 2 ? s[(n - 1) / 2] : (s[n / 2 - 1] + s[n / 2]) / 2;
}

// ---------------------------------------------------------------------------
// Market data
// ---------------------------------------------------------------------------
function pick(rec, ...keys) {
  for (const k of keys) if (rec[k] !== undefined && rec[k] !== null) return rec[k];
  return null;
}

function parseOrders(rows, descending) {
  const out = [];
  for (const r of rows || []) {
    const price = pick(r, "ItemCost", "Price");
    if (price === null) continue;
    const hasQty = ["ItemCount", "Amount", "Quantity"].some((k) => k in r);
    let qty = pick(r, "ItemCount", "Amount", "Quantity");
    if (qty === null) {
      if (!hasQty) continue;
      qty = C.UNLIMITED; // explicit null size: treated as unlimited
    }
    out.push([Number(price), Number(qty)]);
  }
  out.sort((x, y) => (descending ? y[0] - x[0] : x[0] - y[0]));
  return out;
}

export class BookStore {
  // records: the /exchange/full response
  constructor(records) {
    this.books = new Map();
    this.byEx = new Map();
    this.currency = {};
    this.traded = new Map(); // "T.EX" -> units traded in the last 24 hours
    let withOrders = 0;
    for (const rec of records) {
      const t = rec.MaterialTicker, ex = rec.ExchangeCode;
      if (!t || !ex) continue;
      if (!this.byEx.has(ex)) this.byEx.set(ex, new Set());
      this.byEx.get(ex).add(t);
      const cur = pick(rec, "CurrencyCode", "Currency");
      if (typeof cur === "string" && cur.trim()) this.currency[ex] = cur.trim().toUpperCase();
      if (rec.Traded != null) this.traded.set(`${t}.${ex}`, Number(rec.Traded));
      if ("BuyingOrders" in rec || "SellingOrders" in rec) {
        withOrders++;
        this.books.set(`${t}.${ex}`, {
          asks: parseOrders(rec.SellingOrders, false),
          bids: parseOrders(rec.BuyingOrders, true),
        });
      }
    }
    if (!withOrders) throw new Error("FIO's /exchange/full has no order books.");
  }
  get exchanges() { return [...this.byEx.keys()].sort(); }
  tickersAt(ex) { return this.byEx.get(ex) || new Set(); }
  get(ticker, ex) { return this.books.get(`${ticker}.${ex}`) || null; }
  common(a, b) {
    const B = this.tickersAt(b);
    return [...this.tickersAt(a)].filter((t) => B.has(t)).sort();
  }
}

// ---------------------------------------------------------------------------
// Order book walking, VWAP and slippage
// ---------------------------------------------------------------------------
export function walkBook(levels, qty) {
  let filled = 0, value = 0;
  for (const [price, avail] of levels) {
    if (filled >= qty) break;
    const take = Math.min(avail, qty - filled);
    filled += take;
    value += take * price;
  }
  return [filled, value];
}

export function slippage(levels, qty) {
  const [filled, value] = walkBook(levels, qty);
  if (!filled || !levels.length) return NaN;
  return Math.abs(value / filled / levels[0][0] - 1);
}

// Share of the displayed book expected to still be there after `hours`.
export function depthShare(levels, dailyUnits, hours) {
  if (dailyUnits == null || !Number.isFinite(dailyUnits)) return C.DEST_DEPTH_SHARE;
  const depth = levels.reduce((t, [, q]) => t + (q < C.UNLIMITED ? q : 0), 0);
  if (depth <= 0) return 1;
  const used = C.TAKER_SHARE * dailyUnits * hours / 24;
  return Math.max(C.MIN_DEPTH_SHARE, Math.exp(-used / depth));
}

function marketVolume(m, t, ex) {
  const v = m.history.dailyVolume ? m.history.dailyVolume(t, ex) : null;
  return v != null ? v : (m.books.traded.get(`${t}.${ex}`) ?? null);
}

export class Pricing {
  constructor({ fee = C.FEE_RATE, haircut = 0, markup = 0, exportDuty = 0, importDuty = 0 } = {}) {
    Object.assign(this, { fee, haircut, markup, exportDuty, importDuty });
  }
  unitCost(ask) {
    const v = ask * (1 + this.markup);
    return [v * (1 + this.fee + this.exportDuty), v * this.exportDuty];
  }
  unitRev(bid) {
    const v = bid * (1 - this.haircut);
    return [v * (1 - this.fee - this.importDuty), v * this.importDuty];
  }
}

// Walk the buy-side asks and sell-side bids together, one price level at a
// time. Each lot is a stretch where both prices are constant.
export function buildLots(ticker, asks, bids, weight, volume, pricing,
                          { destShare = C.DEST_DEPTH_SHARE, askShare = 1,
                            requireProfit = true, maxQty = null } = {}) {
  const scale = (levels, share) =>
    levels.map(([p, q]) => [p, q >= C.UNLIMITED ? q : Math.floor(q * share)]);
  asks = scale(asks, askShare);
  bids = scale(bids, destShare);
  const lots = [];
  let i = 0, j = 0, total = 0;
  let ra = asks.length ? asks[0][1] : 0;
  let rb = bids.length ? bids[0][1] : 0;
  while (i < asks.length && j < bids.length) {
    const [unitCost, buyDuty] = pricing.unitCost(asks[i][0]);
    const [unitRev, sellDuty] = pricing.unitRev(bids[j][0]);
    if (requireProfit && unitRev <= unitCost) break;
    const q = Math.min(ra, rb);
    if (q > 0) {
      lots.push({ ticker, qty: q, buy: asks[i][0], sell: bids[j][0], unitCost,
                  unitRev, weight, volume, buyDuty, sellDuty,
                  margin: unitRev - unitCost });
      total += q;
      if (maxQty !== null && total >= maxQty) break;
    }
    ra -= q; rb -= q;
    if (ra <= 0) { i++; ra = i < asks.length ? asks[i][1] : 0; }
    if (rb <= 0) { j++; rb = j < bids.length ? bids[j][1] : 0; }
  }
  return lots;
}

// ---------------------------------------------------------------------------
// Solvers
// ---------------------------------------------------------------------------
function naturalCap(lot, capital, maxW, maxV) {
  return Math.floor(Math.min(lot.qty, capital / lot.unitCost, maxW / lot.weight,
                             maxV / lot.volume));
}

function num(v) {
  if (!Number.isFinite(v)) throw new Error(`bad coefficient ${v}`);
  return String(Number(v.toPrecision(15)));
}

// Build a CPLEX LP-format model. rows: [{name, coefs: [[varIndex, coef]], ub}]
function lpModel(sense, objective, rows, caps) {
  const term = (c, k) => `${c < 0 ? "-" : "+"} ${num(Math.abs(c))} x${k}`;
  const wrap = (terms) => {
    const out = [];
    for (let i = 0; i < terms.length; i += 8) out.push("  " + terms.slice(i, i + 8).join(" "));
    return out.join("\n");
  };
  const objTerms = objective.map((c, k) => [c, k]).filter(([c]) => c !== 0).map(([c, k]) => term(c, k));
  const lines = [sense, " obj:", wrap(objTerms.length ? objTerms : ["0 x0"]), "Subject To"];
  for (const r of rows) {
    const terms = r.coefs.filter(([, c]) => c !== 0).map(([k, c]) => term(c, k));
    if (!terms.length) continue;
    lines.push(` ${r.name}:`, wrap(terms), `  <= ${num(r.ub)}`);
  }
  lines.push("Bounds");
  caps.forEach((cap, k) => lines.push(` 0 <= x${k} <= ${num(cap)}`));
  lines.push("General", wrap(caps.map((_, k) => `x${k}`)), "End");
  return lines.join("\n");
}

function runSolver(solver, model) {
  if (!solver) return null;
  try {
    const res = solver.solve(model, { time_limit: C.SOLVER_SECONDS, output_flag: false });
    if (!res || !res.Columns) return null;
    if (!["Optimal", "Time limit reached"].includes(res.Status)) return null;
    return (k) => (res.Columns[`x${k}`] ? res.Columns[`x${k}`].Primal : 0);
  } catch (err) {
    console.warn("solver failed, falling back", err);
    return null;
  }
}

function greedy(items, capital, maxW, maxV) {
  const scarcity = (l) => Math.max(l.unitCost / capital, l.weight / maxW, l.volume / maxV);
  const sorted = [...items].sort((a, b) => b.lot.margin / scarcity(b.lot) - a.lot.margin / scarcity(a.lot));
  let cash = capital, wt = maxW, vol = maxV;
  const chosen = [];
  for (const { lot, cap } of sorted) {
    const u = Math.floor(Math.min(cap, cash / lot.unitCost, wt / lot.weight, vol / lot.volume));
    if (u >= 1) {
      chosen.push([lot, u]);
      cash -= u * lot.unitCost; wt -= u * lot.weight; vol -= u * lot.volume;
    }
  }
  return chosen;
}

// Choose integer units of every lot to maximise margin under capital,
// weight and volume limits (a three-constraint knapsack).
export function optimizeBasket(lots, capital, maxW, maxV, solver) {
  if (capital <= 0) return [];
  const items = [];
  for (const lot of lots) {
    if (lot.margin > 0 && lot.weight > 0 && lot.volume > 0) {
      const cap = naturalCap(lot, capital, maxW, maxV);
      if (cap >= 1) items.push({ lot, cap });
    }
  }
  if (!items.length) return [];
  const model = lpModel("Maximize", items.map((i) => i.lot.margin), [
    { name: "cash", coefs: items.map((i, k) => [k, i.lot.unitCost]), ub: capital },
    { name: "weight", coefs: items.map((i, k) => [k, i.lot.weight]), ub: maxW },
    { name: "volume", coefs: items.map((i, k) => [k, i.lot.volume]), ub: maxV },
  ], items.map((i) => i.cap));
  const x = runSolver(solver, model);
  if (x) {
    const units = items.map((_, k) => Math.floor(x(k) + 1e-6));
    const used = (f) => items.reduce((s, it, k) => s + units[k] * f(it.lot), 0);
    if (used((l) => l.unitCost) <= capital + 1e-6 && used((l) => l.weight) <= maxW + 1e-6
        && used((l) => l.volume) <= maxV + 1e-6) {
      return items.map((it, k) => [it.lot, units[k]]).filter(([, u]) => u >= 1);
    }
  }
  return greedy(items, capital, maxW, maxV);
}

// Both legs of A > B > F as one integer program (see arbitrage.py).
export function optimizeCycle(lots1, lots2, capital, homeUpfront, midFixed, maxW, maxV,
                              midConvertible, solver) {
  const budget = capital - homeUpfront;
  if (!solver || budget <= 0) return null;
  const items1 = lots1.map((l) => ({ lot: l, cap: naturalCap(l, budget, maxW, maxV) })).filter((i) => i.cap >= 1);
  const items2 = lots2.map((l) => ({ lot: l, cap: naturalCap(l, Infinity, maxW, maxV) })).filter((i) => i.cap >= 1);
  if (!items1.length || !items2.length) return null;
  const n1 = items1.length, n2 = items2.length, n = n1 + n2;
  const cost1 = items1.map((i) => i.lot.unitCost), rev1 = items1.map((i) => i.lot.unitRev);
  const cost2 = items2.map((i) => i.lot.unitCost), rev2 = items2.map((i) => i.lot.unitRev);

  // A: rows x columns, ub: right-hand sides
  const A = Array.from({ length: 6 }, () => new Array(n).fill(0));
  items1.forEach((it, k) => {
    A[0][k] = cost1[k]; A[1][k] = it.lot.weight; A[2][k] = it.lot.volume; A[5][k] = -rev1[k];
  });
  items2.forEach((it, k) => {
    A[3][n1 + k] = it.lot.weight; A[4][n1 + k] = it.lot.volume; A[5][n1 + k] = cost2[k];
  });
  const ub = [budget, maxW, maxV, maxW, maxV, -midFixed];
  const objective = midConvertible
    ? [...cost1.map((c, k) => c - rev1[k]), ...cost2.map((c, k) => c - rev2[k])]
    : [...cost1, ...rev2.map((r) => -r)];
  const names = ["cash", "w1", "v1", "w2", "v2", "mid"];
  const model = lpModel("Minimize", objective,
    A.map((row, r) => ({ name: names[r], coefs: row.map((c, k) => [k, c]), ub: ub[r] })),
    [...items1.map((i) => i.cap), ...items2.map((i) => i.cap)]);
  const sol = runSolver(solver, model);
  if (!sol) return null;
  const x = Array.from({ length: n }, (_, k) => Math.round(sol(k)));
  const dot = (row) => row.reduce((s, c, k) => s + c * x[k], 0);

  // Rounding can leave the mid-currency budget a hair short: trim the least
  // efficient leg 2 units until it balances.
  while (dot(A[5]) > ub[5] + 1e-6) {
    const held = [...Array(n2).keys()].filter((j) => x[n1 + j] >= 1);
    if (!held.length) return null;
    const j = held.reduce((b, k) => (rev2[k] / cost2[k] < rev2[b] / cost2[b] ? k : b));
    x[n1 + j] -= Math.max(1, Math.min(x[n1 + j], Math.ceil((dot(A[5]) - ub[5]) / cost2[j])));
  }
  for (let r = 0; r < 5; r++) if (dot(A[r]) > ub[r] + 1e-6) return null;
  const b1 = items1.map((it, k) => [it.lot, x[k]]).filter(([, u]) => u >= 1);
  const b2 = items2.map((it, k) => [it.lot, x[n1 + k]]).filter(([, u]) => u >= 1);
  return b1.length && b2.length ? [b1, b2] : null;
}

// ---------------------------------------------------------------------------
// Border and gateway fees (port of borders.py)
// ---------------------------------------------------------------------------
const FIELDS = ["exit_fee", "entry_fee", "export_duty", "import_duty"];

export function regionFromCode(ex) {
  const m = /^[A-Za-z]+/.exec(ex || "");
  return m ? m[0].toUpperCase() : ex;
}

export class BorderModel {
  // config: {default, regions, routes, exchange_regions} as in borders.json
  constructor(exchanges, fioRegions = {}, config = {}) {
    const clean = (o) => Object.fromEntries(Object.entries(o || {}).filter(([k]) => !k.startsWith("_")));
    const check = (where, rules) => {
      const out = {};
      for (const [k, v] of Object.entries(clean(rules))) {
        if (!FIELDS.includes(k)) throw new Error(`${where}: unknown field ${k}`);
        if (typeof v !== "number" || v < 0) throw new Error(`${where}.${k} must be a number >= 0`);
        if (k.endsWith("duty") && v >= 1) throw new Error(`${where}.${k} is a fraction (0.02 = 2%)`);
        out[k] = v;
      }
      return out;
    };
    this.def = check("default", config.default);
    this.regionRules = {};
    for (const [r, v] of Object.entries(clean(config.regions))) this.regionRules[r.toUpperCase()] = check(`regions.${r}`, v);
    this.routes = {};
    for (const [key, v] of Object.entries(clean(config.routes))) {
      if (!key.includes(">")) throw new Error(`route key ${key} must look like CI1>NC1`);
      const [a, b] = key.split(">").map((s) => s.trim().toUpperCase());
      this.routes[`${a}>${b}`] = check(`routes.${key}`, v);
    }
    const overrides = clean(config.exchange_regions);
    this.regionOf = {};
    for (const ex of exchanges) {
      this.regionOf[ex] = (overrides[ex] || fioRegions[ex] || regionFromCode(ex)).toUpperCase();
    }
    this.cache = new Map();
  }
  region(ex) { return this.regionOf[ex] || regionFromCode(ex); }
  rule(region, key) {
    const r = this.regionRules[region] || {};
    return r[key] ?? this.def[key] ?? 0;
  }
  crossing(a, b) {
    const key = `${a}>${b}`;
    if (this.cache.has(key)) return this.cache.get(key);
    const ra = this.region(a), rb = this.region(b);
    let c = { origin: a, dest: b, fromRegion: ra, toRegion: rb,
              exit_fee: 0, entry_fee: 0, export_duty: 0, import_duty: 0 };
    if (ra !== rb) {
      c = { ...c, exit_fee: this.rule(ra, "exit_fee"), export_duty: this.rule(ra, "export_duty"),
            entry_fee: this.rule(rb, "entry_fee"), import_duty: this.rule(rb, "import_duty") };
    }
    for (const o of [this.routes[`${ra}>${rb}`], this.routes[key]]) if (o) c = { ...c, ...o };
    this.cache.set(key, c);
    return c;
  }
}

export function describeCrossing(c) {
  const parts = [];
  if (c.exit_fee) parts.push(`exit fee ${fmt(c.exit_fee)}`);
  if (c.entry_fee) parts.push(`gateway/entry fee ${fmt(c.entry_fee)}`);
  if (c.export_duty) parts.push(`export duty ${100 * c.export_duty}%`);
  if (c.import_duty) parts.push(`import duty ${100 * c.import_duty}%`);
  return parts.length ? parts.join(", ") : "no fees";
}

export const fmt = (x) => (Number.isFinite(x) ? Math.round(x).toLocaleString("en-US") : "n/a");

// ---------------------------------------------------------------------------
// Fuel (game model: FF = parsecs x 7.2 x reactor usage;
// SF = 0.494 x STL slider x SF loaded, for approach and departure)
// ---------------------------------------------------------------------------
export function sfUnits(ship) {
  if (ship.sfPerTrip != null) return ship.sfPerTrip;
  if (ship.flightModel && ship.sfLoaded) return modelSF(ship.flightModel, ship.stlUsage, ship.sfLoaded);
  if (!ship.sfLoaded) return 0;
  const usage = ship.stlUsage ?? C.DEFAULT_STL_USAGE;
  const dep = ship.stlDepartureShare ?? C.STL_DEPARTURE_SHARE;
  return C.STL_BURN * usage * ship.sfLoaded * (1 + dep);
}

export function fuelUnits(ship, route, roundTrip = false) {
  const legs = roundTrip ? 2 : 1;
  const sf = sfUnits(ship) * legs;
  let ff = 0;
  if (ship.flightModel) {
    if (route.parsecs == null) return null;
    ff = modelFF(ship.flightModel, route.parsecs, ship.reactorUsage) * legs;
  } else if (ship.ffPerParsec && ship.reactorUsage) {
    if (route.parsecs == null) return null;
    ff = route.parsecs * ship.ffPerParsec * ship.reactorUsage * legs;
  }
  return [sf, ff];
}

// Whether the tanks hold the fuel for `flights` flights of route, bought
// before leaving. This is what limits range. Unset tank sizes aren't checked.
export function fuelFits(ship, route, flights = 1) {
  if (ship.stlTank && sfUnits(ship) * flights > ship.stlTank) return false;
  if (ship.ftlTank && route.parsecs != null) {
    const ff = ship.flightModel ? modelFF(ship.flightModel, route.parsecs, ship.reactorUsage)
      : route.parsecs * (ship.ffPerParsec || 0) * ship.reactorUsage;
    return ff * flights <= ship.ftlTank;
  }
  return true;
}

export function fuelCost(ship, books, origin, route, roundTrip = false) {
  const u = fuelUnits(ship, route, roundTrip);
  if (!u) return NaN;
  let total = 0;
  for (const [ticker, units] of [["SF", u[0]], ["FF", u[1]]]) {
    if (units <= 0) continue;
    const book = books.get(ticker, origin);
    if (!book || !book.asks.length) return NaN;
    const [filled, value] = walkBook(book.asks, units);
    if (filled < units * 0.999) return NaN;
    total += value;
  }
  return total;
}

// ---------------------------------------------------------------------------
// Price risk
// ---------------------------------------------------------------------------
export function priceRisk(closes, refPrice, hours, side) {
  if (closes.length < C.MIN_HISTORY_POINTS) {
    return { vol: C.DEFAULT_DAILY_VOL * Math.sqrt(hours / 24), adj: 0, z: NaN, thin: true };
  }
  const rets = [];
  for (let i = 1; i < closes.length; i++) {
    if (closes[i - 1] > 0 && closes[i] > 0) rets.push(Math.log(closes[i] / closes[i - 1]));
  }
  const daily = rets.length >= 2 ? pstdev(rets) : C.DEFAULT_DAILY_VOL;
  const m = mean(closes), sd = pstdev(closes);
  const z = sd > 0 ? (refPrice - m) / sd : 0;
  const gap = side === "sell" ? refPrice - m : m - refPrice;
  const adj = Math.min(C.MAX_HAIRCUT, C.REVERSION * Math.max(0, gap / refPrice));
  return { vol: daily * Math.sqrt(hours / 24), adj, z, thin: false };
}

// ---------------------------------------------------------------------------
// Route evaluation
// ---------------------------------------------------------------------------
function tradable(m, t, src, dst) {
  const w = m.materials[t];
  const a = m.books.get(t, src), b = m.books.get(t, dst);
  if (w && w[0] && w[1] && a && b && a.asks.length && b.bids.length) return { w: w[0], v: w[1], a, b };
  return null;
}

export function legInfo(routes, ship, a, b) {
  const r = routes[`${a}>${b}`];
  if (!r) return null;
  if (!fuelFits(ship, r)) return null; // more fuel than the tanks hold
  if (ship.flightModel) {
    if (r.parsecs == null) return null;
    return { route: r, hours: modelHours(ship.flightModel, r.parsecs, r.jumps, ship.stlUsage,
                                         ship.reactorUsage, ship.sfLoaded || 0) };
  }
  return { route: r, hours: ship.overheadHours + ship.hoursPerJump * r.jumps };
}

export const settingsLabel = (ship) =>
  `${Math.round(100 * ship.stlUsage)}/${Math.round(100 * ship.reactorUsage)}`;

// One-way trip within one currency. Returns a result object or null.
export async function evaluateRoute(origin, dest, leg, ship, s, m) {
  const { books, history } = m;
  const hours = leg.hours * (s.roundTrip ? 2 : 1);
  const out = m.borders.crossing(origin, dest);
  const back = s.roundTrip ? m.borders.crossing(dest, origin) : null;
  const crossings = back ? [out, back] : [out];
  const borderFees = crossings.reduce((t, c) => t + c.exit_fee + c.entry_fee, 0);

  if (s.roundTrip && !fuelFits(ship, leg.route, 2)) return null; // can't carry fuel for both ways
  const fuel = fuelCost(ship, books, origin, leg.route, s.roundTrip);
  if (Number.isNaN(fuel)) return null;
  const goodsBudget = s.capital - fuel - out.exit_fee;
  if (goodsBudget <= 0) return null;

  const base = new Pricing({ fee: s.feeRate, exportDuty: out.export_duty, importDuty: out.import_duty });
  let cands = [];
  for (const t of books.common(origin, dest)) {
    const tr = tradable(m, t, origin, dest);
    if (!tr) continue;
    const spread = base.unitRev(tr.b.bids[0][0])[0] / base.unitCost(tr.a.asks[0][0])[0] - 1;
    if (spread > 0) cands.push([spread, t]);
  }
  cands = cands.sort((x, y) => y[0] - x[0] || (y[1] > x[1] ? 1 : -1)).slice(0, C.MAX_CANDIDATES).map(([, t]) => t);
  if (!cands.length) return null;

  await history.prefetch(cands.map((t) => [t, dest]));
  const lots = [], risk = {}, share = {};
  for (const t of cands) {
    const [w, v] = m.materials[t];
    const a = books.get(t, origin), b = books.get(t, dest);
    risk[t] = priceRisk(history.closes(t, dest), b.bids[0][0], leg.hours, "sell");
    share[t] = depthShare(b.bids, marketVolume(m, t, dest), leg.hours);
    lots.push(...buildLots(t, a.asks, b.bids, w, v, new Pricing({
      fee: s.feeRate, haircut: risk[t].adj, exportDuty: out.export_duty, importDuty: out.import_duty }),
      { destShare: share[t] }));
  }
  const basket = optimizeBasket(lots, goodsBudget, ship.weight, ship.volume, m.solver);
  if (!basket.length) return null;

  const agg = new Map();
  for (const [lot, u] of basket) {
    const r = agg.get(lot.ticker) || { units: 0, cost: 0, rev: 0, duty: 0, rawBuy: 0, rawSell: 0 };
    r.units += u; r.cost += u * lot.unitCost; r.rev += u * lot.unitRev;
    r.duty += u * (lot.buyDuty + lot.sellDuty); r.rawBuy += u * lot.buy; r.rawSell += u * lot.sell;
    agg.set(lot.ticker, r);
  }
  const items = [...agg].map(([t, r]) => ({
    ticker: t, units: r.units, buy_vwap: r.rawBuy / r.units, sell_vwap: r.rawSell / r.units,
    buySlip: slippage(books.get(t, origin).asks, r.units),
    sellSlip: slippage(books.get(t, dest).bids, r.units / share[t]), depthShare: share[t],
    profit: r.rev - r.cost, sales: r.rev, cost: r.cost, duty: r.duty,
    weight: r.units * m.materials[t][0], volume: r.units * m.materials[t][1],
    z: risk[t].z, vol: risk[t].vol, thin: risk[t].thin,
  })).sort((x, y) => y.profit - x.profit);

  const sales = items.reduce((t, i) => t + i.sales, 0);
  const purchases = items.reduce((t, i) => t + i.cost, 0);
  const net = sales - purchases - fuel - borderFees;
  if (net <= 0) return null;
  const sigma = Math.sqrt(items.reduce((t, i) => t + (i.sales * i.vol) ** 2, 0));
  const adj = net - s.riskAversion * sigma;
  return {
    kind: "one-way", label: `${origin}>${dest}`, origin, dest, hours, items,
    legs: [{ from: origin, to: dest, items }],
    weightUsed: items.reduce((t, i) => t + i.weight, 0),
    volumeUsed: items.reduce((t, i) => t + i.volume, 0),
    sales, purchases, fuel, borderFees, duties: items.reduce((t, i) => t + i.duty, 0),
    crossings, netProfit: net, risk: sigma, riskAdj: adj, score: adj / hours,
    capitalUsed: purchases + fuel + out.exit_fee, fuelHome: fuel,
  };
}

function midPrice(book) {
  if (book && book.asks.length && book.bids.length) return (book.asks[0][0] + book.bids[0][0]) / 2;
  if (book && book.asks.length) return book.asks[0][0];
  if (book && book.bids.length) return book.bids[0][0];
  return null;
}

function impliedRate(books, src, dst) {
  const ratios = [];
  for (const t of books.common(src, dst)) {
    const a = midPrice(books.get(t, src)), b = midPrice(books.get(t, dst));
    if (a && b) ratios.push(b / a);
  }
  return ratios.length >= 3 ? median(ratios) : null;
}

function cycleCandidates(m, src, dst, rate, k, pricing) {
  const out = [];
  for (const t of m.books.common(src, dst)) {
    const tr = tradable(m, t, src, dst);
    if (!tr) continue;
    const premium = pricing.unitRev(tr.b.bids[0][0])[0] / (pricing.unitCost(tr.a.asks[0][0])[0] * rate) - 1;
    out.push([premium, t]);
  }
  return out.sort((x, y) => y[0] - x[0] || (y[1] > x[1] ? 1 : -1)).slice(0, k).map(([, t]) => t);
}

function legItems(basket, materials) {
  const agg = new Map();
  for (const [lot, u] of basket) {
    const r = agg.get(lot.ticker) || { units: 0, spent: 0, received: 0, buyDuty: 0, sellDuty: 0, rawBuy: 0, rawSell: 0 };
    r.units += u; r.spent += u * lot.unitCost; r.received += u * lot.unitRev;
    r.buyDuty += u * lot.buyDuty; r.sellDuty += u * lot.sellDuty;
    r.rawBuy += u * lot.buy; r.rawSell += u * lot.sell;
    agg.set(lot.ticker, r);
  }
  return [...agg].sort((x, y) => y[1].spent - x[1].spent).map(([t, r]) => ({
    ticker: t, units: r.units, spent: r.spent, received: r.received,
    buyDuty: r.buyDuty, sellDuty: r.sellDuty,
    buy_vwap: r.rawBuy / r.units, sell_vwap: r.rawSell / r.units,
    weight: r.units * materials[t][0], volume: r.units * materials[t][1],
  }));
}

// A > B > F where F uses A's currency.
export async function evaluateCycle(a, b, f, leg1, leg2, ship, s, m) {
  const { books, history, materials } = m;
  const h1 = leg1.hours, h2 = leg2.hours;
  const midConvertible = sameCurrency(m.currency, a, b);
  const rateAB = impliedRate(books, a, b), rateBF = impliedRate(books, b, f);
  if (!rateAB || !rateBF) return null;
  const fuel1 = fuelCost(ship, books, a, leg1.route);
  const fuel2 = fuelCost(ship, books, b, leg2.route);
  if (Number.isNaN(fuel1) || Number.isNaN(fuel2)) return null;

  const x1 = m.borders.crossing(a, b), x2 = m.borders.crossing(b, f);
  const homeUpfront = fuel1 + x1.exit_fee;
  const midFixed = x1.entry_fee + fuel2 + x2.exit_fee;
  const homeAfter = x2.entry_fee;

  const p1 = new Pricing({ fee: s.feeRate, exportDuty: x1.export_duty, importDuty: x1.import_duty });
  const p2 = new Pricing({ fee: s.feeRate, exportDuty: x2.export_duty, importDuty: x2.import_duty });
  const c1 = cycleCandidates(m, a, b, rateAB, C.MAX_CANDIDATES_CYCLE, p1);
  const c2 = cycleCandidates(m, b, f, rateBF, C.MAX_CANDIDATES_CYCLE, p2);
  if (!c1.length || !c2.length) return null;
  const maxQty = (t) => Math.ceil(Math.min(ship.weight / materials[t][0], ship.volume / materials[t][1])) + 1;

  await history.prefetch([...c1.map((t) => [t, b]), ...c2.map((t) => [t, b]), ...c2.map((t) => [t, f])]);
  const lots1 = [], lots2 = [], risk1 = {}, risk2b = {}, risk2f = {};
  for (const t of c1) {
    const bkA = books.get(t, a), bkB = books.get(t, b);
    risk1[t] = priceRisk(history.closes(t, b), bkB.bids[0][0], h1, "sell");
    lots1.push(...buildLots(t, bkA.asks, bkB.bids, ...materials[t],
      new Pricing({ fee: s.feeRate, haircut: risk1[t].adj, exportDuty: x1.export_duty, importDuty: x1.import_duty }),
      { destShare: depthShare(bkB.bids, marketVolume(m, t, b), h1), requireProfit: false, maxQty: maxQty(t) }));
  }
  for (const t of c2) {
    const bkB = books.get(t, b), bkF = books.get(t, f);
    risk2b[t] = priceRisk(history.closes(t, b), bkB.asks[0][0], h1, "buy");
    risk2f[t] = priceRisk(history.closes(t, f), bkF.bids[0][0], h1 + h2, "sell");
    lots2.push(...buildLots(t, bkB.asks, bkF.bids, ...materials[t],
      new Pricing({ fee: s.feeRate, haircut: risk2f[t].adj, markup: risk2b[t].adj,
                    exportDuty: x2.export_duty, importDuty: x2.import_duty }),
      { askShare: depthShare(bkB.asks, marketVolume(m, t, b), h1),
        destShare: depthShare(bkF.bids, marketVolume(m, t, f), h1 + h2),
        requireProfit: false, maxQty: maxQty(t) }));
  }
  const sol = optimizeCycle(lots1, lots2, s.capital, homeUpfront, midFixed, ship.weight,
                            ship.volume, midConvertible, m.solver);
  if (!sol) return null;
  const [b1, b2] = sol;
  const sum = (bs, f_) => bs.reduce((t, [l, u]) => t + u * f_(l), 0);
  const cost1 = sum(b1, (l) => l.unitCost), rev1 = sum(b1, (l) => l.unitRev);
  const cost2 = sum(b2, (l) => l.unitCost), rev2 = sum(b2, (l) => l.unitRev);
  const leftover = rev1 - midFixed - cost2;
  let net = rev2 - cost1 - homeUpfront - homeAfter;
  if (midConvertible) net += leftover;
  if (net <= 0) return null;

  const items1 = legItems(b1, materials), items2 = legItems(b2, materials);
  const r = midConvertible ? 1 : (cost2 ? rev2 / cost2 : 0);
  const parts = [
    ...items1.map((i) => i.received * r * risk1[i.ticker].vol),
    ...items2.map((i) => i.spent * r * risk2b[i.ticker].vol),
    ...items2.map((i) => i.received * risk2f[i.ticker].vol),
  ];
  const sigma = Math.sqrt(parts.reduce((t, p) => t + p * p, 0));
  const hours = h1 + h2;
  const adj = net - s.riskAversion * sigma;
  const sumW = (it, k) => it.reduce((t, i) => t + i[k], 0);
  const borderHome = x1.exit_fee + x2.entry_fee;
  const borderMid = x1.entry_fee + x2.exit_fee;
  return {
    kind: midConvertible ? "2-leg" : "cycle", label: `${a}>${b}>${f}`,
    origin: a, mid: b, final: f, hours, midConvertible,
    cost1, rev1, fuel1, cost2, rev2, fuel2, cross1: x1, cross2: x2,
    crossings: [x1, x2], duties1: [sumW(items1, "buyDuty"), sumW(items1, "sellDuty")],
    duties2: [sumW(items2, "buyDuty"), sumW(items2, "sellDuty")],
    leftover, netProfit: net, risk: sigma, riskAdj: adj, score: adj / hours, midRate: r,
    leg1: items1, leg2: items2,
    legs: [{ from: a, to: b, items: items1 }, { from: b, to: f, items: items2 }],
    weightUsed: Math.max(sumW(items1, "weight"), sumW(items2, "weight")),
    volumeUsed: Math.max(sumW(items1, "volume"), sumW(items2, "volume")),
    capitalUsed: cost1 + fuel1 + x1.exit_fee,
    homeBorderFees: borderHome, midCosts: borderMid + fuel2,
    borderFees: borderHome + borderMid * r, fuelHome: fuel1 + fuel2 * r,
  };
}

// ---------------------------------------------------------------------------
// Scans
// ---------------------------------------------------------------------------
export async function scan(home, ship, s, m, onProgress = () => {}) {
  const exchanges = m.books.exchanges;
  const plan = [];
  if (s.mode !== "cycle") {
    for (const d of exchanges) {
      if (d === home || !sameCurrency(m.currency, home, d)) continue;
      const leg = legInfo(m.routes, ship, home, d);
      if (leg) plan.push({ kind: "one-way", label: `${home} > ${d}`, run: () => evaluateRoute(home, d, leg, ship, s, m) });
    }
  }
  if (s.mode !== "same" && m.solver) {
    const finals = exchanges.filter((e) => sameCurrency(m.currency, home, e));
    for (const mid of exchanges) {
      if (mid === home) continue;
      const leg1 = legInfo(m.routes, ship, home, mid);
      if (!leg1) continue;
      for (const fin of finals) {
        if (fin === mid) continue;
        const leg2 = legInfo(m.routes, ship, mid, fin);
        if (leg2) plan.push({ kind: "cycle", label: `${home} > ${mid} > ${fin}`,
                              run: () => evaluateCycle(home, mid, fin, leg1, leg2, ship, s, m) });
      }
    }
  }
  const results = [];
  for (let i = 0; i < plan.length; i++) {
    onProgress(`Evaluating ${plan[i].label} (${i + 1}/${plan.length})`, i / plan.length);
    // let the page repaint between routes
    await new Promise((r) => setTimeout(r, 0));
    const res = await plan[i].run();
    if (res) { res.ship = ship; results.push(res); }
  }
  const out = await pickFuelSettings(results, ship, s, m, onProgress);
  onProgress("Done", 1);
  return out.sort((x, y) => y.score - x.score);
}

// ---------------------------------------------------------------------------
// Choosing the STL and reactor sliders (needs a fitted flight model)
// ---------------------------------------------------------------------------
async function reevaluate(res, ship, s, m) {
  let out;
  if (res.kind === "one-way") {
    const leg = legInfo(m.routes, ship, res.origin, res.dest);
    out = leg && await evaluateRoute(res.origin, res.dest, leg, ship, s, m);
  } else {
    const leg1 = legInfo(m.routes, ship, res.origin, res.mid);
    const leg2 = legInfo(m.routes, ship, res.mid, res.final);
    out = leg1 && leg2 && await evaluateCycle(res.origin, res.mid, res.final, leg1, leg2, ship, s, m);
  }
  if (out) out.ship = ship;
  return out || null;
}

// Quick score estimate for res flown by `ship`, keeping the cargo: only fuel,
// hours and price risk (square root of time) change.
function screenSetting(res, ship, s, m) {
  const oneWay = res.kind === "one-way";
  const legs = oneWay ? [[res.origin, res.dest]] : [[res.origin, res.mid], [res.mid, res.final]];
  let hours = 0;
  const fuel = [];
  for (const [a, b] of legs) {
    const leg = legInfo(m.routes, ship, a, b);
    if (!leg) return null;
    hours += leg.hours;
    if (s.roundTrip && oneWay && !fuelFits(ship, leg.route, 2)) return null;
    fuel.push(fuelCost(ship, m.books, a, leg.route, s.roundTrip && oneWay));
  }
  if (fuel.some(Number.isNaN)) return null;
  let oldFuel, newFuel;
  if (oneWay) {
    if (s.roundTrip) hours *= 2;
    oldFuel = res.fuel; newFuel = fuel[0];
  } else {
    oldFuel = res.fuel1 + res.fuel2 * res.midRate;
    newFuel = fuel[0] + fuel[1] * res.midRate;
  }
  const net = res.netProfit + oldFuel - newFuel;
  const risk = res.hours > 0 ? res.risk * Math.sqrt(hours / res.hours) : res.risk;
  return (net - s.riskAversion * risk) / hours;
}

export async function pickFuelSettings(results, ship, s, m, onProgress = () => {}) {
  if (!ship.flightModel || !s.pickFuel || !results.length) return results;
  const variants = modelGrid(ship.flightModel).map(([stl, re]) => ({ ...ship, stlUsage: stl, reactorUsage: re }));
  const sorted = [...results].sort((x, y) => y.score - x.score);
  const head = sorted.slice(0, C.REFINE_TOP), tail = sorted.slice(C.REFINE_TOP);
  const better = [];
  for (let n = 0; n < head.length; n++) {
    const res = head[n];
    onProgress(`Choosing fuel settings for ${res.label} (${n + 1}/${head.length})`, n / head.length);
    await new Promise((r) => setTimeout(r, 0));
    const base = settingsLabel(res.ship);
    const scored = [];
    for (const v of variants) {
      if (settingsLabel(v) === base) continue;
      const sc = screenSetting(res, v, s, m);
      if (sc != null) scored.push([sc, v]);
    }
    scored.sort((x, y) => y[0] - x[0]);
    const tries = [res];
    for (const [, v] of scored.slice(0, C.RERUN_SETTINGS)) tries.push(await reevaluate(res, v, s, m));
    better.push(tries.filter(Boolean).reduce((a, b) => (b.score > a.score ? b : a)));
  }
  return [...better, ...tail];
}
