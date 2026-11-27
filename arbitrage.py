#!/usr/bin/env python3
"""
Spatial arbitrage optimizer for Prosperous Universe.

For a ship sitting at one exchange, this ranks the best trips to other
exchanges. For each destination it walks the real order books, packs the best
multi-commodity cargo under capital, weight and volume limits, deducts every
travel cost (fuel and border crossings), estimates risk from price history,
and scores each trip by risk-adjusted profit per hour.

Currencies
    Each exchange trades in one currency (for example CI1 and CI2 in CIS, NC1 and
    NC2 in NCC). Without FX access, prices in different currencies cannot be
    compared, so there are two ways to trade (see currencies.py):
      same   one-way trips between exchanges that share a currency, such as
             CI1 > CI2.
      cycle  buy at your exchange A, sell at B, spend the proceeds on goods at
             B, then sell them at an exchange that uses A's currency (A itself
             or its sibling). Both legs are optimised together, and all profit
             is counted in A's currency. If B uses a different currency, any
             cash left over at B cannot be converted back, so it is reported
             separately and counted as zero profit. If B shares A's currency
             (a "2-leg" trip), leftover cash is simply profit.
    --mode both (the default) evaluates and ranks both kinds together.

Travel costs
    Fuel     SF and FF bought at the departure exchange by walking its ask book.
             FF follows the game: parsecs flown x 7.2 x reactor usage. SF is a
             per-flight amount measured in the flight planner (see freight.py).
    Gateways the fee charged for jumping through a gateway, per route. FIO
             does not publish these, so copy the cost the in-game flight
             planner (SFC) shows into borders.json (see borders.example.json),
             or use --border-fee for one flat fee on every faction-border
             crossing. Optional duties are supported too (see borders.py):
             they are built into per-unit prices, so the optimizer picks cargo
             with them in mind.

Sections
    1. FIO API client and data stores
    2. Order book walking, VWAP and slippage
    3. Multi-commodity basket optimizer
    4. Route evaluation
    5. Output and main routine

Usage
    pip install requests numpy scipy
    python arbitrage.py --inspect                       # check FIO field names first
    python arbitrage.py --at CI1 --ship MyHauler --capital 500000 \\
        --hours-per-distance 1.5 --mode both
    python arbitrage.py --at CI1 --ship MyHauler --border-fee 5000
    python arbitrage.py --at CI1 --ship MyHauler --record 1   # log trade #1
    python tradelog.py report                                  # predicted vs actual

    Use FIO exchange codes (for example AI1, CI1, IC1, NC1, NC2). Distances are
    fetched automatically from the game's own jump routes between each
    exchange's star system (FIO /exchange/station and /systemstars/jumproute),
    as both a jump count and a distance in parsecs. An optional distances.csv
    (from,to,distance and optional hours and parsecs columns) overrides any
    pair. Use --write-distances FILE to export the fetched table.

Placeholders you should replace with real numbers: --fee-rate (exchange fee),
the ship's hours-per-distance and sf_per_trip, and borders.json.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import itertools
import json
import math
import os
import sqlite3
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from urllib.parse import quote

import numpy as np
import requests

from borders import REGION_KEYS, BorderModel, Crossing
from flightmodel import FlightModel, MeasuredModel, load_model
from currencies import (CURRENCY_KEYS, normalise, resolve_currencies,
                        same_currency)

try:
    from scipy.optimize import Bounds, LinearConstraint, milp
    HAVE_SCIPY = True
except ImportError:  # the greedy fallback is used instead
    HAVE_SCIPY = False

# ----------------------------------------------------------------------------
# Parameters
# ----------------------------------------------------------------------------
BASE = "https://rest.fnar.net"
DEFAULT_FEE_RATE = 0.01  # exchange fee per trade leg (placeholder, --fee-rate)
UNLIMITED = 10 ** 9      # quantity used for orders with no stated size
# Order-book depth on arrival. Other players trade while you fly, so part of
# the bids (or asks) you are counting on will be gone. The share left is
# exp(-TAKER_SHARE * daily units traded * days in transit / units on the book):
# the longer the flight and the busier the market relative to its depth, the
# less is left. Daily volume is the 30-day average with price history, else
# the last 24 hours from /exchange/full.
TAKER_SHARE = 0.5        # share of traded volume assumed to hit this side
MIN_DEPTH_SHARE = 0.1    # never assume less than this is left
DEST_DEPTH_SHARE = 0.5   # fallback when FIO gives no volume for an item
MAX_CANDIDATES = 40      # items per route that get a history lookup
MAX_CANDIDATES_CYCLE = 25  # items per leg of a cycle
HISTORY_DAYS = 30
MIN_HISTORY_POINTS = 10
DEFAULT_DAILY_VOL = 0.06  # volatility assumed when history is too thin
REVERSION = 0.25         # share of an above-average destination price assumed
                         # to mean-revert before you sell
MAX_HAIRCUT = 0.5
STALE_DAYS = 7           # ignore local history older than this
SOLVER_SECONDS = 20
HISTORY_WORKERS = 4      # parallel history downloads (kept low to be polite)

VERBOSE = False


def log(msg):
    if VERBOSE:
        print(msg, file=sys.stderr)


def progress(msg):
    """One overwriting status line on an interactive terminal."""
    if VERBOSE:
        print(msg, file=sys.stderr)
    elif sys.stderr.isatty():
        print(f"\r{msg:<70}", end="", file=sys.stderr, flush=True)


def progress_done():
    if not VERBOSE and sys.stderr.isatty():
        print("\r" + " " * 70 + "\r", end="", file=sys.stderr, flush=True)


# ============================================================================
# 1. FIO API client and data stores
# ============================================================================
def pick(rec, *keys):
    """First non-null value among several possible key names."""
    for k in keys:
        if k in rec and rec[k] is not None:
            return rec[k]
    return None


@dataclass
class Book:
    asks: list = field(default_factory=list)  # (price, qty) cheapest first
    bids: list = field(default_factory=list)  # (price, qty) highest first


class FioClient:
    """GET client with an on-disk cache, retries and an offline mode.

    If the API is unreachable, a stale cached copy is used (with a warning)
    rather than failing the whole run."""

    RETRY_STATUS = {429, 500, 502, 503, 504}

    def __init__(self, base=BASE, cache_dir=".fio_cache", delay=0.15,
                 retries=3, offline=False):
        self.base, self.cache_dir, self.delay = base, cache_dir, delay
        self.retries, self.offline = retries, offline
        self.session = requests.Session()
        # FIO asks applications to identify themselves.
        self.session.headers["User-Agent"] = "pu-arbitrage"
        self.session.headers["X-FIO-Application"] = "pu-arbitrage"
        self._warned_stale = False
        self._lock = threading.Lock()
        os.makedirs(cache_dir, exist_ok=True)

    def _cache_file(self, path):
        return os.path.join(self.cache_dir,
                            hashlib.sha1(path.encode()).hexdigest() + ".json")

    @staticmethod
    def _read_cache(file):
        try:
            age = time.time() - os.path.getmtime(file)
            with open(file) as f:
                return age, json.load(f)
        except (OSError, ValueError):
            return None  # missing or corrupt: treat as not cached

    def get(self, path, ttl=900):
        file = self._cache_file(path)
        cached = self._read_cache(file)
        if cached and (self.offline or cached[0] < ttl):
            return cached[1]
        if self.offline:
            raise requests.ConnectionError(f"{path} is not cached (--offline)")

        last_exc = None
        for attempt in range(self.retries):
            try:
                resp = self.session.get(self.base + path, timeout=60)
                if resp.status_code in self.RETRY_STATUS:
                    raise requests.HTTPError(f"HTTP {resp.status_code}", response=resp)
                resp.raise_for_status()
                data = resp.json()
            except (requests.RequestException, ValueError) as exc:
                last_exc = exc
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status is not None and status not in self.RETRY_STATUS:
                    break  # a 4xx will not fix itself
                time.sleep(1.5 * 2 ** attempt)
                continue
            tmp = f"{file}.{threading.get_ident()}.tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, file)  # atomic: never leaves a half-written cache
            time.sleep(self.delay)
            return data

        if cached:
            with self._lock:
                if not self._warned_stale:
                    print(f"Warning: FIO request failed ({last_exc}); using cached "
                          "data that may be out of date.", file=sys.stderr)
                    self._warned_stale = True
            return cached[1]
        raise last_exc

    def materials(self):
        """{ticker: (weight tons, volume m3)} from /material/allmaterials."""
        rows = self.get("/material/allmaterials", ttl=86400)
        return {m["Ticker"]: (m.get("Weight"), m.get("Volume"))
                for m in rows if m.get("Ticker")}

    def categories(self):
        """{ticker: category} from /material/allmaterials, e.g. SI: metals."""
        rows = self.get("/material/allmaterials", ttl=86400)
        return {m["Ticker"]: m.get("CategoryName") or "" for m in rows if m.get("Ticker")}

    def stations(self):
        try:
            data = self.get("/exchange/station", ttl=7 * 86400)
        except (requests.RequestException, ValueError):
            return []
        return data if isinstance(data, list) else []


def parse_orders(rows, descending):
    """Normalise a list of FIO orders into sorted (price, qty) tuples."""
    out = []
    for r in rows or []:
        price = pick(r, "ItemCost", "Price")
        if price is None:
            continue
        has_qty = any(k in r for k in ("ItemCount", "Amount", "Quantity"))
        qty = pick(r, "ItemCount", "Amount", "Quantity")
        if qty is None:
            if not has_qty:
                continue
            qty = UNLIMITED  # explicit null size: treated as unlimited
        out.append((float(price), float(qty)))
    out.sort(key=lambda x: x[0], reverse=descending)
    return out


def parse_book(rec):
    return Book(asks=parse_orders(rec.get("SellingOrders"), False),
                bids=parse_orders(rec.get("BuyingOrders"), True))


class BookStore:
    """Order books for every ticker and exchange, loaded from /exchange/full.

    If the bulk endpoint lacks order lists, books are fetched per ticker on
    demand from /exchange/{TICKER}.{EXCHANGE}.
    """

    def __init__(self, client):
        self.client = client
        self.books, self.currency, self._by_ex = {}, {}, {}
        self.traded = {}  # (ticker, ex) -> units traded in the last 24 hours
        for rec in client.get("/exchange/full", ttl=300):
            t, ex = rec.get("MaterialTicker"), rec.get("ExchangeCode")
            if not t or not ex:
                continue
            self._by_ex.setdefault(ex, set()).add(t)
            cur = normalise(pick(rec, *CURRENCY_KEYS))
            if cur:
                self.currency[ex] = cur
            if rec.get("Traded") is not None:
                self.traded[(t, ex)] = float(rec["Traded"])
            if "BuyingOrders" in rec or "SellingOrders" in rec:
                self.books[(t, ex)] = parse_book(rec)

    @property
    def exchanges(self):
        return set(self._by_ex)

    def tickers_at(self, ex):
        return self._by_ex.get(ex, set())

    def get(self, ticker, ex):
        key = (ticker, ex)
        if key not in self.books and ticker in self.tickers_at(ex):
            try:
                rec = self.client.get(f"/exchange/{ticker}.{ex}", ttl=300)
                self.books[key] = parse_book(rec)
            except (requests.RequestException, ValueError):
                self.books[key] = Book()
        return self.books.get(key)


class History:
    """Daily closing prices, from the local prun.db if fresh, else from FIO."""

    def __init__(self, client, db_path="prun.db", enabled=True):
        self.client, self.enabled, self.cache = client, enabled, {}
        self.volumes = {}  # (ticker, ex) -> daily units traded, from FIO
        self.conn = (sqlite3.connect(db_path, check_same_thread=False)
                     if enabled and os.path.exists(db_path) else None)
        self._db_lock = threading.Lock()

    def prefetch(self, keys):
        """Load several (ticker, exchange) histories in parallel."""
        todo = [k for k in dict.fromkeys(keys) if k not in self.cache]
        if not self.enabled or len(todo) < 2:
            return
        with ThreadPoolExecutor(HISTORY_WORKERS) as pool:
            list(pool.map(lambda k: self.closes(*k), todo))

    def closes(self, ticker, ex):
        if not self.enabled:
            return []
        key = (ticker, ex)
        if key not in self.cache:
            data = self._from_db(ticker, ex)
            if len(data) < MIN_HISTORY_POINTS:
                data = self._from_api(ticker, ex)
            self.cache[key] = data
        return self.cache[key]

    def _from_db(self, ticker, ex):
        if not self.conn:
            return []
        try:
            with self._db_lock:
                rows = self.conn.execute(
                    "SELECT date, close FROM daily WHERE ticker=? AND exchange=? "
                    "AND close IS NOT NULL AND (interval='' OR interval IS NULL "
                    "OR interval LIKE '%DAY%') ORDER BY date DESC LIMIT ?",
                    (ticker, ex, HISTORY_DAYS)).fetchall()
            if not rows:
                return []
            newest = datetime.strptime(str(rows[0][0])[:10], "%Y-%m-%d")
        except (sqlite3.Error, ValueError):
            return []
        age = datetime.now(timezone.utc) - newest.replace(tzinfo=timezone.utc)
        if age.days > STALE_DAYS:
            return []
        return [float(c) for _, c in reversed(rows)]

    def _from_api(self, ticker, ex):
        try:
            recs = self.client.get(f"/exchange/cxpc/{ticker}.{ex}", ttl=6 * 3600)
        except (requests.RequestException, ValueError):
            return []
        recs = [r for r in recs if isinstance(r, dict)] if isinstance(recs, list) else []
        if any("Interval" in r for r in recs):
            daily = [r for r in recs if "DAY" in str(r.get("Interval", "")).upper()]
            recs = daily or recs
        recs.sort(key=lambda r: pick(r, "DateEpochMs", "Date", "Timestamp") or 0)
        closes = [float(r["Close"]) for r in recs if r.get("Close") is not None]
        traded = [float(r["Traded"]) for r in recs if r.get("Traded") is not None]
        if len(traded) >= MIN_HISTORY_POINTS:
            self.volumes[(ticker, ex)] = statistics.fmean(traded[-HISTORY_DAYS:])
        return closes[-HISTORY_DAYS:]

    def daily_volume(self, ticker, ex):
        """Average daily units traded, if the history download had volumes."""
        return self.volumes.get((ticker, ex))


@dataclass(frozen=True)
class Route:
    """Distance between two exchanges.

    jumps    number of FTL jumps (used for range and travel time)
    parsecs  total FTL distance (used for FTL fuel, as the game does)
    hours    optional fixed travel time from distances.csv"""
    jumps: float
    parsecs: float | None = None
    hours: float | None = None


def load_routes_csv(path):
    """{(from, to): Route} from an optional override CSV with columns
    from,to,distance and optional hours and parsecs columns."""
    routes = {}
    if not path or not os.path.exists(path):
        return routes
    with open(path, newline="") as f:
        for n, r in enumerate(csv.DictReader(f), 2):
            try:
                opt = {k: float(v) for k in ("hours", "parsecs")
                       if (v := (r.get(k) or "").strip())}
                val = Route(float(r["distance"]), opt.get("parsecs"), opt.get("hours"))
                a, b = r["from"].strip(), r["to"].strip()
            except (KeyError, ValueError, AttributeError):
                raise SystemExit(f"{path} line {n}: expected from,to,distance"
                                 f"[,hours][,parsecs], got {dict(r)}") from None
            routes[(a, b)] = routes[(b, a)] = val
    return routes


# Field names that may hold an exchange's code or its star system in the
# /exchange/station records. Several are tried in case FIO renames them.
CODE_KEYS = ("ComexCode", "ExchangeCode", "Code")
SYSTEM_KEYS = ("SystemNaturalId", "SystemName", "SystemId")
DEFAULT_PARSEC_LENGTH = 12.0  # map units per parsec (FIO /global/simulationdata)


def parsec_length(client):
    try:
        data = client.get("/global/simulationdata", ttl=30 * 86400)
        return float(data.get("ParsecLength") or DEFAULT_PARSEC_LENGTH)
    except (requests.RequestException, ValueError, AttributeError, TypeError):
        return DEFAULT_PARSEC_LENGTH


def jump_route(client, src, dst, pc_len):
    """(jumps, parsecs) of the game's route between two systems, or None.

    /systemstars/jumproute lists every hop with its map distance; the game
    burns FTL fuel per parsec, so the hop distances are what matter."""
    path = f"/systemstars/jumproute/{quote(str(src), safe='')}/{quote(str(dst), safe='')}"
    try:
        hops = client.get(path, ttl=30 * 86400)  # the map is static
    except (requests.RequestException, ValueError):
        return None
    if not isinstance(hops, list) or not all(isinstance(h, dict) for h in hops):
        return None
    dist = [h.get("Distance") for h in hops]
    if not all(isinstance(d, (int, float)) for d in dist):
        return None
    return float(len(hops)), sum(dist) / pc_len


def auto_routes(client, exchanges):
    """Jumps and parsecs between every pair of exchanges, fetched from FIO.

    Maps each exchange to its star system with /exchange/station, then asks
    /systemstars/jumproute for each pair. Cached on disk for 30 days.
    Returns ({(a, b): Route}, list of pairs that could not be resolved).
    """
    by_code = {}
    for r in client.stations():
        code = pick(r, *CODE_KEYS)
        if code in exchanges:
            by_code[code] = {k: r[k] for k in SYSTEM_KEYS if r.get(k) is not None}
    pairs = list(itertools.combinations(sorted(by_code), 2))
    unknown = [(a, b) for a, b in itertools.combinations(sorted(exchanges), 2)
               if a not in by_code or b not in by_code]
    if not pairs:
        return {}, unknown
    pc_len = parsec_length(client)

    # Find which system identifier the endpoint accepts, trying a few pairs in
    # case one of them is genuinely unreachable.
    key = None
    for a, b in pairs[:3]:
        key = next((k for k in SYSTEM_KEYS
                    if k in by_code[a] and k in by_code[b]
                    and jump_route(client, by_code[a][k], by_code[b][k], pc_len)),
                   None)
        if key:
            break
    routes, failed = {}, list(unknown)
    for a, b in pairs:
        res = (jump_route(client, by_code[a][key], by_code[b][key], pc_len)
               if key and key in by_code[a] and key in by_code[b] else None)
        if not res:
            failed.append((a, b))
        else:
            routes[(a, b)] = routes[(b, a)] = Route(res[0], res[1])
    return routes, failed


def build_routes(client, exchanges, csv_path):
    """Routes from FIO, with any rows in the override CSV taking priority.
    An override without a parsecs column keeps FIO's parsec distance."""
    routes, failed = auto_routes(client, exchanges)
    overrides = load_routes_csv(csv_path)
    for pair, r in overrides.items():
        base = routes.get(pair)
        routes[pair] = Route(r.jumps, r.parsecs if r.parsecs is not None
                             else (base.parsecs if base else None), r.hours)
    failed = [p for p in failed if p not in routes]
    return routes, failed, len(overrides) // 2


