from decimal import Decimal as D
from liquidity.book import OrderBook
from liquidity.metrics import Metrics
from liquidity.signals import Detectors, DetectorConfig


def make_book(instrument):
    book = OrderBook(instrument)
    book.binance_snapshot(
        {
            "lastUpdateId": 100,
            "bids": [[str(p), "1"] for p in range(95, 100)],
            "asks": [[str(p), "1"] for p in range(101, 106)],
        },
        0,
    )
    book.binance_delta({"U": 99, "u": 100, "pu": 98, "b": [], "a": []}, 0)
    return book


def update(book, p, q, now):
    i = book.last_id
    book.binance_delta({"U": i + 1, "u": i + 1, "pu": i, "b": [[str(p), str(q)]], "a": []}, now)


def test_persistent_and_vanishing_wall(instrument):
    book, m = make_book(instrument), Metrics()
    detector = Detectors(instrument, DetectorConfig(wall_min_quote=1000))
    update(book, 98, 100, 0)
    detector.on_book(book, m, 0, True)
    assert not detector.on_book(book, m, 4999, True)
    signals = detector.on_book(book, m, 5000, True)
    assert signals[0].kind == "persistent_wall_candidate"
    update(book, 98, 1, 6000)
    assert detector.on_book(book, m, 6000, True)[0].kind == "vanishing_wall_candidate"


def test_spoof_risk_requires_repeated_fast_low_execution(instrument):
    book, m = make_book(instrument), Metrics()
    detector = Detectors(instrument, DetectorConfig(wall_min_quote=1000, cooldown_ms=0))
    signals = []
    for i in range(3):
        now = i * 2000
        update(book, 98, 100, now)
        detector.on_book(book, m, now, True)
        update(book, 98, 1, now + 1000)
        signals += detector.on_book(book, m, now + 1000, True)
    assert any(s.kind == "spoof_risk_candidate" for s in signals)
    assert all(s.classification == "inferred" for s in signals)


def test_stale_tape_suppresses_spoof_risk(instrument):
    book, m = make_book(instrument), Metrics()
    d = Detectors(instrument, DetectorConfig(wall_min_quote=1000, cooldown_ms=0))
    out = []
    for i in range(4):
        update(book, 98, 100, i * 2000)
        d.on_book(book, m, i * 2000, False)
        update(book, 98, 1, i * 2000 + 1000)
        out += d.on_book(book, m, i * 2000 + 1000, False)
    assert not any(s.kind == "spoof_risk_candidate" for s in out)


def test_absorption_and_iceberg_need_executions_refills(instrument):
    b, m = make_book(instrument), Metrics()
    d = Detectors(instrument, DetectorConfig(absorption_min_quote=100, cooldown_ms=0))
    d.on_book(b, m, 0, True)
    m.trade("1", D(99), D(5), "sell", 900)
    update(b, 99, 2, 1000)
    d.on_book(b, m, 1000, True)
    update(b, 99, 1, 1200)
    d.on_book(b, m, 1200, True)
    m.trade("2", D(99), D(5), "sell", 1300)
    update(b, 99, 2, 1400)
    d.on_book(b, m, 1400, True)
    out = d.on_trade(b, m, 1400)
    assert {s.kind for s in out} == {"absorption_candidate", "iceberg_candidate"}


def test_large_price_movement_suppresses_absorption(instrument):
    b, m = make_book(instrument), Metrics()
    d = Detectors(instrument, DetectorConfig(absorption_min_quote=100))
    d.mids.extend([(0, 100), (1000, 110)])
    m.trade("1", D(99), D(50), "sell", 1000)
    assert not d.on_trade(b, m, 1000)


def test_reset_clears_candidates(instrument):
    d = Detectors(instrument)
    d.walls["x"] = {}
    d.reset()
    assert not d.walls and not d.refills and not d.cooldowns
