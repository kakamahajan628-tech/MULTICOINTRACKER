from decimal import Decimal as D
import random
import pytest
from liquidity.book import OrderBook, ResyncRequired
from liquidity.models import Instrument


def delta(first=101, last=102, previous=100, bids=None, asks=None):
    return {"U": first, "u": last, "pu": previous, "b": bids or [], "a": asks or []}


def test_binance_bridge_equality(instrument):
    b = OrderBook(instrument)
    b.binance_snapshot({"lastUpdateId": 100, "bids": [["99", "1"]], "asks": [["101", "1"]]}, 0)
    assert not b.ready
    assert not b.binance_delta(delta(98, 99, 97), 1)
    assert b.binance_delta(delta(99, 100, 98), 2)
    assert b.ready


def test_futures_bridge_is_not_spot_plus_one(instrument):
    b = OrderBook(instrument)
    b.binance_snapshot({"lastUpdateId": 100, "bids": [["99", "1"]], "asks": [["101", "1"]]}, 0)
    with pytest.raises(ResyncRequired):
        b.binance_delta(delta(101, 102, 100), 1)


def test_gap_rejected(book):
    before = book.view()
    with pytest.raises(ResyncRequired):
        book.binance_delta(delta(previous=99, bids=[["99", "90"]]), 2)
    assert book.view() == before


def test_absolute_quantity_delete_duplicate(book):
    assert book.binance_delta(delta(bids=[["99", "3"], ["97", "0"]]), 2)
    assert book.bids[D("99")] == 3
    assert not book.binance_delta(delta(bids=[["99", "100"]]), 3)
    assert book.bids[D("99")] == 3


@pytest.mark.parametrize(
    "price,qty",
    [("NaN", "1"), ("0", "1"), ("99.01", "1"), ("99", "-1"), ("99", "0.0001"), ("Infinity", "1")],
)
def test_invalid_precision_atomic(book, price, qty):
    before = book.view()
    with pytest.raises(ValueError):
        book.binance_delta(delta(bids=[["98", "5"], [price, qty]]), 2)
    assert book.view() == before


def test_crossed_update_atomic(book):
    before = book.view()
    with pytest.raises(ResyncRequired):
        book.binance_delta(delta(bids=[["98", "5"], ["102", "1"]]), 2)
    assert book.view() == before


def bybit(kind="snapshot", u=10, seq=100, bids=None):
    return {
        "type": kind,
        "data": {"u": u, "seq": seq, "b": bids or [["99", "1"]], "a": [["101", "1"]]},
    }


def test_bybit_requires_snapshot(instrument):
    b = OrderBook(instrument)
    with pytest.raises(ResyncRequired):
        b.bybit_message(bybit("delta"), 0)


def test_bybit_nonconsecutive_ids_and_duplicate(instrument):
    b = OrderBook(instrument)
    b.bybit_message(bybit(), 0)
    assert b.bybit_message(bybit("delta", 19, 500), 1)
    assert not b.bybit_message(bybit("delta", 19, 500), 2)
    with pytest.raises(ResyncRequired):
        b.bybit_message(bybit("delta", 18, 490), 3)


def test_bybit_restart_overwrites(instrument):
    b = OrderBook(instrument)
    b.bybit_message(bybit(), 0)
    b.bybit_message(bybit("snapshot", 1, 1, [["98", "2"]]), 1)
    assert D("99") not in b.bids
    assert b.epoch == 2


def test_random_absolute_updates_match_reference(book):
    rng = random.Random(27)
    bids = dict(book.bids)
    for i in range(100):
        p, q = D(90 + rng.randrange(10)), D(rng.randrange(10))
        book.binance_delta(delta(101 + i, 101 + i, 100 + i, [[str(p), str(q)]]), i + 2)
        if q:
            bids[p] = q
        else:
            bids.pop(p, None)
        assert dict(book.bids) == bids


def test_does_not_trim_authoritative_book(book):
    changes = [[str(50 + i / 10), "1"] for i in range(300)]
    book.binance_delta(delta(bids=changes), 2)
    book.top("bid", 10)
    assert len(book.bids) == 302


def test_reject_inverse_and_nonusdt():
    with pytest.raises(ValueError):
        Instrument("bybit", "BTCUSD", quote="USD", product="inverse")


def test_resting_prices_keep_data_precision_separate_from_current_order_filters():
    instrument = Instrument(
        "binance",
        "SOLUSDT",
        base="SOL",
        tick="0.01",
        qty_step="0.01",
        price_quantum="0.0001",
        qty_quantum="0.01",
    )
    book = OrderBook(instrument)
    book.binance_snapshot(
        {"lastUpdateId": 100, "bids": [["105.0020", "1.00"]], "asks": [["105.5290", "2.00"]]}, 0
    )
    assert book.binance_delta(delta(99, 101, 98, [["105.0020", "3.00"]]), 1)
    assert book.bids[D("105.0020")] == 3
    assert instrument.tick == "0.01"  # New-order constraints remain explicit.
    with pytest.raises(ValueError):
        book.binance_delta(delta(102, 103, 101, [["105.00201", "1.00"]]), 2)
    assert book.bids[D("105.0020")] == 3


def test_data_quantum_must_be_compatible_with_order_filter():
    with pytest.raises(ValueError):
        Instrument("binance", "SOLUSDT", tick="0.01", price_quantum="0.03")


def test_binance_metadata_keeps_both_data_and_order_precision(monkeypatch):
    import asyncio
    from liquidity import feeds

    async def metadata(*args, **kwargs):
        return {
            "symbols": [
                {
                    "symbol": "SOLUSDT",
                    "contractType": "PERPETUAL",
                    "status": "TRADING",
                    "baseAsset": "SOL",
                    "quoteAsset": "USDT",
                    "pricePrecision": 4,
                    "quantityPrecision": 2,
                    "filters": [
                        {"filterType": "PRICE_FILTER", "tickSize": "0.0100"},
                        {"filterType": "LOT_SIZE", "stepSize": "0.01"},
                    ],
                }
            ]
        }

    monkeypatch.setattr(feeds, "get_json", metadata)
    instrument = asyncio.run(feeds.load_instrument(None, "binance", "SOLUSDT"))
    assert instrument.tick == "0.0100" and instrument.price_quantum == "0.0001"
    assert instrument.qty_step == instrument.qty_quantum == "0.01"
    error = ExceptionGroup("stream tasks", [ValueError("bad level")])
    assert feeds.error_summary(error) == "ValueError: bad level"
