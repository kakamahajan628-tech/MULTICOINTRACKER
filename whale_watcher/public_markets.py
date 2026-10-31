"""Official public USDT perpetual market data; never an account-position fallback."""

import asyncio
import copy
import json
import os
import re
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from .core import now
from .recovery import Recovery

VENUES = ("binance", "bybit", "okx")
HOSTS = {
    "binance": "https://fapi.binance.com",
    "bybit": "https://api.bybit.com",
    "okx": "https://www.okx.com",
}
NATIVE = {
    "ethereum": "ETH",
    "arbitrum": "ETH",
    "base": "ETH",
    "solana": "SOL",
    "polygon": "POL",
    "bsc": "BNB",
}


def symbol_name(value):
    value = str(value).upper()
    if not re.fullmatch(r"[A-Z0-9]{1,24}", value):
        raise ValueError("Use the exact exchange base symbol, e.g. ETH; quote is USDT")
    return value


def numeric(value, minimum=None, positive=False):
    if value is None or value == "" or isinstance(value, bool):
        raise ValueError("Numeric market field missing")
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("Invalid market number") from None
    if (
        not result.is_finite()
        or abs(result) > Decimal("1e40")
        or (minimum is not None and result < minimum)
        or (positive and result <= 0)
    ):
        raise ValueError("Invalid market number")
    return result


def milliseconds(value):
    return datetime.fromtimestamp(
        float(numeric(value, positive=True)) / 1000, timezone.utc
    ).isoformat()


def market_targets(rows, links=None, asset_links=None):
    """Contracts require explicit owner mapping; native coins and exact CEX bases are safe defaults."""
    from .screening import asset_identity

    links = (
        links if links is not None else json.loads(os.getenv("WHALE_SCREEN_ASSET_LINKS_JSON", "{}"))
    )
    asset_links = (
        asset_links
        if asset_links is not None
        else json.loads(os.getenv("WHALE_MARKET_ASSET_LINKS_JSON", "{}"))
    )
    targets, unmapped = {}, set()
    for row in rows:
        asset_id = asset_identity(row, links)
        symbol = asset_links.get(asset_id)
        basis = "owner_verified_asset_link"
        if symbol is None and asset_id == row["chain"] + ":native":
            symbol, basis = NATIVE.get(row["chain"]), "native_coin"
        if (
            symbol is None
            and row["group"].startswith("Futures")
            and row.get("platform", "").lower() in VENUES
        ):
            symbol, basis = row["coin"], "exact_venue_base_symbol"
        # Hyperliquid symbols and contract tickers need a link, avoiding ticker collisions.
        if symbol is None:
            unmapped.add(asset_id)
            continue
        try:
            symbol = symbol_name(symbol)
        except ValueError:
            unmapped.add(asset_id)
            continue
        associations = targets.setdefault(symbol, {})
        associations[asset_id] = basis
    return targets, sorted(unmapped)


