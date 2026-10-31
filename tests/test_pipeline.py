from dataclasses import asdict
import json
import pytest
from liquidity.aggregation import aggregate_levels, cross_venue
from liquidity.backtest import BacktestConfig, backtest, walk_book
from liquidity.book import ResyncRequired
from liquidity.demo import demo_events
from liquidity.engine import Engine
from liquidity.feeds import normalize_binance_market, normalize_bybit_market
from liquidity.liquidations import LiquidationModel
from liquidity.models import Event
from liquidity.storage import Journal, read_events


def test_replay_is_deterministic_and_journal_roundtrip(tmp_path):
    path = tmp_path / "events.sqlite"
    journal = Journal(path)
    live = Engine(journal)
    for e in demo_events(150):
        live.handle(e)
    journal.close()
    replay = Engine()
    for e in read_events(path):
        replay.handle(e)
    assert live.view() == replay.view()
    assert live.signals


def test_engine_gap_invalidates_and_suppresses_metrics(instrument):
    e = Engine()
    e.handle(Event("binance", "BTCUSDT", "instrument", asdict(instrument), 0))
    e.handle(
        Event(
            "binance",
            "BTCUSDT",
            "binance_snapshot",
            {"lastUpdateId": 100, "bids": [["99", "1"]], "asks": [["101", "1"]]},
            1,
        )
    )
    e.handle(
        Event(
            "binance",
            "BTCUSDT",
            "binance_depth",
            {"U": 99, "u": 100, "pu": 98, "b": [], "a": []},
            2,
        )
    )
    with pytest.raises(ResyncRequired):
        e.handle(
            Event(
                "binance",
                "BTCUSDT",
                "binance_depth",
                {"U": 101, "u": 102, "pu": 99, "b": [], "a": []},
                3,
            )
        )
    assert not e.states[instrument.key].book.ready
    assert e.view()["markets"][instrument.key]["derived"] is None


def test_stale_books_excluded_from_normalization():
    e = Engine()
    for event in demo_events(2):
        e.handle(event)
    assert e.view()["cross_venue"]
    assert not e.view(e.now + 4000)["cross_venue"]


def test_decimal_aggregation_preserves_quote():
    result = aggregate_levels([("99.9", "2"), ("99.8", "3")], "1", "bid")
    assert result == [{"price": "99", "base_qty": "5", "quote_notional": "499.2"}]
    assert aggregate_levels([("100.1", "1")], "1", "ask")[0]["price"] == "101"


def test_cross_venue_keeps_contributions():
    e = Engine()
    for event in demo_events(2):
        e.handle(event)
    groups = cross_venue(e.states.values(), e.now)
    contributions = list(groups[0]["depth"]["bid"].values())[0]
    assert set(contributions) == {"binance", "bybit"}


def test_liquidation_model_budget_decay_and_uncertainty():
    m = LiquidationModel()
    m.observe(60000, 1_000_000, 0)
    view = m.view(0)
    assert view["classification"] == "modeled"
    assert "not statistical" in view["uncertainty"]
    for side in ("long", "short"):
        assert (
            sum(b["scenario_quote_notional"] for b in view["bands"] if b["side"] == side)
            <= 1_000_000
        )
    m.observe(60000, 500_000, 1000)
    assert sum(c.notional for c in m.cohorts) == 500_000
    assert m.view(130000)["stale"]


@pytest.mark.parametrize("mark,oi", [(0, 1), (1, -2), (float("nan"), 2)])
def test_invalid_model_data(mark, oi):
    with pytest.raises(ValueError):
        LiquidationModel().observe(mark, oi, 0)


def test_normalizers_aggressor_and_liquidation_semantics():
    kind, t = normalize_binance_market(
        {"e": "aggTrade", "a": 1, "p": "10", "q": "1", "m": True, "T": 1}
    )
    assert kind == "trade" and t["side"] == "sell"
    result = normalize_bybit_market(
        {"topic": "allLiquidation.BTCUSDT", "data": [{"S": "Buy", "p": "10", "v": "1", "T": 1}]}
    )
    assert result[0][1]["position_side"] == "long"


def test_taker_backtest_has_latency_costs_and_no_same_event_fill():
    result = backtest(
        demo_events(600), BacktestConfig(imbalance_threshold=0, require_flow=False, holding_ms=1000)
    )
    assert result["completed_trades"] > 0
    assert all(t["entry_ms"] > t["signal_ms"] for t in result["trades"])
    assert all(t["fees_quote"] > 0 for t in result["trades"])
    assert result["net_quote"] < 0  # Static synthetic mid loses spread and costs.


def test_insufficient_depth_fails_fill(book):
    assert walk_book(book, "buy", 100000) is None
    assert walk_book(book, "buy", 1) == 101


def test_unknown_schema_rejected():
    with pytest.raises(ValueError):
        Engine().handle(Event("binance", "BTCUSDT", "x", {}, schema=2))


def test_jsonl_export(tmp_path):
    p = tmp_path / "events.sqlite"
    j = Journal(p)
    events = list(demo_events(1))
    for e in events:
        j.append(e)
    out = tmp_path / "export.jsonl"
    j.export(out)
    j.close()
    assert [e.record() for e in read_events(out)] == [e.record() for e in events]
    assert all(json.loads(x)["schema"] == 1 for x in out.read_text().splitlines())


def test_feed_start_resets_tape_on_same_metadata_and_replays_across_restart(monkeypatch):
    import asyncio
    from dataclasses import asdict
    from liquidity import feeds
    from liquidity.models import Event, Instrument

    instrument = Instrument("binance", "BTCUSDT")
    recorded = []
    engine = Engine()

    def emit(event):
        recorded.append(event)
        engine.handle(event)

    emit(Event("binance", "BTCUSDT", "instrument", asdict(instrument)))
    emit(
        Event(
            "binance", "BTCUSDT", "trade", {"id": "old", "price": "100", "qty": "5", "side": "buy"}
        )
    )
    assert engine.states[instrument.key].metrics.cvd_base == 5

    async def metadata(*args):
        return instrument

    monkeypatch.setattr(feeds, "load_instrument", metadata)

    async def idle():
        await asyncio.Event().wait()

    async def exercise():
        feed = feeds.LiveFeed(None, "binance", "BTCUSDT", emit)
        feed.binance_book = feed.binance_market = feed.context_loop = idle
        task = asyncio.create_task(feed.run())
        await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(exercise())
    assert engine.states[instrument.key].metrics.cvd_base == 0
    emit(
        Event(
            "binance", "BTCUSDT", "trade", {"id": "new", "price": "100", "qty": "1", "side": "buy"}
        )
    )
    replay = Engine()
    for event in recorded:
        replay.handle(event)
    assert replay.states[instrument.key].metrics.cvd_base == 1
