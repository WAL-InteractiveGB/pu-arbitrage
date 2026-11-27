"""Offline tests with synthetic order books. Run: python -m unittest -v"""
import os
import sys
import tempfile
import math
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import arbitrage as A  # noqa: E402
import flightmodel as FM  # noqa: E402
import tradelog  # noqa: E402
from borders import BorderModel  # noqa: E402


class FakeBooks:
    def __init__(self, books):
        self.b = books

    @property
    def exchanges(self):
        return {e for _, e in self.b}

    def tickers_at(self, ex):
        return {t for t, e in self.b if e == ex}

    def get(self, t, ex):
        return self.b.get((t, ex))


class NoHistory:
    def prefetch(self, keys):
        pass

    def closes(self, ticker, ex):
        return []


REGIONS = {"CI1": "CI", "CI2": "CI", "NC1": "NC"}
BOOKS = FakeBooks({
    ("RAT", "CI1"): A.Book(asks=[(100, 1000)], bids=[(95, 1000)]),
    ("RAT", "CI2"): A.Book(asks=[(130, 1000)], bids=[(120, 1000)]),
    ("RAT", "NC1"): A.Book(asks=[(130, 1000)], bids=[(120, 1000)]),
})
MATS = {"RAT": (1.0, 1.0)}
SHIP = A.Ship(weight=100, volume=100, ff_per_parsec=0)  # fuel-free unless a test sets it
LEG = (A.Route(5, 50.0), 10)


def settings(**kw):
    base = dict(capital=1_000_000, fee_rate=0.0, risk_aversion=0.0)
    base.update(kw)
    return A.Settings(**base)


def evaluate(dest="NC1", config=None, st=None, **border_kw):
    borders = BorderModel(set(REGIONS), REGIONS, config, **border_kw)
    # Treat every exchange as one currency so one-way trips can be compared.
    market = A.Market(BOOKS, MATS, NoHistory(), borders,
                      {e: "CIS" for e in REGIONS}, {})
    return A.evaluate_route("CI1", dest, LEG, SHIP, st or settings(), market)


class OrderBookTests(unittest.TestCase):
    def test_walk_book_and_vwap(self):
        levels = [(10, 5), (11, 5)]
        self.assertEqual(A.walk_book(levels, 7), (7, 72))
        price, filled = A.vwap(levels, 20)
        self.assertEqual(filled, 10)
        self.assertAlmostEqual(price, 10.5)

    def test_lots_stop_when_unprofitable(self):
        lots = A.build_lots("X", [(10, 5), (20, 5)], [(15, 10)], 1, 1,
                            A.Pricing(fee=0.0))
        self.assertEqual(sum(l.qty for l in lots), 5)  # second ask level loses money

    def test_fees_and_duties_in_unit_prices(self):
        p = A.Pricing(fee=0.01, export_duty=0.02, import_duty=0.05)
        self.assertAlmostEqual(p.unit_cost(100)[0], 103.0)
        self.assertAlmostEqual(p.unit_rev(100)[0], 94.0)


