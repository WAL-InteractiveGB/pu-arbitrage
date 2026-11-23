"""
Gateway and border fees for pu-arbitrage.

In Prosperous Universe the money you pay to travel, beyond fuel, is mostly
gateway fees. Gateways are built and run by planetary governments, and a jump
through one charges a fee (paid while the ship waits to jump) plus Vortex Fuel
supplied by the operator. The fees are set in game, change over time, and are
not published by FIO or the handbook, so this tool cannot look them up. Read the
cost the flight planner (SFC) shows for a route and put it in borders.json.

The simplest setup is one flat fee per route you fly:

    {
      "routes": {
        "CI1>NC1": {"entry_fee": 4500},
        "NC1>CI1": {"entry_fee": 4500}
      }
    }

Routes are one direction each, so list both directions if you fly both ways.

More general rules are also supported, for when a fee applies to many routes:

Regions
    Every exchange belongs to a region, which is its faction's country code
    (AI, CI, IC, NC). It is resolved in this order:
      1. "exchange_regions" in borders.json
      2. CountryCode from FIO's /exchange/station data
      3. the exchange code's letters (NC1 -> NC)
    A trip "crosses a border" when the origin and destination regions differ.

Fields, all optional and zero by default
    entry_fee    flat fee for the trip, paid on arrival in the destination
                 exchange's currency. Use this for gateway fees.
    exit_fee     flat fee paid at the origin before departure, in the origin
                 exchange's currency, so it comes out of your trading capital.
    import_duty  share of the cargo's sale value (0.02 = 2%). The base game
    export_duty  has no customs duties as far as I can find, so leave these at
                 zero unless you want to model something specific.

Sections of borders.json
    "default"           fields applied to every border crossing
    "regions"           fields per region: exit_fee/export_duty when leaving
                        it, entry_fee/import_duty when entering it
    "routes"            per-direction overrides by exchange pair ("CI1>NC1")
                        or region pair ("CI>NC"). The exchange pair wins. Route
                        entries also apply within one region.
    "exchange_regions"  fix an exchange's region if FIO's is wrong
    Keys starting with "_" are ignored, so you can leave notes in the file.

Flat fees are charged even with an empty hold, for example on the return leg
of --round-trip.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, replace

FEE_FIELDS = ("exit_fee", "entry_fee")
DUTY_FIELDS = ("export_duty", "import_duty")
ALL_FIELDS = FEE_FIELDS + DUTY_FIELDS
REGION_KEYS = ("CountryCode", "FactionCode", "Faction")


@dataclass(frozen=True)
class Crossing:
    """The cost of one directed trip from origin to dest."""
    origin: str
    dest: str
    from_region: str
    to_region: str
    exit_fee: float = 0.0
    entry_fee: float = 0.0
    export_duty: float = 0.0
    import_duty: float = 0.0

    @property
    def crosses_border(self):
        return self.from_region != self.to_region

    @property
    def is_free(self):
        return not any(getattr(self, f) for f in ALL_FIELDS)

    def describe(self):
        if self.is_free:
            return "no border costs"
        parts = []
        if self.exit_fee:
            parts.append(f"exit fee {self.exit_fee:,.0f}")
        if self.entry_fee:
            parts.append(f"entry fee {self.entry_fee:,.0f}")
        if self.export_duty:
            parts.append(f"export duty {100 * self.export_duty:g}%")
        if self.import_duty:
            parts.append(f"import duty {100 * self.import_duty:g}%")
        return ", ".join(parts)


def _clean(d):
    return {k: v for k, v in (d or {}).items() if not str(k).startswith("_")}


def _check_rules(where, rules):
    if not isinstance(rules, dict):
        raise SystemExit(f"borders config: {where} must be an object.")
    for key, val in rules.items():
        if key not in ALL_FIELDS:
            raise SystemExit(f"borders config: unknown field '{key}' in {where}. "
                             f"Use one of: {', '.join(ALL_FIELDS)}.")
        if not isinstance(val, (int, float)) or isinstance(val, bool) or val < 0:
            raise SystemExit(f"borders config: {where}.{key} must be a number >= 0.")
        if key in DUTY_FIELDS and val >= 1:
            raise SystemExit(f"borders config: {where}.{key} is a fraction "
                             f"(0.02 = 2%), got {val}.")
    return {k: float(v) for k, v in rules.items()}


def region_from_code(exchange):
    """Fallback region: the letters of the exchange code (NC1 -> NC)."""
    m = re.match(r"[A-Za-z]+", exchange or "")
    return m.group(0).upper() if m else exchange


class BorderModel:
    def __init__(self, exchanges, fio_regions=None, config=None,
                 entry_fee=None, import_duty=None, source=None):
        config = _clean(config)
        unknown = set(config) - {"default", "regions", "routes", "exchange_regions"}
        if unknown:
            raise SystemExit(f"borders config: unknown section(s) {sorted(unknown)}.")
        self.source = source
        self.default = _check_rules("default", _clean(config.get("default")))
        # Command line shortcuts set the default for any crossing the file
        # does not cover more specifically.
        if entry_fee is not None:
            self.default["entry_fee"] = _check_rules("--border-fee",
                                                     {"entry_fee": entry_fee})["entry_fee"]
        if import_duty is not None:
            self.default["import_duty"] = _check_rules(
                "--border-duty", {"import_duty": import_duty})["import_duty"]
        self.regions_rules = {r.upper(): _check_rules(f"regions.{r}", v)
                              for r, v in _clean(config.get("regions")).items()}
        self.routes = {}
        for key, v in _clean(config.get("routes")).items():
            if ">" not in key:
                raise SystemExit(f"borders config: route key '{key}' must look "
                                 "like 'CI1>NC1' or 'CI>NC'.")
            a, b = (s.strip().upper() for s in key.split(">", 1))
            self.routes[(a, b)] = _check_rules(f"routes.{key}", v)

        fio_regions = fio_regions or {}
        overrides = {k.upper(): str(v).upper()
                     for k, v in _clean(config.get("exchange_regions")).items()}
        self.region_of, self.region_src = {}, {}
        for ex in sorted(exchanges):
            if ex in overrides:
                self.region_of[ex], self.region_src[ex] = overrides[ex], "override"
            elif fio_regions.get(ex):
                self.region_of[ex], self.region_src[ex] = fio_regions[ex].upper(), "FIO"
            else:
                self.region_of[ex], self.region_src[ex] = region_from_code(ex), "code"
        self._cache = {}

    @classmethod
    def load(cls, path, exchanges, fio_regions=None, entry_fee=None,
             import_duty=None):
        config, source = None, None
        if path and os.path.exists(path):
            try:
                with open(path) as f:
                    config = json.load(f)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path} is not valid JSON: {exc}") from None
            source = path
        return cls(exchanges, fio_regions, config, entry_fee, import_duty, source)

    @property
    def configured(self):
        return bool(self.source or any(self.default.values()))

    def region(self, ex):
        return self.region_of.get(ex) or region_from_code(ex)

    def _rule(self, region, key):
        rules = self.regions_rules.get(region, {})
        return rules.get(key, self.default.get(key, 0.0))

    def crossing(self, a, b):
        key = (a, b)
        if key in self._cache:
            return self._cache[key]
        ra, rb = self.region(a), self.region(b)
        c = Crossing(a, b, ra, rb)
        if ra != rb:
            c = replace(c, exit_fee=self._rule(ra, "exit_fee"),
                        export_duty=self._rule(ra, "export_duty"),
                        entry_fee=self._rule(rb, "entry_fee"),
                        import_duty=self._rule(rb, "import_duty"))
        # Route overrides: region pair first, then the more specific exchange pair.
        for override in (self.routes.get((ra, rb)), self.routes.get((a, b))):
            if override:
                c = replace(c, **override)
        self._cache[key] = c
        return c

    def describe_from(self, origin, exchanges):
        """Lines describing every non-free crossing out of origin and back."""
        lines = []
        for ex in sorted(exchanges):
            if ex == origin:
                continue
            for a, b in ((origin, ex), (ex, origin)):
                c = self.crossing(a, b)
                if not c.is_free:
                    lines.append(f"  {a}>{b} ({c.from_region}>{c.to_region}): "
                                 f"{c.describe()}")
        return lines