# ============================================================================
# 2. Order book walking, VWAP and slippage
# ============================================================================
def walk_book(levels, qty):
    """Consume up to qty units from sorted levels. Returns (filled, value)."""
    filled = value = 0.0
    for price, avail in levels:
        if filled >= qty:
            break
        take = min(avail, qty - filled)
        filled += take
        value += take * price
    return filled, value


def vwap(levels, qty):
    """Volume weighted average price for qty units, and units actually filled."""
    filled, value = walk_book(levels, qty)
    return (value / filled if filled else float("nan")), filled


def slippage(levels, qty):
    """Fractional move of the VWAP away from top of book for qty units."""
    price, filled = vwap(levels, qty)
    if not filled or not levels:
        return float("nan")
    return abs(price / levels[0][0] - 1)


def depth_share(levels, daily_units, hours):
    """Share of the displayed book expected to still be there after `hours`.

    daily_units is how many units of this item change hands per day. Half of
    that volume (TAKER_SHARE) is assumed to trade against the side you need,
    spread evenly over the book. Orders with no stated size (market makers) are
    not counted as depth and are never used up."""
    if daily_units is None:
        return DEST_DEPTH_SHARE
    depth = sum(q for _, q in levels if q < UNLIMITED)
    if depth <= 0:
        return 1.0
    used = TAKER_SHARE * daily_units * hours / 24
    return max(MIN_DEPTH_SHARE, math.exp(-used / depth))


