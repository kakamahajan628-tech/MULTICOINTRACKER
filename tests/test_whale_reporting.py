import asyncio
from decimal import Decimal
import httpx
from whale_watcher.core import Store
from whale_watcher.reporting import (
    holding_state,
    position_state,
    make_cycle,
    spot_evidence,
    telegram_digest,
)
from whale_watcher.free_sources import FreePriorityProvider, FuturesProvider


def row(group="Spot Profit", **changes):
    return {
        "id": "watch-1",
        "label": "Trader #7",
        "address": "A" * 44,
        "chain": "solana",
        "coin": "TEST",
        "rank": 7,
        "group": group,
        "asset": "Mint",
        "platform": "",
        **changes,
    }


def test_states_do_not_invent_trades_or_liquidation():
    assert holding_state(None, Decimal(0)) == "nil_baseline"
    assert holding_state(Decimal(10), Decimal(0)) == "holding_cleared"
    assert holding_state(Decimal(10), Decimal(5)) == "partial_reduction"
    assert position_state(Decimal(-10), Decimal(-5)) == "partial_close"
    assert position_state(Decimal(10), Decimal(-10)) == "direction_flip"
    assert position_state(Decimal(10), Decimal(0)) == "full_close"
    observations = [
        {**row(), "state": "holding_cleared", "amount": "0", "actions": []},
        {**row("Futures Loss"), "state": "full_close", "amount": "0", "liquidated": None},
    ]
    cycle = make_cycle(
        "cycle-1",
        "2026-10-07T00:00:00+00:00",
        "2026-10-07T00:10:00+00:00",
        "2026-10-07T00:00:00+00:00",
        observations,
    )
    assert len(cycle["reports"]["Spot Profit"]["subreports"]["booking_review"]) == 1
    assert cycle["reports"]["Spot Profit"]["subreports"]["profit_booked"] == []
    assert cycle["reports"]["Futures Loss"]["subreports"]["liquidated"] == []
    assert len({r["layout"] for r in cycle["reports"].values()}) == 4
    assert len(set(telegram_digest(cycle))) == 4


def test_spot_swap_vs_transfer_and_failed_tx(monkeypatch):
    monkeypatch.setenv("WHALE_QUOTE_ASSETS_JSON", '{"solana:USDTmint":"USDT"}')
    base = {
        "source": "helius",
        "matched_addresses": ["A" * 44],
        "raw": {
            "timestamp": 1791331500,
            "type": "SWAP",
            "signature": "tx1",
            "tokenTransfers": [
                {
                    "mint": "Mint",
                    "fromUserAccount": "A" * 44,
                    "toUserAccount": "Pool",
                    "tokenAmount": 100,
                },
                {
                    "mint": "USDTmint",
                    "fromUserAccount": "Pool",
                    "toUserAccount": "A" * 44,
                    "tokenAmount": 20,
                },
            ],
        },
    }
    start, end = "2026-10-07T00:00:00+00:00", "2026-10-07T00:10:00+00:00"
    # Derive a timestamp inside the window rather than rely on a literal epoch.
    from datetime import datetime

    base["raw"]["timestamp"] = int(datetime.fromisoformat(start).timestamp()) + 300
    actions = spot_evidence(row(), [base], start, end)
    assert actions[0]["action"] == "swap_to_usdt"
    base["raw"]["type"] = "TRANSFER"
    assert spot_evidence(row(), [base], start, end)[0]["action"] == "transfer_out"
    base["raw"]["transactionError"] = {"code": 1}
    assert spot_evidence(row(), [base], start, end) == []


def test_cycle_outbox_and_budget_persist(tmp_path):
    store = Store("sqlite:///" + (tmp_path / "reports.sqlite").as_posix())
    cycle = make_cycle("c1", "start", "end", "start", [])
    store.save_cycle(cycle, telegram_digest(cycle))
    assert len(store.pending_deliveries()) == 4
    store.delivered(store.pending_deliveries()[0]["id"])
    assert len(store.pending_deliveries()) == 3
    assert len(store.report_history()) == 1
    store.reserve_budget("helius", "2026-10", 3, 3)
    import pytest

    with pytest.raises(ValueError, match="budget exhausted"):
        store.reserve_budget("helius", "2026-10", 1, 3)


