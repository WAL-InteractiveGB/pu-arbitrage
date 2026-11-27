// Trade log for the web app: predicted vs actual. Uses the same JSON format as
// tradelog.py, so an exported log can be read with `python tradelog.py report
// --log trade_log.json` and vice versa.

const KEY = "pua:tradelog";

export function load() {
  try {
    const d = JSON.parse(localStorage.getItem(KEY) || "null");
    if (d && Array.isArray(d.trades)) return d;
  } catch { /* fall through */ }
  return { version: 1, trades: [] };
}

export function save(data) {
  localStorage.setItem(KEY, JSON.stringify(data));
}

export function importJson(text) {
  const d = JSON.parse(text);
  if (!d || !Array.isArray(d.trades)) throw new Error("Not a trade log (no trades list).");
  return d;
}

const now = () => new Date().toISOString().replace(/\.\d+Z$/, "+00:00");
const r2 = (x) => Math.round(x * 100) / 100;

const items = (list) => list.map((i) => ({
  ticker: i.ticker, units: i.units,
  buy_vwap: Math.round(i.buy_vwap * 1e4) / 1e4, sell_vwap: Math.round(i.sell_vwap * 1e4) / 1e4,
}));

export function record(data, res, { home, currency, capital, ship, settings }) {
  const trade = {
    id: data.trades.reduce((m, t) => Math.max(m, t.id), 0) + 1,
    status: "open",
    opened_at: now(),
    route: res.label,
    kind: res.kind,
    home, currency, ship, capital,
    settings: { fee_rate: settings.feeRate, risk_aversion: settings.riskAversion,
                round_trip: settings.roundTrip },
    predicted: {
      net_profit: r2(res.netProfit), risk_1sd: r2(res.risk), hours: r2(res.hours),
      capital_used: r2(res.capitalUsed), fuel: r2(res.fuelHome), border_fees: r2(res.borderFees),
    },
    legs: res.legs.map((l) => ({ from: l.from, to: l.to, items: items(l.items) })),
    actual: null,
    source: "web",
  };
  data.trades.push(trade);
  return trade;
}

// A finished trade that was never logged with a prediction. It counts towards
// profit but not towards the model's accuracy figures.
export function addManual(data, { route, kind, currency, profit, hours = null, ship = null,
                                  opened = null, closed = null, note = null, legs = [], breakdown = null }) {
  const day = (d) => (d ? `${d}T00:00:00+00:00` : now());
  const trade = {
    id: data.trades.reduce((m, t) => Math.max(m, t.id), 0) + 1,
    status: "closed", opened_at: day(opened), closed_at: day(closed),
    route, kind, home: route.split(">")[0], currency, ship, capital: null, settings: null,
    predicted: null, legs,
    actual: { net_profit: r2(profit), hours, hours_measured: hours != null, received: null,
              spent: null, costs: null, breakdown, note },
    source: "manual",
  };
  data.trades.push(trade);
  return trade;
}

export function close(data, id, { profit, hours, note }) {
  const t = data.trades.find((x) => x.id === id);
  if (!t) throw new Error(`No trade #${id}`);
  t.status = "closed";
  t.closed_at = now();
  t.actual = { net_profit: r2(profit), hours: hours ?? t.predicted.hours,
               hours_measured: hours != null, received: null, spent: null, costs: null,
               note: note || null };
  return t;
}

export function abandon(data, id) {
  const t = data.trades.find((x) => x.id === id);
  if (t) { t.status = "abandoned"; t.closed_at = now(); }
}

export function remove(data, id) {
  data.trades = data.trades.filter((x) => x.id !== id);
}

