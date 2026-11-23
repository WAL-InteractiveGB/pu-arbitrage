"""Offline tests with synthetic order books. Run: python -m unittest -v"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import arbitrage as A  # noqa: E402
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