def test_free_fallback_and_native_batching(tmp_path, monkeypatch):
    monkeypatch.setenv("WHALE_HELIUS_KEY", "test")
    monkeypatch.setenv("WHALE_HELIUS_RPC_BUDGET", "0")

    async def run():
        import json

        calls = []

        def handler(request):
            body = json.loads(request.content)
            calls.append(body)
            assert request.url.host == "api.mainnet.solana.com"
            return httpx.Response(
                200, json={"result": {"value": [{"lamports": 10**9} for _ in body["params"][0]]}}
            )

        store = Store("sqlite:///" + (tmp_path / "sources.sqlite").as_posix())
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = FreePriorityProvider(client, store)
            wallets = [row(asset="native", coin="SOL", address=str(i)) for i in range(101)]
            await provider.begin_cycle(wallets)
            assert len(calls) == 2
            assert len(calls[0]["params"][0]) == 100
            assert (await provider.observe(wallets[0]))["source"] == "solana_public"
            assert (await provider.observe(wallets[0]))["amount"] == "1"
            assert provider.attempts[0]["source"] == "helius"

    asyncio.run(run())


def test_public_futures_close_has_realized_pnl_but_not_liquidation(tmp_path):
    async def run():
        import json

        def handler(request):
            body = json.loads(request.content)
            if body["type"] == "clearinghouseState":
                return httpx.Response(200, json={"assetPositions": []})
            if body["type"] == "meta":
                return httpx.Response(200, json={"universe": [{"name": "ETH"}]})
            return httpx.Response(
                200, json=[{"coin": "ETH", "closedPnl": "25", "time": body["startTime"] + 1}]
            )

        store = Store("sqlite:///" + (tmp_path / "future.sqlite").as_posix())
        store.put("position:watch-1", {"amount": "10"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = FuturesProvider(client, store)
            result = await provider.observe(
                row(
                    "Futures Profit",
                    chain="ethereum",
                    platform="hyperliquid",
                    coin="ETH",
                    address="0x" + "a" * 40,
                ),
                "2026-10-07T00:00:00+00:00",
                "2026-10-07T00:10:00+00:00",
            )
            assert result["amount"] == "0" and result["realized_pnl"] == "25"
            assert result["liquidated"] is None

    asyncio.run(run())


def test_reference_price_matches_contract_not_ticker(tmp_path, monkeypatch):
    monkeypatch.delenv("WHALE_HELIUS_KEY", raising=False)

    async def run():
        calls = []

        def handler(request):
            calls.append(request.url.path)
            return httpx.Response(
                200,
                json=[
                    {
                        "chainId": "solana",
                        "baseToken": {"address": "wrong"},
                        "priceUsd": "999",
                        "liquidity": {"usd": 999999},
                    },
                    {
                        "chainId": "solana",
                        "baseToken": {"address": "Mint"},
                        "priceUsd": "2.5",
                        "liquidity": {"usd": 5000},
                        "pairAddress": "Pair",
                    },
                ],
            )

        store = Store("sqlite:///" + (tmp_path / "prices.sqlite").as_posix())
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = FreePriorityProvider(client, store)
            assert (await provider.price(row()))["price_usd"] == "2.5"
            assert (await provider.price(row()))["price_pair"] == "Pair"
            assert len(calls) == 1

    asyncio.run(run())


def test_authorized_exchange_fill_pnl_and_unknown_liquidation(tmp_path):
    async def run():
        from datetime import datetime

        start = "2026-10-07T00:00:00+00:00"
        end = "2026-10-07T00:10:00+00:00"
        timestamp = int(datetime.fromisoformat(start).timestamp() * 1000) + 100

        class Exchange:
            async def fetch_my_trades(self, *args):
                return [{"timestamp": timestamp, "info": {"realizedPnl": "15", "fillPnl": "-5"}}]

        store = Store("sqlite:///" + (tmp_path / "exchange.sqlite").as_posix())
        async with httpx.AsyncClient() as client:
            provider = FuturesProvider(client, store)
            profit = await provider.exchange_fills(
                Exchange(), "binance", row(), "ETH/USDT:USDT", start, end
            )
            loss = await provider.exchange_fills(
                Exchange(), "okx", row(), "ETH/USDT:USDT", start, end
            )
            assert profit["realized_pnl"] == "15" and profit["liquidated"] is None
            assert loss["realized_pnl"] == "-5"

    asyncio.run(run())
