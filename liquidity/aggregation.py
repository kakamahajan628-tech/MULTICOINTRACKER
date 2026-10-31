"""Python port of jose-donato/crypto-orderbook Aggregator bid/ask rounding.

MIT; Copyright (c) 2025 jose-donato. See licenses/crypto-orderbook-MIT.txt.
Changes: Decimal Python port, sorted output, quote notional and venue contributions.
"""

from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from .models import decimal


def aggregate_levels(levels, tick, side):
    tick = decimal(tick)
    if tick < 0:
        raise ValueError("negative bucket size")
    rounding = ROUND_FLOOR if side == "bid" else ROUND_CEILING
    buckets = {}
    for p, q in levels:
        price, qty = decimal(p), decimal(q)
        key = (price / tick).to_integral_value(rounding=rounding) * tick if tick else price
        base, quote = buckets.get(key, (Decimal(0), Decimal(0)))
        buckets[key] = (base + qty, quote + price * qty)
    return [
        {"price": str(p), "base_qty": str(buckets[p][0]), "quote_notional": str(buckets[p][1])}
        for p in sorted(buckets, reverse=side == "bid")
    ]


def cross_venue(states, now, tick=None, stale_ms=3000):
    grouped = {}
    for state in states:
        book, instrument = state.book, state.book.instrument
        if not book.ready or now - book.updated_ms > stale_ms:
            continue
        identity = f"{instrument.base}/{instrument.quote}:{instrument.product}"
        grouped.setdefault(identity, []).append(book)
    output = []
    for market, books in grouped.items():
        anchor = (books[0].bbo()[0] + books[0].bbo()[2]) / 2
        # Price-adaptive grid shared by venues; a fixed $10 grid is unusable for
        # low-priced coins. Respect the coarsest participating native tick.
        bucket = (
            decimal(tick)
            if tick is not None
            else max(
                Decimal(1).scaleb(anchor.adjusted() - 4),
                max(decimal(book.instrument.tick) for book in books),
            )
        )
        venues = {}
        for book in books:
            venues[book.instrument.venue] = {
                "mid": float((book.bbo()[0] + book.bbo()[2]) / 2),
                "bid": aggregate_levels(book.top("bid", 1000), bucket, "bid"),
                "ask": aggregate_levels(book.top("ask", 1000), bucket, "ask"),
            }
        merged = {"bid": {}, "ask": {}}
        for venue, data in venues.items():
            for side in merged:
                for row in data[side]:
                    merged[side].setdefault(row["price"], {})[venue] = row["quote_notional"]
        output.append(
            {
                "market": market,
                "unit": "USDT (not USD FX-adjusted)",
                "bucket_tick": str(bucket),
                "venue_mids": {v: x["mid"] for v, x in venues.items()},
                "depth": merged,
                "note": "Different venue basis; no fillable composite book.",
            }
        )
    return output