def market_volume(market, ticker, ex):
    """Daily units traded: 30-day average if loaded, else the last 24 hours."""
    get = getattr(market.history, "daily_volume", None)
    vol = get(ticker, ex) if get else None
    if vol is None:
        vol = getattr(market.books, "traded", {}).get((ticker, ex))
    return vol


@dataclass
class Lot:
    """A slice of one item with a constant buy and sell price."""
    ticker: str
    qty: float
    buy: float        # raw ask paid
    sell: float       # raw bid received
    unit_cost: float  # buy including exchange fee and export duty
    unit_rev: float   # sell after haircut, exchange fee and import duty
    weight: float
    volume: float
    buy_duty: float = 0.0   # export duty per unit (buy-side currency)
    sell_duty: float = 0.0  # import duty per unit (sell-side currency)

    @property
    def margin(self):
        return self.unit_rev - self.unit_cost


@dataclass(frozen=True)
class Pricing:
    """Per-unit cost adjustments applied when walking two books together."""
    fee: float = DEFAULT_FEE_RATE  # exchange fee, each side
    haircut: float = 0.0           # expected fall in the sale price
    markup: float = 0.0            # expected rise in the purchase price
    export_duty: float = 0.0       # share of purchase value
    import_duty: float = 0.0       # share of sale value

    def unit_cost(self, ask):
        value = ask * (1 + self.markup)
        return value * (1 + self.fee + self.export_duty), value * self.export_duty

    def unit_rev(self, bid):
        value = bid * (1 - self.haircut)
        return value * (1 - self.fee - self.import_duty), value * self.import_duty


def build_lots(ticker, asks, bids, weight, volume, pricing=Pricing(),
               dest_share=DEST_DEPTH_SHARE, ask_share=1.0, require_profit=True,
               max_qty=None):
    """Walk the buy-side asks and sell-side bids together, one price level at a
    time. Each lot is a stretch where both prices are constant, so the cost of
    slippage is explicit.

    With require_profit (same-currency trades) it stops when the next unit would
    lose money. Cross-currency legs pass require_profit=False, because prices in
    two currencies cannot be compared, and stop at max_qty units instead.
    ask_share models buying later, after a flight, when part of the displayed
    asks may be gone."""
    def scale(levels, share):
        return [(p, q if q >= UNLIMITED else math.floor(q * share)) for p, q in levels]

    asks, bids = scale(asks, ask_share), scale(bids, dest_share)
    lots, i, j, total = [], 0, 0, 0.0
    ra = asks[0][1] if asks else 0
    rb = bids[0][1] if bids else 0
    while i < len(asks) and j < len(bids):
        unit_cost, buy_duty = pricing.unit_cost(asks[i][0])
        unit_rev, sell_duty = pricing.unit_rev(bids[j][0])
        if require_profit and unit_rev <= unit_cost:
            break  # asks only rise and bids only fall, so no later lot can pay
        q = min(ra, rb)
        if q > 0:
            lots.append(Lot(ticker, q, asks[i][0], bids[j][0], unit_cost,
                            unit_rev, weight, volume, buy_duty, sell_duty))
            total += q
            if max_qty is not None and total >= max_qty:
                break
        ra -= q
        rb -= q
        if ra <= 0:
            i += 1
            ra = asks[i][1] if i < len(asks) else 0
        if rb <= 0:
            j += 1
            rb = bids[j][1] if j < len(bids) else 0
    return lots


# ============================================================================
# 3. Multi-commodity basket optimizer
# ============================================================================
def _natural_cap(lot, capital, max_w, max_v):
    return math.floor(min(lot.qty, capital / lot.unit_cost,
                          max_w / lot.weight, max_v / lot.volume))


def _greedy(items, capital, max_w, max_v):
    """Fallback packer: best profit per unit of the scarcest resource first."""
    def scarcity(lot):
        return max(lot.unit_cost / capital, lot.weight / max_w, lot.volume / max_v)
    chosen, cash, wt, vol = [], capital, max_w, max_v
    for lot, cap in sorted(items, key=lambda x: x[0].margin / scarcity(x[0]),
                           reverse=True):
        units = math.floor(min(cap, cash / lot.unit_cost, wt / lot.weight,
                               vol / lot.volume))
        if units >= 1:
            chosen.append((lot, units))
            cash -= units * lot.unit_cost
            wt -= units * lot.weight
            vol -= units * lot.volume
    return chosen


def optimize_basket(lots, capital, max_w, max_v):
    """Choose integer units of every lot to maximise net margin subject to
    capital, weight and volume limits (a three-constraint knapsack).

    Because asks rise and bids fall, an item's earlier lots always beat its
    later ones, so the optimiser takes them in order without extra rules."""
    if capital <= 0:
        return []
    items = []
    for lot in lots:
        if lot.margin > 0 and lot.weight > 0 and lot.volume > 0:
            cap = _natural_cap(lot, capital, max_w, max_v)
            if cap >= 1:
                items.append((lot, cap))
    if not items:
        return []

    if HAVE_SCIPY:
        n = len(items)
        c = -np.array([lot.margin for lot, _ in items])
        a = np.array([[lot.unit_cost for lot, _ in items],
                      [lot.weight for lot, _ in items],
                      [lot.volume for lot, _ in items]])
        res = milp(c, integrality=np.ones(n),
                   bounds=Bounds(np.zeros(n), np.array([cap for _, cap in items])),
                   constraints=LinearConstraint(a, -np.inf, [capital, max_w, max_v]),
                   options={"time_limit": SOLVER_SECONDS})
        if res.x is not None:
            x = np.floor(res.x + 1e-6)
            used = a @ x
            if (used <= np.array([capital, max_w, max_v]) + 1e-6).all():
                return [(lot, int(u)) for (lot, _), u in zip(items, x) if u >= 1]
    return _greedy(items, capital, max_w, max_v)


def optimize_cycle(lots1, lots2, capital, home_upfront, mid_fixed, max_w, max_v,
                   mid_convertible=False):
    """Choose both legs of A > B > F together as one integer program.

    Maximise  revenue at F - cost at A   (plus leg 1 profit if B's cash is
                                           worth the same as A's)
    subject to
        origin spend + home_upfront <= capital               (home currency)
        weight and volume limits on each leg separately
        mid spend + mid_fixed <= revenue from selling leg 1  (mid currency)
    home_upfront is fuel and exit fees paid at A; mid_fixed is everything paid
    at B (entry fee, fuel, exit fee). The last line links the legs, and it
    needs no exchange rate."""
    budget = capital - home_upfront
    if not HAVE_SCIPY or budget <= 0:
        return None
    items1 = [(l, c) for l in lots1
              if (c := _natural_cap(l, budget, max_w, max_v)) >= 1]
    items2 = [(l, c) for l in lots2
              if (c := _natural_cap(l, math.inf, max_w, max_v)) >= 1]
    if not items1 or not items2:
        return None
    n1, n2 = len(items1), len(items2)
    cost1 = np.array([l.unit_cost for l, _ in items1])
    rev1 = np.array([l.unit_rev for l, _ in items1])
    cost2 = np.array([l.unit_cost for l, _ in items2])
    rev2 = np.array([l.unit_rev for l, _ in items2])

    a = np.zeros((6, n1 + n2))
    a[0, :n1] = cost1
    a[1, :n1] = [l.weight for l, _ in items1]
    a[2, :n1] = [l.volume for l, _ in items1]
    a[3, n1:] = [l.weight for l, _ in items2]
    a[4, n1:] = [l.volume for l, _ in items2]
    a[5, :n1] = -rev1
    a[5, n1:] = cost2
    ub = np.array([budget, max_w, max_v, max_w, max_v, -mid_fixed])
    caps = np.array([c for _, c in items1] + [c for _, c in items2], dtype=float)

    if mid_convertible:
        objective = np.concatenate([cost1 - rev1, cost2 - rev2])
    else:
        objective = np.concatenate([cost1, -rev2])
    res = milp(objective, integrality=np.ones(n1 + n2),
               bounds=Bounds(np.zeros(n1 + n2), caps),
               constraints=LinearConstraint(a, -np.inf, ub),
               options={"time_limit": SOLVER_SECONDS})
    if res.x is None:
        return None
    x = np.rint(res.x)

    # Rounding can leave the mid-currency budget a hair short, so trim the
    # least efficient leg 2 units until it balances.
    while a[5] @ x > ub[5] + 1e-6:
        held = [j for j in range(n2) if x[n1 + j] >= 1]
        if not held:
            return None
        j = min(held, key=lambda j: rev2[j] / cost2[j])
        x[n1 + j] -= max(1, min(x[n1 + j], math.ceil((a[5] @ x - ub[5]) / cost2[j])))
    if (a[:5] @ x > ub[:5] + 1e-6).any():
        return None
    b1 = [(l, int(u)) for (l, _), u in zip(items1, x[:n1]) if u >= 1]
    b2 = [(l, int(u)) for (l, _), u in zip(items2, x[n1:]) if u >= 1]
    return (b1, b2) if b1 and b2 else None


# ============================================================================
# 4. Route evaluation
# ============================================================================
# Fuel, as the game computes it (community-derived, see README):
#   FTL fuel (FF) = parsecs flown * FF_PER_PARSEC * reactor usage
#   STL fuel (SF) = a roughly fixed amount per flight for departure, approach,
#                   take-off and landing, measured per ship in SFC.
FF_PER_PARSEC = 7.2           # FF per parsec at 100% reactor usage
DEFAULT_REACTOR_USAGE = 0.25  # the cheapest FTL setting in the flight planner
# STL fuel, from the community flight-planner research (prun-toolset). A
# station-to-station flight has two STL phases. The approach burns
# STL_BURN x STL slider x SF loaded. Departure burns the same, unless the gate
# is close enough that the ship needs less (depends on distance and mass, which
# FIO doesn't give us), so STL_DEPARTURE_SHARE = 1 is an upper bound.
STL_BURN = 0.494
DEFAULT_STL_USAGE = 0.5       # the flight planner's default STL fuel setting
STL_DEPARTURE_SHARE = 1.0


