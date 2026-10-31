"""Official Birdeye Standard endpoints; finite CU budget, 1 RPS and strict data checks."""

import asyncio
import hashlib
import json
import os
import time
from decimal import Decimal, localcontext
from datetime import datetime, timezone
from .core import now, EVM, SOL
from .recovery import Recovery

DOCS = "https://data.birdeye.so/docs"
ENDPOINTS = {
    "price": ("GET", "/defi/price", 3),
    "traders": ("GET", "/defi/v2/tokens/top_traders", 30),
    "holders": ("GET", "/defi/v3/token/holder", 30),
    "tokens": ("GET", "/defi/tokenlist", 30),
    "portfolio": ("GET", "/wallet/v2/current-net-worth", 30),
    "balance": ("POST", "/wallet/v2/token-balance", 10),  # Single token, ceil(10 * 1^0.8).
}
CHAINS = ("solana", "ethereum", "arbitrum", "base", "polygon", "bsc")


class Birdeye:
    def __init__(self, client, store, recovery=None):
        self.client, self.store = client, store
        self.recovery = recovery or Recovery(store)
        self.last_request = 0.0
        self.cache_locks = {}

    @staticmethod
    def validate(chain, address=None):
        if chain not in CHAINS:
            raise ValueError("Birdeye: unsupported configured chain")
        if address is not None and not (SOL if chain == "solana" else EVM).fullmatch(address):
            raise ValueError("Birdeye: use canonical token/wallet address, not ticker")

    @staticmethod
    def canonical(chain, address):
        Birdeye.validate(chain, address)
        if chain == "solana":
            return address
        # EIP-55: use CCXT's public Keccak helper; no new runtime dependency.
        from ccxt.base.exchange import Exchange

        lower = address[2:].lower()
        digest = Exchange.hash(lower.encode("ascii"), "keccak")
        return "0x" + "".join(
            char.upper() if char in "abcdef" and int(digest[i], 16) >= 8 else char
            for i, char in enumerate(lower)
        )

    async def request(self, kind, chain, params=None, body=None, ttl=0):
        self.validate(chain)
        key = os.getenv("WHALE_BIRDEYE_KEY", "")
        if not key:
            raise ValueError("WHALE_BIRDEYE_KEY required")
        if kind not in ENDPOINTS:
            raise ValueError("Birdeye endpoint not in verified Standard allowlist")
        if kind in ("holders", "portfolio", "balance") and chain != "solana":
            raise ValueError("This Birdeye adapter is documented for Solana only")
        method, path, cost = ENDPOINTS[kind]
        cache_key = (
            "birdeye_cache:"
            + hashlib.sha256(
                json.dumps([kind, chain, params, body], sort_keys=True).encode()
            ).hexdigest()[:40]
        )
        async with self.cache_locks.setdefault(cache_key, asyncio.Lock()):
            cached = self.store.get(cache_key)
            if ttl and cached and time.time() - cached["retrieved_epoch"] < ttl:
                return cached["body"]

            async def operation():
                # Recovery's source lock serializes all Birdeye calls, including retries.
                wait = max(0, self.last_request + 1.05 - asyncio.get_running_loop().time())
                await asyncio.sleep(wait)
                self.store.reserve_budgets(
                    [
                        (
                            "birdeye_daily",
                            now()[:10],
                            cost,
                            min(30000, int(os.getenv("WHALE_BIRDEYE_DAILY_CU", "1000"))),
                        ),
                        (
                            "birdeye",
                            now()[:7],
                            cost,
                            min(30000, int(os.getenv("WHALE_BIRDEYE_MONTHLY_CU", "27000"))),
                        ),
                    ]
                )
                self.last_request = asyncio.get_running_loop().time()
                response = await self.client.request(
                    method,
                    "https://public-api.birdeye.so" + path,
                    headers={"X-API-KEY": key, "x-chain": chain, "Accept": "application/json"},
                    params=params,
                    json=body,
                )
                response.raise_for_status()
                result = response.json(parse_float=Decimal)
                if (
                    not isinstance(result, dict)
                    or result.get("success") is not True
                    or result.get("data") is None
                ):
                    raise ValueError("Birdeye returned unsuccessful or unavailable data")
                # Store Decimal values as strings, preserving token/price precision.
                result = json.loads(json.dumps(result, default=str))
                result["_retrieved_at"] = now()
                return result

            result = await self.recovery.run("birdeye", operation)
            self.store.put(cache_key, {"retrieved_epoch": time.time(), "body": result})
            return result

    async def price(self, chain, asset):
        asset = self.canonical(chain, asset)
        params = {"address": asset, "include_liquidity": "true"}
        if chain == "solana":
            params["ui_amount_mode"] = "raw"
        body = await self.request(
            "price",
            chain,
            params,
            ttl=min(600, int(os.getenv("WHALE_BIRDEYE_PRICE_CACHE_SECONDS", "600"))),
        )
        data = body["data"]
        value = Decimal(str(data["value"]))
        if not value.is_finite() or value <= 0:
            raise ValueError("Birdeye price unavailable")
        timestamp = data.get("updateUnixTime")
        if timestamp is None or not -30 <= time.time() - float(timestamp) <= int(
            os.getenv("WHALE_BIRDEYE_MAX_PRICE_AGE_SECONDS", "900")
        ):
            raise ValueError("Birdeye price stale or missing timestamp")
        liquidity = Decimal(str(data.get("liquidity", 0)))
        if not liquidity.is_finite() or liquidity < Decimal(
            os.getenv("WHALE_MIN_PRICE_LIQUIDITY_USD", "1000")
        ):
            raise ValueError("Birdeye price has insufficient verified liquidity")
        return {
            "price_usd": str(value),
            "price_source": "birdeye",
            "price_updated_at": datetime.fromtimestamp(float(timestamp), timezone.utc).isoformat(),
            "price_retrieved_at": body["_retrieved_at"],
            "price_liquidity_usd": str(liquidity),
            "valuation_status": "current_reference_quote; not trade execution price",
        }

    async def balance(self, row):
        if row["chain"] != "solana" or row["asset"] == "native":
            raise ValueError(
                "Birdeye token-balance fallback supports Solana mints; wrapped SOL is not native SOL"
            )
        self.validate("solana", row["address"])
        self.validate("solana", row["asset"])
        body = await self.request(
            "balance", "solana", body={"wallet": row["address"], "token_addresses": [row["asset"]]}
        )
        items = body["data"]
        if (
            not isinstance(items, list)
            or len(items) != 1
            or not isinstance(items[0], dict)
            or items[0].get("address") != row["asset"]
        ):
            raise ValueError("Birdeye omitted token balance; absence is not zero")
        item = items[0]
        if item.get("network") not in (None, "solana"):
            raise ValueError("Birdeye balance network does not match requested chain")
        decimals = int(item["decimals"])
        if decimals != row["decimals"] or not 0 <= decimals <= 36:
            raise ValueError("Birdeye decimals disagree with watchlist")
        raw = Decimal(str(item["balance"]))
        if not raw.is_finite() or raw < 0 or raw != raw.to_integral_value():
            raise ValueError("Invalid Birdeye base-unit balance")
        with localcontext() as context:
            context.prec = 100
            amount = raw / Decimal(10**decimals)
        return {
            "amount": str(amount),
            "source": "birdeye",
            "source_observed_at": now(),
            "amount_basis": "raw_base_units",
        }

    async def research(self, kind, chain, address=None, count=100):
        self.validate(chain, address)
        if address is not None:
            address = self.canonical(chain, address)
        if not 1 <= count <= 100:
            raise ValueError("Birdeye research count: 1..100")
        ttl = int(os.getenv("WHALE_BIRDEYE_RESEARCH_CACHE_SECONDS", "86400"))
        # Hash includes ranking direction so profit and loss candidates never share cache.
        result_key = (
            "birdeye_research:"
            + hashlib.sha256(json.dumps([kind, chain, address, count]).encode()).hexdigest()[:40]
        )
        cached = self.store.get(result_key)
        if cached and time.time() - cached["cached_epoch"] < ttl:
            return cached["result"]
        items, complete = [], False
        endpoint = "traders" if kind == "losers" else kind
        if kind not in ("traders", "losers", "holders", "tokens", "portfolio"):
            raise ValueError("Birdeye research: traders, losers, holders, tokens, portfolio")
        if kind != "tokens" and address is None:
            raise ValueError("Birdeye research requires token/wallet address")
        page_size = 10 if endpoint == "traders" else 50 if endpoint == "tokens" else 100
        metadata = {}
        page_times = []
        for offset in range(0, count, page_size):
            limit = min(page_size, count - offset)
            params = {"offset": offset, "limit": limit}
            if endpoint == "traders":
                params.update(
                    address=address,
                    time_frame="24h",
                    sort_by="realized_pnl",
                    sort_type="asc" if kind == "losers" else "desc",
                )
            elif endpoint == "holders":
                params.update(address=address, mode="wallet", ui_amount_mode="raw")
            elif endpoint == "tokens":
                params.update(sort_by="v24hUSD", sort_type="desc", min_liquidity=1000)
            else:
                params.update(wallet=address, sort_type="desc", flags="include_low_liquidity")
            body = await self.request(endpoint, chain, params, ttl=ttl)
            page_times.append(body["_retrieved_at"])
            data = body["data"]
            rows = data["tokens"] if endpoint == "tokens" else data["items"]
            if not isinstance(rows, list):
                raise ValueError("Malformed Birdeye research rows")
            items.extend(rows)
            if endpoint == "portfolio":
                metadata = {
                    k: data.get(k)
                    for k in ("wallet", "total_value", "current_timestamp", "currency")
                }
            if len(rows) < limit:
                complete = True
                break
        result = {
            "source": "birdeye",
            "kind": kind,
            "chain": chain,
            "address": address,
            "retrieved_at": now(),
            "items": items[:count],
            "requested_count": count,
            "count": min(count, len(items)),
            "list_exhausted": complete,
            "page_retrieved_at": page_times,
            "snapshot_consistency": "paginated reads; ranking can move during collection",
            "ranking_basis": "24h realized PnL; provider snapshot, not ten-minute booked PnL"
            if endpoint == "traders"
            else "provider research snapshot",
            "watchlist_changed": False,
            **metadata,
        }
        self.store.put(result_key, {"cached_epoch": time.time(), "result": result})
        return result