// Accuracy figures use only trades logged with a prediction; profit uses all.
export function stats(trades) {
  const closed = trades.filter((t) => t.status === "closed");
  if (!closed.length) return null;
  const sum = (xs) => xs.reduce((s, x) => s + x, 0);
  const act = closed.map((t) => t.actual.net_profit);
  const timed = closed.filter((t) => t.actual.hours);
  const hours = sum(timed.map((t) => t.actual.hours));
  const predicted = closed.filter((t) => t.predicted);
  const pred = predicted.map((t) => t.predicted.net_profit);
  const pact = predicted.map((t) => t.actual.net_profit);
  const risk = predicted.map((t) => t.predicted.risk_1sd);
  const totalPred = sum(pred), totalAct = sum(act);
  const by = (f) => closed.reduce((b, t) => (f(t) > f(b) ? t : b));
  return {
    n: closed.length, nPredicted: predicted.length, totalPred, totalAct,
    capture: totalPred ? sum(pact) / totalPred : NaN,
    winRate: act.filter((a) => a > 0).length / act.length,
    within1sd: predicted.length ? predicted.filter((_, i) => risk[i] > 0 && Math.abs(pact[i] - pred[i]) <= risk[i]).length / predicted.length : NaN,
    meanError: predicted.length ? sum(pact.map((a, i) => a - pred[i])) / predicted.length : NaN,
    perHour: hours ? sum(timed.map((t) => t.actual.net_profit)) / hours : NaN,
    best: by((t) => t.actual.net_profit),
    worst: by((t) => -t.actual.net_profit),
    currencies: [...new Set(closed.map((t) => t.currency || "?"))].sort(),
  };
}

const m = (x) => (Number.isFinite(x) ? Math.round(x).toLocaleString("en-US") : "n/a");
const pct = (x) => (Number.isFinite(x) ? `${Math.round(100 * x)}%` : "n/a");

export function summaryLines(s) {
  const cur = s.currencies.join("/");
  const lines = [
    `Closed trades: ${s.n}${s.currencies.length > 1 ? " (mixed home currencies)" : ""}`,
    `Actual profit: ${m(s.totalAct)} ${cur}`,
    `Profitable trades: ${pct(s.winRate)}`,
    `Actual profit per hour flown: ${m(s.perHour)}`,
  ];
  if (s.nPredicted) {
    lines.push(
      `Trades with a logged prediction: ${s.nPredicted}. Predicted ${m(s.totalPred)} ${cur}, captured ${pct(s.capture)}`,
      `Within the predicted 1 sd risk band: ${pct(s.within1sd)} (about 68% means well calibrated)`,
      `Average error per trade: ${s.meanError >= 0 ? "+" : ""}${m(s.meanError)} (negative = model is optimistic)`);
  } else {
    lines.push("No trades logged with a prediction yet, so no accuracy figures. Press Log this trade before flying.");
  }
  return [...lines,
    `Best: #${s.best.id} ${s.best.route} ${m(s.best.actual.net_profit)}; worst: #${s.worst.id} ${s.worst.route} ${m(s.worst.actual.net_profit)}`,
  ];
}

export function row(t) {
  const p = t.predicted, a = t.actual || {};
  const actual = a.net_profit;
  const err = actual != null && p ? actual - p.net_profit : null;
  const z = err != null && p.risk_1sd ? err / p.risk_1sd : null;
  const hours = a.hours ?? p?.hours;
  return {
    id: t.id, date: t.opened_at.slice(0, 10), route: t.route, kind: t.kind, cur: t.currency || "",
    pred: p ? m(p.net_profit) : "–", risk: p ? m(p.risk_1sd) : "–",
    actual: actual != null ? m(actual) : t.status,
    error: err != null ? `${err >= 0 ? "+" : ""}${m(err)}` : "",
    z: z != null ? `${z >= 0 ? "+" : ""}${z.toFixed(1)}` : "",
    hours: hours != null ? Math.round(hours) : "",
  };
}

export function markdown(data) {
  const s = stats(data.trades);
  const cols = [["id", "#"], ["date", "Opened"], ["route", "Route"], ["kind", "Type"], ["cur", "Cur"],
                ["pred", "Predicted"], ["risk", "Risk 1sd"], ["actual", "Actual"], ["error", "Error"],
                ["z", "Err/sd"], ["hours", "Hrs"]];
  const out = ["# Track record", "",
    `Generated ${now().slice(0, 10)} by pu-arbitrage. Trades logged before they were flown carry the ` +
    "optimizer's prediction; trades added afterwards (Predicted shown as –) count towards profit only.", ""];
  if (s) out.push("## Summary", "", ...summaryLines(s).map((l) => `- ${l}`), "");
  out.push("## Trades", "", `| ${cols.map((c) => c[1]).join(" | ")} |`,
           `|${cols.map(([k]) => (["route", "kind", "cur", "date"].includes(k) ? "---" : "---:")).join("|")}|`);
  for (const t of data.trades) {
    const r = row(t);
    out.push(`| ${cols.map(([k]) => r[k]).join(" | ")} |`);
  }
  out.push("", "Error = actual minus predicted. Err/sd = error in units of the predicted " +
           "one-standard-deviation risk.");
  return out.join("\n") + "\n";
}
