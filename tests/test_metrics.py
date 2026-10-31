from decimal import Decimal as D
from liquidity.metrics import Metrics, book_imbalance


def test_cvd_sign_and_dedup():
    m = Metrics()
    assert m.trade("1", D(100), D(2), "buy", 100)
    assert not m.trade("1", D(100), D(2), "buy", 101)
    m.trade("2", D(101), D(1), "sell", 102)
    assert m.cvd_base == 1
    assert m.cvd_quote == 99


def test_cvd_reset_retains_dedup():
    m = Metrics()
    m.trade("1", D(100), D(2), "buy", 100)
    m.reset_tape()
    assert m.cvd_base == 0
    assert m.cvd_epoch == 1
    assert not m.trade("1", D(100), D(2), "buy", 101)


def test_cont_ofi_price_improvement(book):
    m = Metrics()
    m.on_book(book, 1)
    book.binance_delta({"U": 101, "u": 101, "pu": 100, "b": [["100", "4"]], "a": []}, 2)
    m.on_book(book, 2)
    assert m.last_ofi == 4
    assert -1 <= book_imbalance(book) <= 1


def test_cont_ofi_same_quote_sizes(book):
    m = Metrics()
    m.on_book(book, 1)
    book.binance_delta(
        {"U": 101, "u": 101, "pu": 100, "b": [["99", "12"]], "a": [["101", "23"]]}, 2
    )
    m.on_book(book, 2)
    assert m.last_ofi == -1
    assert m.view(book, 10003)["ofi_10s_base"] == 0


def test_rolling_delta_expires(book):
    m = Metrics()
    m.trade("1", D(100), D(2), "buy", 1)
    assert m.view(book, 6001)["delta_5s_base"] == 0
    assert m.view(book, 6001)["cvd_base"] == 2
