"""
Per-ship flight model, fitted from flights planned in the game's flight
planner (SFC). Planning a flight in SFC is free, so a player can plan the same
trip at a few slider settings and copy the time and fuel it shows.

The model has the shape the community's reverse-engineering points to, with
the constants fitted to the player's own ship instead of assumed:

    hours = overhead                                   docking, fixed legs
          + stl / (STL slider x SF loaded / 1000)      STL is faster with more fuel
          + parsecs x jump x e^(beta x (1 - reactor))  jumps may slow at low reactor
          + charge x (jumps - 1) x reactor             charging between jumps
    SF    = sf_coef x STL slider x SF loaded           (0.494-0.988 per the research)
    FF    = ff_per_parsec x parsecs x reactor          (7.2 for a 2,400 reactor)

Sliders are fractions (0.5 = 50%). A model is only trusted between the lowest
and highest slider values the player actually planned at, so the optimizer
never extrapolates outside the measured range.

If a ship's times don't follow that shape, the formula fits badly. The
fallback, MeasuredModel, assumes no formula at all: for every slider setting
planned on two or more routes it draws a straight line of time against
parsecs, and the optimizer then chooses only among those measured settings.
fit_auto() uses the formula when it fits to within AUTO_MAX_ERROR and the
measured lines otherwise.

docs/js/flightmodel.js is a line-for-line port. Keep them in step.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, fields, replace

GRID = (0.25, 0.5, 0.75, 1.0)  # slider values the optimizer tries
BETA_DEFAULT = 1.123   # community value for the standard 2,400 reactor
BETA_MAX = 3.0
BETA_STEP = 0.02
MIN_FLIGHTS = 4
VARIED = 0.05          # a slider counts as varied if its values span this much
AUTO_MAX_ERROR = 0.25  # hours; a worse formula fit switches to measured settings
MIN_HOURS = 0.5        # measured lines are never trusted below this


@dataclass
class FlightModel:
    overhead: float          # hours
    stl: float               # hours x (slider x SF loaded / 1000)
    jump: float              # hours per parsec at 100% reactor
    beta: float
    charge: float            # hours per extra jump at 100% reactor
    ff_per_parsec: float
    sf_coef: float
    stl_range: tuple         # (lowest, highest) STL slider planned at
    reactor_range: tuple
    flights: int
    rmse_hours: float
    notes: tuple = ()

    kind = "formula"

    def snap(self, stl_usage, reactor):
        """The nearest settings this model can be trusted at."""
        clamp = lambda v, lo, hi: min(hi, max(lo, v))
        return (clamp(stl_usage, *self.stl_range), clamp(reactor, *self.reactor_range))

    def describe(self):
        return (f"between {100 * self.stl_range[0]:g}-{100 * self.stl_range[1]:g}% "
                f"STL and {100 * self.reactor_range[0]:g}-"
                f"{100 * self.reactor_range[1]:g}% reactor")

    def hours(self, parsecs, jumps, stl_usage, reactor, sf_loaded):
        burn = stl_usage * sf_loaded / 1000
        return (self.overhead
                + (self.stl / burn if burn > 0 else 0.0)
                + parsecs * self.jump * math.exp(self.beta * (1 - reactor))
                + self.charge * max(0, jumps - 1) * reactor)

    def sf(self, stl_usage, sf_loaded):
        return self.sf_coef * stl_usage * sf_loaded

    def ff(self, parsecs, reactor):
        return self.ff_per_parsec * parsecs * reactor

    def grid(self):
        """(STL, reactor) pairs to try, inside the measured range only."""
        def values(lo, hi):
            vals = {round(v, 4) for v in GRID if lo - 1e-9 <= v <= hi + 1e-9}
            vals |= {round(lo, 4), round(hi, 4)}
            return sorted(vals)
        return [(s, r) for s in values(*self.stl_range)
                for r in values(*self.reactor_range)]

    def to_dict(self):
        d = asdict(self)
        d["stl_range"], d["reactor_range"] = list(self.stl_range), list(self.reactor_range)
        d["notes"] = list(self.notes)
        d["kind"] = self.kind
        return d

    @classmethod
    def from_dict(cls, d):
        d = dict(d)
        d["stl_range"] = tuple(d["stl_range"])
        d["reactor_range"] = tuple(d["reactor_range"])
        d["notes"] = tuple(d.get("notes", ()))
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})  # web adds extras


@dataclass
class MeasuredModel:
    """No formula: one straight line of time against parsecs per slider
    setting the player planned on two or more routes. Each entry of settings
    is a dict with stl, reactor, base (hours), per_pc (hours per parsec),
    sf_share (SF burned / SF loaded) and ff_per_pc."""
    settings: list
    flights: int
    rmse_hours: float
    notes: tuple = ()
    kind = "measured"

    def _nearest(self, stl_usage, reactor):
        return min(self.settings, key=lambda x: ((x["stl"] - stl_usage) ** 2
                                                 + (x["reactor"] - reactor) ** 2,
                                                 x["stl"], x["reactor"]))

    def snap(self, stl_usage, reactor):
        x = self._nearest(stl_usage, reactor)
        return x["stl"], x["reactor"]

    @property
    def stl_range(self):
        v = [x["stl"] for x in self.settings]
        return min(v), max(v)

    @property
    def reactor_range(self):
        v = [x["reactor"] for x in self.settings]
        return min(v), max(v)

    def describe(self):
        return "among your measured settings (" + ", ".join(
            f"{100 * x['stl']:g}/{100 * x['reactor']:g}" for x in self.settings) + ")"

    def hours(self, parsecs, jumps, stl_usage, reactor, sf_loaded):
        x = self._nearest(stl_usage, reactor)
        return max(MIN_HOURS, x["base"] + x["per_pc"] * parsecs)

    def sf(self, stl_usage, sf_loaded):
        # SF burned scales with SF loaded (see the formula model); the reactor
        # setting doesn't change it, so any setting with this STL value will do.
        stl = min((y["stl"] for y in self.settings), key=lambda v: (abs(v - stl_usage), v))
        same = [y for y in self.settings if y["stl"] == stl]
        return sum(y["sf_share"] for y in same) / len(same) * sf_loaded

    def ff(self, parsecs, reactor):
        re_ = min((y["reactor"] for y in self.settings), key=lambda v: (abs(v - reactor), v))
        same = [y for y in self.settings if y["reactor"] == re_]
        return sum(y["ff_per_pc"] for y in same) / len(same) * parsecs

    def grid(self):
        return [(x["stl"], x["reactor"]) for x in self.settings]

    def to_dict(self):
        return {"kind": self.kind, "settings": [dict(x) for x in self.settings],
                "flights": self.flights, "rmse_hours": self.rmse_hours,
                "notes": list(self.notes)}

    @classmethod
    def from_dict(cls, d):
        return cls(settings=[dict(x) for x in d["settings"]], flights=d["flights"],
                   rmse_hours=d["rmse_hours"], notes=tuple(d.get("notes", ())))


def load_model(d):
    """A saved model of either kind (older saves have no kind: formula)."""
    if not d:
        return None
    return (MeasuredModel if d.get("kind") == "measured" else FlightModel).from_dict(d)


def nnls(cols, y, iters=3000):
    """Least squares with every coefficient >= 0, by coordinate descent.
    cols is a list of columns (lists). Small and dependency free so the web
    port gives the same answer."""
    k = len(cols)
    g = [[sum(a * b for a, b in zip(cols[i], cols[j])) for j in range(k)]
         for i in range(k)]
    h = [sum(a * b for a, b in zip(cols[i], y)) for i in range(k)]
    w = [0.0] * k
    for _ in range(iters):
        moved = 0.0
        for j in range(k):
            if g[j][j] <= 0:
                continue
            grad = sum(g[j][i] * w[i] for i in range(k)) - h[j]
            new = max(0.0, w[j] - grad / g[j][j])
            moved = max(moved, abs(new - w[j]))
            w[j] = new
        if moved < 1e-12:
            break
    return w


def _through_origin(xs, ys):
    sxx = sum(x * x for x in xs)
    return sum(x * y for x, y in zip(xs, ys)) / sxx if sxx > 0 else None


def fit(flights):
    """Fit a FlightModel. Each flight is a dict with parsecs, jumps,
    stl_usage, reactor (fractions), sf_loaded, hours, and optionally sf and
    ff. Raises ValueError with a readable reason if the data can't support a
    fit."""
    rows = _complete(flights)
    if len(rows) < MIN_FLIGHTS:
        raise ValueError(f"Need at least {MIN_FLIGHTS} complete flights "
                         f"(time, both sliders and SF loaded); have {len(rows)}.")
    stl_vals = [f["stl_usage"] for f in rows]
    re_vals = [f["reactor"] for f in rows]
    notes = []
    if max(stl_vals) - min(stl_vals) < VARIED:
        notes.append("STL slider was not varied, so the tool will keep it at "
                     f"{100 * stl_vals[0]:g}%.")
    re_varied = max(re_vals) - min(re_vals) >= VARIED
    if not re_varied:
        notes.append("Reactor slider was not varied, so the tool will keep it at "
                     f"{100 * re_vals[0]:g}%.")
    if len({round(f["parsecs"], 1) for f in rows}) < 2:
        notes.append("All flights are the same route, so times for other "
                     "routes are a guess. Add a route of a different length.")

    y = [f["hours"] for f in rows]
    burn = [f["stl_usage"] * f["sf_loaded"] / 1000 for f in rows]
    best = None
    betas = ([round(i * BETA_STEP, 4) for i in range(int(BETA_MAX / BETA_STEP) + 1)]
             if re_varied else [BETA_DEFAULT])
    for beta in betas:
        cols = [[1.0] * len(rows),
                [1 / b for b in burn],
                [f["parsecs"] * math.exp(beta * (1 - f["reactor"])) for f in rows],
                [max(0, f["jumps"] - 1) * f["reactor"] for f in rows]]
        w = nnls(cols, y)
        sse = sum((sum(w[j] * cols[j][i] for j in range(4)) - y[i]) ** 2
                  for i in range(len(rows)))
        if best is None or sse < best[0] - 1e-12:
            best = (sse, beta, w)
    sse, beta, w = best

    ff_rows = [f for f in rows if f.get("ff") is not None]
    ff_pc = _through_origin([f["parsecs"] * f["reactor"] for f in ff_rows],
                            [f["ff"] for f in ff_rows])
    sf_rows = [f for f in rows if f.get("sf") is not None]
    sf_coef = _through_origin([f["stl_usage"] * f["sf_loaded"] for f in sf_rows],
                              [f["sf"] for f in sf_rows])
    if ff_pc is None:
        ff_pc = 7.2
        notes.append("No FF figures, so the game constant 7.2 FF per parsec is used.")
    if sf_coef is None:
        sf_coef = 0.494 * 2
        notes.append("No SF figures, so the community STL estimate is used.")

    return FlightModel(
        overhead=w[0], stl=w[1], jump=w[2], beta=beta, charge=w[3],
        ff_per_parsec=ff_pc, sf_coef=sf_coef,
        stl_range=(min(stl_vals), max(stl_vals)),
        reactor_range=(min(re_vals), max(re_vals)),
        flights=len(rows), rmse_hours=math.sqrt(sse / len(rows)),
        notes=tuple(notes))


def _complete(flights):
    return [f for f in flights if f.get("hours") and f.get("parsecs")
            and f.get("stl_usage") and f.get("reactor") and f.get("sf_loaded")]


def fit_measured(flights):
    """Straight lines of time against parsecs, one per slider setting that was
    planned on at least two routes of different length."""
    rows = _complete(flights)
    groups = {}
    for f in rows:
        groups.setdefault((round(f["stl_usage"], 4), round(f["reactor"], 4)), []).append(f)
    settings, used, skipped = [], [], []
    for (stl, re_), fs in sorted(groups.items()):
        pcs = [f["parsecs"] for f in fs]
        if max(pcs) - min(pcs) < 1:
            skipped.append(f"{100 * stl:g}/{100 * re_:g}")
            continue
        n = len(fs)
        mx, my = sum(pcs) / n, sum(f["hours"] for f in fs) / n
        sxx = sum((x - mx) ** 2 for x in pcs)
        per_pc = sum((x - mx) * (f["hours"] - my) for x, f in zip(pcs, fs)) / sxx
        base = my - per_pc * mx
        sf_fs = [f for f in fs if f.get("sf") is not None]
        sf_share = (sum(f["sf"] / f["sf_loaded"] for f in sf_fs) / len(sf_fs)
                    if sf_fs else 0.494 * 2 * stl)
        ff_fs = [f for f in fs if f.get("ff") is not None]
        ff_pc = (_through_origin([f["parsecs"] for f in ff_fs], [f["ff"] for f in ff_fs])
                 if ff_fs else 7.2 * re_)
        settings.append(dict(stl=stl, reactor=re_, base=base, per_pc=per_pc,
                             sf_share=sf_share, ff_per_pc=ff_pc,
                             pc_min=min(pcs), pc_max=max(pcs)))
        used += [(f, base + per_pc * f["parsecs"]) for f in fs]
    if not settings:
        raise ValueError("Measured settings need each slider setting planned on "
                         "at least two routes of different length; none were.")
    lo = max(x["pc_min"] for x in settings)
    hi = min(x["pc_max"] for x in settings)
    notes = [f"Times are measured between {lo:.0f} and {hi:.0f} parsecs; shorter or "
             "longer routes are estimated by extending the lines. Plan a short and "
             "a long route to cover more."]
    if skipped:
        notes.append("Planned on one route only, so not used: "
                     + ", ".join(skipped) + " (STL/reactor %).")
    sse = sum((pred - f["hours"]) ** 2 for f, pred in used)
    return MeasuredModel(settings=settings, flights=len(used),
                         rmse_hours=math.sqrt(sse / len(used)), notes=tuple(notes))


def fit_auto(flights, mode="auto"):
    """mode: "formula", "measured", or "auto" (the formula if it fits to within
    AUTO_MAX_ERROR, else measured settings if there are any)."""
    if mode == "measured":
        return fit_measured(flights)
    formula = fit(flights)
    if mode == "formula" or formula.rmse_hours <= AUTO_MAX_ERROR:
        return formula
    try:
        measured = fit_measured(flights)
    except ValueError as exc:
        return replace(formula, notes=formula.notes + (
            f"The formula misses by {60 * formula.rmse_hours:.0f} min on average. "
            f"{exc} Plan your settings on a second route to use them instead.",))
    return replace(measured, notes=measured.notes + (
        f"The formula missed by {60 * formula.rmse_hours:.0f} min on average, so "
        "the tool uses your measured settings instead.",))


def parse_duration(text):
    """Hours from '6h 20m 57s', '1d 2h', '6:20', '6:20:57' or '6.35'."""
    s = str(text).strip().lower()
    if not s:
        return None
    if ":" in s:
        parts = [float(p) for p in s.split(":")]
        while len(parts) < 3:
            parts.append(0.0)
        return parts[0] + parts[1] / 60 + parts[2] / 3600
    units = {"d": 24, "h": 1, "m": 1 / 60, "s": 1 / 3600}
    total, num, seen = 0.0, "", False
    for ch in s.replace(" ", ""):
        if ch.isdigit() or ch == ".":
            num += ch
        elif ch in units and num:
            total += float(num) * units[ch]
            num, seen = "", True
        else:
            raise ValueError(f"Can't read the time '{text}'. Use e.g. 6h 20m or 6:20.")
    if num:
        if seen:
            raise ValueError(f"Can't read the time '{text}'. Use e.g. 6h 20m or 6:20.")
        total += float(num)
    return total
