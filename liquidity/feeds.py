"""Public production feeds; no credentials or trading endpoints."""

import asyncio
from dataclasses import asdict
from decimal import Decimal
import json
import logging
import random
import time
import aiohttp
from .models import Event, Instrument

LOG = logging.getLogger(__name__)
BINANCE_REST = "https://fapi.binance.com"
BINANCE_PUBLIC = "wss://fstream.binance.com/public"
BINANCE_MARKET = "wss://fstream.binance.com/market"
BYBIT_REST = "https://api.bybit.com"
BYBIT_WS = "wss://stream.bybit.com/v5/public/linear"


async def get_json(session, url, params=None):
    async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as response:
        if response.status in (418, 429):
            retry = min(300, max(1, int(response.headers.get("Retry-After", "60"))))
            await asyncio.sleep(retry)
        response.raise_for_status()
        data = await response.json()
        if isinstance(data, dict) and data.get("retCode", 0) != 0:
            raise ValueError(f"Bybit API error {data['retCode']}: {data.get('retMsg')}")
        return data


def data_quantum(precision):
    precision = int(precision)
    if not 0 <= precision <= 36:
        raise ValueError("Unsupported exchange data precision")
    return format(Decimal(1).scaleb(-precision), "f")


def error_summary(error):
    if isinstance(error, BaseExceptionGroup):
        return "; ".join(error_summary(child) for child in error.exceptions[:3])[:220]
    return f"{type(error).__name__}: {str(error)[:180]}"


async def load_instrument(session, venue, symbol):
    if venue == "binance":
        info = await get_json(session, BINANCE_REST + "/fapi/v1/exchangeInfo")
        row = next(x for x in info["symbols"] if x["symbol"] == symbol)
        if row["contractType"] != "PERPETUAL" or row["status"] != "TRADING":
            raise ValueError("instrument is not a trading perpetual")
        filters = {x["filterType"]: x for x in row["filters"]}
        return Instrument(
            venue,
            symbol,
            row["baseAsset"],
            row["quoteAsset"],
            filters["PRICE_FILTER"]["tickSize"],
            filters["LOT_SIZE"]["stepSize"],
            price_quantum=data_quantum(row["pricePrecision"]),
            qty_quantum=data_quantum(row["quantityPrecision"]),
        )
    info = await get_json(
        session,
        BYBIT_REST + "/v5/market/instruments-info",
        {"category": "linear", "symbol": symbol},
    )
    row = info["result"]["list"][0]
    if row["contractType"] != "LinearPerpetual" or row["status"] != "Trading":
        raise ValueError("instrument is not a trading linear perpetual")
    return Instrument(
        venue,
        symbol,
        row["baseCoin"],
        row["quoteCoin"],
        row["priceFilter"]["tickSize"],
        row["lotSizeFilter"]["qtyStep"],
    )


def normalize_binance_market(data):
    kind = data.get("e")
    if kind == "aggTrade":
        return "trade", {
            "id": str(data["a"]),
            "price": data["p"],
            "qty": data["q"],
            "side": "sell" if data["m"] else "buy",
            "exchange_ms": data["T"],
            "aggregation": "exchange aggTrade",
        }
    if kind == "forceOrder":
        order = data["o"]
        return "liquidation", {
            "position_side": "long" if order["S"] == "SELL" else "short",
            "price": order.get("ap", order["p"]),
            "qty": order.get("z", order["q"]),
            "exchange_ms": order["T"],
            "coverage": "sampled latest event per symbol per 1000ms",
        }
    return None


def normalize_bybit_market(message):
    topic, rows = message.get("topic", ""), message.get("data", [])
    if topic.startswith("publicTrade."):
        return [
            (
                "trade",
                {
                    "id": str(t["i"]),
                    "price": t["p"],
                    "qty": t["v"],
                    "side": t["S"].lower(),
                    "exchange_ms": t["T"],
                    "aggregation": "exchange publicTrade",
                },
            )
            for t in rows
        ]
    if topic.startswith("allLiquidation."):
        return [
            (
                "liquidation",
                {
                    "position_side": "long" if t["S"] == "Buy" else "short",
                    "price": t["p"],
                    "qty": t["v"],
                    "exchange_ms": t["T"],
                    "price_type": "bankruptcy price",
                    "coverage": "Bybit allLiquidation reported feed",
                },
            )
            for t in rows
        ]
    return []


