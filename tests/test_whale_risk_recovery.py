import asyncio
import json
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import httpx
import pytest
from whale_watcher.core import Store
from whale_watcher.risk import liquidation_risk, summarize_legs
from whale_watcher.reporting import make_cycle
from whale_watcher.reporting import risk_digest
from whale_watcher.recovery import Recovery
from whale_watcher.free_sources import BudgetRPC, FuturesProvider
from whale_watcher.sources import source_activation


@pytest.mark.parametrize(
    "side,mark,liq,band,distance",
    [
        ("long", "100", "99", "critical", "1"),
        ("short", "100", "101", "critical", "1"),
        ("long", "100", "96", "near", "4"),
        ("short", "100", "106", "outside_band", "6"),
        ("long", "100", "101", "threshold_crossed", "-1"),
        ("short", "100", "99", "threshold_crossed", "-1"),
    ],
)
def test_liquidation_distance_is_directional(side, mark, liq, band, distance):
    result = liquidation_risk(side, mark, liq)
    assert result["risk_band"] == band
    assert Decimal(result["distance_pct"]) == Decimal(distance)


def test_missing_stale_and_hedged_risk_are_not_fabricated():
    assert liquidation_risk("long", "100", None)["risk_band"] == "unknown"
    assert liquidation_risk("short", "NaN", "101")["risk_band"] == "unknown"
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    assert liquidation_risk("long", "100", "99", old)["risk_band"] == "unknown"
    combined = summarize_legs(
        [
            {"side": "long", "mark_price": "100", "liquidation_price": "96"},
            {"side": "short", "mark_price": "100", "liquidation_price": "101"},
        ]
    )
    assert combined["side"] == "short" and combined["risk_band"] == "critical"
    assert len(combined["risk_legs"]) == 2


def test_loss_close_not_hidden_by_profitable_fills_and_unavailable_not_fresh():
    rows = [
        {
            "id": "1",
            "label": "Trader 1",
            "address": "address",
            "chain": "ethereum",
            "coin": "ETH",
            "group": "Futures Profit",
            "amount": "5",
            "state": "holding",
            "realized_pnl": "10",
            "fills": [{"closedPnl": "-20", "tid": 1}, {"closedPnl": "30", "tid": 2}],
            "risk_legs": [{"side": "long", "mark_price": "100", "liquidation_price": "99"}],
        },
        {
            "id": "2",
            "label": "Trader 2",
            "address": "other",
            "chain": "ethereum",
            "coin": "ETH",
            "group": "Futures Loss",
            "amount": None,
            "state": "unavailable",
            "fills": [{"closedPnl": "-50"}],
            "risk_legs": [{"side": "long", "mark_price": "100", "liquidation_price": "99"}],
        },
    ]
    report = make_cycle(
        "id",
        "2026-10-08T00:00:00+00:00",
        "2026-10-08T00:10:00+00:00",
        "2026-10-08T00:00:00+00:00",
        rows,
    )
    assert len(report["risk_report"]["closed_at_loss"]) == 1
    assert "-20" in risk_digest(report)
    assert (
        report["risk_report"]["closed_at_loss"][0]["loss_exit_evidence"][0]["realized_pnl"] == "-20"
    )
    assert len(report["risk_report"]["near_liquidation"]) == 1
    assert report["reports"]["Futures Loss"]["subreports"]["near_liquidation"] == []


