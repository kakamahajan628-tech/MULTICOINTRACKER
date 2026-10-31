"""Optional official API adapters. Calls are manual until budget/reporting rules are agreed."""

import os
from .core import now
import json
from .recovery import Recovery

SOURCES = [
    {
        "name": "DEX Screener",
        "role": "Free keyless reference token USD valuation",
        "status": "Exact asset matching and liquid-pair selection; reference quote, not execution price",
        "docs": "https://docs.dexscreener.com/api/reference",
    },
    {
        "name": "Free source policy",
        "role": "Priority and fallback",
        "status": "Helius → configured free RPCs → public Solana RPC → budget-limited Nansen; failures remain visible. Dune is research only; no cross-venue position substitution.",
        "docs": "https://www.helius.dev/docs/billing/plans",
    },
    {
        "name": "Hyperliquid",
        "role": "Public futures positions, risk and realized fill PnL",
        "status": "Position + mark + liquidation fields; bounded retries and budget-limited Nansen same-venue fallback; fills outages preserve position",
        "docs": "https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals",
    },
    {
        "name": "Binance Public Markets",
        "role": "Free unsigned perpetual market context",
        "status": "Mark/index, 24h ticker, OI, taker flow and account/top-trader ratios; optional separate market API key where required; no private positions",
        "docs": "https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Mark-Price",
    },
    {
        "name": "Bybit Public Markets",
        "role": "Free keyless perpetual market context",
        "status": "Mark/index, funding, OI, 24h quote turnover and account long/short ratio; public aggregates only",
        "docs": "https://bybit-exchange.github.io/docs/v5/market/tickers",
    },
    {
        "name": "OKX Public Markets",
        "role": "Free keyless perpetual market context",
        "status": "Mark, funding interval, base/contract/USD OI, ticker and coin-level account long/short ratio; public aggregates only",
        "docs": "https://www.okx.com/docs-v5/en/",
    },
    {
        "name": "Binance / Bybit / OKX",
        "role": "Authorized private futures accounts",
        "status": "CCXT read-only positions and realized-fill adapters; keys required. Bybit BustTrade evidence flags liquidation; incomplete histories remain unknown.",
        "docs": "https://docs.ccxt.com",
    },
    {
        "name": "Helius",
        "role": "Solana RPC + enhanced webhook evidence",
        "status": "RPC and authenticated receiver implemented; provider webhook registration required",
        "docs": "https://www.helius.dev/docs/webhooks",
    },
    {
        "name": "Nansen",
        "role": "Balances and same-venue Hyperliquid position fallback",
        "status": "Fresh-timestamp perp fallback and on-demand balance/label adapters; key and credits required; no private exchange substitution",
        "docs": "https://docs.nansen.ai/api/profiler/address-perp-positions",
    },
    {
        "name": "Dune",
        "role": "Custom SQL ranking / PnL results",
        "status": "On-demand latest-results adapter implemented; query ID/key required; no automatic watchlist changes",
        "docs": "https://docs.dune.com/api-reference/executions/endpoint/get-query-result",
    },
    {
        "name": "Birdeye",
        "role": "Standard/free token prices, Solana balance fallback and research",
        "status": "Official price/balance/Top Traders/losers/holders/token-list/portfolio adapters; 1 RPS, CU caps and cache; API key/live entitlement required",
        "docs": "https://data.birdeye.so/docs/guides/data-accessibility-by-packages",
    },
    {
        "name": "Arkham",
        "role": "Entity labels and fund flows",
        "status": "Access application and account-specific API documentation required",
        "docs": "https://info.arkm.com/announcements/the-new-arkham-api",
    },
    {
        "name": "GMGN",
        "role": "Smart-wallet reference / approved data access",
        "status": "Pending approved API access and schema; no unofficial endpoints",
        "docs": "https://docs.gmgn.ai/index/gmgn-agent-api",
    },
    {
        "name": "Bubblemaps",
        "role": "Holder cluster analysis",
        "status": "Planned; API entitlement/schema required; clusters are evidence, not proof of shared ownership",
        "docs": "https://bubblemaps.io",
    },
    {
        "name": "DeBank",
        "role": "EVM portfolio enrichment",
        "status": "Planned; official API key/entitlement required",
        "docs": "https://docs.cloud.debank.com",
    },
    {
        "name": "Zerion",
        "role": "Tracked fungible balance fallback",
        "status": "Budget-limited wallet-position adapter implemented; free-start account key/entitlement required; no missing token inferred as zero",
        "docs": "https://developers.zerion.io/quickstart",
    },
    {
        "name": "DEXCheck",
        "role": "Trader analytics",
        "status": "Planned; endpoint/schema and access need verification",
        "docs": "https://dexcheck.ai",
    },
    {
        "name": "Flipside",
        "role": "Custom query analytics",
        "status": "Planned; current service/API availability needs verification",
        "docs": "https://flipsidecrypto.xyz",
    },
    {
        "name": "Chainalysis",
        "role": "Entity intelligence",
        "status": "Planned only if licensed API access supplied",
        "docs": "https://www.chainalysis.com",
    },
]