@dataclass
class Ship:
    weight: float
    volume: float
    hours_per_distance: float | None = None  # hours per jump
    overhead_hours: float = 0.0
    sf_per_trip: float | None = None   # measured SF per flight (overrides the model)
    reactor_usage: float = DEFAULT_REACTOR_USAGE  # fraction, 0.25 = 25%
    ff_per_parsec: float = FF_PER_PARSEC
    sf_loaded: float | None = None     # SF in the tank at take-off
    stl_usage: float = DEFAULT_STL_USAGE  # STL fuel slider, fraction
    stl_departure_share: float = STL_DEPARTURE_SHARE
    flight_model: FlightModel | MeasuredModel | None = None  # fitted from SFC flights
    ftl_tank: float | None = None  # FF the tank holds; longer legs are skipped
    stl_tank: float | None = None  # SF the tank holds

    def ff_units(self, parsecs):
        if self.flight_model:
            return self.flight_model.ff(parsecs, self.reactor_usage)
        return parsecs * self.ff_per_parsec * self.reactor_usage

    def leg_hours(self, route):
        """Flight time from the fitted model, or None without one."""
        if not self.flight_model or route.parsecs is None:
            return None
        return self.flight_model.hours(route.parsecs, route.jumps, self.stl_usage,
                                       self.reactor_usage, self.sf_loaded or 0)

    @property
    def settings_label(self):
        return f"{100 * self.stl_usage:.0f}/{100 * self.reactor_usage:.0f}"

    def sf_units(self):
        """SF burned on one flight: the measured figure if given, else the
        game model from the SF loaded and the STL slider."""
        if self.sf_per_trip is not None:
            return self.sf_per_trip
        if self.flight_model and self.sf_loaded:
            return self.flight_model.sf(self.stl_usage, self.sf_loaded)
        if not self.sf_loaded:
            return 0.0
        approach = STL_BURN * self.stl_usage * self.sf_loaded
        return approach * (1 + self.stl_departure_share)

    def fuel_fits(self, route, flights=1):
        """Whether the tanks hold the fuel for `flights` flights of route
        bought before leaving. Range comes from this: a ship can only fly as
        far as the fuel it can carry. Unset tank sizes are not checked."""
        if self.stl_tank and self.sf_units() * flights > self.stl_tank:
            return False
        if self.ftl_tank and route.parsecs is not None:
            return self.ff_units(route.parsecs) * flights <= self.ftl_tank
        return True

    @property
    def has_sf(self):
        return self.sf_per_trip is not None or bool(self.sf_loaded)


@dataclass
class Settings:
    capital: float
    round_trip: bool = False
    risk_aversion: float = 1.0
    fee_rate: float = DEFAULT_FEE_RATE
    pick_fuel: bool = True   # try slider settings when the ship has a flight model


REFINE_TOP = 5      # best trips whose slider settings get optimised
RERUN_SETTINGS = 2  # settings per trip re-run in full after screening


@dataclass
class Market:
    """Everything the evaluators read, bundled to keep signatures short."""
    books: BookStore
    materials: dict
    history: History
    borders: BorderModel
    currency: dict
    routes: dict


@dataclass
class RouteResult:
    origin: str
    dest: str
    distance: float
    hours: float
    items: list
    weight_used: float
    volume_used: float
    sales: float          # after exchange fee, import duty and risk haircut
    purchases: float      # including exchange fee and export duty
    fuel: float
    border_fees: float    # flat fees, out and (with --round-trip) back
    duties: float         # import and export duty included in sales/purchases
    crossings: list       # Crossing objects for each leg flown
    net_profit: float
    risk: float
    risk_adj_profit: float
    score: float

    kind = "one-way"

    @property
    def label(self):
        return f"{self.origin}>{self.dest}"

    @property
    def capital_used(self):
        return self.purchases + self.fuel + self.crossings[0].exit_fee

    @property
    def travel_cost(self):
        return self.fuel + self.border_fees


@dataclass
class CycleResult:
    """Buy at origin, sell at mid, spend the proceeds at mid, sell at final.
    Origin and final share a currency, and net_profit is counted in it."""
    origin: str
    mid: str
    final: str
    hours: float
    mid_convertible: bool  # mid uses the home currency
    cost1: float      # spent at origin, home currency
    rev1: float       # received at mid, mid currency
    fuel1: float      # home currency
    cost2: float      # spent at mid, mid currency
    rev2: float       # received at final, home currency
    fuel2: float      # mid currency
    cross1: Crossing  # origin > mid
    cross2: Crossing  # mid > final
    duties1: tuple    # (export duty in home, import duty in mid currency)
    duties2: tuple    # (export duty in mid, import duty in home currency)
    leftover: float   # mid currency cash after leg 2 purchases
    net_profit: float
    risk: float
    risk_adj_profit: float
    score: float
    mid_rate: float   # home currency per unit of mid currency achieved
    leg1: list
    leg2: list
    w1: float
    v1: float
    w2: float
    v2: float

    @property
    def kind(self):
        return "2-leg" if self.mid_convertible else "cycle"

    @property
    def label(self):
        return f"{self.origin}>{self.mid}>{self.final}"

    @property
    def capital_used(self):
        return self.cost1 + self.fuel1 + self.cross1.exit_fee

    @property
    def items(self):
        return self.leg1 + self.leg2

    @property
    def weight_used(self):
        return max(self.w1, self.w2)

    @property
    def volume_used(self):
        return max(self.v1, self.v2)

    @property
    def home_border_fees(self):
        return self.cross1.exit_fee + self.cross2.entry_fee

    @property
    def mid_costs(self):
        """Fuel and border fees paid at the middle exchange, mid currency."""
        return self.cross1.entry_fee + self.fuel2 + self.cross2.exit_fee

    @property
    def travel_cost(self):
        """All fuel and border fees, with mid-currency costs converted at the
        rate this cycle achieved (an estimate, for display only)."""
        return self.fuel1 + self.home_border_fees + self.mid_costs * self.mid_rate

    @property
    def border_fees(self):
        return (self.home_border_fees
                + (self.cross1.entry_fee + self.cross2.exit_fee) * self.mid_rate)


def fuel_units(ship, route, round_trip=False):
    """(SF, FF) units for a flight, or None if the parsec distance is unknown."""
    legs = 2 if round_trip else 1
    sf = ship.sf_units() * legs
    if ship.ff_per_parsec and ship.reactor_usage:
        if route.parsecs is None:
            return None
        ff = ship.ff_units(route.parsecs) * legs
    else:
        ff = 0.0
    return sf, ff


def fuel_cost(ship, books, origin, route, round_trip=False):
    """Fuel bought at the origin by walking its SF and FF ask books. NaN if
    the books are too thin or the distance in parsecs is unknown. SF is left
    out if the ship has no measured sf_per_trip. With round_trip, fuel for
    both directions is bought up front."""
    units = fuel_units(ship, route, round_trip)
    if units is None:
        return math.nan
    total = 0.0
    for ticker, units in (("SF", units[0]), ("FF", units[1])):
        if units <= 0:
            continue
        book = books.get(ticker, origin)
        if not book or not book.asks:
            return math.nan
        filled, value = walk_book(book.asks, units)
        if filled < units * 0.999:
            return math.nan
        total += value
    return total


def price_risk(history, ticker, ex, ref_price, hours, side):
    """Volatility over a time window plus a mean-reversion adjustment.

    side="sell": a price well above its recent mean is assumed to revert, so the
                 sale price is cut by `adj` (a haircut).
    side="buy":  a price well below its mean is assumed to revert upward, so the
                 purchase price is raised by `adj` (a markup)."""
    closes = history.closes(ticker, ex)
    if len(closes) < MIN_HISTORY_POINTS:
        vol = DEFAULT_DAILY_VOL * math.sqrt(hours / 24)
        return dict(vol=vol, adj=0.0, z=float("nan"), thin=True)
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    daily = statistics.pstdev(rets) if len(rets) >= 2 else DEFAULT_DAILY_VOL
    mean, sd = statistics.fmean(closes), statistics.pstdev(closes)
    z = (ref_price - mean) / sd if sd > 0 else 0.0
    gap = (ref_price - mean) if side == "sell" else (mean - ref_price)
    adj = min(MAX_HAIRCUT, REVERSION * max(0.0, gap / ref_price))
    return dict(vol=daily * math.sqrt(hours / 24), adj=adj, z=z, thin=False)


def tradable(books, materials, t, src, dst):
    """Weight, volume and both books if t can be bought at src and sold at dst."""
    w, v = materials.get(t, (None, None))
    a, b = books.get(t, src), books.get(t, dst)
    if w and v and a and b and a.asks and b.bids:
        return w, v, a, b
    return None


