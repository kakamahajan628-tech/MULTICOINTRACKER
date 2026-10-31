import asyncio
from dataclasses import asdict
import json

import pytest
from fastapi.testclient import TestClient

from liquidity.demo import demo_events
from liquidity.models import Event, Instrument, Signal
from whale_watcher.core import Store
from whale_watcher.liquidity_context import correlate_cycle, summarize_context
from whale_watcher.liquidity_service import LiquidityService
from whale_watcher.screening import features_from_cycles, merge_features, build_screen


class MemoryStore:
    def __init__(self, rows=()):
        self.values = {"wallets": list(rows)}
        self.outbox = []

    def get(self, key, default=None):
        return self.values.get(key, default)

    def enqueue_report(self, source, text):
        self.outbox.append((source, text))


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


def cycle():
    from whale_watcher.reporting import make_cycle

    row = wallet(
        amount="12",
        previous="10",
        source="mock_rpc",
        state="increased",
        actions=[],
        previous_observed_at="2026-10-28T10:00:00+00:00",
        observed_at="2026-10-28T10:10:10+00:00",
    )
    return make_cycle(
        "cycle-1",
        "2026-10-28T10:10:00+00:00",
        "2026-10-28T10:10:30+00:00",
        "2026-10-28T10:00:00+00:00",
        [row],
    )


def context():
    return {
        "mode": "live",
        "received_ms": 1000000,
        "markets": {
            "binance:ETHUSDT": {
                "quality": {"fresh": True, "tape_connected": True, "tape_age_ms": 10},
                "derived": {"cvd_base": 5, "ofi_10s_base": 2},
            }
        },
        "inferred_signals": [
            Signal(
                "absorption_candidate", "binance", "ETHUSDT", 999999, {"passive_side": "bid"}, 0.6
            ).record()
        ],
    }


def test_one_feed_per_coin_venue_for_1000_wallets(monkeypatch):
    monkeypatch.setenv("WHALE_LIQUIDITY_ENABLED", "true")
    monkeypatch.setenv("WHALE_LIQUIDITY_MODE", "live")
    store = MemoryStore([wallet(id=str(i), address="0x" + f"{i:040x}") for i in range(1000)])
    calls = []

    class FakeFeed:
        def __init__(self, session, venue, symbol, emit):
            calls.append((venue, symbol))

        async def run(self):
            await asyncio.Event().wait()

    async def exercise():
        service = LiquidityService(store, feed_factory=FakeFeed)
        await service.reconcile()
        await service.reconcile()
        assert len(service.tasks) == 2
        assert calls == [("binance", "ETHUSDT"), ("bybit", "ETHUSDT")]
        store.values["wallets"] = []
        await service.reconcile()
        assert not service.tasks
        await service.close()

    asyncio.run(exercise())


def test_contracts_need_explicit_mapping_and_cap(monkeypatch):
    monkeypatch.setenv("WHALE_LIQUIDITY_ENABLED", "true")
    monkeypatch.setenv("WHALE_LIQUIDITY_MAX_MARKETS", "1")
    store = MemoryStore([wallet(asset="0x" + "b" * 40, coin="ETH")])

    async def exercise():
        service = LiquidityService(store)
        await service.reconcile()
        assert not service.targets and service.unmapped
        store.values["market_asset_links"] = {"ethereum:0x" + "b" * 40: "ETH"}

        class FakeFeed:
            def __init__(self, *args):
                pass

            async def run(self):
                await asyncio.Event().wait()

        service.feed_factory = FakeFeed
        await service.reconcile()
        assert service.targets == ["binance:ETHUSDT"]
        assert service.deferred == ["bybit:ETHUSDT"]
        await service.close()

    asyncio.run(exercise())


def test_join_uses_verified_asset_and_keeps_wallet_evidence_separate():
    result = correlate_cycle(cycle(), context())
    asset = result["assets"][0]
    assert asset["wallet_evidence"]["net_spot_quantity"] == "2"
    assert asset["candidates"] == ["wallet_increase_with_bid_absorption_candidate"]
    assert asset["classification"] == "inferred_association"
    assert "not a confirmed trade" in asset["wallet_evidence"]["meaning"]
    assert "scan start" in asset["temporal_scope"]


