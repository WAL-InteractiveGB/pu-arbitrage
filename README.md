# pu-arbitrage

*Built by [Arthur Pierce](https://github.com/Arthur-Pierce) · [WAL Interactive](https://github.com/WAL-InteractiveGB).*

**Use it in your browser: https://wal-interactivegb.github.io/pu-arbitrage/** (no install).

A spatial arbitrage optimizer for [Prosperous Universe](https://prosperousuniverse.com).
Tell it where your ship is, how much cash you have and how much it can carry.
It reads the live order books on every commodity exchange and tells you where
to fly, what to buy, how much, and what you should expect to make after fuel,
gateway fees, slippage and price risk.

```
#  Route        Type       Hrs    Capital  Wt%  Vol%     Fuel   Border  Net profit  Risk 1sd  Score/hr
1  CI1>IC1>CI1  cycle     36.0    499,980  100   100   34,150        0     102,368    49,262     1,475
2  CI1>AI1>CI1  cycle     27.0    499,901  100    52   32,339        0      52,800    44,851       294
3  CI1>NC2>CI1  cycle     81.0    499,976  100    85   62,246        0      76,481   111,849      -437

Best cycle: CI1>IC1>CI1
  CIS: 602,348 back - 484,255 out - fuel 15,726 - border fees 0 = 102,368 CIS net

Leg 1: buy at CI1, sell at IC1
Item     Units  Buy VWAP  Sell VWAP   Spent CIS     Got ICA
DCM         76   4940.00    5700.00     379,194     428,868
WM           8  12800.00   15000.00     103,424     118,515
...
```

It also keeps a **track record**: every trade you take is logged with the
model's prediction, you record the real result, and it reports how accurate
the model has been. See [Track record](#track-record).

---

## Two ways to use it

- **Website** (`docs/`): open the page, fill in your ship and press *Find trades*.
  Everything runs in your browser: it reads market data straight from FIO and
  solves the cargo problem locally. Settings and your trade log stay in your
  browser. This is what most players should use.
- **Python** (this folder): the same engine as a command-line tool, for people
  who want to script it, change the model, or keep their log in files.

Both give the same answers: the web engine is a direct port, checked against
the Python version on the same market snapshot.

## Quick start (Python)

You need Python 3.9 or newer.

```bash
git clone https://github.com/WAL-InteractiveGB/pu-arbitrage.git
cd pu-arbitrage
pip install -r requirements.txt
cp ships.example.json ships.json      # then edit your ship's cargo limits
python arbitrage.py --at CI1 --ship MyHauler --capital 500000
```

That works straight away: market data, exchange currencies and distances all
come from the community [FIO](https://doc.fnar.net) API. The first run takes a
few minutes while it downloads price history. After that it is cached and
takes seconds.

For accurate numbers, do the two setup steps below: fuel and gateway fees.

### 1. Fuel

Fuel follows the game's mechanics, as worked out by the player community (the
game does not publish them):

- **FTL fuel (FF) = parsecs flown x 7.2 x reactor usage.** Parsecs come from
  the game's own jump route between the two exchanges (via FIO). Reactor usage
  is the FTL slider in the flight planner (`SFC`); 25% is the cheapest and the
  default. Set `reactor_usage` in `ships.json` or pass `--reactor-usage 40` if
  you fly at another setting.
- **STL fuel (SF)** is a roughly fixed amount per flight (take-off, departure,
  approach, landing) that depends on your ship, so you measure it once:
  1. Plan two or three fully loaded flights in `SFC` and note the SF and FF it
     shows and the reactor % you planned at.
  2. Put them in `flights.csv` (see `flights.example.csv`).
  3. Run `python freight.py calibrate --ship MyHauler`. It saves `sf_per_trip`
     to `ships.json` and checks your FF against the 7.2 formula.

Check it with `python freight.py quote --ship MyHauler --at CI1 --ticker RAT`.

### 2. Enter gateway fees (if you use gateways)

Jumping through a gateway costs a fee set by the planetary government that runs
it. FIO does not publish these and they change, so copy them from the game:

1. `cp borders.example.json borders.json`
2. Plan the route in `SFC`, which shows the gateway cost, and put it under
   `routes`, e.g. `"CI1>NC1": {"entry_fee": 4500}`. Each direction is a
   separate entry.

Or pass one flat fee for every faction-border crossing: `--border-fee 4500`.
`borders.py` documents the other options.

### Useful options

| Option | What it does |
|---|---|
| `--mode same\|cycle\|both` | one-way trips within one currency, round trips through another currency, or both (default) |
| `--round-trip` | count the empty flight home on one-way trips |
| `--risk-aversion 1.0` | standard deviations of price risk subtracted from profit when ranking |
| `--fee-rate 0.01` | exchange fee per trade |
| `--max-range 30` | longest leg the ship will fly, in jumps |
| `--reactor-usage 25` | FTL reactor setting you fly at, as in SFC |
| `--csv results.csv` | save every result |
| `--offline` | use cached data only |
| `-v` | explain why routes were skipped |

`python arbitrage.py --help` lists everything.

---

## Track record

```bash
python arbitrage.py --at CI1 --ship MyHauler --record 1   # log result #1 before you fly it
python tradelog.py list                                   # open trades
python tradelog.py close 1 --profit 87250 --hours 38      # the real result
python tradelog.py report --markdown track_record.md      # predicted vs actual
```

The report shows, over all closed trades:

- **Capture:** actual profit as a share of predicted profit.
- **Calibration:** how often the result landed within the predicted
  one-standard-deviation risk band. About 68% means the risk estimate is honest.
- **Bias:** the average error. A negative number means the model is optimistic.
- **Profit per hour flown**, plus the best and worst trades.

Each prediction is saved at the moment you record it, before the trade happens,
so the track record can't be fitted after the fact.

---

## How it works

The game is a small, self-contained version of physical commodity trading, and
the model is built the way you would think about a real cargo: buy where a good
is cheap, pay to move it, and sell where it is dear, but only if the spread
survives every cost and the risk of the price moving while it travels.

**1. Real executable prices, not quotes.** The tool walks each exchange's order
book level by level instead of using the best bid and ask. Buying 1,000 units
moves you up the ask ladder, and that slippage is often what kills a spread that
looks good on screen. It also assumes only half the bids at the destination will
still be there when you arrive, because other traders keep trading while you fly.

**2. Freight and fixed costs.** Fuel is bought at the departure exchange at its
real ask prices: FTL fuel per parsec of the actual jump route at your reactor
setting, plus each ship's measured STL fuel per flight. Gateway fees and any border
costs are deducted in the currency they are paid in. Costs paid before you leave
come out of your trading capital, so they reduce how much cargo you can buy.

**3. Choosing the cargo.** Picking what to load is a knapsack problem with three
limits at once: cash, cargo weight and cargo volume. It is solved exactly as an
integer program (SciPy's HiGHS solver) over every price level of every candidate
good, so the answer is often a mix: a dense, valuable good to use up the weight
limit alongside a light, bulky one to fill the remaining volume.

**4. Price risk in transit.** A cargo is an unhedged position for the whole
flight. For each good the tool estimates daily volatility from 30 days of price
history and scales it to the flight time. If the destination price is well
above its recent average, it assumes part of that premium fades before you
arrive (mean reversion). Routes are ranked by risk-adjusted profit per hour:
expected profit, minus a multiple of its standard deviation, divided by time.

**5. Trading across currencies without FX.** Each faction has its own currency
and the tool assumes you cannot convert between them. Profit in another currency
is only worth something if you can turn it back into goods and bring them home.
A *cycle* (A > B > A) is therefore solved as one problem: leg 2 can only spend
what leg 1 earned at B, all profit is measured in your home currency, and any
cash stranded at B counts as zero. This is the same constraint as a trader who
must reinvest local proceeds in a return cargo because they cannot repatriate
the cash.

### Known limitations

- The exchange fee (`--fee-rate`, default 1%) and the 50% destination-depth
  assumption are placeholders. Adjust them to what you see in game.
- Price moves of different goods are treated as independent, which understates
  risk if they move together.
- Fuel uses community-derived formulas (7.2 FF per parsec at 100% reactor).
  They match measured flights but are not official.
- Flight time is a single hours-per-jump number per ship.
- The order books are a snapshot. Prices move while you fly, and other players
  running the same tool will compete away the same gaps. That is the point:
  more arbitrage means better-connected, more liquid markets.

---

## Project layout

| File | Purpose |
|---|---|
| `arbitrage.py` | the optimizer (order books, cargo solver, route ranking, output) |
| `borders.py` | gateway and border fee model |
| `currencies.py` | which currency each exchange uses |
| `freight.py` | fuel calibration and freight quotes |
| `tradelog.py` | track record: predictions vs results |
| `docs/` | the website: `index.html`, `style.css`, `js/engine.js` (the engine), `js/fio.js` (data), `js/app.js` (page), `js/tradelog.js` |
| `tests/` | offline tests on synthetic order books: `python -m unittest discover -s tests` |

Your own files (`ships.json`, `flights.csv`, `borders.json`, `trade_log.json`)
are listed in `.gitignore` and stay private.

## Publishing the website

The site is plain static files, so GitHub Pages hosts it for free: in the repo
on GitHub go to *Settings > Pages*, choose *Deploy from a branch*, branch
`main`, folder `/docs`. It appears at `https://wal-interactivegb.github.io/pu-arbitrage/`.
To try it locally first: `python3 -m http.server --directory docs` and open
http://localhost:8000.

## Data and credits

Market and map data comes from [FIO](https://doc.fnar.net), a community project
that collects Prosperous Universe data. Please be gentle with it: the tool
caches every response and identifies itself in its requests. This project is
not affiliated with Simulogics or FIO.

## License

MIT. See [LICENSE](LICENSE).