def evaluate_route(origin, dest, leg, ship, settings, market):
    """Best basket for one origin to destination trip, or None if unprofitable."""
    books, materials, history = market.books, market.materials, market.history
    route, hours_one_way = leg
    distance = route.jumps
    hours = hours_one_way * (2 if settings.round_trip else 1)
    fee = settings.fee_rate

    out = market.borders.crossing(origin, dest)
    back = market.borders.crossing(dest, origin) if settings.round_trip else None
    crossings = [out] + ([back] if back else [])
    border_fees = sum(c.exit_fee + c.entry_fee for c in crossings)

    if settings.round_trip and not ship.fuel_fits(route, 2):
        log(f"  {origin}>{dest}: skipped, the tanks can't hold fuel for both ways")
        return None
    fuel = fuel_cost(ship, books, origin, route, settings.round_trip)
    if math.isnan(fuel):
        log(f"  {origin}>{dest}: skipped, not enough SF/FF on sale at {origin}")
        return None
    # Fuel and the exit fee are paid before departure, so they reduce what is
    # left to spend on cargo.
    goods_budget = settings.capital - fuel - out.exit_fee
    if goods_budget <= 0:
        log(f"  {origin}>{dest}: skipped, fuel and exit fee use all capital")
        return None

    # Pass 1: items with a positive top-of-book spread after fees and duties.
    base = Pricing(fee=fee, export_duty=out.export_duty, import_duty=out.import_duty)
    cands = []
    for t in books.tickers_at(origin) & books.tickers_at(dest):
        tr = tradable(books, materials, t, origin, dest)
        if not tr:
            continue
        spread = base.unit_rev(tr[3].bids[0][0])[0] / base.unit_cost(tr[2].asks[0][0])[0] - 1
        if spread > 0:
            cands.append((spread, t))
    cands = [t for _, t in sorted(cands, reverse=True)[:MAX_CANDIDATES]]
    if not cands:
        return None

    # Pass 2: risk per candidate, then walk both books into lots.
    history.prefetch((t, dest) for t in cands)
    lots, risk, share = [], {}, {}
    for t in cands:
        w, v = materials[t]
        a, b = books.get(t, origin), books.get(t, dest)
        risk[t] = price_risk(history, t, dest, b.bids[0][0], hours_one_way, "sell")
        share[t] = depth_share(b.bids, market_volume(market, t, dest), hours_one_way)
        pricing = Pricing(fee=fee, haircut=risk[t]["adj"],
                          export_duty=out.export_duty, import_duty=out.import_duty)
        lots += build_lots(t, a.asks, b.bids, w, v, pricing, dest_share=share[t])

    basket = optimize_basket(lots, goods_budget, ship.weight, ship.volume)
    if not basket:
        return None

    # Aggregate the chosen lots per item.
    agg = {}
    for lot, u in basket:
        r = agg.setdefault(lot.ticker, dict(units=0, cost=0.0, rev=0.0, duty=0.0,
                                            raw_buy=0.0, raw_sell=0.0))
        r["units"] += u
        r["cost"] += u * lot.unit_cost
        r["rev"] += u * lot.unit_rev
        r["duty"] += u * (lot.buy_duty + lot.sell_duty)
        r["raw_buy"] += u * lot.buy
        r["raw_sell"] += u * lot.sell

    items = []
    for t, r in agg.items():
        a, b = books.get(t, origin), books.get(t, dest)
        items.append(dict(
            ticker=t, units=int(r["units"]),
            buy_vwap=r["raw_buy"] / r["units"], sell_vwap=r["raw_sell"] / r["units"],
            buy_slip=slippage(a.asks, r["units"]),
            sell_slip=slippage(b.bids, r["units"] / share[t]),
            depth_share=share[t],
            profit=r["rev"] - r["cost"], sales=r["rev"], cost=r["cost"],
            duty=r["duty"],
            weight=r["units"] * materials[t][0], volume=r["units"] * materials[t][1],
            z=risk[t]["z"], vol=risk[t]["vol"], thin=risk[t]["thin"]))
    items.sort(key=lambda i: i["profit"], reverse=True)

    sales = sum(i["sales"] for i in items)
    purchases = sum(i["cost"] for i in items)
    net = sales - purchases - fuel - border_fees
    if net <= 0:
        log(f"  {origin}>{dest}: cargo margin {sales - purchases:,.0f} does not "
            f"cover fuel {fuel:,.0f} + border fees {border_fees:,.0f}")
        return None
    # One standard deviation of sale value at risk, assuming items move
    # independently (which understates risk if they are correlated).
    sigma = math.sqrt(sum((i["sales"] * i["vol"]) ** 2 for i in items))
    adj = net - settings.risk_aversion * sigma
    return RouteResult(origin, dest, distance, hours, items,
                       sum(i["weight"] for i in items),
                       sum(i["volume"] for i in items), sales, purchases, fuel,
                       border_fees, sum(i["duty"] for i in items), crossings,
                       net, sigma, adj, adj / hours)


# ----------------------------------------------------------------------------
# Cycles: A > B > F where F uses A's currency
# ----------------------------------------------------------------------------
def mid_price(book):
    if book and book.asks and book.bids:
        return (book.asks[0][0] + book.bids[0][0]) / 2
    if book and book.asks:
        return book.asks[0][0]
    if book and book.bids:
        return book.bids[0][0]
    return None


def implied_rate(books, src, dst):
    """Typical price level of dst relative to src: the median ratio of mid
    prices over items traded at both. Only used to shortlist items, never to
    value a trade, since the actual profit comes from the goods themselves."""
    ratios = []
    for t in books.tickers_at(src) & books.tickers_at(dst):
        a, b = mid_price(books.get(t, src)), mid_price(books.get(t, dst))
        if a and b:
            ratios.append(b / a)
    return statistics.median(ratios) if len(ratios) >= 3 else None


def cycle_candidates(books, materials, src, dst, rate, k, pricing):
    """Items that look unusually cheap at src relative to dst's price level,
    after fees and duties."""
    out = []
    for t in books.tickers_at(src) & books.tickers_at(dst):
        tr = tradable(books, materials, t, src, dst)
        if not tr:
            continue
        premium = (pricing.unit_rev(tr[3].bids[0][0])[0]
                   / (pricing.unit_cost(tr[2].asks[0][0])[0] * rate)) - 1
        out.append((premium, t))
    return [t for _, t in sorted(out, reverse=True)[:k]]


def _leg_items(basket, materials):
    agg = {}
    for lot, u in basket:
        r = agg.setdefault(lot.ticker, dict(units=0, spent=0.0, received=0.0,
                                            buy_duty=0.0, sell_duty=0.0,
                                            raw_buy=0.0, raw_sell=0.0))
        r["units"] += u
        r["spent"] += u * lot.unit_cost
        r["received"] += u * lot.unit_rev
        r["buy_duty"] += u * lot.buy_duty
        r["sell_duty"] += u * lot.sell_duty
        r["raw_buy"] += u * lot.buy
        r["raw_sell"] += u * lot.sell
    return [dict(ticker=t, units=int(r["units"]), spent=r["spent"],
                 received=r["received"], buy_duty=r["buy_duty"],
                 sell_duty=r["sell_duty"], buy_vwap=r["raw_buy"] / r["units"],
                 sell_vwap=r["raw_sell"] / r["units"],
                 weight=r["units"] * materials[t][0],
                 volume=r["units"] * materials[t][1])
            for t, r in sorted(agg.items(), key=lambda kv: -kv[1]["spent"])]


def evaluate_cycle(a, b, f, leg1, leg2, ship, settings, market):
    """Best A > B > F trip, where A and F share a currency. leg1 and leg2 are
    (distance, hours). Returns a CycleResult or None."""
    books, materials, history = market.books, market.materials, market.history
    (r1, h1), (r2, h2) = leg1, leg2
    fee = settings.fee_rate
    mid_convertible = same_currency(market.currency, a, b)
    rate_ab, rate_bf = implied_rate(books, a, b), implied_rate(books, b, f)
    if not rate_ab or not rate_bf:
        return None
    fuel1 = fuel_cost(ship, books, a, r1)   # bought at a, home currency
    fuel2 = fuel_cost(ship, books, b, r2)   # bought at b, mid currency
    if math.isnan(fuel1) or math.isnan(fuel2):
        log(f"  {a}>{b}>{f}: skipped, not enough SF/FF on sale")
        return None

    x1, x2 = market.borders.crossing(a, b), market.borders.crossing(b, f)
    home_upfront = fuel1 + x1.exit_fee                   # paid at a
    mid_fixed = x1.entry_fee + fuel2 + x2.exit_fee       # paid at b
    home_after = x2.entry_fee                            # paid at f

    p1 = Pricing(fee=fee, export_duty=x1.export_duty, import_duty=x1.import_duty)
    p2 = Pricing(fee=fee, export_duty=x2.export_duty, import_duty=x2.import_duty)
    c1 = cycle_candidates(books, materials, a, b, rate_ab, MAX_CANDIDATES_CYCLE, p1)
    c2 = cycle_candidates(books, materials, b, f, rate_bf, MAX_CANDIDATES_CYCLE, p2)
    if not c1 or not c2:
        return None

    def max_qty(t):
        w, v = materials[t]
        return math.ceil(min(ship.weight / w, ship.volume / v)) + 1

    history.prefetch([(t, b) for t in c1] + [(t, b) for t in c2]
                     + [(t, f) for t in c2])
    lots1, lots2, risk1, risk2b, risk2f = [], [], {}, {}, {}
    for t in c1:  # buy at a now, sell at b after leg 1
        bk_a, bk_b = books.get(t, a), books.get(t, b)
        risk1[t] = price_risk(history, t, b, bk_b.bids[0][0], h1, "sell")
        lots1 += build_lots(t, bk_a.asks, bk_b.bids, *materials[t],
                            pricing=Pricing(fee, haircut=risk1[t]["adj"],
                                            export_duty=x1.export_duty,
                                            import_duty=x1.import_duty),
                            dest_share=depth_share(
                                bk_b.bids, market_volume(market, t, b), h1),
                            require_profit=False, max_qty=max_qty(t))
    for t in c2:  # buy at b after leg 1, sell at f after leg 2
        bk_b, bk_f = books.get(t, b), books.get(t, f)
        risk2b[t] = price_risk(history, t, b, bk_b.asks[0][0], h1, "buy")
        risk2f[t] = price_risk(history, t, f, bk_f.bids[0][0], h1 + h2, "sell")
        lots2 += build_lots(t, bk_b.asks, bk_f.bids, *materials[t],
                            pricing=Pricing(fee, haircut=risk2f[t]["adj"],
                                            markup=risk2b[t]["adj"],
                                            export_duty=x2.export_duty,
                                            import_duty=x2.import_duty),
                            ask_share=depth_share(
                                bk_b.asks, market_volume(market, t, b), h1),
                            dest_share=depth_share(
                                bk_f.bids, market_volume(market, t, f), h1 + h2),
                            require_profit=False,
                            max_qty=max_qty(t))

    sol = optimize_cycle(lots1, lots2, settings.capital, home_upfront, mid_fixed,
                         ship.weight, ship.volume, mid_convertible)
    if not sol:
        return None
    b1, b2 = sol
    cost1 = sum(u * l.unit_cost for l, u in b1)
    rev1 = sum(u * l.unit_rev for l, u in b1)
    cost2 = sum(u * l.unit_cost for l, u in b2)
    rev2 = sum(u * l.unit_rev for l, u in b2)
    leftover = rev1 - mid_fixed - cost2
    net = rev2 - cost1 - home_upfront - home_after
    if mid_convertible:
        net += leftover  # same currency: unspent cash at b is still yours
    if net <= 0:
        return None

    items1, items2 = _leg_items(b1, materials), _leg_items(b2, materials)
    # Express every risk in the home currency using the rate leg 2 achieved,
    # then combine as independent one standard deviation moves.
    r = 1.0 if mid_convertible else (rev2 / cost2 if cost2 else 0.0)
    parts = [i["received"] * r * risk1[i["ticker"]]["vol"] for i in items1]
    parts += [i["spent"] * r * risk2b[i["ticker"]]["vol"] for i in items2]
    parts += [i["received"] * risk2f[i["ticker"]]["vol"] for i in items2]
    sigma = math.sqrt(sum(p * p for p in parts))
    hours = h1 + h2
    adj = net - settings.risk_aversion * sigma
    return CycleResult(
        a, b, f, hours, mid_convertible, cost1, rev1, fuel1, cost2, rev2, fuel2,
        x1, x2,
        (sum(i["buy_duty"] for i in items1), sum(i["sell_duty"] for i in items1)),
        (sum(i["buy_duty"] for i in items2), sum(i["sell_duty"] for i in items2)),
        leftover, net, sigma, adj, adj / hours, r, items1, items2,
        sum(i["weight"] for i in items1), sum(i["volume"] for i in items1),
        sum(i["weight"] for i in items2), sum(i["volume"] for i in items2))