@pytest.mark.parametrize("change", ["stale", "stale_tape", "synthetic"])
def test_no_live_association_from_stale_or_synthetic(change):
    c = context()
    if change == "stale":
        c["markets"]["binance:ETHUSDT"]["quality"]["fresh"] = False
    elif change == "stale_tape":
        c["markets"]["binance:ETHUSDT"]["quality"]["tape_age_ms"] = 99999
    else:
        c["mode"] = "demo"
    assert correlate_cycle(cycle(), c)["assets"][0]["candidates"] == []


def test_no_contract_join_from_same_ticker():
    c = cycle()
    c["reports"]["Spot Profit"]["rows"][0]["asset"] = "0x" + "b" * 40
    result = correlate_cycle(c, context())
    assert result["assets"] == [] and result["unmapped_assets"]


def test_baseline_source_change_and_duplicate_wallet_are_excluded():
    c = cycle()
    row = c["reports"]["Spot Profit"]["rows"][0]
    row["balance_source_changed"] = True
    result = correlate_cycle(c, context())
    assert result["assets"][0]["wallet_evidence"]["comparable_spot_wallets"] == 0


def test_hourly_and_four_hour_context_do_not_sum_flow():
    c = cycle()
    c["wallet_liquidity"] = correlate_cycle(c, context())
    c["window_end"] = c["started_at"]
    f = features_from_cycles([c])
    assert len(f[0]["liquidity_samples"]) == 1
    report = build_screen([c], "1h", 0)
    summary = report["assets"][0]["metrics"]["liquidity_context"]
    assert summary["samples"] == 1
    assert summary["latest_markets"]["binance"]["derived"]["cvd_base"] == 5
    merged = merge_features([report, report])
    assert len(merged[0]["liquidity_samples"]) == 1
    assert summarize_context(merged[0]["liquidity_samples"])["causal_attribution"] is False


def test_compact_snapshot_is_bounded_and_removes_heatmap(monkeypatch):
    service = LiquidityService(MemoryStore(), clock=lambda: 1700000006000)
    for event in demo_events(30):
        service.emit(event)
    snapshot = service.compact_snapshot()
    assert "cross_venue" not in snapshot
    for market in snapshot["markets"].values():
        assert "history" not in market
        assert "book" not in market["observed"]
    assert len(json.dumps(snapshot)) < 50000


def test_existing_telegram_outbox_is_reused(tmp_path, monkeypatch):
    monkeypatch.setenv("WHALE_LIQUIDITY_ALERTS", "true")
    monkeypatch.setenv("WHALE_TELEGRAM_OWNER_ID", "123")
    store = Store("sqlite:///" + (tmp_path / "outbox.sqlite").as_posix())
    service = LiquidityService(store)
    service.pending_alerts.append(Signal("iceberg_candidate", "binance", "ETHUSDT", 1, {}, 0.8))
    service.flush_alerts()
    assert "iceberg_candidate" in store.pending_deliveries()[0]["text"]
    store.engine.dispose()


def test_alert_optin_and_synthetic_do_not_send(monkeypatch):
    monkeypatch.setenv("WHALE_LIQUIDITY_MODE", "demo")
    store = MemoryStore()
    service = LiquidityService(store)
    service.pending_alerts.append(Signal("iceberg_candidate", "binance", "BTCUSDT", 1, {}, 0.8))
    service.flush_alerts()
    assert store.outbox == []


def test_liquidity_routes_share_wallet_auth(tmp_path, monkeypatch):
    import whale_watcher.app as module

    monkeypatch.setenv("WHALE_DATABASE_URL", "sqlite:///" + (tmp_path / "app.sqlite").as_posix())
    monkeypatch.setenv("WHALE_ADMIN_TOKEN", "test-secret-unit-testing-only-32-characters")
    monkeypatch.setenv("WHALE_LIQUIDITY_ENABLED", "false")
    monkeypatch.delenv("RENDER", raising=False)
    with TestClient(module.app) as client:
        assert client.get("/api/liquidity").status_code == 401
        auth = ("whale", "test-secret-unit-testing-only-32-characters")
        assert client.get("/api/liquidity", auth=auth).status_code == 200
        assert client.get("/api/liquidity/state", auth=auth).status_code == 200
        page = client.get("/liquidity", auth=auth)
        assert page.status_code == 200 and "/liquidity-static/app.js" in page.text
        assert "Wallet activity + liquidity context" in client.get("/", auth=auth).text
        result = asyncio.run(module.command("/liquidity ETH"))
        assert "LIQUIDITY CONTEXT" in result["report"]


