#!/usr/bin/env python3
"""
Fuel calibration and freight quotes for pu-arbitrage.

How the game uses fuel (community-derived; the game does not publish it):
    FTL fuel (FF) = parsecs flown x 7.2 x reactor usage
        Reactor usage is the FTL slider in the flight planner (SFC), 25% at
        its cheapest. Higher settings burn more FF for a slightly faster jump.
    STL fuel (SF) = a roughly fixed amount per flight, for take-off,
        departure, approach and landing. It depends on the ship, so it is
        measured rather than calculated.
Distances in parsecs come from the game's own jump routes via FIO.

Usage
    1. Add your ship to ships.json (see ships.example.json).
    2. Plan a few fully loaded flights in SFC. For each, note the SF and FF it
       shows and the FTL reactor setting you planned it at. Put them in
       flights.csv with columns ship,from,to,sf,ff,reactor (see
       flights.example.csv). reactor is optional but makes the fit exact.
    3. python freight.py calibrate --ship MyHauler
           measures SF per flight, checks FF against the game formula, and
           saves the results to ships.json
    4. python freight.py quote --ship MyHauler --at CI1 --ticker RAT
           shows the fuel and its cost for every trip from CI1 at today's
           prices, and the freight cost per unit of RAT for a full hold
"""
import argparse
import csv
import json
import math
import sys

import numpy as np

from arbitrage import (DEFAULT_REACTOR_USAGE, FF_PER_PARSEC, BookStore,
                       FioClient, build_routes, reactor_fraction, walk_book)

SHIPS_PATH = "ships.json"
FLIGHTS_PATH = "flights.csv"


def load_ships(path=SHIPS_PATH):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        sys.exit(f"{path} not found. Copy ships.example.json to {path} first.")


def get_ship(ships, name):
    try:
        return ships["ships"][name]
    except KeyError:
        sys.exit(f"Ship '{name}' not in {SHIPS_PATH}. Known: "
                 f"{', '.join(ships.get('ships', {})) or 'none'}")


def distances(client, books, csv_path):
    """{(a, b): Route} with jumps and parsecs."""
    routes, _, _ = build_routes(client, books.exchanges, csv_path)
    return routes


def calibrate(ship_name, csv_path):
    ships = load_ships()
    ship = get_ship(ships, ship_name)
    try:
        with open(FLIGHTS_PATH, newline="") as f:
            flights = [r for r in csv.DictReader(f) if r["ship"] == ship_name]
    except FileNotFoundError:
        sys.exit(f"{FLIGHTS_PATH} not found. See flights.example.csv.")
    if not flights:
        sys.exit(f"No flights for {ship_name} in {FLIGHTS_PATH}.")
    client = FioClient()
    routes = distances(client, BookStore(client), csv_path)
    bad = [f"{r['from']}-{r['to']}" for r in flights
           if routes.get((r["from"], r["to"])) is None
           or routes[(r["from"], r["to"])].parsecs is None]
    if bad:
        sys.exit(f"No parsec distance for {', '.join(bad)}.")

    pc = np.array([routes[(r["from"], r["to"])].parsecs for r in flights])
    sf = np.array([float(r["sf"]) for r in flights])
    ff = np.array([float(r["ff"]) for r in flights])
    reactor = [reactor_fraction(r["reactor"]) if (r.get("reactor") or "").strip()
               else None for r in flights]

    print(f"{'route':<10}{'parsecs':>8}{'SF':>7}{'FF':>7}{'reactor':>9}"
          f"{'FF/pc':>7}{'game FF':>9}")
    for r, p, s_, f_, re_ in zip(flights, pc, sf, ff, reactor):
        implied = f_ / (p * FF_PER_PARSEC)
        rtxt = f"{100 * re_:.0f}%" if re_ else f"~{100 * implied:.0f}%*"
        game = f"{p * FF_PER_PARSEC * re_:.0f}" if re_ else ""
        print(f"{r['from'] + '-' + r['to']:<10}{p:>8.1f}{s_:>7.0f}{f_:>7.0f}"
              f"{rtxt:>9}{f_ / p:>7.2f}{game:>9}")

    sf_per_trip = float(sf.mean())
    ship["sf_per_trip"] = round(sf_per_trip, 2)
    print(f"\nSF per flight: {sf_per_trip:.1f} (spread {sf.min():.0f}-{sf.max():.0f})")

    known = [(p, f_, re_) for p, f_, re_ in zip(pc, ff, reactor) if re_]
    if known:
        x = np.array([p * re_ for p, _, re_ in known])
        y = np.array([f_ for _, f_, _ in known])
        fitted = float((x * y).sum() / (x * x).sum())
        print(f"FF per parsec at 100% reactor: {fitted:.2f} measured vs "
              f"{FF_PER_PARSEC} game formula")
        if abs(fitted / FF_PER_PARSEC - 1) > 0.05:
            ship["ff_per_parsec"] = round(fitted, 3)
            print("More than 5% off the formula, so the measured value is saved "
                  "for this ship.")
        else:
            ship.pop("ff_per_parsec", None)
            print("Matches the game formula.")
        used = sorted({re_ for _, _, re_ in known})
        if "reactor_usage" not in ship:
            ship["reactor_usage"] = used[0]
            print(f"Reactor usage set to {100 * used[0]:.0f}% (your lowest planned "
                  "setting). Change reactor_usage in ships.json if you fly another.")
    else:
        print("* No reactor column: the % shown is the reactor setting the FF "
              f"implies under the game formula ({FF_PER_PARSEC} FF/pc x usage). "
              "Add a reactor column to flights.csv for an exact check.")
        ship.setdefault("reactor_usage", DEFAULT_REACTOR_USAGE)
    ship.pop("ff_per_distance", None)  # old jump-based model

    with open(SHIPS_PATH, "w") as f:
        json.dump(ships, f, indent=2)
    print(f"Saved to {SHIPS_PATH}: sf_per_trip={ship['sf_per_trip']}, "
          f"reactor_usage={ship['reactor_usage']}"
          + (f", ff_per_parsec={ship['ff_per_parsec']}" if "ff_per_parsec" in ship else ""))