class FuelTests(unittest.TestCase):
    def test_game_ftl_formula(self):
        ship = A.Ship(weight=1, volume=1, sf_per_trip=450, reactor_usage=0.25)
        sf, ff = A.fuel_units(ship, A.Route(10, 70.0))
        self.assertEqual(sf, 450)
        self.assertAlmostEqual(ff, 70 * 7.2 * 0.25)  # parsecs x 7.2 x reactor
        sf2, ff2 = A.fuel_units(ship, A.Route(10, 70.0), round_trip=True)
        self.assertEqual((sf2, ff2), (2 * sf, 2 * ff))

    def test_unknown_parsecs_is_not_free(self):
        ship = A.Ship(weight=1, volume=1)
        self.assertIsNone(A.fuel_units(ship, A.Route(10, None)))

    def test_reactor_percent_or_fraction(self):
        self.assertEqual(A.reactor_fraction(25), 0.25)
        self.assertEqual(A.reactor_fraction(0.4), 0.4)
        with self.assertRaises(SystemExit):
            A.reactor_fraction(150)

    def test_fuel_cost_walks_books(self):
        books = FakeBooks({("SF", "CI1"): A.Book(asks=[(10, 1000)]),
                           ("FF", "CI1"): A.Book(asks=[(20, 50), (30, 1000)])})
        ship = A.Ship(weight=1, volume=1, sf_per_trip=100, reactor_usage=0.25)
        # FF = 40 pc x 7.2 x 0.25 = 72 units: 50 at 20 + 22 at 30
        cost = A.fuel_cost(ship, books, "CI1", A.Route(5, 40.0))
        self.assertAlmostEqual(cost, 100 * 10 + 50 * 20 + 22 * 30)


    def test_stl_fuel_follows_loaded_fuel_and_slider(self):
        base = dict(weight=500, volume=500)
        self.assertEqual(A.Ship(**base).sf_units(), 0.0)
        ship = A.Ship(**base, sf_loaded=1000, stl_usage=0.5)
        # approach + departure, each 0.494 x 50% x 1000
        self.assertAlmostEqual(ship.sf_units(), 2 * 0.494 * 0.5 * 1000)
        faster = A.Ship(**base, sf_loaded=1000, stl_usage=1.0)
        self.assertAlmostEqual(faster.sf_units(), 2 * ship.sf_units())
        short_dep = A.Ship(**base, sf_loaded=1000, stl_usage=0.5,
                           stl_departure_share=0.0)
        self.assertAlmostEqual(short_dep.sf_units(), 0.494 * 0.5 * 1000)
        measured = A.Ship(**base, sf_loaded=1000, sf_per_trip=300)
        self.assertEqual(measured.sf_units(), 300)


class DepthTests(unittest.TestCase):
    def test_depth_left_falls_with_time_and_volume(self):
        bids = [(10.0, 600), (9.0, 400)]          # 1000 units on the book
        self.assertEqual(A.depth_share(bids, None, 10), A.DEST_DEPTH_SHARE)
        self.assertEqual(A.depth_share(bids, 0, 10), 1.0)
        short = A.depth_share(bids, 1000, 6)
        long = A.depth_share(bids, 1000, 48)
        self.assertAlmostEqual(short, math.exp(-0.5 * 1000 * 6 / 24 / 1000))
        self.assertGreater(short, long)
        busier = A.depth_share(bids, 4000, 6)
        self.assertGreater(short, busier)
        self.assertEqual(A.depth_share(bids, 10 ** 6, 48), A.MIN_DEPTH_SHARE)

    def test_market_makers_are_not_depth(self):
        bids = [(10.0, A.UNLIMITED)]
        self.assertEqual(A.depth_share(bids, 1000, 24), 1.0)


