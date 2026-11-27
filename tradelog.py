#!/usr/bin/env python3
"""
Trade log: what the optimizer predicted versus what you actually made.

A model is only as good as its track record. This keeps one: each trade you
take is saved with the optimizer's prediction at the moment you took it, and
when the trade is finished you record the real result. The report then shows
how well the predictions hold up.

Workflow
    1. Run the optimizer and record the route you are going to fly:
           python arbitrage.py --at CI1 --ship MyHauler --record 1
       (1 is the route's # in the results table.)
    2. Fly it in game. When the last sale is done, record what happened:
           python tradelog.py close 3 --profit 87250 --hours 38
       or let it do the sum:
           python tradelog.py close 3 --received 571000 --spent 484000 --costs 21000
       All amounts are in your home currency (the one you started with).
    3. See how the model is doing:
           python tradelog.py report
           python tradelog.py report --markdown track_record.md

Other commands
    python tradelog.py add --route CI1>AI1>CI1 --kind cycle --currency CIS \
        --profit 50700 --hours 72 --note "..."
                                       a finished trade that was never logged
                                       with a prediction (counts towards
                                       profit, not towards model accuracy)
    python tradelog.py list            open trades
    python tradelog.py show 3          everything stored for one trade
    python tradelog.py abandon 3       trade not taken or not finished

The log is a plain JSON file (trade_log.json by default), kept out of git.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime, timezone

DEFAULT_PATH = "trade_log.json"


# ----------------------------------------------------------------------------
# Storage
# ----------------------------------------------------------------------------
def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load(path=DEFAULT_PATH):
    if not os.path.exists(path):
        return {"version": 1, "trades": []}
    try:
        with open(path) as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        sys.exit(f"{path} is not valid JSON ({exc}). Fix or move it aside.")
    data.setdefault("trades", [])
    return data


def save(data, path=DEFAULT_PATH):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)  # never leaves a half-written log


def find(data, trade_id):
    for t in data["trades"]:
        if t["id"] == trade_id:
            return t
    sys.exit(f"No trade #{trade_id}. Run: python tradelog.py list")


# ----------------------------------------------------------------------------
# Recording a prediction (called by arbitrage.py --record)
# ----------------------------------------------------------------------------
def _items(items):
    return [{"ticker": i["ticker"], "units": i["units"],
             "buy_vwap": round(i["buy_vwap"], 4),
             "sell_vwap": round(i["sell_vwap"], 4)} for i in items]


def record_result(result, *, home, currency, capital, ship, settings,
                   path=DEFAULT_PATH):
    """Append an open trade built from an arbitrage.py result object."""
    if result.kind == "one-way":
        legs = [{"from": result.origin, "to": result.dest,
                 "items": _items(result.items)}]
        fuel = result.fuel
    else:
        legs = [{"from": result.origin, "to": result.mid, "items": _items(result.leg1)},
                {"from": result.mid, "to": result.final, "items": _items(result.leg2)}]
        fuel = result.fuel1 + result.fuel2 * result.mid_rate
    data = load(path)
    trade = {
        "id": max((t["id"] for t in data["trades"]), default=0) + 1,
        "status": "open",
        "opened_at": now(),
        "route": result.label,
        "kind": result.kind,
        "home": home,
        "currency": currency,
        "ship": ship,
        "capital": capital,
        "settings": {"fee_rate": settings.fee_rate,
                     "risk_aversion": settings.risk_aversion,
                     "round_trip": settings.round_trip},
        "predicted": {"net_profit": round(result.net_profit, 2),
                      "risk_1sd": round(result.risk, 2),
                      "hours": round(result.hours, 2),
                      "capital_used": round(result.capital_used, 2),
                      "fuel": round(fuel, 2),
                      "border_fees": round(result.border_fees, 2)},
        "legs": legs,
        "actual": None,
    }
    data["trades"].append(trade)
    save(data, path)
    return trade


def add_manual(data, *, route, kind, currency, profit, hours=None, home=None,
               ship=None, opened_at=None, closed_at=None, legs=None,
               breakdown=None, note=None):
    """Append a finished trade that has no logged prediction."""
    trade = {
        "id": max((t["id"] for t in data["trades"]), default=0) + 1,
        "status": "closed",
        "opened_at": opened_at or now(),
        "closed_at": closed_at or now(),
        "route": route, "kind": kind, "home": home or route.split(">")[0],
        "currency": currency, "ship": ship, "capital": None, "settings": None,
        "predicted": None,
        "legs": legs or [],
        "actual": {"net_profit": round(profit, 2), "hours": hours,
                   "hours_measured": hours is not None, "received": None,
                   "spent": None, "costs": None, "breakdown": breakdown,
                   "note": note},
        "source": "manual",
    }
    data["trades"].append(trade)
    return trade


# ----------------------------------------------------------------------------
# Closing a trade
# ----------------------------------------------------------------------------
def close(data, trade_id, profit=None, received=None, spent=None, costs=0.0,
          hours=None, note=None):
    t = find(data, trade_id)
    if profit is None:
        if received is None or spent is None:
            sys.exit("Give --profit, or both --received and --spent "
                     "(plus --costs for fuel and fees you paid).")
        profit = received - spent - (costs or 0.0)
    t["status"] = "closed"
    t["closed_at"] = now()
    t["actual"] = {"net_profit": round(profit, 2),
                   "hours": hours if hours is not None else t["predicted"]["hours"],
                   "hours_measured": hours is not None,
                   "received": received, "spent": spent, "costs": costs or None,
                   "note": note}
    return t


# ----------------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------------
def stats(trades):
    """Summary numbers over closed trades. Accuracy figures use only trades
    that were logged with a prediction; profit figures use all of them."""
    closed = [t for t in trades if t["status"] == "closed"]
    if not closed:
        return None
    act = [t["actual"]["net_profit"] for t in closed]
    timed = [t for t in closed if t["actual"].get("hours")]
    hours = sum(t["actual"]["hours"] for t in timed)
    predicted = [t for t in closed if t.get("predicted")]
    pred = [t["predicted"]["net_profit"] for t in predicted]
    pact = [t["actual"]["net_profit"] for t in predicted]
    risk = [t["predicted"]["risk_1sd"] for t in predicted]
    within = sum(1 for p, a, r in zip(pred, pact, risk) if r > 0 and abs(a - p) <= r)
    total_pred, total_act = sum(pred), sum(act)
    return {
        "n": len(closed),
        "n_predicted": len(predicted),
        "total_predicted": total_pred,
        "total_actual": total_act,
        "capture": sum(pact) / total_pred if total_pred else math.nan,
        "win_rate": sum(1 for a in act if a > 0) / len(act),
        "within_1sd": within / len(predicted) if predicted else math.nan,
        "mean_error": (sum(a - p for p, a in zip(pred, pact)) / len(predicted)
                       if predicted else math.nan),
        "profit_per_hour": (sum(t["actual"]["net_profit"] for t in timed) / hours
                            if hours else math.nan),
        "best": max(closed, key=lambda t: t["actual"]["net_profit"]),
        "worst": min(closed, key=lambda t: t["actual"]["net_profit"]),
        "currencies": sorted({t["currency"] or "?" for t in closed}),
    }


def m(x):
    return f"{x:,.0f}"


def pct(x):
    return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{100 * x:.0f}%"


def row(t):
    p = t.get("predicted")
    a = t["actual"] or {}
    actual = a.get("net_profit")
    err = (actual - p["net_profit"]) if actual is not None and p else None
    z = (err / p["risk_1sd"]) if err is not None and p["risk_1sd"] else None
    hours = a.get("hours") if a.get("hours") is not None else (p or {}).get("hours")
    return {
        "id": t["id"], "date": t["opened_at"][:10], "route": t["route"],
        "kind": t["kind"], "cur": t["currency"] or "",
        "pred": m(p["net_profit"]) if p else "-",
        "risk": m(p["risk_1sd"]) if p else "-",
        "actual": m(actual) if actual is not None else t["status"],
        "error": (f"{err:+,.0f}" if err is not None else ""),
        "z": f"{z:+.1f}" if z is not None else "",
        "hours": f"{hours:.0f}" if hours is not None else "",
    }


COLUMNS = [("id", "#", 4), ("date", "Opened", 11), ("route", "Route", 13),
           ("kind", "Type", 8), ("cur", "Cur", 5), ("pred", "Predicted", 11),
           ("risk", "Risk 1sd", 10), ("actual", "Actual", 11),
           ("error", "Error", 10), ("z", "Err/sd", 7), ("hours", "Hrs", 5)]


def summary_lines(s):
    cur = "/".join(s["currencies"])
    note = (" (mixed home currencies: totals add different currencies)"
            if len(s["currencies"]) > 1 else "")
    lines = [
        f"Closed trades: {s['n']}{note}",
        f"Actual profit:    {m(s['total_actual'])} {cur}",
        f"Profitable trades: {pct(s['win_rate'])}",
        f"Actual profit per hour flown: {m(s['profit_per_hour'])}",
    ]
    if s["n_predicted"]:
        lines += [
            f"Trades with a logged prediction: {s['n_predicted']}. Predicted "
            f"{m(s['total_predicted'])} {cur}, captured {pct(s['capture'])}",
            f"Results within the predicted 1 sd risk band: {pct(s['within_1sd'])} "
            "(about 68% means the risk estimate is well calibrated)",
            f"Average error per trade: {s['mean_error']:+,.0f} "
            "(negative means the model is optimistic)",
        ]
    else:
        lines.append("No trades logged with a prediction yet, so no accuracy "
                     "figures. Use --record (or Log this trade) before flying.")
    return lines + [
        f"Best: #{s['best']['id']} {s['best']['route']} "
        f"{m(s['best']['actual']['net_profit'])}; worst: #{s['worst']['id']} "
        f"{s['worst']['route']} {m(s['worst']['actual']['net_profit'])}",
    ]


def print_report(data):
    trades = data["trades"]
    if not trades:
        print("No trades yet. Record one with: python arbitrage.py ... --record 1")
        return
    print("".join(f"{h:<{w}}" if k in ("id", "date", "route", "kind", "cur")
                  else f"{h:>{w}}" for k, h, w in COLUMNS))
    for t in trades:
        r = row(t)
        print("".join(f"{r[k]:<{w}}" if k in ("id", "date", "route", "kind", "cur")
                      else f"{r[k]:>{w}}" for k, _, w in COLUMNS))
    s = stats(trades)
    print()
    if s:
        for line in summary_lines(s):
            print(line)
    else:
        print("No closed trades yet, so no statistics.")


def markdown(data):
    trades = data["trades"]
    s = stats(trades)
    out = ["# Track record", "",
           f"Generated {now()[:10]} by `tradelog.py`. Trades logged before they "
           "were flown carry the optimizer's prediction; trades added afterwards "
           "(Predicted shown as -) count towards profit only.", ""]
    if s:
        out += ["## Summary", ""] + [f"- {line}" for line in summary_lines(s)] + [""]
    out += ["## Trades", "",
            "| " + " | ".join(h for _, h, _ in COLUMNS) + " |",
            "|" + "|".join("---:" if k not in ("route", "kind", "cur", "date") else "---"
                           for k, _, _ in COLUMNS) + "|"]
    for t in trades:
        r = row(t)
        out.append("| " + " | ".join(str(r[k]) for k, _, _ in COLUMNS) + " |")
    out += ["", "Error = actual minus predicted. Err/sd = error in units of the "
            "predicted one-standard-deviation risk."]
    return "\n".join(out) + "\n"


def show(t):
    print(json.dumps(t, indent=2))


# ----------------------------------------------------------------------------
# Command line
# ----------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="Predicted vs actual trade log.")
    ap.add_argument("--log", default=DEFAULT_PATH, help="log file")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="show open trades")
    p = sub.add_parser("show", help="show everything stored for one trade")
    p.add_argument("id", type=int)
    p = sub.add_parser("close", help="record the actual result of a trade")
    p.add_argument("id", type=int)
    p.add_argument("--profit", type=float, help="actual net profit, home currency")
    p.add_argument("--received", type=float, help="total sales, home currency")
    p.add_argument("--spent", type=float, help="total purchases, home currency")
    p.add_argument("--costs", type=float, default=0.0,
                   help="fuel and fees paid, home currency")
    p.add_argument("--hours", type=float, help="actual hours from departure to "
                   "last sale (defaults to the predicted hours)")
    p.add_argument("--note", help="anything worth remembering")
    p = sub.add_parser("add", help="add a finished trade that had no logged prediction")
    p.add_argument("--route", required=True, help="e.g. CI1>AI1>CI1")
    p.add_argument("--kind", default="one-way", choices=["one-way", "cycle", "2-leg"])
    p.add_argument("--currency", required=True, help="home currency, e.g. CIS")
    p.add_argument("--profit", type=float, required=True, help="net profit, home currency")
    p.add_argument("--hours", type=float, help="hours from departure to last sale")
    p.add_argument("--ship")
    p.add_argument("--opened", help="date flown out, YYYY-MM-DD")
    p.add_argument("--closed", help="date of the last sale, YYYY-MM-DD")
    p.add_argument("--details", metavar="JSON",
                   help="optional JSON file with legs and a cost breakdown")
    p.add_argument("--note")
    p = sub.add_parser("abandon", help="mark a trade as not taken")
    p.add_argument("id", type=int)
    p = sub.add_parser("report", help="predicted vs actual for every trade")
    p.add_argument("--markdown", metavar="FILE",
                   help="also write the report as Markdown, e.g. track_record.md")
    args = ap.parse_args(argv)

    data = load(args.log)
    if args.cmd == "list":
        open_ = [t for t in data["trades"] if t["status"] == "open"]
        if not open_:
            print("No open trades.")
        for t in open_:
            print(f"#{t['id']}  {t['opened_at'][:16]}  {t['route']}  predicted "
                  f"{m(t['predicted']['net_profit'])} {t['currency'] or ''} over "
                  f"{t['predicted']['hours']:.0f}h")
    elif args.cmd == "show":
        show(find(data, args.id))
    elif args.cmd == "close":
        t = close(data, args.id, args.profit, args.received, args.spent,
                  args.costs, args.hours, args.note)
        save(data, args.log)
        diff = t["actual"]["net_profit"] - t["predicted"]["net_profit"]
        print(f"Closed #{t['id']} {t['route']}: actual {m(t['actual']['net_profit'])} "
              f"vs predicted {m(t['predicted']['net_profit'])} ({diff:+,.0f}).")
    elif args.cmd == "add":
        extra = {}
        if args.details:
            with open(args.details) as f:
                extra = json.load(f)
        day = lambda d: f"{d}T00:00:00+00:00" if d else None
        t = add_manual(data, route=args.route, kind=args.kind, currency=args.currency,
                       profit=args.profit, hours=args.hours, ship=args.ship,
                       opened_at=day(args.opened), closed_at=day(args.closed),
                       legs=extra.get("legs"), breakdown=extra.get("breakdown"),
                       note=args.note)
        save(data, args.log)
        print(f"Added #{t['id']} {t['route']}: {m(t['actual']['net_profit'])} "
              f"{t['currency']} (no prediction logged).")
    elif args.cmd == "abandon":
        t = find(data, args.id)
        t["status"], t["closed_at"] = "abandoned", now()
        save(data, args.log)
        print(f"Marked #{t['id']} as abandoned.")
    elif args.cmd == "report":
        print_report(data)
        if args.markdown:
            with open(args.markdown, "w") as f:
                f.write(markdown(data))
            print(f"\nWrote {args.markdown}")


if __name__ == "__main__":
    main()