def leg_info(routes, ship, a, b):
    """(Route, hours) for one leg, or None if unknown or beyond the FTL tank."""
    r = routes.get((a, b))
    if r is None:
        return None
    if not ship.fuel_fits(r):
        return None  # more fuel than the tanks hold
    if r.hours is not None:
        hours = r.hours
    elif ship.flight_model:
        hours = ship.leg_hours(r)
        if hours is None:
            return None
    else:
        hours = ship.overhead_hours + ship.hours_per_distance * r.jumps
    return r, hours


# ============================================================================
# 5. Output and main routine
# ============================================================================
def money(x):
    return f"{x:,.0f}"


def exchange_meta(client, books):
    """({exchange: currency}, {exchange: region}) as reported by FIO."""
    cur, region = dict(books.currency), {}
    for r in client.stations():
        code = pick(r, *CODE_KEYS)
        if not code:
            continue
        c = normalise(pick(r, *CURRENCY_KEYS))
        if c:
            cur.setdefault(code, c)
        reg = pick(r, *REGION_KEYS)
        if isinstance(reg, str) and reg.strip():
            region[code] = reg.strip()
    return cur, region


def build_ship(args):
    """Ship from ships.json (optional) with command line overrides on top."""
    cfg = {}
    if args.ship:
        try:
            with open(args.ships_file) as f:
                ships = json.load(f).get("ships", {})
        except FileNotFoundError:
            raise SystemExit(f"--ship given but {args.ships_file} not found.") from None
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{args.ships_file} is not valid JSON: {exc}") from None
        if args.ship not in ships:
            raise SystemExit(f"Ship '{args.ship}' not in {args.ships_file}. "
                             f"Known: {', '.join(ships) or 'none'}")
        cfg = ships[args.ship]
    if "ff_per_distance" in cfg and "reactor_usage" not in cfg:
        print("Note: ff_per_distance in ships.json is no longer used. FTL fuel now "
              "follows the game (parsecs x reactor usage). Re-run "
              "freight.py calibrate to set reactor_usage.")
    cfg = {"weight": cfg.get("cargo_weight"), "volume": cfg.get("cargo_volume"),
           "hours_per_distance": cfg.get("hours_per_distance"),
           "overhead_hours": cfg.get("overhead_hours"),
           "sf_per_trip": cfg.get("sf_per_trip"),
           "reactor_usage": cfg.get("reactor_usage"),
           "ff_per_parsec": cfg.get("ff_per_parsec"),
           "sf_loaded": cfg.get("sf_loaded"),
           "stl_usage": cfg.get("stl_usage"),
           "stl_departure_share": cfg.get("stl_departure_share"),
           "flight_model": cfg.get("flight_model"),
           "stl_tank": cfg.get("stl_tank"), "ftl_tank": cfg.get("ftl_tank")}
    for key, val in (("weight", args.weight), ("volume", args.volume),
                     ("hours_per_distance", args.hours_per_distance),
                     ("overhead_hours", args.overhead_hours),
                     ("sf_per_trip", args.sf_per_trip),
                     ("reactor_usage", args.reactor_usage),
                     ("ff_per_parsec", args.ff_per_parsec),
                     ("sf_loaded", args.sf_loaded),
                     ("stl_usage", args.stl_usage),
                     ("ftl_tank", args.ftl_tank),
                     ("stl_tank", args.stl_tank)):
        if val is not None:
            cfg[key] = val
    if not cfg["weight"] or not cfg["volume"]:
        raise SystemExit("Give the ship's capacity with --weight and --volume "
                         "(or --ship NAME from ships.json).")
    if cfg["stl_tank"] and cfg["sf_loaded"] and cfg["sf_loaded"] > cfg["stl_tank"]:
        raise SystemExit(f"SF loaded ({cfg['sf_loaded']:g}) is more than the STL "
                         f"tank holds ({cfg['stl_tank']:g}).")
    ship = Ship(weight=cfg["weight"], volume=cfg["volume"],
                hours_per_distance=cfg["hours_per_distance"],
                overhead_hours=cfg["overhead_hours"] or 0.0,
                sf_per_trip=cfg["sf_per_trip"],
                reactor_usage=reactor_fraction(cfg["reactor_usage"]),
                ff_per_parsec=(FF_PER_PARSEC if cfg["ff_per_parsec"] is None
                               else cfg["ff_per_parsec"]),
                sf_loaded=cfg["sf_loaded"],
                stl_usage=(DEFAULT_STL_USAGE if cfg["stl_usage"] is None
                           else reactor_fraction(cfg["stl_usage"], "STL usage")),
                stl_departure_share=(STL_DEPARTURE_SHARE
                                     if cfg["stl_departure_share"] is None
                                     else cfg["stl_departure_share"]),
                flight_model=load_flight_model(cfg["flight_model"], cfg["sf_loaded"]),
                ftl_tank=cfg["ftl_tank"], stl_tank=cfg["stl_tank"])
    if ship.flight_model:
        # Start from the nearest settings the model can be trusted at.
        ship.stl_usage, ship.reactor_usage = ship.flight_model.snap(
            ship.stl_usage, ship.reactor_usage)
    return ship


def load_flight_model(data, sf_loaded):
    if not data:
        return None
    if not sf_loaded:
        print("Note: this ship has a flight model but no sf_loaded, so the model "
              "is not used. Set sf_loaded in ships.json or pass --sf-loaded.")
        return None
    try:
        return load_model(data)
    except (TypeError, KeyError) as exc:
        print(f"Note: flight_model in ships.json is not readable ({exc}); "
              "re-run freight.py calibrate.")
        return None


def reactor_fraction(value, name="Reactor usage"):
    """A flight-planner slider as a fraction. Accepts 0.25 or 25 (percent)."""
    if value is None:
        return DEFAULT_REACTOR_USAGE
    value = float(value)
    if value > 1:
        value /= 100
    if not 0 < value <= 1:
        raise SystemExit(f"{name} must be between 0% and 100%, got {value}.")
    return value


def inspect(client):
    """Print one raw record of each endpoint so field names can be checked."""
    full = client.get("/exchange/full", ttl=0)
    rec = full[0]
    print("/exchange/full record keys:", sorted(rec.keys()))
    for side in ("BuyingOrders", "SellingOrders"):
        if rec.get(side):
            print(f"{side}[0]:", rec[side][0])
    t, ex = rec.get("MaterialTicker"), rec.get("ExchangeCode")
    hist = client.get(f"/exchange/cxpc/{t}.{ex}", ttl=0)
    print(f"/exchange/cxpc/{t}.{ex} sample:", hist[0] if hist else "empty")
    stations = client.get("/exchange/station", ttl=0)
    print("/exchange/station record:", stations[0] if stations else "empty")
    if isinstance(stations, list) and len(stations) > 1:
        for k in SYSTEM_KEYS:
            if stations[0].get(k) and stations[1].get(k):
                path = (f"/systemstars/jumproute/{quote(str(stations[0][k]), safe='')}"
                        f"/{quote(str(stations[1][k]), safe='')}")
                try:
                    print(f"{path} ->", client.get(path, ttl=0))
                except (requests.RequestException, ValueError) as exc:
                    print(f"{path} failed: {exc}")


def border_line(c):
    tag = (f"{c.from_region}>{c.to_region}" if c.crosses_border
           else f"within {c.from_region}")
    return f"{c.origin}>{c.dest} [{tag}]: {c.describe()}"


CATEGORIES = {}  # ticker -> FIO category (metals, minerals, consumables (basic)...)


def category(ticker):
    return CATEGORIES.get(ticker, "")


def print_one_way(best):
    print(f"\nBest one-way basket: {best.label}")
    print(f"  sales {money(best.sales)} - purchases {money(best.purchases)} "
          f"- fuel {money(best.fuel)} - border fees {money(best.border_fees)} "
          f"= {money(best.net_profit)} net")
    if best.duties:
        print(f"  (sales and purchases already include {money(best.duties)} "
              "of import/export duty)")
    for c in best.crossings:
        print("  " + border_line(c))
    print(f"{'Item':<6}{'Category':<24}{'Units':>8}{'Buy VWAP':>10}{'Sell VWAP':>11}"
          f"{'BuySlip':>9}{'SellSlip':>9}{'Duty':>9}{'Profit':>10}{'z':>6}{'Vol':>7}")
    for i in best.items:
        z = "n/a" if math.isnan(i["z"]) else f"{i['z']:.1f}"
        flag = "*" if i["thin"] else ""
        print(f"{i['ticker']:<6}{category(i['ticker']):<24}{i['units']:>8}"
              f"{i['buy_vwap']:>10.2f}"
              f"{i['sell_vwap']:>11.2f}{100 * i['buy_slip']:>8.1f}%"
              f"{100 * i['sell_slip']:>8.1f}%{money(i['duty']):>9}"
              f"{money(i['profit']):>10}{z:>6}{100 * i['vol']:>6.1f}%{flag}")
    print("z = destination price vs its 30 day mean in standard deviations. "
          "Vol = expected price move over transit. * = thin history, default "
          "volatility assumed. Sell slippage is measured against the share of "
          "destination bids expected to be left when you arrive (shorter "
          "flights and quieter markets leave more).")


