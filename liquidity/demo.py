"""Deterministic synthetic exchange-shaped event stream. No network."""

import asyncio
from dataclasses import asdict
import math
import time
from .models import Event, Instrument


def demo_events(steps=300, start_ms=1700000000000):
    instruments = [Instrument("binance", "BTCUSDT"), Instrument("bybit", "BTCUSDT")]
    for instrument in instruments:
        yield Event(instrument.venue, instrument.symbol, "instrument", asdict(instrument), start_ms)
        yield Event(
            instrument.venue,
            instrument.symbol,
            "status",
            {"scope": "market", "connected": True},
            start_ms,
        )
        bids = [[f"{60000 - i * 0.1:.1f}", "0.050"] for i in range(100)]
        asks = [[f"{60000.1 + i * 0.1:.1f}", "0.050"] for i in range(100)]
        if instrument.venue == "binance":
            yield Event(
                "binance",
                "BTCUSDT",
                "binance_snapshot",
                {"lastUpdateId": 100, "bids": bids, "asks": asks},
                start_ms,
            )
        else:
            yield Event(
                "bybit",
                "BTCUSDT",
                "bybit_depth",
                {"type": "snapshot", "data": {"u": 100, "seq": 100, "b": bids, "a": asks}},
                start_ms,
            )
        yield Event(
            instrument.venue,
            "BTCUSDT",
            "context",
            {
                "mark": "60000",
                "oi_quote": "5000000000",
                "oi_base": "83333.3",
                "funding_rate": "0.0001",
                "source": "synthetic",
            },
            start_ms,
        )
    for step in range(steps):
        now = start_ms + (step + 1) * 200
        for venue in ("binance", "bybit"):
            # Persistent interior wall; fast repeating ask wall withdrawal.
            wall = "10.000" if step % 100 < 70 else "0.050"
            asks = [["60000.5", "5.000" if step % 12 < 8 else "0.050"]]
            bids = [["59999.5", wall], ["60000.0", f"{0.05 + (step % 3) * 0.01:.3f}"]]
            if venue == "binance":
                data = {
                    "U": 100 + step,
                    "u": 101 + step,
                    "pu": 100 + step,
                    "b": bids,
                    "a": asks,
                    "E": now,
                }
                yield Event(venue, "BTCUSDT", "binance_depth", data, now)
            else:
                yield Event(
                    venue,
                    "BTCUSDT",
                    "bybit_depth",
                    {
                        "type": "delta",
                        "data": {"u": 102 + step * 2, "seq": 102 + step * 3, "b": bids, "a": asks},
                    },
                    now,
                )
            yield Event(
                venue,
                "BTCUSDT",
                "trade",
                {
                    "id": f"{start_ms}:{step}",
                    "price": "60000.0",
                    "qty": "0.300",
                    "side": "sell" if step % 10 < 8 else "buy",
                    "exchange_ms": now,
                },
                now,
            )
            if step % 150 == 0:
                yield Event(
                    venue,
                    "BTCUSDT",
                    "context",
                    {
                        "mark": str(60000 + math.sin(step) * 10),
                        "oi_quote": str(5000000000 + step * 100000),
                    },
                    now,
                )


async def run_demo(emit):
    while True:
        start = time.time_ns() // 1_000_000
        for event in demo_events(steps=1800, start_ms=start):
            # Sleep to an absolute deadline so processing/rendering time cannot accumulate drift.
            await asyncio.sleep(max(0, event.received_ms - time.time_ns() // 1_000_000) / 1000)
            emit(event)
        for venue in ("binance", "bybit"):
            emit(
                Event(
                    venue,
                    "BTCUSDT",
                    "status",
                    {"scope": "all", "connected": False, "reason": "demo cycle restart"},
                )
            )
