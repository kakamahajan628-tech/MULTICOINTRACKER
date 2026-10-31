import asyncio
import json
import time
import httpx
import pytest
from whale_watcher.birdeye import Birdeye
from whale_watcher.core import Store, now
from whale_watcher.free_sources import FreePriorityProvider
from whale_watcher.sources import source_activation

MINT = "So11111111111111111111111111111111111111112"
WALLET = "11111111111111111111111111111111"


def no_wait(monkeypatch):
    async def sleep(*args):
        pass

    monkeypatch.setattr(asyncio, "sleep", sleep)


def test_price_headers_precision_cache_and_cu(tmp_path, monkeypatch):
    monkeypatch.setenv("WHALE_BIRDEYE_KEY", "secret-test")
    no_wait(monkeypatch)

    async def run():
        calls = []

        def handler(request):
            calls.append(request)
            assert (
                request.headers["X-API-KEY"] == "secret-test"
                and request.headers["x-chain"] == "solana"
            )
            assert (
                request.url.params["address"] == MINT
                and request.url.params["ui_amount_mode"] == "raw"
            )
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "value": "2.1234567890123456789",
                        "liquidity": 5000,
                        "updateUnixTime": time.time(),
                    },
                },
            )

        store = Store("sqlite:///" + (tmp_path / "price.sqlite").as_posix())
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = Birdeye(client, store)
            first = await adapter.price("solana", MINT)
            second = await adapter.price("solana", MINT)
            assert (
                first["price_usd"] == "2.1234567890123456789" and first["price_source"] == "birdeye"
            )
            assert first["price_retrieved_at"] == second["price_retrieved_at"]
            assert len(calls) == 1 and store.get("budget:birdeye:" + now()[:7]) == 3
            assert store.get("budget:birdeye_daily:" + now()[:10]) == 3

    asyncio.run(run())


def test_top100_pagination_cost_and_profit_loss_cache_separation(tmp_path, monkeypatch):
    monkeypatch.setenv("WHALE_BIRDEYE_KEY", "test")
    no_wait(monkeypatch)

    async def run():
        calls = []

        def handler(request):
            offset = int(request.url.params["offset"])
            limit = int(request.url.params["limit"])
            assert limit <= 10 and request.url.params["sort_by"] == "realized_pnl"
            calls.append(request)
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "items": [
                            {"owner": str(i), "realizedPnl": i}
                            for i in range(offset, offset + limit)
                        ]
                    },
                },
            )

        store = Store("sqlite:///" + (tmp_path / "traders.sqlite").as_posix())
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = Birdeye(client, store)
            top = await adapter.research("traders", "solana", MINT, 100)
            assert len(calls) == 10 and top["count"] == 100 and top["watchlist_changed"] is False
            assert store.get("budget:birdeye:" + now()[:7]) == 300
            await adapter.research("traders", "solana", MINT, 100)
            assert len(calls) == 10
            await adapter.research("losers", "solana", MINT, 10)
            assert calls[-1].url.params["sort_type"] == "asc" and len(calls) == 11

    asyncio.run(run())


def test_wallet_fallback_uses_raw_units_and_never_missing_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("WHALE_BIRDEYE_KEY", "test")
    no_wait(monkeypatch)

    async def run():
        missing = False

        def handler(request):
            assert request.method == "POST" and request.url.path == "/wallet/v2/token-balance"
            assert json.loads(request.content) == {"wallet": WALLET, "token_addresses": [MINT]}
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": [None]
                    if missing
                    else [
                        {
                            "address": MINT,
                            "balance": "1234567890",
                            "decimals": 9,
                            "amount": "WRONG_DISPLAY_AMOUNT",
                        }
                    ],
                },
            )

        store = Store("sqlite:///" + (tmp_path / "balance.sqlite").as_posix())
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = Birdeye(client, store)
            row = {"chain": "solana", "address": WALLET, "asset": MINT, "decimals": 9}
            assert (await adapter.balance(row))["amount"] == "1.23456789"
            missing = True
            with pytest.raises(ValueError, match="absence is not zero"):
                await adapter.balance(row)

    asyncio.run(run())


def test_daily_budget_blocks_http_and_uses_daily_reset(tmp_path, monkeypatch):
    monkeypatch.setenv("WHALE_BIRDEYE_KEY", "test")
    monkeypatch.setenv("WHALE_BIRDEYE_DAILY_CU", "2")
    no_wait(monkeypatch)

    async def run():
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={})

        store = Store("sqlite:///" + (tmp_path / "budget.sqlite").as_posix())
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = Birdeye(client, store)
            with pytest.raises(ValueError):
                await adapter.price("solana", MINT)
            assert calls == [] and store.get("budget:birdeye:" + now()[:7], 0) == 0
            status = adapter.recovery.health["birdeye"]
            assert (
                status["state"] == "budget_exhausted"
                and 0 < status["retry_at"] - time.time() < 86401
            )

    asyncio.run(run())