def print_cycle(best, cur):
    home = cur.get(best.origin) or "home"
    away = cur.get(best.mid) or "mid"
    print(f"\nBest {best.kind}: {best.label}")
    print(f"  {home}: {money(best.rev2)} back - {money(best.cost1)} out "
          f"- fuel {money(best.fuel1)} - border fees {money(best.home_border_fees)}"
          + (f" + {money(best.leftover)} unspent at {best.mid}"
             if best.mid_convertible else "")
          + f" = {money(best.net_profit)} {home} net")
    print(f"  Paid at {best.mid} in {away}: fuel {money(best.fuel2)}, border fees "
          f"{money(best.cross1.entry_fee + best.cross2.exit_fee)}")
    if not best.mid_convertible:
        print(f"  Leftover {away} cash that cannot be converted back: "
              f"{money(best.leftover)} (counted as zero profit)")
    for c in (best.cross1, best.cross2):
        print("  " + border_line(c))
    for title, items, cin, cout, duties in (
            (f"Leg 1: buy at {best.origin}, sell at {best.mid}", best.leg1,
             home, away, best.duties1),
            (f"Leg 2: buy at {best.mid}, sell at {best.final}", best.leg2,
             away, home, best.duties2)):
        print(f"\n{title}")
        if any(duties):
            print(f"  duties included: export {money(duties[0])} {cin}, "
                  f"import {money(duties[1])} {cout}")
        print(f"{'Item':<6}{'Category':<24}{'Units':>8}{'Buy VWAP':>10}"
              f"{'Sell VWAP':>11}{'Spent ' + cin:>12}{'Got ' + cout:>12}")
        for i in items:
            print(f"{i['ticker']:<6}{category(i['ticker']):<24}{i['units']:>8}"
                  f"{i['buy_vwap']:>10.2f}"
                  f"{i['sell_vwap']:>11.2f}{money(i['spent']):>12}"
                  f"{money(i['received']):>12}")


def print_results(results, top, ship, cur=None):
    cur = cur or {}
    if not results:
        print("\nNo profitable routes found. Try more capital or a different "
              "--mode, check the tank sizes (legs needing more fuel than the "
              "tanks hold are skipped; -v shows which), and that distances "
              "cover this origin.")
        return
    modelled = bool(ship.flight_model)
    print(f"\n{'#':<3}{'Route':<13}{'Type':<8}"
          + (f"{'STL/FTL%':>9}" if modelled else "")
          + f"{'Hrs':>6}{'Capital':>11}"
          f"{'Wt%':>5}{'Vol%':>6}{'Fuel':>9}{'Border':>9}{'Net profit':>12}"
          f"{'Risk 1sd':>10}{'Score/hr':>10}")
    for n, r in enumerate(results[:top], 1):
        fuel = r.fuel if r.kind == "one-way" else r.fuel1 + r.fuel2 * r.mid_rate
        sl = getattr(r, "ship", ship).settings_label
        print(f"{n:<3}{r.label:<13}{r.kind:<8}"
              + (f"{sl:>9}" if modelled else "")
              + f"{r.hours:>6.1f}"
              f"{money(r.capital_used):>11}{100 * r.weight_used / ship.weight:>5.0f}"
              f"{100 * r.volume_used / ship.volume:>6.0f}{money(fuel):>9}"
              f"{money(r.border_fees):>9}{money(r.net_profit):>12}"
              f"{money(r.risk):>10}{money(r.score):>10}")
    print("All amounts are in the currency of the exchange the ship starts at. "
          "Net profit is after fuel, border fees and duties. For cycles, costs "
          "paid at the middle exchange are converted at the rate the cycle "
          "achieved (display only).")
    if modelled:
        print("STL/FTL% = the STL fuel and FTL reactor sliders to set in SFC for "
              "that trip. Hours and fuel come from your ship's flight model.")

    best_one = next((r for r in results if r.kind == "one-way"), None)
    best_cycle = next((r for r in results if r.kind != "one-way"), None)
    for best in sorted([r for r in (best_one, best_cycle) if r],
                       key=lambda r: -r.score):
        if best.kind == "one-way":
            print_one_way(best)
        else:
            print_cycle(best, cur)
    ff = (f"FTL fuel at {100 * ship.reactor_usage:g}% reactor usage "
          f"({ship.ff_per_parsec:g} FF per parsec at 100%)")
    if ship.sf_per_trip is not None:
        print(f"\nFuel: {ff}, plus {ship.sf_per_trip:g} SF per flight (measured).")
    elif ship.sf_loaded:
        print(f"\nFuel: {ff}, plus {ship.sf_units():.0f} SF per flight "
              f"({ship.sf_loaded:g} SF loaded at {100 * ship.stl_usage:g}% STL "
              "fuel usage).")
    else:
        print(f"\nFuel: {ff}. STL fuel (SF) is not included: pass --sf-loaded "
              "(and --stl-usage), or --sf-per-trip.")


def write_csv(results, path):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["kind", "route", "hours", "net_profit", "risk", "score",
                    "fuel", "border_fees", "leg", "ticker", "category", "units", "buy_vwap",
                    "sell_vwap"])
        for r in results:
            if r.kind == "one-way":
                legs, fuel = [(1, r.items)], r.fuel
            else:
                legs, fuel = [(1, r.leg1), (2, r.leg2)], r.fuel1 + r.fuel2 * r.mid_rate
            for leg, items in legs:
                for i in items:
                    w.writerow([r.kind, r.label, round(r.hours, 2),
                                round(r.net_profit), round(r.risk), round(r.score),
                                round(fuel), round(r.border_fees), leg,
                                i["ticker"], category(i["ticker"]), i["units"],
                                round(i["buy_vwap"], 2), round(i["sell_vwap"], 2)])


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n")[1],
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--at", help="exchange code where the ship is now")
    ap.add_argument("--capital", type=float, default=100000,
                    help="cash available, in the start exchange's currency")
    ship = ap.add_argument_group("ship")
    ship.add_argument("--ship", help="ship name in ships.json")
    ship.add_argument("--ships-file", default="ships.json")
    ship.add_argument("--weight", type=float, help="max cargo weight, tons")
    ship.add_argument("--volume", type=float, help="max cargo volume, m3")
    ship.add_argument("--hours-per-distance", type=float,
                      help="hours per unit of distance (per jump when fetched "
                           "automatically)")
    ship.add_argument("--overhead-hours", type=float,
                      help="fixed hours per leg (undocking, STL legs)")
    ship.add_argument("--sf-loaded", type=float,
                      help="SF in the tank at take-off. STL fuel per flight is "
                           "worked out from this and --stl-usage")
    ship.add_argument("--stl-usage", type=float,
                      help="STL fuel usage slider in SFC: 50 or 0.5 "
                           f"(default {100 * DEFAULT_STL_USAGE:g}%%)")
    ship.add_argument("--stl-tank", type=float,
                      help="SF the STL tank holds; trips needing more are skipped")
    ship.add_argument("--ftl-tank", type=float,
                      help="FF the FTL tank holds; legs needing more are skipped")
    ship.add_argument("--sf-per-trip", type=float,
                      help="measured SF per flight; overrides --sf-loaded")
    ship.add_argument("--reactor-usage", type=float,
                      help="FTL reactor usage you fly at, as in SFC: 25 or 0.25 "
                           f"(default {100 * DEFAULT_REACTOR_USAGE:g}%%)")
    ship.add_argument("--ff-per-parsec", type=float,
                      help=f"FF per parsec at 100%% reactor (default {FF_PER_PARSEC})")
    costs = ap.add_argument_group("costs")
    costs.add_argument("--fee-rate", type=float, default=DEFAULT_FEE_RATE,
                       help="exchange fee per trade, as a fraction")
    costs.add_argument("--borders", default="borders.json",
                       help="border cost config (see borders.example.json)")
    costs.add_argument("--border-fee", type=float,
                       help="flat fee (e.g. a typical gateway fee) for every "
                            "faction-border crossing not set in the borders file")
    costs.add_argument("--border-duty", type=float,
                       help="import duty (fraction of sale value, 0.02 = 2%%) "
                            "for every crossing not set in the borders file")
    ap.add_argument("--distances", default="distances.csv",
                    help="optional override CSV (from,to,distance[,hours]); "
                         "pairs not in it are fetched automatically")
    ap.add_argument("--write-distances", metavar="FILE",
                    help="save the distance table to a CSV and exit")
    ap.add_argument("--mode", choices=["same", "cycle", "both"], default="both",
                    help="same: one-way trips within one currency. cycle: buy A, "
                         "sell B, buy B, sell back in A's currency. both: rank all")
    ap.add_argument("--currency", action="append", metavar="EX=CUR",
                    help="override an exchange's currency, e.g. --currency NC2=NCC")
    ap.add_argument("--round-trip", action="store_true",
                    help="same mode only: count the empty return flight's "
                         "hours, fuel and border fees")
    ap.add_argument("--risk-aversion", type=float, default=1.0,
                    help="standard deviations of risk subtracted from profit")
    ap.add_argument("--no-history", action="store_true",
                    help="skip price history (faster, no risk adjustment)")
    ap.add_argument("--offline", action="store_true",
                    help="use only cached FIO data, never the network")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--fixed-fuel", action="store_true",
                    help="with a flight model, fly at --stl-usage/--reactor-usage "
                         "instead of choosing the best settings per trip")
    ap.add_argument("--csv", help="write all results to this CSV file")
    ap.add_argument("--record", type=int, metavar="N",
                    help="save result #N to the trade log as a trade you are "
                         "taking (see tradelog.py)")
    ap.add_argument("--trade-log", default="trade_log.json",
                    help="trade log file used by --record")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="show progress and why routes were skipped")
    ap.add_argument("--inspect", action="store_true",
                    help="print raw FIO records to check field names")
    args = ap.parse_args(argv)

    if args.record is not None and args.record < 1:
        ap.error("--record takes a result number from the table, starting at 1")
    if args.capital <= 0:
        ap.error("--capital must be positive")
    if not 0 <= args.fee_rate < 0.5:
        ap.error("--fee-rate is a fraction, e.g. 0.01 for 1%")
    overrides = {}
    for item in args.currency or []:
        ex, sep, c = item.partition("=")
        if not sep or not ex.strip() or not c.strip():
            ap.error(f"--currency expects EX=CUR, got '{item}'")
        overrides[ex.strip().upper()] = c.strip().upper()
    args.currency = overrides
    if args.at:
        args.at = args.at.upper()
    return args


