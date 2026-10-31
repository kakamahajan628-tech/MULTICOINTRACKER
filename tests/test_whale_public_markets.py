import asyncio
import copy
import time
from datetime import datetime, timezone

import httpx
import pytest

from whale_watcher.core import Store, GROUPS
from whale_watcher.public_markets import (
    PublicMarkets,
    market_targets,
    market_digest,
)
from whale_watcher.screening import build_screen
from whale_watcher.sources import source_activation


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.setenv("WHALE_MARKET_RPS", "5")
    monkeypatch.setenv("WHALE_MARKETS_ENABLED", "true")
    monkeypatch.setenv("WHALE_MARKET_MONTHLY_CALLS", "100000")
    monkeypatch.delenv("WHALE_BINANCE_MARKET_KEY", raising=False)


def wallet(**changes):
    return {
        "id": "w1",
        "group": "Spot Profit",
        "chain": "ethereum",
        "asset": "native",
        "coin": "ETH",
        "address": "0x" + "a" * 40,
        "label": "Trader 1",
        "rank": 1,
        "platform": "",
        **changes,
    }


def handler(request):
    stamp = int(time.time() * 1000)
    symbol = request.url.params.get("symbol", "ETHUSDT")
    path = request.url.path
    if request.url.host == "fapi.binance.com":
        if path.endswith("premiumIndex"):
            body = {
                "symbol": symbol,
                "markPrice": "100",
                "indexPrice": "101",
                "lastFundingRate": ".0001",
                "nextFundingTime": stamp + 3600000,
                "time": stamp,
            }
        elif path.endswith("24hr"):
            body = {
                "symbol": symbol,
                "lastPrice": "99",
                "priceChangePercent": "-2",
                "quoteVolume": "100000",
                "closeTime": stamp,
            }
        elif path.endswith("openInterest"):
            body = {"symbol": symbol, "openInterest": "10", "time": stamp}
        elif path.endswith("takerlongshortRatio"):
            body = [{"buySellRatio": "2", "buyVol": "20", "sellVol": "10", "timestamp": stamp}]
        else:
            body = [{"symbol": symbol, "longShortRatio": "1.5", "timestamp": stamp}]
    elif request.url.host == "api.bybit.com":
        if path.endswith("tickers"):
            item = {
                "symbol": symbol,
                "markPrice": "102",
                "indexPrice": "101",
                "lastPrice": "103",
                "fundingRate": "-.0002",
                "openInterest": "50",
                "openInterestValue": "5100",
                "turnover24h": "200000",
                "price24hPcnt": ".03",
                "nextFundingTime": str(stamp + 3600000),
                "fundingIntervalHour": "4",
            }
        else:
            item = {"symbol": symbol, "buyRatio": ".6", "sellRatio": ".4", "timestamp": str(stamp)}
        body = {"retCode": 0, "time": stamp, "result": {"category": "linear", "list": [item]}}
    else:
        instrument = request.url.params.get("instId", "ETH-USDT-SWAP")
        if path.endswith("long-short-account-ratio"):
            data = [[str(stamp), "1.3"]]
        else:
            data = [
                {
                    "instId": instrument,
                    "ts": str(stamp),
                    "last": "104",
                    "open24h": "100",
                    "volCcy24h": "500",
                    "markPx": "105",
                    "oi": "100",
                    "oiCcy": "10",
                    "oiUsd": "1050",
                    "fundingRate": ".0003",
                    "fundingTime": str(stamp + 3600000),
                    "nextFundingTime": str(stamp + 3600000 * 9),
                }
            ]
        body = {"code": "0", "data": data}
    return httpx.Response(200, json=body)


