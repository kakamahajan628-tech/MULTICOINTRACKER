import asyncio
import json
import os
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from urllib.parse import urlparse
from .core import now
from .providers import RPCProvider
from .recovery import Recovery
from .birdeye import Birdeye


class BudgetRPC(RPCProvider):
    def __init__(self, client, endpoints, store, name, budget, rps=3, recovery=None):
        super().__init__(client, endpoints, rps)
        self.store, self.name, self.budget = store, name, budget
        self.recovery = recovery or Recovery(store)

    async def rpc(self, chain, method, params):
        async def request():
            return await super(BudgetRPC, self).rpc(chain, method, params)

        return await self.recovery.run(self.name, request, (now()[:7], 0, self.budget), attempts=2)

    async def before_request(self):
        self.store.reserve_budget(self.name, now()[:7], 1, self.budget)


class FreePriorityProvider:
    def __init__(self, client, store, recovery=None):
        self.client, self.store = client, store
        self.recovery = recovery or Recovery(store)
        helius_key = os.getenv("WHALE_HELIUS_KEY", "")
        self.birdeye = Birdeye(client, store, self.recovery)
        configured_sol = os.getenv("WHALE_RPC_SOLANA", "")
        helius = (
            f"https://mainnet.helius-rpc.com/?api-key={helius_key}"
            if helius_key
            else configured_sol
            if "helius" in (urlparse(configured_sol).hostname or "")
            else ""
        )
        self.chains = {}
        for chain in ("solana", "ethereum", "arbitrum", "base", "polygon", "bsc"):
            candidates = []
            if chain == "solana" and helius:
                candidates.append(
                    ("helius", helius, int(os.getenv("WHALE_HELIUS_RPC_BUDGET", "850000")))
                )
            primary = os.getenv("WHALE_RPC_" + chain.upper(), "")
            if primary and primary != helius:
                candidates.append(
                    (
                        chain + "_configured",
                        primary,
                        int(os.getenv("WHALE_RPC_MONTHLY_BUDGET", "1000000")),
                    )
                )
            for n, endpoint in enumerate(
                os.getenv("WHALE_RPC_" + chain.upper() + "_FALLBACKS", "").split(",")
            ):
                if endpoint.strip():
                    candidates.append(
                        (
                            chain + "_fallback_" + str(n),
                            endpoint.strip(),
                            int(os.getenv("WHALE_RPC_MONTHLY_BUDGET", "1000000")),
                        )
                    )
            if chain == "solana":
                candidates.append(
                    (
                        "solana_public",
                        "https://api.mainnet.solana.com",
                        int(os.getenv("WHALE_PUBLIC_RPC_BUDGET", "150000")),
                    )
                )
            self.chains[chain] = [
                BudgetRPC(
                    client,
                    {chain: url},
                    store,
                    name,
                    budget,
                    float(os.getenv("WHALE_RPC_RPS", "3")),
                    self.recovery,
                )
                for name, url, budget in candidates
            ]
        self.attempts, self.cache, self.native_cache = [], {}, {}
        self.price_cache, self.price_gate, self.price_last = {}, asyncio.Lock(), 0.0

    async def begin_cycle(self, rows):
        self.attempts, self.cache, self.native_cache = [], {}, {}
        self.price_cache = {}
        for sources in self.chains.values():
            for source in sources:
                source.begin_cycle()
        addresses = list(
            dict.fromkeys(
                r["address"]
                for r in rows
                if r["chain"] == "solana"
                and r["asset"] == "native"
                and r["group"].startswith("Spot")
            )
        )
        for offset in range(0, len(addresses), 100):
            batch = addresses[offset : offset + 100]
            for source in self.chains["solana"]:
                try:
                    result = await source.rpc(
                        "solana",
                        "getMultipleAccounts",
                        [
                            batch,
                            {
                                "commitment": "finalized",
                                "encoding": "base64",
                                "dataSlice": {"offset": 0, "length": 0},
                            },
                        ],
                    )
                    if len(result["value"]) != len(batch):
                        raise ValueError("Incomplete account batch")
                    for address, account in zip(batch, result["value"], strict=True):
                        self.native_cache[address] = {
                            "amount": str(
                                Decimal(account["lamports"] if account else 0) / Decimal(10**9)
                            ),
                            "source": source.name,
                        }
                    break
                except Exception:
                    self.attempts.append(
                        {
                            "source": source.name,
                            "status": "failed_or_budget_exhausted",
                            "scope": "solana_native_batch",
                        }
                    )

    async def inventory(self, address):
        from .portfolio import read_inventory

        for source in self.chains["solana"]:
            try:
                result = await read_inventory(source.rpc, address)
                return {**result, "source": source.name}
            except Exception:
                self.attempts.append(
                    {
                        "source": source.name,
                        "status": "failed_or_budget_exhausted",
                        "scope": "solana_token_inventory",
                    }
                )
        raise ValueError("Complete token inventory unavailable")

    async def observe(self, row):
        key = (row["chain"], row["address"], row["asset"])
        if key not in self.cache:
            self.cache[key] = asyncio.create_task(self._observe(row))
        result = dict(await self.cache[key])
        if os.getenv("WHALE_PRICES_ENABLED", "true").lower() == "true":
            try:
                result.update(await self.price(row))
                with localcontext() as context:
                    context.prec = 100
                    result["value_usd"] = str(
                        Decimal(result["amount"]) * Decimal(result["price_usd"])
                    )
            except Exception:
                result["valuation_status"] = "price_unavailable"
        return result

    async def price(self, row):
        asset = row["asset"]
        if asset == "native":
            asset = json.loads(os.getenv("WHALE_NATIVE_PRICE_TOKENS_JSON", "{}")).get(row["chain"])
            if not asset:
                raise ValueError("Native price proxy token not configured")
        key = (row["chain"], asset)
        if key not in self.price_cache:
            self.price_cache[key] = asyncio.create_task(self._price(row["chain"], asset))
        return await self.price_cache[key]

    async def _price(self, chain, asset):
        if os.getenv("WHALE_BIRDEYE_KEY"):
            try:
                return await self.birdeye.price(chain, asset)
            except Exception:
                self.attempts.append(
                    {
                        "source": "birdeye",
                        "scope": "price",
                        "status": "failed_stale_or_budget_exhausted",
                    }
                )
        async with self.price_gate:
            loop = asyncio.get_running_loop()
            await asyncio.sleep(max(0, self.price_last + 0.3 - loop.time()))
            self.price_last = loop.time()

        async def request():
            response = await self.client.get(
                f"https://api.dexscreener.com/token-pairs/v1/{chain}/{asset}"
            )
            response.raise_for_status()
            return response

        response = await self.recovery.run(
            "dexscreener",
            request,
            (now()[:7], 1, int(os.getenv("WHALE_PRICE_MONTHLY_CALLS", "100000"))),
        )
        pairs = [
            p
            for p in response.json()
            if p.get("chainId") == chain
            and (
                p.get("baseToken", {}).get("address") == asset
                if chain == "solana"
                else str(p.get("baseToken", {}).get("address")).lower() == asset.lower()
            )
            and p.get("priceUsd")
            and Decimal(str((p.get("liquidity") or {}).get("usd") or 0))
            >= Decimal(os.getenv("WHALE_MIN_PRICE_LIQUIDITY_USD", "1000"))
        ]
        if not pairs:
            raise ValueError("No matching liquid pair")
        pair = max(pairs, key=lambda p: Decimal(str(p["liquidity"]["usd"])))
        price = Decimal(str(pair["priceUsd"]))
        if not price.is_finite() or price <= 0:
            raise ValueError("Invalid quote")
        return {
            "price_usd": str(price),
            "price_source": "dexscreener",
            "price_pair": pair.get("pairAddress"),
            "price_retrieved_at": now(),
            "valuation_status": "current_reference_quote; not trade execution price",
        }

    async def _observe(self, row):
        if (
            row["chain"] == "solana"
            and row["asset"] == "native"
            and row["address"] in self.native_cache
        ):
            return self.native_cache[row["address"]]
        for source in self.chains.get(row["chain"], []):
            try:
                amount = await source.balance(row)
                return {"amount": str(amount), "source": source.name}
            except Exception:
                self.attempts.append(
                    {
                        "source": source.name,
                        "status": "failed_or_budget_exhausted",
                        "watch_id": row["id"],
                    }
                )
        if os.getenv("WHALE_BIRDEYE_KEY") and row["chain"] == "solana" and row["asset"] != "native":
            try:
                return await self.birdeye.balance(row)
            except Exception:
                self.attempts.append(
                    {
                        "source": "birdeye",
                        "scope": "token_balance",
                        "status": "failed_or_budget_exhausted",
                        "watch_id": row["id"],
                    }
                )
        if os.getenv("WHALE_ZERION_KEY") and row["asset"] != "native":
            try:

                async def request():
                    response = await self.client.get(
                        f"https://api.zerion.io/v1/wallets/{row['address']}/positions/",
                        auth=(os.getenv("WHALE_ZERION_KEY"), ""),
                        params={
                            "filter[chain_ids]": row["chain"],
                            "filter[position_types]": "wallet",
                            "page[size]": 100,
                        },
                    )
                    response.raise_for_status()
                    return response

                response = await self.recovery.run(
                    "zerion",
                    request,
                    (now()[:10], 1, int(os.getenv("WHALE_ZERION_DAILY_CALLS", "10"))),
                )
                body = response.json()
                for item in body["data"]:
                    attributes = item["attributes"]
                    if (
                        item.get("relationships", {}).get("chain", {}).get("data", {}).get("id")
                        != row["chain"]
                    ):
                        continue
                    implementations = attributes.get("fungible_info", {}).get("implementations", [])
                    if any(
                        t.get("chain_id") == row["chain"]
                        and (
                            str(t.get("address")) == row["asset"]
                            if row["chain"] == "solana"
                            else str(t.get("address")).lower() == row["asset"].lower()
                        )
                        for t in implementations
                    ):
                        quantity = attributes["quantity"]
                        return {
                            "amount": str(
                                Decimal(quantity["int"]) / Decimal(10 ** int(quantity["decimals"]))
                            ),
                            "source": "zerion",
                            "source_updated_at": attributes.get("updated_at"),
                        }
            except Exception:
                self.attempts.append(
                    {
                        "source": "zerion",
                        "status": "failed_or_budget_exhausted",
                        "watch_id": row["id"],
                    }
                )
        if os.getenv("WHALE_NANSEN_KEY"):
            budget = int(os.getenv("WHALE_NANSEN_DAILY_CREDITS", "10"))

            # One page per fallback: absence is not zero unless pagination is complete.
            async def request():
                response = await self.client.post(
                    "https://api.nansen.ai/api/v1/profiler/address/current-balance",
                    headers={"apiKey": os.getenv("WHALE_NANSEN_KEY")},
                    json={
                        "address": row["address"],
                        "chain": "bnb" if row["chain"] == "bsc" else row["chain"],
                        "pagination": {"page": 1, "per_page": 100},
                    },
                )
                response.raise_for_status()
                return response

            response = await self.recovery.run("nansen", request, (now()[:10], 1, budget))
            body = response.json()
            matches = [
                t
                for t in body["data"]
                if t.get("token_address") == row["asset"]
                or (
                    row["asset"] == "native"
                    and t.get("token_symbol") == row["coin"]
                    and t.get("token_address")
                    in (
                        "native",
                        "0x0000000000000000000000000000000000000000",
                        "11111111111111111111111111111111",
                    )
                )
            ]
            if len(matches) == 1:
                return {
                    "amount": str(matches[0]["token_amount"]),
                    "price_usd": matches[0].get("price_usd"),
                    "source": "nansen",
                    "complete": body.get("pagination", {}).get("is_last_page") is True,
                }
        raise ValueError("No free source returned a verified tracked-asset balance")


