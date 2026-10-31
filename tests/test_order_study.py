import asyncio
from copy import deepcopy
from decimal import Decimal

from fastapi.testclient import TestClient
import pytest

from whale_watcher.core import Store
from whale_watcher.orders import (
    OrderStudy,
    intent,
    normalize_ccxt_orders,
    normalize_fills,
    normalize_hyperliquid_orders,
)

ADDRESS = "0x" + "a" * 40


def watch(**changes):
    return {
        "id": "watch",
        "address": ADDRESS,
        "platform": "hyperliquid",
        "chain": "hyperliquid",
        "coin": "ZEC",
        "asset": "perpetual:ZEC",
        "group": "Futures Unclassified",
        "label": "Test wallet",
        "rank": 1,
        **changes,
    }


def open_order(oid=1, **changes):
    return {
        "oid": oid,
        "coin": "ZEC",
        "side": "B",
        "sz": "2",
        "origSz": "2",
        "limitPx": "100",
        "isTrigger": False,
        "reduceOnly": False,
        "timestamp": 1000,
        "orderType": "Limit",
        **changes,
    }


def fill(tid=1, **changes):
    return {
        "tid": tid,
        "oid": 1,
        "coin": "ZEC",
        "side": "A",
        "sz": "5",
        "px": "100",
        "time": 2000,
        "startPosition": "5",
        "closedPnl": "20",
        "fee": "0.05",
        "feeToken": "USDC",
        **changes,
    }


class Provider:
    def __init__(self):
        self.cache = {}
        self.orders = [open_order()]
        self.status = "canceled"
        self.calls = []
        self.fail = False

    async def info(self, body):
        self.calls.append(body)
        if body["type"] == "frontendOpenOrders":
            if self.fail:
                raise ValueError("unavailable")
            return deepcopy(self.orders)
        assert body["type"] == "orderStatus"
        if self.status is None:
            return {"status": "unknownOid"}
        return {
            "status": "order",
            "order": {
                "order": open_order(body["oid"]),
                "status": self.status,
                "statusTimestamp": 3000,
            },
        }


def position(**changes):
    return {
        "amount": "5",
        "source": "hyperliquid",
        "fills": [],
        "fill_coverage": "complete_window",
        **changes,
    }


def previous(**changes):
    return {"amount": "5", "source": "hyperliquid", **changes}


@pytest.mark.parametrize(
    "before,after,condition",
    [
        ("5", "10", "position_increase"),
        ("5", "2", "partial_close"),
        ("5", "0", "full_close"),
        ("5", "-2", "direction_flip"),
        ("-5", "-10", "position_increase"),
        ("-5", "-2", "partial_close"),
    ],
)
def test_original_position_conditions_are_used_for_executed_fills(before, after, condition):
    delta = Decimal(after) - Decimal(before)
    row = normalize_fills(
        [fill(startPosition=before, sz=str(abs(delta)), side="B" if delta > 0 else "A")], "ZEC"
    )[0]
    assert row["position_after_fill"] == after
    assert row["position_condition"] == condition


def test_close_and_reopen_fills_are_visible_even_with_unchanged_net_position():
    rows = normalize_fills([fill(), fill(2, side="B", startPosition="0", closedPnl="0")], "ZEC")
    assert [r["position_condition"] for r in rows] == ["full_close", "position_increase"]
    assert len(normalize_fills([fill(), fill()], "ZEC")) == 1
    orders = normalize_hyperliquid_orders([open_order(reduceOnly=True)], "ZEC")
    assert intent(orders[0], Decimal("5")) == "reduce_only_instruction"
    assert intent(orders[0], Decimal("5"), True) == "unknown_hedged_position"