def scan_one_way(home, ship, settings, market):
    results = []
    dests = [d for d in sorted(market.books.exchanges)
             if d != home and same_currency(market.currency, home, d)]
    for n, dest in enumerate(dests, 1):
        leg = leg_info(market.routes, ship, home, dest)
        if not leg:
            log(f"  {home}>{dest}: no distance, or needs more FF than the tank holds")
            continue
        progress(f"Evaluating {home} > {dest} ({n}/{len(dests)})")
        res = evaluate_route(home, dest, leg, ship, settings, market)
        if res:
            res.ship = ship
            results.append(res)
    return results


def scan_cycles(home, ship, settings, market):
    exchanges = sorted(market.books.exchanges)
    finals = [e for e in exchanges if same_currency(market.currency, home, e)]
    plan = []
    for mid in exchanges:
        leg1 = leg_info(market.routes, ship, home, mid) if mid != home else None
        if not leg1:
            continue
        for fin in finals:
            leg2 = leg_info(market.routes, ship, mid, fin) if fin != mid else None
            if leg2:
                plan.append((mid, fin, leg1, leg2))
    results = []
    for n, (mid, fin, leg1, leg2) in enumerate(plan, 1):
        progress(f"Evaluating {home} > {mid} > {fin} ({n}/{len(plan)})")
        res = evaluate_cycle(home, mid, fin, leg1, leg2, ship, settings, market)
        if res:
            res.ship = ship
            results.append(res)
    return results


def reevaluate(res, ship, settings, market):
    """The same trip as res, flown by `ship` (other slider settings)."""
    if res.kind == "one-way":
        leg = leg_info(market.routes, ship, res.origin, res.dest)
        out = leg and evaluate_route(res.origin, res.dest, leg, ship, settings, market)
    else:
        leg1 = leg_info(market.routes, ship, res.origin, res.mid)
        leg2 = leg_info(market.routes, ship, res.mid, res.final)
        out = (leg1 and leg2 and
               evaluate_cycle(res.origin, res.mid, res.final, leg1, leg2, ship,
                              settings, market))
    if out:
        out.ship = ship
    return out


def screen_setting(res, ship, settings, market):
    """Quick estimate of res's score if flown by `ship`, keeping the cargo:
    only fuel, hours and price risk (which grows with the square root of
    time) change. Used to pick which settings get a full re-run."""
    legs = ([(res.origin, res.dest)] if res.kind == "one-way"
            else [(res.origin, res.mid), (res.mid, res.final)])
    hours, fuel = 0.0, []
    for a, b in legs:
        leg = leg_info(market.routes, ship, a, b)
        if not leg:
            return None
        hours += leg[1]
        both_ways = settings.round_trip and res.kind == "one-way"
        if both_ways and not ship.fuel_fits(leg[0], 2):
            return None
        fuel.append(fuel_cost(ship, market.books, a, leg[0], both_ways))
    if any(math.isnan(f) for f in fuel):
        return None
    if res.kind == "one-way":
        hours *= 2 if settings.round_trip else 1
        old_fuel, new_fuel = res.fuel, fuel[0]
    else:
        old_fuel = res.fuel1 + res.fuel2 * res.mid_rate
        new_fuel = fuel[0] + fuel[1] * res.mid_rate
    net = res.net_profit + old_fuel - new_fuel
    risk = res.risk * math.sqrt(hours / res.hours) if res.hours > 0 else res.risk
    return (net - settings.risk_aversion * risk) / hours


def pick_fuel_settings(results, ship, settings, market, top=REFINE_TOP,
                       rerun=RERUN_SETTINGS):
    """Choose the STL and reactor sliders for the best trips.

    Every setting the ship's flight model covers is screened with the cargo
    held fixed, then the most promising ones are re-run in full (new cargo,
    depth and risk for the new flight time). Faster settings cost more fuel but
    cut price risk, depth lost on arrival and hours; slower ones are cheaper.
    The first scan is done at the current settings, so a trip that only pays
    at another setting can be missed."""
    if not (ship.flight_model and settings.pick_fuel) or not results:
        return results
    variants = [replace(ship, stl_usage=s, reactor_usage=r)
                for s, r in ship.flight_model.grid()]
    results = sorted(results, key=lambda r: r.score, reverse=True)
    head, tail = results[:top], results[top:]
    better = []
    for n, res in enumerate(head, 1):
        progress(f"Choosing fuel settings for {res.label} ({n}/{len(head)})")
        base = res.ship.settings_label
        scored = [(sc, v) for v in variants if v.settings_label != base
                  for sc in [screen_setting(res, v, settings, market)] if sc is not None]
        scored.sort(key=lambda x: -x[0])
        tries = [res] + [reevaluate(res, v, settings, market) for _, v in scored[:rerun]]
        better.append(max((t for t in tries if t), key=lambda t: t.score))
    return better + tail


def main(argv=None):
    global VERBOSE
    args = parse_args(argv)
    VERBOSE = args.verbose
    client = FioClient(offline=args.offline)
    if args.inspect:
        return inspect(client)

    try:
        books = BookStore(client)
    except (requests.RequestException, ValueError) as exc:
        raise SystemExit(f"Could not load exchange data from FIO: {exc}") from None
    exchanges = books.exchanges
    routes, failed, n_override = build_routes(client, exchanges, args.distances)
    if not routes:
        raise SystemExit("Could not fetch distances from FIO. Run --inspect to "
                         "see the raw station data, or supply distances.csv.")
    print(f"Distances: {len(routes) // 2} exchange pairs from FIO jump routes"
          + (f" ({n_override} overridden by {args.distances})" if n_override else "")
          + ".")
    if failed:
        print("Could not resolve:", ", ".join(f"{a}-{b}" for a, b in failed),
              "(add them to distances.csv to include these routes)")
    if args.write_distances:
        with open(args.write_distances, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["from", "to", "distance", "parsecs"])
            for (a, b), r in sorted(routes.items()):
                if a < b:
                    w.writerow([a, b, r.jumps,
                                "" if r.parsecs is None else round(r.parsecs, 3)])
        return print(f"Wrote {args.write_distances}")

    if not args.at:
        raise SystemExit("Give the ship's current exchange with --at (e.g. NC1).")
    if args.at not in exchanges:
        raise SystemExit(f"Unknown exchange {args.at}. Known: {', '.join(sorted(exchanges))}")
    home = args.at

    ship = build_ship(args)
    needs_hours = any(r.hours is None for r in routes.values())
    if needs_hours and ship.hours_per_distance is None and not ship.flight_model:
        raise SystemExit("No travel time for some routes. Set --hours-per-distance "
                         "(or hours_per_distance in ships.json), or add an hours "
                         "column to distances.csv.")

    fio_cur, fio_region = exchange_meta(client, books)
    cur, cur_src = resolve_currencies(exchanges, fio_cur, args.currency)
    print("Currencies: " + ", ".join(
        f"{e}={cur[e] or '?'} ({cur_src[e]})" for e in sorted(exchanges)))
    unknown = [e for e in exchanges if cur[e] is None]
    if unknown:
        print(f"Unknown currency for {', '.join(sorted(unknown))}: they will only "
              "be used as the middle stop of a cycle. Fix with --currency EX=CUR.")

    borders = BorderModel.load(args.borders, exchanges, fio_region,
                               args.border_fee, args.border_duty)
    print("Regions: " + ", ".join(f"{e}={borders.region(e)}" for e in sorted(exchanges)))
    if borders.configured:
        src = borders.source or "command line"
        lines = borders.describe_from(home, exchanges)
        print(f"Gateway/border fees ({src}):" if lines
              else f"Gateway/border fees ({src}): none apply to trips from {home}.")
        for line in lines:
            print(line)
    else:
        print(f"Gateway/border fees: none configured ({args.borders} not found). "
              "Copy borders.example.json to borders.json and enter the fees SFC "
              "shows, or use --border-fee.")

    settings = Settings(args.capital, args.round_trip, args.risk_aversion,
                        args.fee_rate, pick_fuel=not args.fixed_fuel)
    if ship.flight_model:
        fm = ship.flight_model
        print(f"Flight model: fitted from {fm.flights} SFC flights (typical error "
              f"{60 * fm.rmse_hours:.0f} min). "
              + (f"Choosing STL/reactor settings per trip {fm.describe()}."
                 if settings.pick_fuel else
                 f"Fixed at {ship.settings_label} STL/reactor (--fixed-fuel)."))
    CATEGORIES.update(client.categories())
    market = Market(books, client.materials(),
                    History(client, enabled=not args.no_history), borders, cur,
                    routes)
    print(f"Capital {money(args.capital)} {cur[home] or ''} at {home}; "
          f"exchange fee {100 * args.fee_rate:g}%; mode: {args.mode}.")

    results = []
    if args.mode in ("same", "both"):
        if not HAVE_SCIPY:
            print("scipy not installed: using the greedy packer, which can be "
                  "slightly below optimal. pip install scipy for the exact solver.")
        results += scan_one_way(home, ship, settings, market)
    if args.mode in ("cycle", "both"):
        if not HAVE_SCIPY:
            print("Cycle mode needs scipy (pip install scipy). Skipping cycles.")
        else:
            results += scan_cycles(home, ship, settings, market)
    results = pick_fuel_settings(results, ship, settings, market)
    progress_done()

    results.sort(key=lambda r: r.score, reverse=True)
    print_results(results, args.top, ship, cur)
    if args.csv and results:
        write_csv(results, args.csv)
        print(f"\nWrote {args.csv}")
    if args.record is not None:
        shown = results[:args.top]
        if args.record > len(shown):
            raise SystemExit(f"--record {args.record}: only {len(shown)} results "
                             "were listed, so nothing was recorded.")
        import tradelog
        t = tradelog.record_result(shown[args.record - 1], home=home,
                                   currency=cur[home], capital=args.capital,
                                   ship=args.ship, settings=settings,
                                   path=args.trade_log)
        print(f"\nRecorded {t['route']} as trade #{t['id']} in {args.trade_log} "
              f"(predicted {money(t['predicted']['net_profit'])}). When it is "
              f"done: python tradelog.py close {t['id']} --profit <actual>")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        progress_done()
        sys.exit("Interrupted.")
