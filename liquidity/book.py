"""Atomic L2 state and venue sequencing. Never trim authoritative state to top N."""

from itertools import islice
from sortedcontainers import SortedDict
from .models import Instrument


class ResyncRequired(ValueError):
    pass


class OrderBook:
    def __init__(self, instrument: Instrument):
        self.instrument = instrument
        self.bids, self.asks = SortedDict(), SortedDict()
        self.ready = False
        self.last_id = None
        self.seq = None
        self.updated_ms = 0
        self.awaiting_bridge = False
        self.epoch = 0

    def invalidate(self):
        self.ready = False
        self.last_id = None
        self.awaiting_bridge = False
        self.bids.clear()
        self.asks.clear()

    def _apply(self, bids, asks, snapshot=False):
        parsed = [[self.instrument.validate_level(p, q) for p, q in x] for x in (bids, asks)]
        sides = (SortedDict(), SortedDict()) if snapshot else (self.bids, self.asks)
        undo = []
        try:
            for side, updates in zip(sides, parsed, strict=True):
                for p, q in updates:
                    undo.append((side, p, side.get(p)))
                    if q:
                        side[p] = q
                    else:
                        side.pop(p, None)
            if not sides[0] or not sides[1] or sides[0].peekitem(-1)[0] >= sides[1].peekitem(0)[0]:
                raise ResyncRequired("empty or crossed book")
        except Exception:
            for side, p, old in reversed(undo):
                if old is None:
                    side.pop(p, None)
                else:
                    side[p] = old
            raise
        self.bids, self.asks = sides

    def binance_snapshot(self, data, now):
        self._apply(data["bids"], data["asks"], snapshot=True)
        self.last_id = int(data["lastUpdateId"])
        self.awaiting_bridge = True
        self.ready = False
        self.updated_ms = now
        self.epoch += 1

    def binance_delta(self, data, now):
        if self.last_id is None:
            raise ResyncRequired("snapshot required")
        first, last = int(data["U"]), int(data["u"])
        if first > last:
            raise ResyncRequired("invalid update range")
        if self.awaiting_bridge:
            if last < self.last_id:
                return False
            # USD-M includes equality; this is not the spot +1 protocol.
            if not first <= self.last_id <= last:
                raise ResyncRequired("snapshot too old to bridge buffered events")
        else:
            if last <= self.last_id:
                return False
            if int(data["pu"]) != self.last_id:
                raise ResyncRequired("Binance pu chain broken")
        self._apply(data["b"], data["a"])
        self.last_id, self.updated_ms = last, now
        self.ready, self.awaiting_bridge = True, False
        return True

    def bybit_message(self, message, now):
        data = message["data"]
        update_id, seq = int(data["u"]), int(data["seq"])
        snapshot = message["type"] == "snapshot"
        if update_id == 1 and not snapshot:
            raise ResyncRequired("Bybit restart needs snapshot")
        if not snapshot:
            if not self.ready:
                raise ResyncRequired("Bybit delta before snapshot")
            if update_id == self.last_id and seq == self.seq:
                return False
            if update_id <= self.last_id or seq <= self.seq:
                raise ResyncRequired("Bybit sequence regressed")
            # u/seq are NOT documented as consecutive. Do not enforce +1.
        self._apply(data["b"], data["a"], snapshot=snapshot)
        self.last_id, self.seq, self.updated_ms = update_id, seq, now
        self.ready = True
        if snapshot:
            self.epoch += 1
        return True

    def top(self, side, count=20):
        items = reversed(self.bids.items()) if side == "bid" else iter(self.asks.items())
        return list(islice(items, count))

    def bbo(self):
        return (*self.bids.peekitem(-1), *self.asks.peekitem(0))

    def view(self, count=50):
        return {
            side: [[str(p), str(q)] for p, q in self.top(side, count)] for side in ("bid", "ask")
        }
