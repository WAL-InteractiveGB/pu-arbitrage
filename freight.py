#!/usr/bin/env python3
"""
Fuel calibration and freight quotes for pu-arbitrage.

How the game uses fuel (community-derived; the game does not publish it):
    FTL fuel (FF) = parsecs flown x 7.2 x reactor usage
        Reactor usage is the FTL slider in the flight planner (SFC), 25% at
        its cheapest. Higher settings burn more FF for a slightly faster jump.
    STL fuel (SF) does not scale with distance. Between stations there are two
        STL phases. The approach burns 0.494 x STL slider x SF loaded. Departure
        burns the same at most, less if the jump gate is close to the station.
        (From the prun-toolset flight-planner research.)
Distances in parsecs come from the game's own jump routes via FIO.

Usage
    1. Add your ship to ships.json (see ships.example.json).
    2. Plan a few fully loaded flights in SFC. For each, note the SF and FF it
       shows and the FTL reactor setting you planned it at. Put them in
       flights.csv with columns ship,from,to,sf,ff,reactor,sf_loaded,stl_usage,hours
       (see flights.example.csv). reactor and stl_usage are the SFC sliders,
       sf_loaded the SF in the tank and hours the flight time SFC shows
       (6h 20m, 6:20 or 6.33). With hours filled in, calibrate fits a full
       flight model and arbitrage.py then picks the best sliders per trip.
       Plan at least 6 flights: one route at a few STL and reactor settings,
       plus a route of a different length.
    3. python freight.py calibrate --ship MyHauler
           fits the STL model (or the average SF per flight without
           sf_loaded), checks FF against the game formula, and saves the
           results to ships.json
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

from arbitrage import (DEFAULT_REACTOR_USAGE, DEFAULT_STL_USAGE, FF_PER_PARSEC,
                       STL_BURN, STL_DEPARTURE_SHARE, BookStore, Ship,
                       FioClient, build_routes, reactor_fraction, walk_book)
from flightmodel import fit_auto, parse_duration

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


def calibrate(ship_name, csv_path, mode="auto"):
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

    if any((r.get("hours") or "").strip() for r in flights):
        return calibrate_model(ships, ship, flights, routes, mode)

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

    stl = []
    for r, s_ in zip(flights, sf):
        loaded = (r.get("sf_loaded") or "").strip()
        usage = (r.get("stl_usage") or "").strip()
        if loaded and usage:
            stl.append((s_, float(loaded), reactor_fraction(usage, "STL usage")))
    if stl:
        # SF = 0.494 x usage x loaded x (1 + departure share)
        shares = [s_ / (STL_BURN * u * l) - 1 for s_, l, u in stl if u * l > 0]
        dep = min(1.0, max(0.0, float(np.mean(shares))))
        print(f"\nSTL model: departure burns {100 * dep:.0f}% of the approach "
              f"(per flight: {', '.join(f'{100 * x:.0f}%' for x in shares)}).")
        if max(shares) - min(shares) > 0.3:
            print("The flights disagree a lot. The gate distance differs by route, "
                  "so treat this as an average.")
        ship["stl_departure_share"] = round(dep, 3)
        ship["sf_loaded"] = max(l for _, l, _ in stl)
        ship["stl_usage"] = stl[-1][2]
        ship.pop("sf_per_trip", None)
    else:
        sf_per_trip = float(sf.mean())
        ship["sf_per_trip"] = round(sf_per_trip, 2)
        print(f"\nSF per flight: {sf_per_trip:.1f} (spread {sf.min():.0f}-{sf.max():.0f})")
        print("Add sf_loaded and stl_usage columns to fit the STL model, which "
              "follows your SFC settings instead of one fixed number.")

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
    stl_txt = (f"sf_per_trip={ship['sf_per_trip']}" if "sf_per_trip" in ship else
               f"sf_loaded={ship['sf_loaded']}, stl_usage={ship['stl_usage']}, "
               f"stl_departure_share={ship['stl_departure_share']}")
    print(f"Saved to {SHIPS_PATH}: {stl_txt}, "
          f"reactor_usage={ship['reactor_usage']}"
          + (f", ff_per_parsec={ship['ff_per_parsec']}" if "ff_per_parsec" in ship else ""))


def calibrate_model(ships, ship, flights, routes, mode="auto"):
    """Fit the full flight model (time and fuel vs both sliders)."""
    rows = []
    for r in flights:
        route = routes[(r["from"], r["to"])]
        num = lambda k: float(r[k]) if (r.get(k) or "").strip() else None
        rows.append(dict(
            label=f"{r['from']}-{r['to']}", parsecs=route.parsecs, jumps=route.jumps,
            stl_usage=(reactor_fraction(r["stl_usage"], "STL usage")
                       if (r.get("stl_usage") or "").strip() else None),
            reactor=(reactor_fraction(r["reactor"])
                     if (r.get("reactor") or "").strip() else None),
            sf_loaded=num("sf_loaded"), sf=num("sf"), ff=num("ff"),
            hours=parse_duration(r["hours"]) if (r.get("hours") or "").strip() else None))
    try:
        model = fit_auto(rows, mode)
    except ValueError as exc:
        sys.exit(f"Can't fit a flight model: {exc}")
    print(f"{'route':<10}{'STL':>5}{'FTL':>5}{'hours':>8}{'model':>8}"
          f"{'SF':>7}{'model':>7}{'FF':>7}{'model':>7}")
    for f in rows:
        if not (f["hours"] and f["stl_usage"] and f["reactor"] and f["sf_loaded"]):
            print(f"{f['label']:<10} skipped (missing time, slider or SF loaded)")
            continue
        h = model.hours(f["parsecs"], f["jumps"], f["stl_usage"], f["reactor"],
                        f["sf_loaded"])
        print(f"{f['label']:<10}{100 * f['stl_usage']:>4.0f}%{100 * f['reactor']:>4.0f}%"
              f"{f['hours']:>8.2f}{h:>8.2f}"
              f"{f['sf'] if f['sf'] is not None else float('nan'):>7.0f}"
              f"{model.sf(f['stl_usage'], f['sf_loaded']):>7.0f}"
              f"{f['ff'] if f['ff'] is not None else float('nan'):>7.0f}"
              f"{model.ff(f['parsecs'], f['reactor']):>7.0f}")
    print(f"\nTypical time error: {60 * model.rmse_hours:.0f} min over "
          f"{model.flights} flights.")
    if model.kind == "formula":
        print(f"Model: the formula. FTL: {model.ff_per_parsec:.2f} FF per parsec at "
              f"100% reactor (game formula {FF_PER_PARSEC}). STL: "
              f"{model.sf_coef:.3f} x slider x SF loaded (research: "
              f"{STL_BURN}-{2 * STL_BURN:.3f}).")
    else:
        print("Model: measured settings, one straight line per setting:")
        for x in model.settings:
            print(f"  STL {100 * x['stl']:g}% / reactor {100 * x['reactor']:g}%: "
                  f"{x['base']:.2f} h + {60 * x['per_pc']:.1f} min per parsec, "
                  f"SF {100 * x['sf_share']:.0f}% of loaded, "
                  f"{x['ff_per_pc']:.2f} FF per parsec")
    for note in model.notes:
        print("Note:", note)
    ship["flight_model"] = model.to_dict()
    ship["sf_loaded"] = max(f["sf_loaded"] for f in rows if f["sf_loaded"])
    for key in ("sf_per_trip", "stl_departure_share", "ff_per_distance"):
        ship.pop(key, None)
    with open(SHIPS_PATH, "w") as f:
        json.dump(ships, f, indent=2)
    print(f"Saved the flight model to {SHIPS_PATH}. arbitrage.py --ship "
          f"{next(k for k, v in ships['ships'].items() if v is ship)} will now "
          "choose the STL and reactor settings for each trip.")


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
    sf_u = Ship(weight=1, volume=1, sf_per_trip=s.get("sf_per_trip"),
                sf_loaded=s.get("sf_loaded"),
                stl_usage=reactor_fraction(s.get("stl_usage", DEFAULT_STL_USAGE),
                                           "STL usage"),
                stl_departure_share=s.get("stl_departure_share",
                                          STL_DEPARTURE_SHARE)).sf_units()
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
          + (f"{sf_u:.0f} SF per flight" if sf_u else "SF not set (left out)"))
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
    ap.add_argument("--model", choices=["auto", "formula", "measured"], default="auto",
                    help="calibrate: the formula, straight lines through your "
                         "measured settings, or auto (formula unless it fits badly)")
    ap.add_argument("--distances", default="distances.csv",
                    help="optional override CSV; otherwise fetched from FIO")
    args = ap.parse_args()
    if args.cmd == "calibrate":
        calibrate(args.ship, args.distances, args.model)
    else:
        if not args.at:
            ap.error("quote needs --at")
        quote(args.ship, args.at.upper(), args.ticker, args.distances)


if __name__ == "__main__":
    main()