class FlightModelTests(unittest.TestCase):
    TRUE = FM.FlightModel(0.4, 1.5, 0.10, 1.1, 0.12, 7.2, 0.9, (0.25, 1), (0.25, 1), 0, 0)

    def flights(self, settings, routes=((61.4, 9), (94.0, 12))):
        out = []
        for pc, j in routes:
            for s, r in settings:
                out.append(dict(parsecs=pc, jumps=j, stl_usage=s, reactor=r,
                                sf_loaded=1000, hours=self.TRUE.hours(pc, j, s, r, 1000),
                                sf=self.TRUE.sf(s, 1000), ff=self.TRUE.ff(pc, r)))
        return out

    def test_fit_recovers_time_and_fuel(self):
        m = FM.fit(self.flights([(0.5, 0.5), (0.25, 0.5), (1, 0.5), (0.5, 0.25), (0.5, 1)]))
        for pc, j, s, r in [(70, 10, 0.75, 0.75), (120, 15, 0.25, 1.0)]:
            self.assertAlmostEqual(m.hours(pc, j, s, r, 1000),
                                   self.TRUE.hours(pc, j, s, r, 1000), delta=0.05)
        self.assertAlmostEqual(m.sf_coef, 0.9)
        self.assertAlmostEqual(m.ff_per_parsec, 7.2)
        self.assertLess(m.rmse_hours, 0.01)

    def test_grid_stays_inside_measured_range(self):
        m = FM.fit(self.flights([(0.5, 0.25), (1, 0.25), (0.75, 0.25)]))
        self.assertEqual({r for _, r in m.grid()}, {0.25})
        self.assertEqual({s for s, _ in m.grid()}, {0.5, 0.75, 1.0})
        self.assertTrue(any("Reactor" in n for n in m.notes))

    def test_too_few_flights(self):
        with self.assertRaises(ValueError):
            FM.fit(self.flights([(0.5, 0.5)], routes=((61.4, 9),)))

    def test_parse_duration(self):
        self.assertAlmostEqual(FM.parse_duration("6h 20m 57s"), 6 + 20 / 60 + 57 / 3600)
        self.assertAlmostEqual(FM.parse_duration("6:30"), 6.5)
        self.assertAlmostEqual(FM.parse_duration("1d 2h"), 26)
        self.assertAlmostEqual(FM.parse_duration("6.25"), 6.25)
        with self.assertRaises(ValueError):
            FM.parse_duration("six hours")

    def test_ship_uses_model(self):
        m = FM.fit(self.flights([(0.5, 0.5), (0.25, 0.5), (1, 0.5), (0.5, 0.25), (0.5, 1)]))
        ship = A.Ship(weight=500, volume=500, sf_loaded=1000, stl_usage=0.5,
                      reactor_usage=1.0, flight_model=m)
        route = A.Route(9, 61.4, None)
        self.assertAlmostEqual(ship.sf_units(), 450, delta=0.5)
        self.assertAlmostEqual(ship.ff_units(61.4), 7.2 * 61.4, delta=0.5)
        self.assertAlmostEqual(ship.leg_hours(route),
                               self.TRUE.hours(61.4, 9, 0.5, 1.0, 1000), delta=0.05)
        self.assertEqual(ship.settings_label, "50/100")


class FallbackAndTankTests(unittest.TestCase):
    @staticmethod
    def odd_ship_flights():
        # time grows with the square of distance: not the formula's shape
        t = lambda pc, s, r: 1 + 0.002 * pc * pc / r ** 0.3 + 2 / s
        return [dict(parsecs=pc, jumps=j, stl_usage=s, reactor=r, sf_loaded=1000,
                     hours=t(pc, s, r), sf=900 * s, ff=7.2 * pc * r)
                for pc, j in ((40, 6), (61.4, 9), (94, 12))
                for s, r in ((0.5, 0.5), (1, 1), (0.25, 0.25))]

    def test_auto_switches_to_measured_when_formula_fits_badly(self):
        fl = self.odd_ship_flights()
        formula = FM.fit_auto(fl, "formula")
        auto = FM.fit_auto(fl)
        self.assertEqual(auto.kind, "measured")
        self.assertLess(auto.rmse_hours, formula.rmse_hours)
        self.assertEqual(sorted(auto.grid()), [(0.25, 0.25), (0.5, 0.5), (1, 1)])
        self.assertTrue(any("formula missed" in n for n in auto.notes))

    def test_measured_snaps_to_measured_settings(self):
        m = FM.fit_measured(self.odd_ship_flights())
        self.assertEqual(m.snap(0.6, 0.45), (0.5, 0.5))
        self.assertAlmostEqual(m.sf(0.5, 1000), 450)
        self.assertAlmostEqual(m.ff(10, 1.0), 72)

    def test_measured_needs_two_routes_per_setting(self):
        fl = [f for f in self.odd_ship_flights() if f["parsecs"] == 40]
        with self.assertRaises(ValueError):
            FM.fit_measured(fl)

    def test_saved_models_round_trip_and_tolerate_web_extras(self):
        m = FM.fit_measured(self.odd_ship_flights())
        self.assertEqual(FM.load_model(m.to_dict()).grid(), m.grid())
        f = FM.fit_auto(self.odd_ship_flights(), "formula")
        d = f.to_dict()
        d["sf_loaded"] = 1000  # the web app stores this too
        self.assertEqual(FM.load_model(d).kind, "formula")

    def test_ftl_tank_limits_range(self):
        routes = {("CI1", "AI1"): A.Route(9, 61.4, 10.0)}
        ship = A.Ship(weight=500, volume=500, reactor_usage=1.0)
        self.assertIsNotNone(A.leg_info(routes, ship, "CI1", "AI1"))
        small = A.Ship(weight=500, volume=500, reactor_usage=1.0, ftl_tank=400)
        self.assertIsNone(A.leg_info(routes, small, "CI1", "AI1"))  # needs ~442 FF
        slow = A.Ship(weight=500, volume=500, reactor_usage=0.5, ftl_tank=400)
        self.assertIsNotNone(A.leg_info(routes, slow, "CI1", "AI1"))