class PublicMarkets:
    def __init__(self, client, store, recovery=None):
        self.client, self.store = client, store
        self.recovery = recovery or Recovery(store)
        self.locks, self.gates, self.last_request = {}, {}, {}
        self.semaphore = asyncio.Semaphore(3)

    async def request(self, venue, endpoint, path, params, parser):
        """Meter actual attempts, validate before recording endpoint health, and pace every retry."""

        async def operation():
            async with self.gates.setdefault(venue, asyncio.Lock()):
                interval = 1 / max(0.1, min(5, float(os.getenv("WHALE_MARKET_RPS", "2"))))
                await asyncio.sleep(
                    max(0, self.last_request.get(venue, 0) + interval - time.monotonic())
                )
                self.last_request[venue] = time.monotonic()
                self.store.reserve_budget(
                    "market_" + venue,
                    now()[:7],
                    1,
                    int(os.getenv("WHALE_MARKET_MONTHLY_CALLS", "100000")),
                )
                headers = {}
                # Only this separate optional public-data key is used; no account secret or signing.
                if (
                    venue == "binance"
                    and path.startswith("/futures/data/")
                    and os.getenv("WHALE_BINANCE_MARKET_KEY")
                ):
                    headers["X-MBX-APIKEY"] = os.environ["WHALE_BINANCE_MARKET_KEY"]
                response = await self.client.get(
                    HOSTS[venue] + path,
                    params=params,
                    headers=headers,
                    timeout=float(os.getenv("WHALE_MARKET_TIMEOUT_SECONDS", "8")),
                )
                response.raise_for_status()
                body = response.json()
                if venue == "bybit" and body.get("retCode") != 0:
                    raise ValueError("Public market API rejected request")
                if venue == "okx" and str(body.get("code")) != "0":
                    raise ValueError("Public market API rejected request")
                fields, timestamp = parser(body)
                observed_at = milliseconds(timestamp)
                age = time.time() - datetime.fromisoformat(observed_at).timestamp()
                if not -60 <= age <= int(os.getenv("WHALE_MARKET_MAX_AGE_SECONDS", "1200")):
                    raise ValueError("Stale or future-dated public market data")
                return fields, observed_at

        instrument = str(params.get("symbol") or params.get("instId") or params.get("ccy"))
        return await self.recovery.run(
            "market_" + venue + "_" + endpoint + "_" + instrument, operation, attempts=2
        )

    async def venue_snapshot(self, venue, symbol):
        instrument = symbol + "-USDT-SWAP" if venue == "okx" else symbol + "USDT"
        row = {
            "venue": venue,
            "symbol": symbol,
            "instrument": instrument,
            "quote_currency": "USDT",
            "market_type": "linear_perpetual",
            "observed_at": now(),
            "fields": {},
            "field_times": {},
            "errors": [],
        }

        def exact(body, name="symbol"):
            if body.get(name) != instrument:
                raise ValueError("Market instrument mismatch")
            return body

        def values(body, mapping):
            return {
                target: str(numeric(body[source], minimum=minimum))
                for target, (source, minimum) in mapping.items()
            }

        def binance_mark(body):
            exact(body)
            fields = values(
                body,
                {
                    "mark_price": ("markPrice", Decimal("1e-30")),
                    "index_price": ("indexPrice", Decimal("1e-30")),
                    "funding_rate": ("lastFundingRate", None),
                },
            )
            fields["next_funding_at"] = milliseconds(body["nextFundingTime"])
            return fields, body["time"]

        def binance_ticker(body):
            exact(body)
            return values(
                body,
                {
                    "last_price": ("lastPrice", Decimal("1e-30")),
                    "change_24h_pct": ("priceChangePercent", None),
                    "volume_24h_quote": ("quoteVolume", 0),
                },
            ), body["closeTime"]

        def binance_oi(body):
            exact(body)
            return {
                "open_interest_base": str(numeric(body["openInterest"], minimum=0)),
                "open_interest_basis": "base_asset",
            }, body["time"]

        def binance_ratio(body, scope):
            item = max(body, key=lambda r: int(r["timestamp"]))
            exact(item)
            return {
                scope: str(numeric(item["longShortRatio"], minimum=0)),
                "ratio_period": "5m",
            }, item["timestamp"]

        def binance_taker(body):
            item = max(body, key=lambda r: int(r["timestamp"]))
            return values(
                item,
                {
                    "taker_buy_sell_ratio": ("buySellRatio", 0),
                    "taker_buy_volume_base": ("buyVol", 0),
                    "taker_sell_volume_base": ("sellVol", 0),
                },
            ), item["timestamp"]

        def bybit_item(body):
            if body["result"].get("category") != "linear":
                raise ValueError("Unexpected Bybit market category")
            return exact(
                next(item for item in body["result"]["list"] if item.get("symbol") == instrument)
            )

        def bybit_ticker(body):
            item = bybit_item(body)
            fields = values(
                item,
                {
                    "mark_price": ("markPrice", Decimal("1e-30")),
                    "index_price": ("indexPrice", Decimal("1e-30")),
                    "last_price": ("lastPrice", Decimal("1e-30")),
                    "funding_rate": ("fundingRate", None),
                    "open_interest_base": ("openInterest", 0),
                    "open_interest_quote": ("openInterestValue", 0),
                    "volume_24h_quote": ("turnover24h", 0),
                },
            )
            fields.update(
                change_24h_pct=str(numeric(item["price24hPcnt"]) * 100),
                next_funding_at=milliseconds(item["nextFundingTime"]),
                open_interest_basis="base_asset_both_sides",
            )
            if item.get("fundingIntervalHour"):
                fields["funding_interval_hours"] = str(
                    numeric(item["fundingIntervalHour"], positive=True)
                )
            return fields, body["time"]

        def bybit_ratio(body):
            item = max(body["result"]["list"], key=lambda r: int(r["timestamp"]))
            exact(item)
            buy, sell = (
                numeric(item["buyRatio"], minimum=0),
                numeric(item["sellRatio"], positive=True),
            )
            if buy > 1 or sell > 1 or abs(buy + sell - 1) > Decimal(".02"):
                raise ValueError("Invalid account proportions")
            return {"long_short_account_ratio": str(buy / sell), "ratio_period": "5min"}, item[
                "timestamp"
            ]

        def okx_item(body):
            return exact(
                next(item for item in body["data"] if item.get("instId") == instrument), "instId"
            )

        def okx_ticker(body):
            item = okx_item(body)
            fields = values(item, {"last_price": ("last", Decimal("1e-30"))})
            opening = numeric(item["open24h"], positive=True)
            fields["change_24h_pct"] = str((numeric(item["last"]) / opening - 1) * 100)
            # OKX derivatives volCcy24h is base currency, not quote turnover.
            fields["volume_24h_base"] = str(numeric(item["volCcy24h"], minimum=0))
            return fields, item["ts"]

        def okx_mark(body):
            item = okx_item(body)
            return {"mark_price": str(numeric(item["markPx"], positive=True))}, item["ts"]

        def okx_oi(body):
            item = okx_item(body)
            return values(
                item,
                {
                    "open_interest_base": ("oiCcy", 0),
                    "open_interest_usd": ("oiUsd", 0),
                    "open_interest_contracts": ("oi", 0),
                },
            ) | {"open_interest_basis": "base_asset"}, item["ts"]

        def okx_funding(body):
            item = okx_item(body)
            fields = {
                "funding_rate": str(numeric(item["fundingRate"])),
                "next_funding_at": milliseconds(item["fundingTime"]),
            }
            if item.get("nextFundingTime"):
                interval = (
                    numeric(item["nextFundingTime"]) - numeric(item["fundingTime"])
                ) / 3600000
                if interval > 0:
                    fields["funding_interval_hours"] = str(interval)
            return fields, item["ts"]

        def okx_ratio(body):
            item = max(body["data"], key=lambda r: int(r[0]))
            return {
                "long_short_account_ratio": str(numeric(item[1], minimum=0)),
                "ratio_period": "5m",
                "ratio_scope": "coin_contract_accounts_all_quotes",
            }, item[0]

        if venue == "binance":
            specs = [
                ("mark", "/fapi/v1/premiumIndex", {"symbol": instrument}, binance_mark),
                ("ticker", "/fapi/v1/ticker/24hr", {"symbol": instrument}, binance_ticker),
                ("oi", "/fapi/v1/openInterest", {"symbol": instrument}, binance_oi),
            ]
            for endpoint, path, scope in [
                ("account_ratio", "globalLongShortAccountRatio", "long_short_account_ratio"),
                ("top_accounts", "topLongShortAccountRatio", "top_trader_account_ratio"),
                ("top_positions", "topLongShortPositionRatio", "top_trader_position_ratio"),
            ]:
                specs.append(
                    (
                        endpoint,
                        "/futures/data/" + path,
                        {"symbol": instrument, "period": "5m", "limit": 1},
                        lambda body, scope=scope: binance_ratio(body, scope),
                    )
                )
            specs.append(
                (
                    "taker",
                    "/futures/data/takerlongshortRatio",
                    {"symbol": instrument, "period": "5m", "limit": 1},
                    binance_taker,
                )
            )
        elif venue == "bybit":
            specs = [
                (
                    "ticker",
                    "/v5/market/tickers",
                    {"category": "linear", "symbol": instrument},
                    bybit_ticker,
                ),
                (
                    "account_ratio",
                    "/v5/market/account-ratio",
                    {"category": "linear", "symbol": instrument, "period": "5min", "limit": 1},
                    bybit_ratio,
                ),
            ]
        else:
            specs = [
                ("ticker", "/api/v5/market/ticker", {"instId": instrument}, okx_ticker),
                (
                    "mark",
                    "/api/v5/public/mark-price",
                    {"instType": "SWAP", "instId": instrument},
                    okx_mark,
                ),
                (
                    "oi",
                    "/api/v5/public/open-interest",
                    {"instType": "SWAP", "instId": instrument},
                    okx_oi,
                ),
                ("funding", "/api/v5/public/funding-rate", {"instId": instrument}, okx_funding),
                (
                    "account_ratio",
                    "/api/v5/rubik/stat/contracts/long-short-account-ratio",
                    {"ccy": symbol, "period": "5m"},
                    okx_ratio,
                ),
            ]
        deadline = time.monotonic() + 25
        for endpoint, path, params, parser in specs:
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError("Venue collection deadline reached")
                fields, timestamp = await asyncio.wait_for(
                    self.request(venue, endpoint, path, params, parser), timeout=remaining
                )
                row["fields"].update(fields)
                row["field_times"].update({key: timestamp for key in fields})
            except Exception:
                row["errors"].append(
                    {
                        "endpoint": endpoint,
                        "status": "unavailable_or_access_required",
                        "optional_market_key": venue == "binance"
                        and path.startswith("/futures/data/"),
                    }
                )
        if "funding_rate" in row["fields"]:
            row["fields"]["funding_rate_pct"] = str(Decimal(row["fields"]["funding_rate"]) * 100)
            row["field_times"]["funding_rate_pct"] = row["field_times"]["funding_rate"]
        row["status"] = (
            "partial"
            if row["errors"] and row["fields"]
            else "unavailable"
            if not row["fields"]
            else "active"
        )
        return row

    async def snapshot(self, symbol):
        symbol = symbol_name(symbol)
        async with self.locks.setdefault(symbol, asyncio.Lock()):
            key = "public_market:" + symbol
            cached = self.store.get(key)
            ttl = max(0, min(600, int(os.getenv("WHALE_MARKET_CACHE_SECONDS", "600"))))
            if os.getenv("WHALE_MARKETS_ENABLED", "true").lower() != "true":
                return {
                    "symbol": symbol,
                    "status": "disabled",
                    "venues": [],
                    "collected_at": now(),
                    "scope": "exchange_aggregate_not_wallet_positions",
                }
            times = [
                at
                for row in (cached or {}).get("venues", [])
                for at in row.get("field_times", {}).values()
            ]
            fresh = all(
                -60
                <= time.time() - datetime.fromisoformat(at).timestamp()
                <= int(os.getenv("WHALE_MARKET_MAX_AGE_SECONDS", "1200"))
                for at in times
            )
            if (
                cached
                and fresh
                and 0
                <= time.time() - datetime.fromisoformat(cached["collected_at"]).timestamp()
                < ttl
            ):
                return copy.deepcopy(cached)

            async def one(venue):
                async with self.semaphore:
                    return await self.venue_snapshot(venue, symbol)

            venues = await asyncio.gather(*(one(venue) for venue in VENUES))
            reference = next(
                (
                    {
                        "venue": r["venue"],
                        "mark_price": r["fields"]["mark_price"],
                        "observed_at": r["field_times"]["mark_price"],
                        "quote_currency": "USDT",
                    }
                    for r in venues
                    if "mark_price" in r["fields"]
                ),
                None,
            )
            result = {
                "symbol": symbol,
                "collected_at": now(),
                "venues": venues,
                "reference": reference,
                "status": "available" if reference else "unavailable",
                "scope": "exchange_aggregate_not_wallet_positions",
            }
            if cached:
                missing = {r["venue"] for r in venues if r["status"] == "unavailable"}
                previous = cached.get("last_known", {}) | {
                    r["venue"]: r for r in cached["venues"] if r["fields"]
                }
                result["last_known"] = {v: r for v, r in previous.items() if v in missing}
            for row in venues:
                status = {
                    "state": row["status"],
                    "scope": "public_market_data",
                    "reason": "some_components_unavailable" if row["errors"] else None,
                }
                if row["fields"]:
                    status["last_success"] = result["collected_at"]
                else:
                    status["last_failure"] = result["collected_at"]
                self.recovery.health["market_" + row["venue"]] = status
            self.store.put(key, result)
            self.recovery.flush()
            return copy.deepcopy(result)

    async def collect(self, rows, links=None, asset_links=None):
        targets, unmapped = market_targets(rows, links, asset_links)
        cap = max(1, min(50, int(os.getenv("WHALE_MARKET_MAX_SYMBOLS", "10"))))
        symbols = sorted(targets)[:cap]

        async def one(symbol):
            result = await self.snapshot(symbol)
            return {**result, "asset_associations": targets[symbol]}

        tasks = {asyncio.create_task(one(symbol)): symbol for symbol in symbols}
        try:
            if tasks:
                done, pending = await asyncio.wait(
                    tasks,
                    timeout=max(
                        1, min(90, float(os.getenv("WHALE_MARKET_CYCLE_TIMEOUT_SECONDS", "60")))
                    ),
                )
            else:
                done, pending = set(), set()
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        snapshots, deferred = [], sorted(targets)[cap:]
        for task in done:
            if task.cancelled() or task.exception():
                deferred.append(tasks[task])
            else:
                snapshots.append(task.result())
        deferred += [tasks[task] for task in pending]
        return {
            "snapshots": sorted(snapshots, key=lambda r: r["symbol"]),
            "unmapped_assets": unmapped,
            "deferred_symbols": sorted(deferred),
            "scope": "exchange_aggregate_not_wallet_positions",
        }