def test_exact_quantities_and_ccxt_contract_multiplier():
    row = normalize_hyperliquid_orders(
        [open_order(sz="123456789012345678.123456789", limitPx="100000000.00000001")], "ZEC"
    )[0]
    with __import__("decimal").localcontext() as context:
        context.prec = 100
        expected = Decimal("123456789012345678.123456789") * Decimal("100000000.00000001")
    assert Decimal(row["display_notional_quote"]) == expected
    order = {
        "id": "x",
        "symbol": "ZEC/USDT:USDT",
        "side": "sell",
        "remaining": "2",
        "amount": "5",
        "price": "100",
        "reduceOnly": True,
    }
    row = normalize_ccxt_orders([order], "ZEC", "ZEC/USDT:USDT", "10")[0]
    assert row["remaining_base"] == "20" and row["display_notional_quote"] == "2000"


def test_disappearance_is_not_a_cancellation_without_confirmed_status(tmp_path):
    async def exercise():
        store = Store("sqlite:///" + (tmp_path / "orders.sqlite").as_posix())
        provider = Provider()
        baseline = await OrderStudy(store, provider).capture(watch(), position(), previous())
        assert baseline["snapshot_comparison"] == "baseline_or_truncated"
        provider.cache = {}
        provider.orders = []
        provider.status = None
        report = await OrderStudy(store, provider).capture(watch(), position(), previous())
        assert report["unresolved_disappearances"] == 1
        assert report["changes"][0]["venue_status"] is None
        assert report["changes"][0]["basis"] == "not_in_latest_open_order_snapshot"
        store.engine.dispose()

    asyncio.run(exercise())


@pytest.mark.parametrize("status", ["filled", "canceled", "triggered", "liquidatedCanceled"])
def test_confirmed_order_status_is_preserved_without_claiming_position_liquidation(
    tmp_path, status
):
    async def exercise():
        store = Store("sqlite:///" + (tmp_path / (status + ".sqlite")).as_posix())
        provider = Provider()
        await OrderStudy(store, provider).capture(watch(), position(), previous())
        provider.cache = {}
        provider.orders = []
        provider.status = status
        report = await OrderStudy(store, provider).capture(watch(), position(), previous())
        assert report["changes"][0]["venue_status"] == status
        assert report["changes"][0]["basis"] == "venue_order_status_response"
        assert "liquidated" not in report
        store.engine.dispose()

    asyncio.run(exercise())


def test_fills_remain_durable_on_order_outage_and_are_idempotent(tmp_path):
    async def exercise():
        store = Store("sqlite:///" + (tmp_path / "outage.sqlite").as_posix())
        provider = Provider()
        baseline = await OrderStudy(store, provider).capture(watch(), position(), previous())
        provider.cache = {}
        provider.fail = True
        for _ in range(2):
            provider.cache = {}
            report = await OrderStudy(store, provider).capture(
                watch(), position(fills=[fill()]), previous()
            )
            assert report["status"] == "unavailable" and report["open_orders"] is None
            assert report["last_success_at"] == baseline["observed_at"]
            assert report["fills"][0]["fill_id"] == "1"
        assert sum(event["kind"] == "fill" for event in store.order_history()) == 1
        store.engine.dispose()

    asyncio.run(exercise())


def test_status_budget_truncation_and_shared_snapshot_cache(tmp_path):
    async def exercise():
        store = Store("sqlite:///" + (tmp_path / "limits.sqlite").as_posix())
        provider = Provider()
        provider.orders = [open_order(i) for i in range(5)]
        first = await OrderStudy(store, provider, max_orders=2).capture(
            watch(), position(), previous()
        )
        assert first["open_count"] == 5 and len(first["open_orders"]) == 2 and not first["complete"]
        provider.cache = {}
        study = OrderStudy(store, provider)
        await asyncio.gather(
            study.capture(watch(), position(), previous()),
            study.capture(watch(id="copy"), position(), previous()),
        )
        assert sum(call["type"] == "frontendOpenOrders" for call in provider.calls) == 2
        provider.cache = {}
        provider.orders = []
        report = await OrderStudy(store, provider, status_budget=1).capture(
            watch(), position(), previous()
        )
        assert sum(call["type"] == "orderStatus" for call in provider.calls) == 1
        assert report["unresolved_disappearances"] == 4
        store.engine.dispose()

    asyncio.run(exercise())