class ManualTradeTests(unittest.TestCase):
    def test_manual_trade_counts_profit_not_accuracy(self):
        data = {"version": 1, "trades": []}
        tradelog.add_manual(data, route="CI1>AI1>CI1", kind="cycle", currency="CIS",
                            profit=50699, hours=72)
        s = tradelog.stats(data["trades"])
        self.assertEqual(s["n"], 1)
        self.assertEqual(s["n_predicted"], 0)
        self.assertAlmostEqual(s["total_actual"], 50699)
        self.assertAlmostEqual(s["profit_per_hour"], 50699 / 72)
        self.assertTrue(math.isnan(s["within_1sd"]))
        r = tradelog.row(data["trades"][0])
        self.assertEqual((r["pred"], r["actual"], r["hours"]), ("-", "50,699", "72"))
        self.assertIn("| - |", tradelog.markdown(data))


class OptimizerTests(unittest.TestCase):
    def test_respects_capital_weight_volume(self):
        lots = [A.Lot("A", 1000, 10, 12, 10, 12, 1, 1),
                A.Lot("B", 1000, 50, 70, 50, 70, 3, 1)]
        basket = A.optimize_basket(lots, 500, 60, 1000)
        cost = sum(u * l.unit_cost for l, u in basket)
        weight = sum(u * l.weight for l, u in basket)
        self.assertLessEqual(cost, 500 + 1e-9)
        self.assertLessEqual(weight, 60 + 1e-9)
        self.assertTrue(basket)


class RouteTests(unittest.TestCase):
    def test_no_border_costs(self):
        r = evaluate()
        self.assertEqual(r.net_profit, 100 * 20)
        self.assertEqual(r.border_fees, 0)

    def test_flat_fee(self):
        self.assertEqual(evaluate(entry_fee=500).net_profit, 1500)

    def test_import_duty(self):
        r = evaluate(import_duty=0.05)  # 5% of 120 = 6 per unit
        self.assertAlmostEqual(r.net_profit, 100 * 14)
        self.assertAlmostEqual(r.duties, 600)

    def test_duty_can_kill_route(self):
        self.assertIsNone(evaluate(config={"regions": {"NC": {"import_duty": 0.25}}}))

    def test_same_region_is_free_unless_route_listed(self):
        self.assertEqual(evaluate("CI2", entry_fee=500).border_fees, 0)
        r = evaluate("CI2", config={"routes": {"CI1>CI2": {"entry_fee": 300}}})
        self.assertEqual(r.border_fees, 300)

    def test_exit_fee_comes_out_of_capital(self):
        r = evaluate(config={"regions": {"CI": {"exit_fee": 500}}},
                     st=settings(capital=10_000))
        self.assertEqual(r.items[0]["units"], 95)
        self.assertLessEqual(r.capital_used, 10_000)
        self.assertEqual(r.net_profit, 95 * 20 - 500)

    def test_round_trip_pays_both_crossings(self):
        r = evaluate(entry_fee=500, st=settings(round_trip=True))
        self.assertEqual(r.border_fees, 1000)

    def test_bad_config_rejected(self):
        for bad in ({"regions": {"NC": {"import_duty": 2}}},
                    {"regions": {"NC": {"bogus": 1}}},
                    {"routes": {"CI1-NC1": {}}}):
            with self.assertRaises(SystemExit):
                BorderModel({"CI1"}, {}, bad)