def test_stale_birdeye_quote_falls_back_to_dexscreener(tmp_path, monkeypatch):
    monkeypatch.setenv("WHALE_BIRDEYE_KEY", "test")
    no_wait(monkeypatch)

    async def run():
        def handler(request):
            if request.url.host == "public-api.birdeye.so":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {
                            "value": 2,
                            "liquidity": 5000,
                            "updateUnixTime": time.time() - 7200,
                        },
                    },
                )
            return httpx.Response(
                200,
                json=[
                    {
                        "chainId": "solana",
                        "baseToken": {"address": MINT},
                        "priceUsd": "2.5",
                        "liquidity": {"usd": 5000},
                    }
                ],
            )

        store = Store("sqlite:///" + (tmp_path / "fallback.sqlite").as_posix())
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = FreePriorityProvider(client, store)
            result = await provider.price({"chain": "solana", "asset": MINT})
            assert result["price_source"] == "dexscreener" and result["price_usd"] == "2.5"
            assert provider.attempts[0]["source"] == "birdeye"

    asyncio.run(run())


def test_access_rejection_cools_down_no_secret_in_health(tmp_path, monkeypatch):
    monkeypatch.setenv("WHALE_BIRDEYE_KEY", "DO_NOT_LOG")
    no_wait(monkeypatch)

    async def run():
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(403, json={"success": False})

        store = Store("sqlite:///" + (tmp_path / "access.sqlite").as_posix())
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = Birdeye(client, store)
            for _ in range(2):
                with pytest.raises(ValueError):
                    await adapter.price("solana", MINT)
            assert len(calls) == 1 and "DO_NOT_LOG" not in json.dumps(adapter.recovery.health)
            states = {r["name"]: r for r in source_activation(adapter.recovery.health)}
            assert states["Birdeye"]["state"] == "awaiting_access"

    asyncio.run(run())


def test_evm_checksum_and_sol_case_identity():
    assert (
        Birdeye.canonical("ethereum", "0x52908400098527886e0f7030069857d2e4169ee7")
        == "0x52908400098527886E0F7030069857D2E4169EE7"
    )
    assert Birdeye.canonical("solana", MINT) == MINT


def test_holder_wallet_mode_portfolio_pagination_and_nulls(tmp_path, monkeypatch):
    monkeypatch.setenv("WHALE_BIRDEYE_KEY", "test")
    no_wait(monkeypatch)

    async def run():
        calls = []

        def handler(request):
            calls.append(request)
            if request.url.path.endswith("holder"):
                assert (
                    request.url.params["mode"] == "wallet"
                    and request.url.params["ui_amount_mode"] == "raw"
                )
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {"items": [{"owner": WALLET, "amount": "0", "decimals": 9}]},
                    },
                )
            assert request.url.params["wallet"] == WALLET
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "items": [],
                        "wallet": WALLET,
                        "total_value": "0",
                        "current_timestamp": "2026-10-08T00:00:00Z",
                    },
                },
            )

        store = Store("sqlite:///" + (tmp_path / "research.sqlite").as_posix())
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = Birdeye(client, store)
            holders = await adapter.research("holders", "solana", MINT, 100)
            assert holders["count"] == 1 and holders["list_exhausted"]
            portfolio = await adapter.research("portfolio", "solana", WALLET, 100)
            assert portfolio["total_value"] == "0" and portfolio["count"] == 0

    asyncio.run(run())


def test_rate_limit_serializes_concurrent_requests(tmp_path, monkeypatch):
    monkeypatch.setenv("WHALE_BIRDEYE_KEY", "test")

    async def run():
        sent = []

        def handler(request):
            sent.append(asyncio.get_running_loop().time())
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {"value": 2, "liquidity": 5000, "updateUnixTime": time.time()},
                },
            )

        store = Store("sqlite:///" + (tmp_path / "rate.sqlite").as_posix())
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = Birdeye(client, store)
            await asyncio.gather(adapter.price("solana", MINT), adapter.price("solana", WALLET))
            assert len(sent) == 2 and sent[1] - sent[0] >= 1

    asyncio.run(run())


def test_private_api_and_telegram_query_use_shared_adapter(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from whale_watcher import app as module

    monkeypatch.setenv("WHALE_DATABASE_URL", "sqlite:///" + (tmp_path / "api.sqlite").as_posix())
    monkeypatch.setenv("WHALE_ADMIN_TOKEN", "test-secret-unit-testing-only-32-characters")
    monkeypatch.delenv("RENDER", raising=False)

    class Adapter:
        async def research(self, kind, chain, address, count):
            return {
                "kind": kind,
                "chain": chain,
                "address": address,
                "count": count,
                "watchlist_changed": False,
            }

    with TestClient(module.app) as client:
        module.provider.birdeye = Adapter()
        params = {"chain": "solana", "address": MINT, "count": 100}
        assert client.post("/api/birdeye/traders", params=params).status_code == 401
        response = client.post(
            "/api/birdeye/traders",
            params=params,
            auth=("whale", "test-secret-unit-testing-only-32-characters"),
        )
        assert response.json()["count"] == 100 and response.json()["watchlist_changed"] is False
        result = client.portal.call(
            module.command, "/source birdeye holders solana " + MINT + " 100"
        )
        assert result["kind"] == "holders"