class FuturesProvider:
    """Public Hyperliquid or authorized Binance/Bybit/OKX accounts, never cross-venue substitution."""

    def __init__(self, client, store, recovery=None):
        self.client, self.store = client, store
        self.recovery = recovery or Recovery(store)
        self.cache, self.exchanges = {}, {}
        self.gate = asyncio.Lock()
        self.last_request = 0.0

    def begin_cycle(self):
        self.cache = {}

    async def info(self, body):
        async with self.gate:
            loop = asyncio.get_running_loop()
            await asyncio.sleep(max(0, self.last_request + 0.5 - loop.time()))
            self.last_request = loop.time()

        async def request():
            response = await self.client.post("https://api.hyperliquid.xyz/info", json=body)
            response.raise_for_status()
            return response.json()

        return await self.recovery.run(
            "hyperliquid",
            request,
            (now()[:7], 1, int(os.getenv("WHALE_HYPERLIQUID_MONTHLY_CALLS", "1000000"))),
        )

    async def observe(self, row, start, end):
        platform = row.get("platform", "").lower()
        if platform == "hyperliquid":
            if row["chain"] == "solana":
                raise ValueError("Hyperliquid requires an EVM address")
            dex = row["coin"].split(":")[0] if ":" in row["coin"] else ""
            key = ("hyperliquid", row["address"], dex)
            if key not in self.cache:
                self.cache[key] = asyncio.create_task(
                    self.info({"type": "clearinghouseState", "user": row["address"], "dex": dex})
                )
            source = "hyperliquid"
            try:
                state = await self.cache[key]
            except Exception:
                if dex or not os.getenv("WHALE_NANSEN_KEY"):
                    raise
                fallback_key = ("nansen_perps", row["address"])
                if fallback_key not in self.cache:
                    self.cache[fallback_key] = asyncio.create_task(
                        self.nansen_positions(row["address"])
                    )
                state = await self.cache[fallback_key]
                source = "nansen_hyperliquid"
            if not isinstance(state.get("assetPositions"), list):
                raise ValueError("Incomplete position response")
            position = next(
                (
                    p["position"]
                    for p in state["assetPositions"]
                    if p["position"]["coin"] == row["coin"]
                ),
                None,
            )
            if position is None:
                meta_key = ("hyperliquid_meta", dex)
                if meta_key not in self.cache:
                    self.cache[meta_key] = asyncio.create_task(
                        self.info({"type": "meta", "dex": dex})
                    )
                meta = await self.cache[meta_key]
                if not isinstance(meta, dict) or row["coin"] not in {
                    asset["name"] for asset in meta.get("universe", [])
                }:
                    raise ValueError(
                        "Unrecognized futures coin; absence cannot establish a nil position"
                    )
            amount = str(position["szi"]) if position else "0"
            result = {
                "amount": amount,
                "source": source,
                "unrealized_pnl": position.get("unrealizedPnl") if position else "0",
                "entry_price": position.get("entryPx") if position else None,
                "notional_usd": position.get("positionValue") if position else "0",
                "realized_pnl": None,
                "liquidated": None,
                "fills": [],
                "fill_coverage": "baseline",
            }
            result["risk_legs"] = []
            if position:
                mark = None
                try:
                    mark_key = ("hyperliquid_marks", dex)
                    if mark_key not in self.cache:
                        self.cache[mark_key] = asyncio.create_task(
                            self.info({"type": "metaAndAssetCtxs", "dex": dex})
                        )
                    contexts = await self.cache[mark_key]
                    for asset, context in zip(contexts[0]["universe"], contexts[1], strict=True):
                        if asset["name"] == row["coin"]:
                            mark = context["markPx"]
                            break
                except Exception:
                    pass
                if source == "nansen_hyperliquid" and not state.get("source_observed_at"):
                    mark = None
                result["risk_legs"] = [
                    {
                        "side": "long" if Decimal(amount) > 0 else "short",
                        "mark_price": mark,
                        "liquidation_price": position.get("liquidationPx"),
                        "entry_price": position.get("entryPx"),
                        "leverage": position.get("leverage"),
                        "size": amount,
                        "risk_observed_at": state.get("source_observed_at") or now(),
                        "mark_source": "hyperliquid_mark",
                    }
                ]
            result["source_observed_at"] = state.get("source_observed_at") or now()
            previous = self.store.get("position:" + row["id"])
            if previous:
                fills_key = ("hyperliquid_fills", row["address"])
                from datetime import datetime

                # Position observation baseline, not process start, bounds realized PnL.
                start_ms = int(datetime.fromisoformat(start).timestamp() * 1000)
                end_ms = int(datetime.fromisoformat(end).timestamp() * 1000)
                if fills_key not in self.cache:
                    self.cache[fills_key] = asyncio.create_task(
                        self.info(
                            {
                                "type": "userFillsByTime",
                                "user": row["address"],
                                "startTime": start_ms,
                                "endTime": end_ms,
                            }
                        )
                    )
                try:
                    fills = await self.cache[fills_key]
                    if not isinstance(fills, list):
                        raise ValueError("Incomplete fills response")
                    matched = [
                        f
                        for f in fills
                        if f["coin"] == row["coin"] and start_ms < f["time"] <= end_ms
                    ]
                    from .orders import unique_fills

                    matched = unique_fills(matched)
                    result["fills"] = matched
                    result["fill_coverage"] = (
                        "complete_window" if len(fills) < 2000 else "truncated"
                    )
                    if len(fills) < 2000:
                        result["realized_pnl"] = str(
                            sum((Decimal(f["closedPnl"]) for f in matched), Decimal(0))
                        )
                except Exception:
                    result["fill_coverage"] = "unavailable; position snapshot preserved"
                # Do not infer liquidation from a zero position or a liquidation price.
            return result
        if platform not in ("binance", "bybit", "okx"):
            raise ValueError("Set platform hyperliquid, binance, bybit or okx")
        accounts = json.loads(os.getenv("WHALE_EXCHANGE_ACCOUNTS_JSON", "{}"))
        config = accounts.get(platform + ":" + row["address"])
        if not config:
            raise ValueError("Authorized read-only exchange account credentials/mapping required")
        if platform + ":" + row["address"] not in self.exchanges:
            import ccxt.async_support as ccxt

            factory = getattr(ccxt, platform)
            self.exchanges[platform + ":" + row["address"]] = factory(
                {**config, "enableRateLimit": True, "options": {"defaultType": "swap"}}
            )
        exchange = self.exchanges[platform + ":" + row["address"]]
        symbol = row["coin"] + "/USDT:USDT"
        await self.recovery.run(platform + "_markets", exchange.load_markets)
        if symbol not in exchange.markets:
            raise ValueError(
                "Unsupported perpetual symbol; absence cannot establish a nil position"
            )
        key = (platform, row["address"], "positions")
        if key not in self.cache:
            self.cache[key] = asyncio.create_task(
                self.recovery.run(platform + "_positions", exchange.fetch_positions)
            )
        positions = [
            p
            for p in await self.cache[key]
            if p.get("symbol") == symbol and Decimal(str(p.get("contracts") or 0)) != 0
        ]
        gross = sum(
            (
                Decimal(str(p["contracts"])) * Decimal(str(p.get("contractSize") or 1))
                for p in positions
            ),
            Decimal(0),
        )
        size = -gross if len(positions) == 1 and positions[0].get("side") == "short" else gross
        result = {
            "amount": str(size),
            "source": platform + "_authorized",
            "positions": positions,
            "direction": "hedged"
            if len(positions) > 1
            else positions[0].get("side")
            if positions
            else "nil",
            "unrealized_pnl": str(
                sum((Decimal(str(p.get("unrealizedPnl") or 0)) for p in positions), Decimal(0))
            ),
            "realized_pnl": None,
            "liquidated": None,
            "fills": [],
            "fill_coverage": "exchange_closed_pnl_adapter_pending",
        }
        result["source_observed_at"] = now()
        result["risk_legs"] = [
            {
                "side": p.get("side"),
                "mark_price": p.get("markPrice"),
                "liquidation_price": p.get("liquidationPrice"),
                "entry_price": p.get("entryPrice"),
                "leverage": p.get("leverage"),
                "size": str(p["contracts"]),
                "risk_observed_at": now(),
                "mark_source": platform + "_position_mark",
            }
            for p in positions
        ]
        if self.store.get("position:" + row["id"]):
            try:
                result.update(
                    await self.recovery.run(
                        platform + "_fills",
                        lambda: self.exchange_fills(exchange, platform, row, symbol, start, end),
                    )
                )
            except Exception:
                result["fill_coverage"] = "unavailable; position snapshot preserved"
        return result

    async def nansen_positions(self, address):
        async def request():
            response = await self.client.post(
                "https://api.nansen.ai/api/v1/profiler/perp-positions",
                headers={"apiKey": os.getenv("WHALE_NANSEN_KEY")},
                json={"address": address},
            )
            response.raise_for_status()
            body = response.json()["data"]
            if not isinstance(body.get("assetPositions"), list):
                raise ValueError("Incomplete fallback positions")
            positions = []
            for item in body["assetPositions"]:
                p = item["position"]
                positions.append(
                    {
                        "position": {
                            "coin": p["token_symbol"],
                            "szi": p["size"],
                            "entryPx": p.get("entry_price_usd"),
                            "liquidationPx": p.get("liquidation_price_usd"),
                            "positionValue": p.get("position_value_usd"),
                            "unrealizedPnl": p.get("unrealized_pnl_usd"),
                            "leverage": {
                                "value": p.get("leverage_value"),
                                "type": p.get("leverage_type"),
                            },
                        }
                    }
                )
            at = (
                datetime.fromtimestamp(body["time"] / 1000, timezone.utc).isoformat()
                if body.get("time")
                else None
            )
            if at is None or not -30 <= (
                datetime.now(timezone.utc) - datetime.fromisoformat(at)
            ).total_seconds() <= int(os.getenv("WHALE_RISK_MAX_AGE_SECONDS", "900")):
                raise ValueError("Fallback position snapshot has no fresh timestamp")
            return {"assetPositions": positions, "source_observed_at": at}

        return await self.recovery.run(
            "nansen", request, (now()[:10], 1, int(os.getenv("WHALE_NANSEN_DAILY_CREDITS", "10")))
        )

    async def exchange_fills(self, exchange, platform, row, symbol, start, end):
        from datetime import datetime

        start_ms, end_ms = (
            int(datetime.fromisoformat(start).timestamp() * 1000),
            int(datetime.fromisoformat(end).timestamp() * 1000),
        )
        if platform == "bybit":
            response = await exchange.privateGetV5PositionClosedPnl(
                {
                    "category": "linear",
                    "symbol": row["coin"] + "USDT",
                    "startTime": start_ms,
                    "endTime": end_ms,
                    "limit": 100,
                }
            )
            if response.get("retCode") != 0:
                raise ValueError("Closed PnL unavailable")
            records = response["result"]["list"]
            complete = not response["result"].get("nextPageCursor")
            # An order's closedPnl can be updated across partial fills. Count only newly
            # created orders wholly inside this window; cumulative updates remain unknown.
            exact = [
                r
                for r in records
                if start_ms < int(r["createdTime"]) <= end_ms and int(r["updatedTime"]) <= end_ms
            ]
            pnl = (
                sum((Decimal(r["closedPnl"]) for r in exact), Decimal(0))
                if complete and len(exact) == len(records)
                else None
            )
            executions = await exchange.fetch_my_trades(symbol, start_ms, 100, {"endTime": end_ms})
            liquidated = any(
                t.get("info", {}).get("execType") == "BustTrade"
                for t in executions
                if start_ms < int(t["timestamp"]) <= end_ms
            )
            return {
                "realized_pnl": str(pnl) if pnl is not None else None,
                "liquidated": True if liquidated else None,
                "fills": records,
                "fill_coverage": "complete_closed_orders"
                if pnl is not None
                else "partial_or_updated_orders",
            }
        limit = 1000 if platform == "binance" else 100
        params = {"endTime": end_ms} if platform == "binance" else {"until": end_ms}
        trades = await exchange.fetch_my_trades(symbol, start_ms, limit, params)
        exact = [
            t
            for t in trades
            if t.get("timestamp") is not None and start_ms < int(t["timestamp"]) <= end_ms
        ]
        field = "realizedPnl" if platform == "binance" else "fillPnl"
        complete = len(trades) < limit and all(field in t.get("info", {}) for t in exact)
        pnl = sum((Decimal(t["info"][field]) for t in exact), Decimal(0)) if complete else None
        # Binance realizedPnl and OKX fillPnl exclude funding and should not be
        # presented as net all-cost profitability. Liquidation history is separate.
        return {
            "realized_pnl": str(pnl) if pnl is not None else None,
            "liquidated": None,
            "fills": exact,
            "fill_coverage": "complete_fill_window" if complete else "truncated_or_missing_pnl",
            "pnl_basis": "venue_reported_realized_fill_pnl; funding/fees treatment venue-specific",
        }

    async def close(self):
        for exchange in self.exchanges.values():
            await exchange.close()