class CycleTests(unittest.TestCase):
    def test_cycle_counts_fees_in_both_currencies(self):
        books = FakeBooks({
            # leg 1: cheap at CI1, dear at NC1 (prices in NCC are ~2x)
            ("A", "CI1"): A.Book(asks=[(100, 1000)], bids=[(99, 1000)]),
            ("A", "NC1"): A.Book(asks=[(260, 1000)], bids=[(250, 1000)]),
            # leg 2: cheap at NC1, dear back at CI1
            ("B", "CI1"): A.Book(asks=[(160, 1000)], bids=[(150, 1000)]),
            ("B", "NC1"): A.Book(asks=[(200, 1000)], bids=[(195, 1000)]),
            ("C", "CI1"): A.Book(asks=[(101, 1000)], bids=[(100, 1000)]),
            ("C", "NC1"): A.Book(asks=[(201, 1000)], bids=[(200, 1000)]),
        })
        mats = {t: (1.0, 1.0) for t in "ABC"}
        cur = {"CI1": "CIS", "NC1": "NCC"}
        regions = {"CI1": "CI", "NC1": "NC"}

        def run(cfg):
            b = BorderModel(set(cur), regions, cfg)
            m = A.Market(books, mats, NoHistory(), b, cur, {})
            return A.evaluate_cycle("CI1", "NC1", "CI1", LEG, LEG,
                                    SHIP, settings(), m)

        free = run(None)
        self.assertIsNotNone(free)
        self.assertEqual(free.kind, "cycle")
        # A fee paid in CIS on the way home cuts home profit one for one.
        paid = run({"routes": {"NC1>CI1": {"entry_fee": 400}}})
        self.assertAlmostEqual(free.net_profit - paid.net_profit, 400)
        # A fee paid in NCC at NC1 must be covered by NCC from selling leg 1,
        # alongside leg 2 purchases, and it lowers profit.
        fee = 7000
        mid = run({"routes": {"CI1>NC1": {"entry_fee": fee}}})
        self.assertLessEqual(mid.cost2 + fee, mid.rev1 + 1e-6)
        self.assertGreaterEqual(mid.leftover, -1e-6)
        self.assertLess(mid.net_profit, free.net_profit)


class TradeLogTests(unittest.TestCase):
    def test_record_close_report(self):
        r = evaluate(entry_fee=500)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "log.json")
            t = tradelog.record_result(r, home="CI1", currency="CIS",
                                       capital=1_000_000, ship="Test",
                                       settings=settings(), path=path)
            self.assertEqual(t["id"], 1)
            self.assertEqual(t["predicted"]["net_profit"], 1500)
            data = tradelog.load(path)
            tradelog.close(data, 1, received=12_000, spent=10_000, costs=600, hours=12)
            tradelog.save(data, path)
            s = tradelog.stats(tradelog.load(path)["trades"])
            self.assertEqual(s["n"], 1)
            self.assertEqual(s["total_actual"], 1400)
            self.assertAlmostEqual(s["capture"], 1400 / 1500)
            self.assertIn("| 1 |", tradelog.markdown(tradelog.load(path)))


if __name__ == "__main__":
    unittest.main()