def summarize_markets(samples):
    """Same venue/instrument/time-basis only; never sum OI across exchanges or 24h volumes across scans."""
    groups = {}
    for sample in samples:
        groups.setdefault((sample["venue"], sample["instrument"]), []).append(sample)
    result = []
    for (venue, instrument), points in sorted(groups.items()):
        points = sorted(points, key=lambda r: r["observed_at"])
        latest = points[-1]
        row = {
            "venue": venue,
            "instrument": instrument,
            "quote_currency": "USDT",
            "scope": "exchange_aggregate_not_wallet_positions",
            "samples": len({p["observed_at"] for p in points}),
            "first_at": points[0]["observed_at"],
            "last_at": latest["observed_at"],
            "latest": latest["fields"],
            "field_times": latest["field_times"],
        }
        for field, output in [
            ("open_interest_base", "open_interest_change_pct"),
            ("mark_price", "mark_price_change_pct"),
        ]:
            unique = {
                p["field_times"][field]: p["fields"][field]
                for p in points
                if field in p["fields"] and field in p["field_times"]
            }
            ordered = sorted(unique.items())
            row[output] = None
            if len(ordered) >= 2:
                first, last = Decimal(ordered[0][1]), Decimal(ordered[-1][1])
                if first > 0:
                    row[output] = round(float((last / first - 1) * 100), 4)
        result.append(row)
    return result


def market_digest(snapshots):
    lines = [
        "PUBLIC EXCHANGE MARKETS | USDT perpetuals",
        "Exchange aggregates; individual wallets and account P&L require separate evidence.",
    ]
    for snapshot in snapshots[:8]:
        lines.append(snapshot["symbol"] + " | collected " + snapshot["collected_at"])
        for row in snapshot["venues"]:
            f = row["fields"]
            lines.append(
                f"{row['venue']} [{row['status']}] mark {f.get('mark_price', '?')} USDT | OI {f.get('open_interest_base', '?')} {snapshot['symbol']} | funding {f.get('funding_rate_pct', '?')}% | L/S accounts {f.get('long_short_account_ratio', '?')}"
            )
        if snapshot.get("reference"):
            lines.append("Reference source: " + snapshot["reference"]["venue"])
    return "\n".join(lines)[:3900]