def test_all_three_public_venues_normalization_cache_and_no_credentials(tmp_path):
    async def run():
        calls = []

        def record(request):
            calls.append(request)
            assert request.method == "GET" and not request.content
            assert "authorization" not in request.headers and "X-MBX-APIKEY" not in request.headers
            assert wallet()["address"] not in str(request.url)
            return handler(request)

        store = Store("sqlite:///" + (tmp_path / "markets.sqlite").as_posix())
        async with httpx.AsyncClient(transport=httpx.MockTransport(record)) as client:
            api = PublicMarkets(client, store)
            first, second = await asyncio.gather(api.snapshot("ETH"), api.snapshot("ETH"))
            assert first == second and len(calls) == 14
            venues = {r["venue"]: r for r in first["venues"]}
            assert all(v["status"] == "active" for v in venues.values())
            assert first["reference"]["venue"] == "binance"
            assert venues["binance"]["fields"]["funding_rate_pct"] == "0.0100"
            assert venues["bybit"]["fields"]["funding_rate_pct"] == "-0.0200"
            assert venues["bybit"]["fields"]["long_short_account_ratio"] == "1.5"
            assert venues["bybit"]["fields"]["change_24h_pct"] == "3.00"
            assert venues["okx"]["fields"]["volume_24h_base"] == "500"
            assert "volume_24h_quote" not in venues["okx"]["fields"]
            assert venues["okx"]["fields"]["funding_interval_hours"] == "8"
            assert venues["okx"]["fields"]["ratio_scope"] == "coin_contract_accounts_all_quotes"
            assert "base_asset_both_sides" == venues["bybit"]["fields"]["open_interest_basis"]
            assert await PublicMarkets(client, store).snapshot("ETH") == first  # restart cache
            assert len(calls) == 14
            assert "individual wallets" in market_digest([first])
            assert all(
                r["state"] == "active"
                for r in source_activation(api.recovery.health)
                if r["name"].endswith("Public Markets")
            )

    asyncio.run(run())


def test_partial_binance_ratios_do_not_erase_core_and_optional_key_is_isolated(
    tmp_path, monkeypatch
):
    async def run():
        def reject_ratios(request):
            if request.url.path.startswith("/futures/data/"):
                return httpx.Response(401, json={"code": -2015})
            return handler(request)

        store = Store("sqlite:///" + (tmp_path / "partial.sqlite").as_posix())
        async with httpx.AsyncClient(transport=httpx.MockTransport(reject_ratios)) as client:
            row = (await PublicMarkets(client, store).snapshot("ETH"))["venues"][0]
            assert row["status"] == "partial" and row["fields"]["mark_price"] == "100"
            assert row["fields"]["open_interest_base"] == "10" and len(row["errors"]) == 4
        monkeypatch.setenv("WHALE_BINANCE_MARKET_KEY", "public-data-only-test")

        def check_key(request):
            should_have_key = (
                request.url.host == "fapi.binance.com"
                and request.url.path.startswith("/futures/data/")
            )
            assert ("X-MBX-APIKEY" in request.headers) == should_have_key
            return handler(request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(check_key)) as client:
            row = (await PublicMarkets(client, Store("sqlite://")).snapshot("ETH"))["venues"][0]
            assert row["status"] == "active"

    asyncio.run(run())


