"""
Exchange currencies for WAL Interactive.

Each Prosperous Universe commodity exchange trades in one currency, and prices
in different currencies cannot be compared or netted against each other
without foreign exchange. Without FX access you can only:

    * trade between exchanges that share a currency (for example CI1 and CI2), or
    * run a cycle: buy at A, sell at B for B's currency, spend all of that on
      goods at B, then sell those back at an exchange that uses A's currency.

Currencies are resolved in this order, highest priority first:
    1. OVERRIDES below (or --currency EX=CUR on arbitrage.py)
    2. a currency field in FIO's /exchange/full or /exchange/station data
    3. DEFAULT_CURRENCY below

DEFAULT_CURRENCY is my understanding of the game's exchanges and I could not
verify it against a source. arbitrage.py prints the table it actually used so
you can check it. An exchange with no known currency is treated as its own
currency, so it is never paired with another exchange by mistake.
"""

DEFAULT_CURRENCY = {
    "AI1": "AIC",
    "CI1": "CIS",
    "CI2": "CIS",
    "IC1": "ICA",
    "NC1": "NCC",
    "NC2": "NCC",
}

# Edit this to correct or extend the map, for example {"NC2": "NCC"}.
OVERRIDES = {}

CURRENCY_KEYS = ("CurrencyCode", "Currency", "CurrencyName")


def normalise(value):
    """Reduce whatever FIO sends for a currency to an upper-case code."""
    if isinstance(value, dict):
        for k in ("Code", "CurrencyCode", "Name"):
            if isinstance(value.get(k), str):
                return value[k].strip().upper() or None
        return None
    if isinstance(value, str):
        return value.strip().upper() or None
    return None


def resolve_currencies(exchanges, from_data=None, overrides=None):
    """Return ({exchange: currency or None}, {exchange: source description})."""
    from_data = from_data or {}
    merged = {**OVERRIDES, **(overrides or {})}
    cur, src = {}, {}
    for ex in sorted(exchanges):
        if merged.get(ex):
            cur[ex], src[ex] = merged[ex].upper(), "override"
        elif from_data.get(ex):
            cur[ex], src[ex] = from_data[ex], "FIO"
        elif ex in DEFAULT_CURRENCY:
            cur[ex], src[ex] = DEFAULT_CURRENCY[ex], "built-in map"
        else:
            cur[ex], src[ex] = None, "unknown"
    return cur, src


def same_currency(cur, a, b):
    """True if two exchanges share a known currency (an exchange always matches
    itself)."""
    return a == b or (cur.get(a) is not None and cur.get(a) == cur.get(b))