class LiveFeed:
    def __init__(self, session, venue, symbol, emit):
        self.session, self.venue, self.symbol, self.emit = session, venue, symbol, emit

    def send(self, kind, data, received_ms=None):
        event = Event(self.venue, self.symbol, kind, data)
        if received_ms is not None:
            event.received_ms = received_ms
        self.emit(event)

    async def retry(self, scope, operation):
        delay = 1
        while True:
            try:
                await operation()
                raise ConnectionError("stream closed")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Do not include request objects or secrets in logs.
                reason = error_summary(exc)
                LOG.warning("%s %s %s: %s", self.venue, self.symbol, scope, reason)
                self.send("status", {"scope": scope, "connected": False, "reason": reason})
                await asyncio.sleep(delay + random.random())
                delay = min(30, delay * 2)

    async def run(self):
        while True:
            try:
                instrument = await load_instrument(self.session, self.venue, self.symbol)
                self.send("instrument", asdict(instrument))
                # Persist the stream boundary even after an unclean prior process exit.
                self.send(
                    "status", {"scope": "all", "connected": False, "reason": "feed session start"}
                )
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.warning(
                    "Metadata unavailable %s:%s (%s)", self.venue, self.symbol, type(exc).__name__
                )
                await asyncio.sleep(15)
        async with asyncio.TaskGroup() as tasks:
            if self.venue == "binance":
                tasks.create_task(self.retry("book", self.binance_book))
                tasks.create_task(self.retry("market", self.binance_market))
            else:
                tasks.create_task(self.retry("all", self.bybit))
            tasks.create_task(self.context_loop())

    async def binance_book(self):
        self.send("status", {"scope": "book", "connected": False, "reason": "synchronizing"})
        url = BINANCE_PUBLIC + f"/ws/{self.symbol.lower()}@depth@100ms"
        async with self.session.ws_connect(url, heartbeat=20, max_msg_size=4 * 1024 * 1024) as ws:
            queue = asyncio.Queue(maxsize=10000)

            async def receive():
                while True:
                    message = await ws.receive(timeout=10)
                    if message.type != aiohttp.WSMsgType.TEXT:
                        raise ConnectionError(f"depth stream ended: {message.type}")
                    data = json.loads(message.data)
                    if data.get("e") == "depthUpdate":
                        # QueueFull aborts this session: a dropped delta must never be hidden.
                        queue.put_nowait((data, time.time_ns() // 1_000_000))

            async def synchronize():
                # Reader runs BEFORE REST to preserve the snapshot/diff bridge.
                await asyncio.sleep(0)
                snapshot = await get_json(
                    self.session,
                    BINANCE_REST + "/fapi/v1/depth",
                    {"symbol": self.symbol, "limit": 1000},
                )
                self.send("binance_snapshot", snapshot)
                started = time.monotonic()
                while time.monotonic() - started < 600:
                    data, received = await asyncio.wait_for(queue.get(), 10)
                    if time.time_ns() // 1_000_000 - received > 3000:
                        raise ConnectionError(
                            "depth bootstrap/processing queue lag exceeds 3 seconds"
                        )
                    self.send("binance_depth", data, received)
                raise ConnectionError("scheduled depth refresh")

            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(receive())
                tasks.create_task(synchronize())

    async def binance_market(self):
        symbol = self.symbol.lower()
        url = BINANCE_MARKET + f"/stream?streams={symbol}@aggTrade/{symbol}@forceOrder"
        async with self.session.ws_connect(url, heartbeat=20) as ws:
            self.send("status", {"scope": "market", "connected": True})
            started = time.monotonic()
            while time.monotonic() - started < 23 * 3600:
                message = await ws.receive(timeout=30)
                if message.type != aiohttp.WSMsgType.TEXT:
                    raise ConnectionError("market stream closed")
                raw = json.loads(message.data)
                result = normalize_binance_market(raw.get("data", raw))
                if result:
                    self.send(*result)
            raise ConnectionError("scheduled market reconnect")

    async def bybit(self):
        self.send("status", {"scope": "all", "connected": False, "reason": "synchronizing"})
        async with self.session.ws_connect(
            BYBIT_WS, heartbeat=20, max_msg_size=4 * 1024 * 1024
        ) as ws:
            await ws.send_json(
                {
                    "op": "subscribe",
                    "args": [
                        f"orderbook.200.{self.symbol}",
                        f"publicTrade.{self.symbol}",
                        f"allLiquidation.{self.symbol}",
                    ],
                }
            )

            async def ping():
                while True:
                    await asyncio.sleep(20)
                    await ws.send_json({"op": "ping"})

            async def receive():
                started = time.monotonic()
                while time.monotonic() - started < 600:
                    message = await ws.receive(timeout=30)
                    if message.type != aiohttp.WSMsgType.TEXT:
                        raise ConnectionError("Bybit stream closed")
                    data = json.loads(message.data)
                    if data.get("op") == "subscribe":
                        if not data.get("success"):
                            raise ValueError(f"subscription rejected: {data.get('ret_msg')}")
                        self.send("status", {"scope": "market", "connected": True})
                    if data.get("topic", "").startswith("orderbook."):
                        self.send("bybit_depth", data)
                    for result in normalize_bybit_market(data):
                        self.send(*result)
                raise ConnectionError("scheduled Bybit refresh")

            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(ping())
                tasks.create_task(receive())

    async def context_loop(self):
        while True:
            try:
                if self.venue == "binance":
                    mark, oi = await asyncio.gather(
                        get_json(
                            self.session,
                            BINANCE_REST + "/fapi/v1/premiumIndex",
                            {"symbol": self.symbol},
                        ),
                        get_json(
                            self.session,
                            BINANCE_REST + "/fapi/v1/openInterest",
                            {"symbol": self.symbol},
                        ),
                    )
                    data = {
                        "mark": mark["markPrice"],
                        "oi_quote": str(float(oi["openInterest"]) * float(mark["markPrice"])),
                        "oi_base": oi["openInterest"],
                        "funding_rate": mark.get("lastFundingRate"),
                        "exchange_ms": oi["time"],
                        "oi_quote_source": "OI base times latest observed mark",
                    }
                else:
                    result = await get_json(
                        self.session,
                        BYBIT_REST + "/v5/market/tickers",
                        {"category": "linear", "symbol": self.symbol},
                    )
                    row = result["result"]["list"][0]
                    data = {
                        "mark": row["markPrice"],
                        "oi_quote": row["openInterestValue"],
                        "oi_base": row["openInterest"],
                        "funding_rate": row.get("fundingRate"),
                        "exchange_ms": result["time"],
                        "oi_quote_source": "exchange reported",
                    }
                self.send("context", data)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.warning(
                    "Context unavailable %s:%s (%s)", self.venue, self.symbol, type(exc).__name__
                )
            await asyncio.sleep(30)
