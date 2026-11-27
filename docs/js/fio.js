// FIO data access for the web app: market data, map routes and price history.
// Slow-changing data (materials, stations, jump routes, price history) is kept
// in localStorage so repeat visits are fast and light on FIO.

import { C, regionFromCode } from "./engine.js";

export const DEFAULT_API = "https://rest.fnar.net";
const HOUR = 3600e3, DAY = 24 * HOUR;
const PREFIX = "pua:fio:";

function storeGet(key, ttlMs) {
  try {
    const raw = localStorage.getItem(PREFIX + key);
    if (!raw) return undefined;
    const { t, d } = JSON.parse(raw);
    return Date.now() - t < ttlMs ? d : undefined;
  } catch { return undefined; }
}

function storeSet(key, data) {
  try {
    localStorage.setItem(PREFIX + key, JSON.stringify({ t: Date.now(), d: data }));
  } catch {
    // Storage full or blocked (private mode): fine, just no cache.
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export class Fio {
  constructor(base = DEFAULT_API) {
    this.base = base.replace(/\/$/, "");
    this.mem = new Map();
  }

  async fetchJson(path) {
    let last;
    for (let attempt = 0; attempt < 3; attempt++) {
      try {
        const res = await fetch(this.base + path, {
          headers: { "X-FIO-Application": "pu-arbitrage-web" },
        });
        if (res.status === 429 || res.status >= 500) throw new Error(`HTTP ${res.status}`);
        if (!res.ok) { const e = new Error(`HTTP ${res.status} for ${path}`); e.final = true; throw e; }
        return await res.json();
      } catch (err) {
        last = err;
        if (err.final) break;
        await sleep(1500 * 2 ** attempt);
      }
    }
    throw last;
  }

  // ttlMs: how long a saved copy stays good. persist: keep it in localStorage.
  async get(path, { ttlMs = 5 * 60e3, persist = false } = {}) {
    const m = this.mem.get(path);
    if (m && Date.now() - m.t < ttlMs) return m.d;
    if (persist) {
      const saved = storeGet(path, ttlMs);
      if (saved !== undefined) { this.mem.set(path, { t: Date.now(), d: saved }); return saved; }
    }
    const data = await this.fetchJson(path);
    this.mem.set(path, { t: Date.now(), d: data });
    if (persist) storeSet(path, data);
    return data;
  }

  // Everything the engine needs, except price history.
  async loadMarket(onStatus = () => {}) {
    onStatus("Loading order books, materials and stations from FIO...");
    const [full, mats, stations, sim] = await Promise.all([
      this.get("/exchange/full", { ttlMs: 5 * 60e3 }),
      this.get("/material/allmaterials", { ttlMs: DAY, persist: true }),
      this.get("/exchange/station", { ttlMs: 7 * DAY, persist: true }),
      this.get("/global/simulationdata", { ttlMs: 30 * DAY, persist: true }).catch(() => ({})),
    ]);
    const materials = {}, categories = {}, names = {};
    for (const m of mats) {
      if (!m.Ticker) continue;
      materials[m.Ticker] = [m.Weight, m.Volume];
      categories[m.Ticker] = m.CategoryName || "";
      names[m.Ticker] = m.Name || "";
    }

    const system = {}, currency = {}, region = {};
    for (const s of Array.isArray(stations) ? stations : []) {
      const code = s.ComexCode || s.ExchangeCode || s.Code;
      if (!code) continue;
      if (s.SystemNaturalId) system[code] = s.SystemNaturalId;
      if (s.CurrencyCode) currency[code] = String(s.CurrencyCode).toUpperCase();
      region[code] = String(s.CountryCode || regionFromCode(code)).toUpperCase();
    }
    const pcLen = Number(sim && sim.ParsecLength) || 12;
    return { full, materials, categories, names, system, currency, region, pcLen };
  }

  // {"A>B": {jumps, parsecs}} for every pair of exchanges, from the game's
  // own jump routes (the map is static, so these are kept for 30 days).
  async loadRoutes(exchanges, system, pcLen, onStatus = () => {}) {
    const routes = {}, failed = [];
    const pairs = [];
    for (let i = 0; i < exchanges.length; i++)
      for (let j = i + 1; j < exchanges.length; j++) pairs.push([exchanges[i], exchanges[j]]);
    let done = 0;
    await pool(pairs, 4, async ([a, b]) => {
      if (!system[a] || !system[b]) { failed.push(`${a}-${b}`); return; }
      try {
        const hops = await this.get(
          `/systemstars/jumproute/${encodeURIComponent(system[a])}/${encodeURIComponent(system[b])}`,
          { ttlMs: 30 * DAY, persist: true });
        if (!Array.isArray(hops)) throw new Error("bad route");
        const r = { jumps: hops.length,
                    parsecs: hops.reduce((t, h) => t + Number(h.Distance || 0), 0) / pcLen };
        routes[`${a}>${b}`] = routes[`${b}>${a}`] = r;
      } catch { failed.push(`${a}-${b}`); }
      onStatus(`Loading jump routes (${++done}/${pairs.length})...`);
    });
    return { routes, failed };
  }
}

// Run fn over items with at most n in flight.
export async function pool(items, n, fn) {
  let next = 0;
  const workers = Array.from({ length: Math.min(n, items.length) }, async () => {
    while (next < items.length) await fn(items[next++]);
  });
  await Promise.all(workers);
}

// Daily closing prices for the risk model (same rules as arbitrage.py).
export class History {
  constructor(fio, enabled = true, onStatus = () => {}) {
    this.fio = fio;
    this.enabled = enabled;
    this.onStatus = onStatus;
    this.cache = new Map();
  }

  closes(ticker, ex) {
    return this.cache.get(`${ticker}.${ex}`)?.c || [];
  }

  // Average daily units traded over the history window, or null.
  dailyVolume(ticker, ex) {
    const v = this.cache.get(`${ticker}.${ex}`)?.v;
    return v == null ? null : v;
  }

  async prefetch(keys) {
    if (!this.enabled) return;
    const todo = [...new Set(keys.map(([t, e]) => `${t}.${e}`))].filter((k) => !this.cache.has(k));
    if (!todo.length) return;
    let done = 0;
    await pool(todo, 6, async (key) => {
      const saved = storeGet(`h2:${key}`, 6 * HOUR);
      if (saved !== undefined) { this.cache.set(key, saved); return; }
      let closes = [], vol = null;
      try {
        let recs = await this.fio.fetchJson(`/exchange/cxpc/${encodeURIComponent(key)}`);
        recs = Array.isArray(recs) ? recs.filter((r) => r && typeof r === "object") : [];
        if (recs.some((r) => "Interval" in r)) {
          const daily = recs.filter((r) => String(r.Interval || "").toUpperCase().includes("DAY"));
          if (daily.length) recs = daily;
        }
        recs.sort((x, y) => (x.DateEpochMs || x.Date || 0) - (y.DateEpochMs || y.Date || 0));
        closes = recs.filter((r) => r.Close != null).map((r) => Number(r.Close)).slice(-C.HISTORY_DAYS);
        const traded = recs.filter((r) => r.Traded != null).map((r) => Number(r.Traded)).slice(-C.HISTORY_DAYS);
        if (traded.length >= C.MIN_HISTORY_POINTS) vol = traded.reduce((a, b) => a + b, 0) / traded.length;
      } catch { closes = []; vol = null; }
      const entry = { c: closes, v: vol };
      this.cache.set(key, entry);
      storeSet(`h2:${key}`, entry); // ~30 numbers, so cheap to keep
      if (++done % 5 === 0 || done === todo.length) this.onStatus(`Price history ${done}/${todo.length}`);
    });
  }
}