def test_recording_failure_is_visible_and_collector_fails_closed(monkeypatch):
    class BrokenJournal:
        def append(self, event):
            raise OSError("disk full")

    service = LiquidityService(MemoryStore())
    service.engine.journal = BrokenJournal()
    instrument = Instrument("binance", "BTCUSDT")
    with pytest.raises(OSError):
        service.emit(Event("binance", "BTCUSDT", "instrument", asdict(instrument), 1))
    assert "Recording unavailable" in service.fatal_error
    with pytest.raises(RuntimeError):
        service.emit(Event("binance", "BTCUSDT", "instrument", asdict(instrument), 2))


def test_delayed_receipt_does_not_refresh_book_health(instrument):
    from liquidity.engine import Engine

    e = Engine()
    e.handle(Event("binance", "BTCUSDT", "instrument", asdict(instrument), 10000))
    e.handle(
        Event(
            "binance",
            "BTCUSDT",
            "binance_snapshot",
            {"lastUpdateId": 100, "bids": [["99", "1"]], "asks": [["101", "1"]]},
            10000,
        )
    )
    e.handle(
        Event(
            "binance",
            "BTCUSDT",
            "binance_depth",
            {"U": 99, "u": 100, "pu": 98, "b": [], "a": []},
            10000,
        )
    )
    # Another venue advances the engine clock while Binance delivers an old buffered event.
    e.now = 20000
    e.handle(
        Event(
            "binance",
            "BTCUSDT",
            "binance_depth",
            {"U": 101, "u": 101, "pu": 100, "b": [], "a": []},
            11000,
        )
    )
    assert e.states[instrument.key].book.updated_ms == 11000
    assert e.view()["markets"][instrument.key]["derived"] is None


def test_demo_trade_ids_do_not_repeat_across_cycles():
    first = {e.payload["id"] for e in demo_events(2, start_ms=1000) if e.kind == "trade"}
    second = {e.payload["id"] for e in demo_events(2, start_ms=2000) if e.kind == "trade"}
    assert first.isdisjoint(second)


def test_view_does_not_mutate_trade_history(book):
    from liquidity.metrics import Metrics
    from decimal import Decimal

    m = Metrics()
    m.trade("1", Decimal(100), Decimal(1), "buy", 100)
    original = list(m.trades)
    m.view(book, 100000)
    assert list(m.trades) == original


def test_durable_on_off_control_restores_only_needed_feeds(monkeypatch):
    monkeypatch.setenv("WHALE_LIQUIDITY_ENABLED", "true")
    store = MemoryStore([wallet()])
    store.put = lambda key, value: store.values.update({key: value})

    class FakeFeed:
        def __init__(self, *args):
            pass

        async def run(self):
            await asyncio.Event().wait()

    async def exercise():
        service = LiquidityService(store, feed_factory=FakeFeed)
        await service.start()
        assert len(service.tasks) == 2
        await service.set_enabled(False)
        assert not service.tasks and store.values["liquidity_enabled"] is False
        await service.set_enabled(True)
        assert len(service.tasks) == 2
        await service.close()

    asyncio.run(exercise())


def test_demo_alerts_remain_suppressed_even_when_enabled(monkeypatch):
    monkeypatch.setenv("WHALE_LIQUIDITY_MODE", "demo")
    monkeypatch.setenv("WHALE_LIQUIDITY_ALERTS", "true")
    monkeypatch.setenv("WHALE_TELEGRAM_OWNER_ID", "123")
    store = MemoryStore()
    service = LiquidityService(store)
    service.pending_alerts.append(Signal("iceberg_candidate", "binance", "BTCUSDT", 1, {}, 0.8))
    service.flush_alerts()
    assert not store.outbox