def source_activation(health):
    """Configured is not active: only successful runtime requests earn active status."""
    public = {
        "DEX Screener": "dexscreener",
        "Hyperliquid": "hyperliquid",
        **{
            v.title() + " Public Markets" if v != "okx" else "OKX Public Markets": "market_" + v
            for v in ("binance", "bybit", "okx")
        },
    }
    keyed = {
        "Helius": (
            "helius",
            bool(os.getenv("WHALE_HELIUS_KEY") or "helius" in os.getenv("WHALE_RPC_SOLANA", "")),
        ),
        "Nansen": ("nansen", bool(os.getenv("WHALE_NANSEN_KEY"))),
        "Zerion": ("zerion", bool(os.getenv("WHALE_ZERION_KEY"))),
        "Birdeye": ("birdeye", bool(os.getenv("WHALE_BIRDEYE_KEY"))),
        "Dune": ("dune", bool(os.getenv("WHALE_DUNE_KEY"))),
    }
    result = []
    for item in SOURCES:
        row = dict(item)
        name = item["name"]
        if name in public:
            row.update(health.get(public[name], {"state": "enabled_unverified"}))
            if (
                name.endswith("Public Markets")
                and os.getenv("WHALE_MARKETS_ENABLED", "true").lower() != "true"
            ):
                row["state"] = "disabled"
        elif name in keyed:
            key, configured = keyed[name]
            row.update(
                health.get(key, {"state": "enabled_unverified"})
                if configured
                else {"state": "awaiting_key"}
            )
        elif name == "Binance / Bybit / OKX":
            try:
                accounts = json.loads(os.getenv("WHALE_EXCHANGE_ACCOUNTS_JSON", "{}"))
            except ValueError:
                accounts = {}
            row["state"] = "enabled_unverified" if accounts else "awaiting_authorized_account"
            row["venues"] = {
                venue: health.get(
                    venue + "_positions",
                    {
                        "state": "enabled_unverified"
                        if any(k.startswith(venue + ":") for k in accounts)
                        else "awaiting_authorized_account"
                    },
                )
                for venue in ("binance", "bybit", "okx")
            }
        elif name == "Free source policy":
            row["state"] = "configured_policy"
        else:
            row["state"] = "adapter_or_access_pending"
        result.append(row)
    return result


class OfficialSources:
    def __init__(self, client, store=None, recovery=None):
        self.client = client
        self.store = store
        self.recovery = recovery or (Recovery(store) if store else None)

    async def request(self, source, operation, cost):
        if self.recovery:
            limit = (
                int(os.getenv("WHALE_NANSEN_DAILY_CREDITS", "10"))
                if source == "nansen"
                else int(os.getenv("WHALE_DUNE_DAILY_CALLS", "5"))
            )
            try:
                return await self.recovery.run(source, operation, (now()[:10], cost, limit))
            finally:
                self.recovery.flush()
        return await operation()

    async def nansen(self, row, kind="balances"):
        key = os.getenv("WHALE_NANSEN_KEY", "")
        if not key:
            raise ValueError("WHALE_NANSEN_KEY required")
        endpoint = "current-balance" if kind == "balances" else "labels"
        all_data, complete = [], False
        for page in range(1, 11):

            async def request(page=page):
                response = await self.client.post(
                    "https://api.nansen.ai/api/v1/profiler/address/" + endpoint,
                    headers={"apiKey": key},
                    json={
                        "address": row["address"],
                        "chain": "bnb" if row["chain"] == "bsc" else row["chain"],
                        "pagination": {"page": page, "per_page": 100},
                    },
                )
                response.raise_for_status()
                return response

            response = await self.request("nansen", request, 1 if kind == "balances" else 100)
            body = response.json()
            all_data.extend(body["data"])
            if body.get("pagination", {}).get("is_last_page") is True:
                complete = True
                break
        return {
            "source": "nansen",
            "kind": kind,
            "watch_id": row["id"],
            "retrieved_at": now(),
            "complete": complete,
            "data": all_data,
        }

    async def dune(self, query_id):
        key = os.getenv("WHALE_DUNE_KEY", "")
        if not key:
            raise ValueError("WHALE_DUNE_KEY required")
        query_id = int(query_id)
        if query_id < 1:
            raise ValueError("Query ID must be positive")

        async def request():
            response = await self.client.get(
                f"https://api.dune.com/api/v1/query/{query_id}/results",
                headers={"X-DUNE-API-KEY": key},
                params={"limit": 1000},
            )
            response.raise_for_status()
            return response

        response = await self.request("dune", request, 1)
        body = response.json()
        return {
            "source": "dune",
            "retrieved_at": now(),
            "complete": not bool(body.get("next_uri") or body.get("next_offset")),
            "result": body,
        }