def test_recovery_retry_then_success_counts_actual_rpc_attempts(tmp_path, monkeypatch):
    async def run():
        store = Store("sqlite:///" + (tmp_path / "retry.sqlite").as_posix())
        count = 0

        async def no_sleep(*args):
            pass

        monkeypatch.setattr(asyncio, "sleep", no_sleep)

        def handler(request):
            nonlocal count
            count += 1
            return (
                httpx.Response(503) if count == 1 else httpx.Response(200, json={"result": "0x0"})
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            recovery = Recovery(store)
            provider = BudgetRPC(
                client, {"ethereum": "https://rpc.test"}, store, "test_rpc", 10, 10000, recovery
            )
            assert (
                await provider.rpc("ethereum", "eth_getBalance", ["address", "finalized"]) == "0x0"
            )
            from whale_watcher.core import now

            assert store.get("budget:test_rpc:" + now()[:7]) == 2
            assert recovery.health["test_rpc"]["state"] == "active"

    asyncio.run(run())


def test_auth_failure_cools_down_without_leaking_secret_and_restart_retains_state(tmp_path):
    async def run():
        url = "sqlite:///" + (tmp_path / "health.sqlite").as_posix()
        store = Store(url)
        recovery = Recovery(store)
        calls = 0

        async def bad():
            nonlocal calls
            calls += 1
            request = httpx.Request("GET", "https://api.test/?secret=DO_NOT_LOG")
            response = httpx.Response(401, request=request)
            response.raise_for_status()

        with pytest.raises(ValueError):
            await recovery.run("source", bad)
        with pytest.raises(ValueError):
            await recovery.run("source", bad)
        assert calls == 1 and recovery.health["source"]["state"] == "awaiting_access"
        recovery.flush()
        restored = Recovery(Store(url))
        assert restored.health["source"]["state"] == "awaiting_access"
        assert "DO_NOT_LOG" not in json.dumps(restored.health)

    asyncio.run(run())


def test_futures_fill_outage_preserves_valid_position_and_risk(tmp_path):
    async def run():
        store = Store("sqlite:///" + (tmp_path / "perps.sqlite").as_posix())
        store.put("position:watch", {"amount": "10"})
        async with httpx.AsyncClient() as client:
            provider = FuturesProvider(client, store)

            async def info(body):
                if body["type"] == "clearinghouseState":
                    return {
                        "assetPositions": [
                            {"position": {"coin": "ETH", "szi": "5", "liquidationPx": "99"}}
                        ]
                    }
                if body["type"] == "metaAndAssetCtxs":
                    return [{"universe": [{"name": "ETH"}]}, [{"markPx": "100"}]]
                raise ValueError("fill service unavailable")

            provider.info = info
            row = {
                "id": "watch",
                "chain": "ethereum",
                "address": "0x" + "a" * 40,
                "platform": "hyperliquid",
                "coin": "ETH",
            }
            result = await provider.observe(
                row, "2026-10-08T00:00:00+00:00", "2026-10-08T00:10:00+00:00"
            )
            assert result["amount"] == "5" and result["realized_pnl"] is None
            assert result["risk_legs"][0]["mark_price"] == "100"
            assert result["fill_coverage"].startswith("unavailable")

    asyncio.run(run())


def test_sources_without_access_do_not_claim_active(monkeypatch):
    monkeypatch.delenv("WHALE_NANSEN_KEY", raising=False)
    sources = {r["name"]: r for r in source_activation({"nansen": {"state": "active"}})}
    assert sources["Nansen"]["state"] == "awaiting_key"
    assert sources["Arkham"]["state"] == "adapter_or_access_pending"
    assert sources["Hyperliquid"]["state"] == "enabled_unverified"


def test_nansen_same_venue_fallback_and_stale_rejection(tmp_path, monkeypatch):
    monkeypatch.setenv("WHALE_NANSEN_KEY", "test-key")

    async def run():
        stale = False

        def handler(request):
            assert request.url.path == "/api/v1/profiler/perp-positions"
            timestamp = int(datetime.now(timezone.utc).timestamp() * 1000) - (
                3600000 if stale else 0
            )
            return httpx.Response(
                200,
                json={
                    "data": {
                        "time": timestamp,
                        "assetPositions": [
                            {
                                "position": {
                                    "token_symbol": "ETH",
                                    "size": "-3",
                                    "liquidation_price_usd": "101",
                                    "entry_price_usd": "105",
                                    "unrealized_pnl_usd": "-5",
                                }
                            }
                        ],
                    }
                },
            )

        store = Store("sqlite:///" + (tmp_path / "fallback.sqlite").as_posix())
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = FuturesProvider(client, store)
            row = {
                "id": "watch",
                "chain": "ethereum",
                "address": "0x" + "a" * 40,
                "platform": "hyperliquid",
                "coin": "ETH",
            }

            async def unavailable(body):
                raise ValueError("primary down")

            provider.info = unavailable
            result = await provider.observe(
                row, "2026-10-08T00:00:00+00:00", "2026-10-08T00:10:00+00:00"
            )
            assert result["source"] == "nansen_hyperliquid" and result["amount"] == "-3"
            assert result["risk_legs"][0]["mark_price"] is None
            stale = True
            with pytest.raises(ValueError):
                await provider.nansen_positions(row["address"])

    asyncio.run(run())