def test_order_report_is_integrated_with_scan_api_history_and_commands(tmp_path, monkeypatch):
    from whale_watcher import app as module

    monkeypatch.setenv("WHALE_DATABASE_URL", "sqlite:///" + (tmp_path / "scan.sqlite").as_posix())
    monkeypatch.setenv("WHALE_ORDERS_ENABLED", "true")

    class Futures(Provider):
        def begin_cycle(self):
            self.cache = {}

        async def observe(self, row, start, end):
            return position(amount="10", fills=[fill()])

        async def close(self):
            pass

    with TestClient(module.app) as client:
        monkeypatch.setattr(module, "futures_provider", Futures())
        monkeypatch.setattr(module, "public_markets", None)
        module.store.put("wallets", [watch()])
        module.store.put("position:watch", previous())
        client.portal.call(module.scan)
        auth = ("whale", "unit-test-admin-password-32-characters")
        assert client.get("/api/orders").status_code == 401
        report = client.get("/api/orders", params={"coin": "ZEC", "limit": 1}, auth=auth).json()
        assert report["summary"]["orders_observed"] == 1
        assert report["summary"]["position_conditions"] == {"position_increase": 1}
        assert report["wallets"][0]["orders"]["fills"][0]["position_condition"] == "full_close"
        cursor = report["next_before_id"]
        second = client.get(
            "/api/orders", params={"coin": "ZEC", "limit": 1, "before_id": cursor}, auth=auth
        ).json()
        assert second["history"][0]["storage_id"] < cursor
        for command in ("/orders ZEC", "/report orders ZEC"):
            assert (
                "ORDERS & POSITION CONDITIONS"
                in client.portal.call(module.command, command)["report"]
            )
        assert "Orders, fills" in client.get("/", auth=auth).text


def test_hedged_or_source_changed_positions_are_not_comparable(tmp_path):
    async def exercise():
        store = Store("sqlite:///" + (tmp_path / "source.sqlite").as_posix())
        provider = Provider()
        report = await OrderStudy(store, provider).capture(
            watch(), position(direction="hedged"), previous()
        )
        assert report["position_comparison"] == "baseline_or_source_change_or_hedged"
        assert report["open_orders"][0]["inferred_effect"] == "unknown_hedged_position"
        assert not any(row["kind"] == "position_snapshot_change" for row in report["changes"])
        store.engine.dispose()

    asyncio.run(exercise())


def test_zero_quantity_conditional_orders_remain_unknown_not_discarded():
    rows = normalize_hyperliquid_orders(
        [open_order(sz="0", origSz="0", isPositionTpsl=True, isTrigger=True)], "ZEC"
    )
    assert rows[0]["remaining_base"] is None and rows[0]["display_notional_quote"] is None
    assert intent(rows[0], Decimal("5")) == "unknown_dynamic_quantity"


def test_duplicate_source_fills_cannot_double_realized_pnl(tmp_path):
    from whale_watcher.free_sources import FuturesProvider
    import httpx

    async def exercise():
        store = Store("sqlite:///" + (tmp_path / "duplicates.sqlite").as_posix())
        store.put("position:watch", previous())
        async with httpx.AsyncClient() as client:
            provider = FuturesProvider(client, store)

            async def info(body):
                if body["type"] == "clearinghouseState":
                    return {"assetPositions": [{"position": {"coin": "ZEC", "szi": "5"}}]}
                if body["type"] == "metaAndAssetCtxs":
                    return [{"universe": [{"name": "ZEC"}]}, [{"markPx": "100"}]]
                return [fill(time=1793232001000), fill(time=1793232001000)]

            provider.info = info
            result = await provider.observe(
                watch(), "2026-10-29T00:00:00+00:00", "2026-10-29T00:10:00+00:00"
            )
            assert len(result["fills"]) == 1 and result["realized_pnl"] == "20"
            await provider.close()
        store.engine.dispose()

    asyncio.run(exercise())