def test_journal_roundtrip_includes_failed_sequence_and_resync(tmp_path, instrument):
    from liquidity.storage import Journal, read_events
    from liquidity.book import ResyncRequired
    from liquidity.engine import Engine

    events = [
        Event("binance", "BTCUSDT", "instrument", asdict(instrument), 1),
        Event(
            "binance",
            "BTCUSDT",
            "binance_snapshot",
            {"lastUpdateId": 100, "bids": [["99", "1"]], "asks": [["101", "1"]]},
            2,
        ),
        Event(
            "binance",
            "BTCUSDT",
            "binance_depth",
            {"U": 99, "u": 100, "pu": 98, "b": [], "a": []},
            3,
        ),
        Event(
            "binance",
            "BTCUSDT",
            "binance_depth",
            {"U": 101, "u": 102, "pu": 99, "b": [], "a": []},
            4,
        ),
        Event(
            "binance",
            "BTCUSDT",
            "binance_snapshot",
            {"lastUpdateId": 200, "bids": [["99", "1"]], "asks": [["101", "1"]]},
            5,
        ),
        Event(
            "binance",
            "BTCUSDT",
            "binance_depth",
            {"U": 199, "u": 200, "pu": 198, "b": [], "a": []},
            6,
        ),
    ]
    journal = Journal(tmp_path / "gap.sqlite")
    original = Engine(journal)

    def run(engine, sequence):
        gaps = 0
        for event in sequence:
            try:
                engine.handle(event)
            except ResyncRequired:
                gaps += 1
        return gaps

    assert run(original, events) == 1
    journal.close()
    replay = Engine()
    assert run(replay, read_events(tmp_path / "gap.sqlite")) == 1
    assert replay.view() == original.view()


def test_low_priced_coin_uses_price_adaptive_shared_buckets():
    from types import SimpleNamespace
    from liquidity.book import OrderBook
    from liquidity.aggregation import cross_venue
    from decimal import Decimal

    states = []
    for venue in ("binance", "bybit"):
        book = OrderBook(
            Instrument(venue, "PEPEUSDT", base="PEPE", tick="0.00000001", qty_step="1")
        )
        book.binance_snapshot(
            {
                "lastUpdateId": 100,
                "bids": [["0.00001000", "1000000"]],
                "asks": [["0.00001001", "1000000"]],
            },
            1,
        )
        book.binance_delta({"U": 99, "u": 100, "pu": 98, "b": [], "a": []}, 2)
        states.append(SimpleNamespace(book=book))
    group = cross_venue(states, 2)[0]
    assert Decimal(group["bucket_tick"]) == Decimal("0.00000001")
    assert all(Decimal(p) > 0 for p in group["depth"]["bid"])
    assert max(Decimal(p) for p in group["depth"]["ask"]) < Decimal("0.001")


def test_incremental_history_returns_only_new_columns(monkeypatch):
    service = LiquidityService(MemoryStore(), clock=lambda: 1700000006000)
    for e in demo_events(30):
        service.emit(e)
    full = service.snapshot(history_market="binance:BTCUSDT")
    rows = full["markets"]["binance:BTCUSDT"]["history"]
    assert rows
    last = rows[-1]["t"]
    incremental = service.snapshot(history_market="binance:BTCUSDT", since_ms=last)
    assert incremental["markets"]["binance:BTCUSDT"]["history"] == []
    assert len(json.dumps(incremental)) < len(json.dumps(full))


def test_bybit_snapshot_resets_chart_epoch(instrument):
    from liquidity.engine import Engine

    e = Engine()
    for event in demo_events(5):
        e.handle(event)
    state = e.states["bybit:BTCUSDT"]
    previous_epoch = state.book.epoch
    e.handle(
        Event(
            "bybit",
            "BTCUSDT",
            "bybit_depth",
            {
                "type": "snapshot",
                "data": {"u": 1, "seq": 1, "b": [["59999", "1"]], "a": [["60001", "1"]]},
            },
            e.now + 1,
        )
    )
    assert state.book.epoch == previous_epoch + 1
    assert len(state.history) == 1