def fuel_value(books, ex, ticker, units):
    """Cost of buying units of fuel at ex, walking the ask book. NaN if thin."""
    book = books.get(ticker, ex)
    if not book or not book.asks:
        return math.nan
    filled, value = walk_book(book.asks, units)
    return value if filled >= units * 0.999 else math.nan


def quote(ship_name, at, ticker, csv_path):
    s = get_ship(load_ships(), ship_name)
    reactor = reactor_fraction(s.get("reactor_usage"))
    ff_pc = s.get("ff_per_parsec", FF_PER_PARSEC)
    sf_u = s.get("sf_per_trip")
    client = FioClient()
    books = BookStore(client)
    if at not in books.exchanges:
        sys.exit(f"Unknown exchange {at}. Known: {', '.join(sorted(books.exchanges))}")
    routes = distances(client, books, csv_path)
    w, v = client.materials().get(ticker, (None, None)) if ticker else (None, None)
    cap = math.floor(min(s["cargo_weight"] / w, s["cargo_volume"] / v)) if w and v else None
    if ticker and not cap:
        sys.exit(f"Unknown ticker {ticker} or it has no weight/volume.")

    print(f"{ship_name}: {100 * reactor:g}% reactor, {ff_pc:g} FF/pc at 100%, "
          + (f"{sf_u:g} SF per flight" if sf_u else "SF not measured (left out)"))
    if cap:
        print(f"{ticker}: {cap} units fill the hold")
    print(f"\n{'to':<6}{'jumps':>6}{'parsecs':>9}{'SF':>7}{'FF':>7}{'fuel cost':>12}"
          + (f"{'per unit':>10}" if cap else ""))
    for (a, b), r in sorted(routes.items()):
        if a != at or r.parsecs is None:
            continue
        ff_u = r.parsecs * ff_pc * reactor
        cost = fuel_value(books, at, "FF", ff_u)
        if sf_u:
            cost += fuel_value(books, at, "SF", sf_u)
        line = f"{b:<6}{r.jumps:>6.0f}{r.parsecs:>9.1f}{sf_u or 0:>7.0f}{ff_u:>7.0f}"
        line += f"{cost:>12,.0f}" if not math.isnan(cost) else f"{'thin book':>12}"
        if cap and not math.isnan(cost):
            line += f"{cost / cap:>10,.2f}"
        print(line)
    print(f"\nOne-way fuel bought at {at}, in {at}'s currency, at current asks.")


def main():
    ap = argparse.ArgumentParser(description="Fuel calibration and freight quotes.")
    ap.add_argument("cmd", choices=["calibrate", "quote"])
    ap.add_argument("--ship", required=True)
    ap.add_argument("--at", help="exchange the ship departs from (quote)")
    ap.add_argument("--ticker", help="material to price freight for (quote)")
    ap.add_argument("--distances", default="distances.csv",
                    help="optional override CSV; otherwise fetched from FIO")
    args = ap.parse_args()
    if args.cmd == "calibrate":
        calibrate(args.ship, args.distances)
    else:
        if not args.at:
            ap.error("quote needs --at")
        quote(args.ship, args.at.upper(), args.ticker, args.distances)


if __name__ == "__main__":
    main()