def test_reference_fallback_and_unknown_positions_are_not_fabricated(tmp_path):
    async def run():
        def unavailable(request):
            return (
                httpx.Response(403) if request.url.host == "fapi.binance.com" else handler(request)
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as client:
            result = await PublicMarkets(client, Store("sqlite://")).snapshot("ETH")
            assert result["reference"]["venue"] == "bybit"
            assert result["venues"][0]["status"] == "unavailable"
            assert result["scope"] == "exchange_aggregate_not_wallet_positions"
            assert not any("positions" in v or "realized_pnl" in v for v in result["venues"])

    asyncio.run(run())


def test_stale_nan_wrong_instrument_and_api_error_are_not_zero(tmp_path):
    async def run():
        def malformed(request):
            response = handler(request)
            body = response.json()
            if request.url.host == "fapi.binance.com":
                if request.url.path.endswith("premiumIndex"):
                    body["markPrice"] = "NaN"
                elif request.url.path.endswith("openInterest"):
                    body["time"] = 1000
                elif request.url.path.endswith("24hr"):
                    body["symbol"] = "WRONGUSDT"
                else:
                    return httpx.Response(403)
            elif request.url.host == "api.bybit.com":
                body["retCode"] = 10001
            return httpx.Response(200, json=body)

        async with httpx.AsyncClient(transport=httpx.MockTransport(malformed)) as client:
            result = await PublicMarkets(client, Store("sqlite://")).snapshot("ETH")
            assert result["reference"]["venue"] == "okx"
            assert result["venues"][0]["fields"] == result["venues"][1]["fields"] == {}
            assert result["venues"][0]["status"] == "unavailable"

    asyncio.run(run())


def test_budget_caps_actual_requests_and_one_bad_symbol_does_not_block_another(
    tmp_path, monkeypatch
):
    async def run():
        calls = []

        def mixed(request):
            calls.append(request)
            if (
                request.url.params.get("symbol", "").startswith("BAD")
                or request.url.params.get("instId", "").startswith("BAD")
                or request.url.params.get("ccy") == "BAD"
            ):
                return httpx.Response(400)
            return handler(request)

        store = Store("sqlite:///" + (tmp_path / "budget.sqlite").as_posix())
        async with httpx.AsyncClient(transport=httpx.MockTransport(mixed)) as client:
            api = PublicMarkets(client, store)
            await api.snapshot("BAD")
            assert (await api.snapshot("ETH"))["reference"]["venue"] == "binance"
            monkeypatch.setenv("WHALE_MARKET_MONTHLY_CALLS", "1")
            capped = PublicMarkets(client, Store("sqlite://"))
            calls.clear()
            result = await capped.snapshot("ETH")
            assert len(calls) == 3  # one actual call per venue, no extra attempts
            assert result["venues"][0]["status"] == "partial"

    asyncio.run(run())


def test_explicit_contract_mapping_native_defaults_and_per_coin_dedup(tmp_path, monkeypatch):
    token1, token2 = "ethereum:0x" + "1" * 40, "ethereum:0x" + "2" * 40
    rows = [
        wallet(),
        wallet(id="native-2"),
        wallet(id="token1", coin="SAME", asset="0x" + "1" * 40),
        wallet(id="token2", coin="SAME", asset="0x" + "2" * 40),
        wallet(id="future", group="Futures Profit", platform="bybit"),
        wallet(id="hl", group="Futures Profit", platform="hyperliquid"),
    ]
    targets, unmapped = market_targets(rows, {}, {token1: "SAME"})
    assert list(targets) == ["ETH", "SAME"]
    assert token2 in unmapped and "perp:hyperliquid:ETH" in unmapped
    assert targets["ETH"] == {
        "ethereum:native": "native_coin",
        "perp:bybit:ETH": "exact_venue_base_symbol",
    }
    assert len(market_targets(rows, {"hyperliquid:ETH": "ethereum:native"}, {})[0]["ETH"]) == 2

    async def run():
        api = PublicMarkets(None, Store("sqlite://"))
        calls = []

        async def fake(symbol):
            calls.append(symbol)
            return {"symbol": symbol, "venues": []}

        api.snapshot = fake
        monkeypatch.setenv("WHALE_MARKET_MAX_SYMBOLS", "1")
        result = await api.collect(rows, {}, {token1: "SAME"})
        assert calls == ["ETH"] and result["deferred_symbols"] == ["SAME"]
        assert len(result["snapshots"]) == 1

    asyncio.run(run())


def test_disabled_has_no_network_even_with_warm_cache(tmp_path, monkeypatch):
    async def run():
        api = PublicMarkets(None, Store("sqlite://"))
        api.store.put(
            "public_market:ETH",
            {"symbol": "ETH", "collected_at": datetime.now(timezone.utc).isoformat(), "venues": []},
        )
        monkeypatch.setenv("WHALE_MARKETS_ENABLED", "false")
        assert (await api.snapshot("ETH"))["status"] == "disabled"

    asyncio.run(run())


def screen_cycle(index):
    at = datetime.fromtimestamp(1791504000 + index * 600, timezone.utc).isoformat()
    row = {**wallet(), "state": "unavailable", "amount": None, "previous": None, "actions": []}
    field_at = at
    market = {
        "venue": "bybit",
        "symbol": "ETH",
        "instrument": "ETHUSDT",
        "observed_at": at,
        "fields": {
            "mark_price": str(100 + index),
            "open_interest_base": str(10 + index),
            "funding_rate": ".001",
            "funding_rate_pct": ".1",
        },
        "field_times": {
            key: field_at
            for key in ("mark_price", "open_interest_base", "funding_rate", "funding_rate_pct")
        },
    }
    return {
        "id": "c" + str(index),
        "started_at": at,
        "completed_at": at,
        "window_start": at,
        "window_end": at,
        "reports": {g: {"rows": [row] if g == "Spot Profit" else []} for g in GROUPS},
        "public_markets": {
            "snapshots": [
                {
                    "collected_at": at,
                    "asset_associations": {"ethereum:native": "native_coin"},
                    "venues": [market],
                }
            ]
        },
    }


def test_hourly_and_four_hour_context_dedup_not_wallet_evidence():
    cycles = [screen_cycle(i) for i in range(1, 7)]
    report = build_screen(cycles, "1h", 1)
    item = report["assets"][0]
    assert item["phase"] == "insufficient_data" and item["metrics"]["coverage_pct"] == 0
    assert item["metrics"]["public_markets"][0]["samples"] == 6
    assert item["metrics"]["public_markets"][0]["open_interest_change_pct"] == 45.4545
    assert "market_open_interest_and_funding_not_available" not in item["gaps"]
    assert not any(
        h["eligible"] for h in item["hypotheses"]
    )  # aggregate OI/funding cannot prove a wallet squeeze
    hourlies = [
        build_screen([screen_cycle(i) for i in range(start, start + 6)], "1h", 1)
        for start in (1, 7, 13, 19)
    ]
    four = build_screen(hourlies, "4h", 1)
    summary = four["assets"][0]["metrics"]["public_markets"][0]
    assert summary["samples"] == 24 and summary["open_interest_change_pct"] == 209.0909
    repeated = copy.deepcopy(cycles)
    for cycle in repeated:
        cycle["public_markets"] = copy.deepcopy(cycles[0]["public_markets"])
    cached = build_screen(repeated, "1h", 1)["assets"][0]
    assert cached["metrics"]["public_markets"][0]["samples"] == 1
    assert cached["metrics"]["public_markets"][0]["open_interest_change_pct"] is None


def test_scan_preserves_wallets_when_public_market_collection_fails(tmp_path, monkeypatch):
    from whale_watcher import app as module

    async def run():
        store = Store("sqlite:///" + (tmp_path / "scan.sqlite").as_posix())
        store.put("wallets", [wallet()])

        class Provider:
            async def observe(self, row):
                return {"amount": "12", "source": "test_rpc"}

        class BrokenMarket:
            async def collect(self, *args):
                raise ValueError("Market outage")

        monkeypatch.setattr(module, "store", store)
        monkeypatch.setattr(module, "provider", Provider())
        monkeypatch.setattr(module, "futures_provider", None)
        monkeypatch.setattr(module, "public_markets", BrokenMarket())
        monkeypatch.setattr(module, "recovery", None)
        monkeypatch.setattr(module, "scan_lock", asyncio.Lock())
        await module.scan()
        cycle = store.get("latest_cycle")
        assert cycle["coverage"]["observed"] == 1
        assert cycle["reports"]["Spot Profit"]["rows"][0]["amount"] == "12"
        assert cycle["public_markets"]["status"].startswith("collection_failed")

    asyncio.run(run())


def test_api_auth_telegram_commands_and_owner_verified_mapping(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from whale_watcher import app as module

    monkeypatch.setenv("WHALE_DATABASE_URL", "sqlite:///" + (tmp_path / "api.sqlite").as_posix())
    monkeypatch.setenv("WHALE_ADMIN_TOKEN", "market-test-unit-testing-only-32-characters")
    monkeypatch.delenv("RENDER", raising=False)

    async def fake(self, symbol):
        return {
            "symbol": symbol,
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "venues": [],
            "scope": "exchange_aggregate_not_wallet_positions",
        }

    monkeypatch.setattr(PublicMarkets, "snapshot", fake)
    with TestClient(module.app) as client:
        auth = ("whale", "market-test-unit-testing-only-32-characters")
        assert (
            client.get("/api/markets").status_code
            == client.post("/api/markets/ETH").status_code
            == 401
        )
        assert client.post("/api/markets/ETH", auth=auth).json()["symbol"] == "ETH"
        assert client.post("/api/markets/BTC-USDT", auth=auth).status_code == 400
        module.store.put("wallets", [wallet()])
        result = asyncio.run(module.command("/market ETH"))
        assert "ETH" in result["report"]
        assert "ETH" in asyncio.run(module.command("/report market ETH"))["report"]
        assert (
            asyncio.run(module.command("/marketlink ethereum:native ETH"))["market_symbol"] == "ETH"
        )
        revision = module.store.get("watch_revision")
        assert not asyncio.run(module.command("/marketlink ethereum:native ETH"))["changed"]
        assert module.store.get("watch_revision") == revision
        with pytest.raises(ValueError):
            asyncio.run(module.command("/marketlink ethereum:unknown ETH"))
