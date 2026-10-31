"""Bounded real endpoint probe; records reachability separately from schema validation."""

import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import aiohttp
from .feeds import (
    BINANCE_REST,
    BINANCE_PUBLIC,
    BINANCE_MARKET,
    BYBIT_REST,
    BYBIT_WS,
    get_json,
    load_instrument,
    normalize_binance_market,
    normalize_bybit_market,
)


async def probe_apis():
    report = {"checked_at_utc": datetime.now(timezone.utc).isoformat(), "results": []}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:

        async def check(name, operation):
            try:
                result = await asyncio.wait_for(operation(), 22)
                return {"name": name, "status": "passed", "details": result}
            except Exception as exc:
                return {
                    "name": name,
                    "status": "unverified",
                    "error": f"{type(exc).__name__}: {str(exc)[:250]}",
                }

        async def metadata(venue):
            return asdict(await load_instrument(session, venue, "BTCUSDT"))

        async def snapshot():
            data = await get_json(
                session, BINANCE_REST + "/fapi/v1/depth", {"symbol": "BTCUSDT", "limit": 1000}
            )
            assert all(k in data for k in ("lastUpdateId", "bids", "asks"))
            return {
                "lastUpdateId": data["lastUpdateId"],
                "bid_levels": len(data["bids"]),
                "ask_levels": len(data["asks"]),
            }

        async def bybit_context():
            data = await get_json(
                session,
                BYBIT_REST + "/v5/market/tickers",
                {"category": "linear", "symbol": "BTCUSDT"},
            )
            row = data["result"]["list"][0]
            return {k: row[k] for k in ("symbol", "markPrice", "openInterestValue")}

        async def websocket(url, venue, channel):
            async with session.ws_connect(url, heartbeat=10) as ws:
                if venue == "bybit":
                    await ws.send_json(
                        {
                            "op": "subscribe",
                            "args": ["orderbook.200.BTCUSDT", "publicTrade.BTCUSDT"],
                        }
                    )
                found = {}
                while True:
                    raw = await ws.receive_json(timeout=15)
                    data = raw.get("data", raw)
                    if venue == "binance":
                        kind = data.get("e")
                        if channel == "depth" and kind == "depthUpdate":
                            assert all(k in data for k in ("U", "u", "pu", "b", "a"))
                            return {"event": kind, "U": data["U"], "u": data["u"], "pu": data["pu"]}
                        if channel == "market" and normalize_binance_market(data):
                            return {"event": kind, "schema": "normalized"}
                    else:
                        topic = raw.get("topic", "")
                        if topic.startswith("orderbook."):
                            assert all(k in data for k in ("b", "a", "u", "seq"))
                            found["book"] = {
                                "u": data["u"],
                                "seq": data["seq"],
                                "type": raw["type"],
                            }
                        if normalize_bybit_market(raw):
                            found["trade"] = "normalized"
                        if "book" in found and "trade" in found:
                            return found

        operations = [
            ("binance_instrument", lambda: metadata("binance")),
            ("bybit_instrument", lambda: metadata("bybit")),
            ("binance_snapshot", snapshot),
            ("bybit_context", bybit_context),
            (
                "binance_depth_ws",
                lambda: websocket(BINANCE_PUBLIC + "/ws/btcusdt@depth@100ms", "binance", "depth"),
            ),
            (
                "binance_market_ws",
                lambda: websocket(BINANCE_MARKET + "/ws/btcusdt@aggTrade", "binance", "market"),
            ),
            ("bybit_book_and_trade_ws", lambda: websocket(BYBIT_WS, "bybit", "depth")),
        ]
        report["results"] = await asyncio.gather(*(check(n, op) for n, op in operations))
    return report
