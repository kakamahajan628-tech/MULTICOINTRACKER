"""Derived measurements from visible L2 and exchange-reported aggressor trades."""

from collections import deque
from decimal import Decimal


def book_imbalance(book, n=20):
    # Adapted from mamonet/orderbook-heatmap index.html bookImbalance, MIT.
    # Copyright (c) 2026 mamonet. Changes: Python/Decimal and sorted L2 state.
    bid = sum((q for _, q in book.top("bid", n)), Decimal(0))
    ask = sum((q for _, q in book.top("ask", n)), Decimal(0))
    return float((bid - ask) / (bid + ask)) if bid + ask else 0.0


class Metrics:
    def __init__(self):
        self.cvd_base = Decimal(0)
        self.cvd_quote = Decimal(0)
        self.last_bbo = None
        self.ofi = deque(maxlen=100000)
        self.last_ofi = Decimal(0)
        self.seen, self.ids = set(), deque()
        self.trades = deque(maxlen=100000)
        self.cvd_epoch = 0

    def reset_book(self):
        self.last_bbo = None
        self.ofi.clear()
        self.last_ofi = Decimal(0)

    def reset_tape(self):
        self.cvd_base = self.cvd_quote = Decimal(0)
        self.trades.clear()
        self.cvd_epoch += 1

    def trade(self, trade_id, price, qty, side, now):
        if trade_id in self.seen:
            return False
        self.seen.add(trade_id)
        self.ids.append(trade_id)
        if len(self.ids) > 50000:
            self.seen.remove(self.ids.popleft())
        signed = qty if side == "buy" else -qty
        self.cvd_base += signed
        self.cvd_quote += signed * price
        self.trades.append((now, price, qty, side))
        self.trim(now)
        return True

    def trim(self, now):
        while self.trades and self.trades[0][0] < now - 10000:
            self.trades.popleft()
        while self.ofi and self.ofi[0][0] < now - 10000:
            self.ofi.popleft()

    def on_book(self, book, now):
        bid, bq, ask, aq = book.bbo()
        self.last_ofi = Decimal(0)
        if self.last_bbo:
            pb, pbq, pa, paq = self.last_bbo
            e = (bq if bid >= pb else 0) - (pbq if bid <= pb else 0)
            e -= (aq if ask <= pa else 0) - (paq if ask >= pa else 0)
            self.last_ofi = Decimal(e)
            self.ofi.append((now, self.last_ofi))
        self.last_bbo = (bid, bq, ask, aq)
        self.trim(now)

    def view(self, book, now):
        bid, bq, ask, aq = book.bbo()
        mid = (bid + ask) / 2
        rolling = sum(
            (q if s == "buy" else -q for t, p, q, s in self.trades if t >= now - 5000), Decimal(0)
        )
        return {
            "mid": float(mid),
            "spread_bps": float((ask - bid) / mid * 10000),
            "microprice": float((ask * bq + bid * aq) / (bq + aq)),
            "imbalance_top20": book_imbalance(book),
            "cvd_base": float(self.cvd_base),
            "cvd_quote": float(self.cvd_quote),
            "cvd_epoch": self.cvd_epoch,
            "delta_5s_base": float(rolling),
            "ofi_last_base": float(self.last_ofi),
            "ofi_10s_base": float(sum((e for t, e in self.ofi if t >= now - 10000), Decimal(0))),
        }
