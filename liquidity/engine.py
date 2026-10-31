from collections import deque
from dataclasses import asdict, dataclass, field
from .book import OrderBook, ResyncRequired
from .metrics import Metrics
from .models import Instrument, decimal
from .signals import Detectors, DetectorConfig
from .liquidations import LiquidationModel
from .aggregation import cross_venue


@dataclass
class MarketState:
    book: OrderBook
    metrics: Metrics
    detectors: Detectors
    liquidation_model: LiquidationModel = field(default_factory=LiquidationModel)
    tape_connected: bool = False
    last_trade_ms: int = 0
    last_error: str = ""
    resyncs: int = 0
    observations: deque = field(default_factory=lambda: deque(maxlen=100))
    history: deque = field(default_factory=lambda: deque(maxlen=240))
    last_history_ms: int = 0


class Engine:
    def __init__(self, journal=None, config=None):
        self.states = {}
        self.journal = journal
        self.config = config or DetectorConfig()
        self.signals = deque(maxlen=500)
        self.event_count = 0
        self.now = 0

    def handle(self, event):
        if event.schema != 1:
            raise ValueError("unsupported event schema")
        self.now = max(self.now, event.received_ms)
        event_id = self.journal.append(event) if self.journal else None
        self.event_count += 1
        data, now = event.payload, self.now
        if event.kind == "instrument":
            instrument = Instrument(**data)
            if instrument.key != event.key:
                raise ValueError("instrument/event identity mismatch")
            if event.key in self.states:
                # Metadata may change on reconnect: discard incompatible state.
                old = self.states[event.key]
                if old.book.instrument == instrument:
                    return []
            self.states[event.key] = MarketState(
                OrderBook(instrument), Metrics(), Detectors(instrument, self.config)
            )
            return []
        if event.key not in self.states:
            raise ValueError("instrument metadata must precede observations")
        state = self.states[event.key]
        book, metrics, detectors = state.book, state.metrics, state.detectors
        output = []
        try:
            if event.kind == "status":
                if data["scope"] in ("book", "all"):
                    book.invalidate()
                    detectors.reset()
                    metrics.reset_book()
                    state.resyncs += 1
                if data["scope"] in ("market", "all"):
                    state.tape_connected = data.get("connected", False)
                    if not state.tape_connected:
                        metrics.reset_tape()
                        detectors.reset()
                state.last_error = data.get("reason", "")
            elif event.kind == "binance_snapshot":
                book.binance_snapshot(data, event.received_ms)
                state.history.clear()
                state.last_history_ms = 0
                metrics.reset_book()
                detectors.reset()
            elif event.kind in ("binance_depth", "bybit_depth"):
                was_ready, previous_ms = book.ready, book.updated_ms
                snapshot = event.kind == "bybit_depth" and data["type"] == "snapshot"
                if snapshot:
                    state.history.clear()
                    state.last_history_ms = 0
                if snapshot or now - previous_ms > self.config.stale_ms:
                    metrics.reset_book()
                    detectors.reset()
                changed = (
                    book.binance_delta(data, event.received_ms)
                    if event.kind == "binance_depth"
                    else book.bybit_message(data, event.received_ms)
                )
                if changed:
                    state.last_error = ""
                    metrics.on_book(book, now)
                    tape_fresh = (
                        state.tape_connected and now - state.last_trade_ms <= self.config.stale_ms
                    )
                    if now - book.updated_ms <= self.config.stale_ms:
                        if was_ready and not snapshot:
                            output += detectors.on_book(book, metrics, now, tape_fresh)
                        else:
                            detectors.on_book(book, metrics, now, False)
                    else:
                        detectors.reset()
                    if now - state.last_history_ms >= 500:
                        state.history.append(
                            {
                                "t": now,
                                "mid": float((book.bbo()[0] + book.bbo()[2]) / 2),
                                "cvd": float(metrics.cvd_base),
                                **book.view(100),
                            }
                        )
                        state.last_history_ms = now
            elif event.kind == "trade":
                p, q = book.instrument.validate_level(data["price"], data["qty"])
                if q <= 0 or data["side"] not in ("buy", "sell"):
                    raise ValueError("invalid trade")
                if metrics.trade(str(data["id"]), p, q, data["side"], now):
                    state.last_trade_ms = now
                    if (
                        state.tape_connected
                        and book.ready
                        and now - book.updated_ms <= self.config.stale_ms
                    ):
                        output += detectors.on_trade(book, metrics, now)
            elif event.kind == "context":
                state.liquidation_model.observe(
                    float(decimal(data["mark"])), float(decimal(data["oi_quote"])), now
                )
                state.observations.append({"kind": "mark_and_oi", "received_ms": now, **data})
            elif event.kind == "liquidation":
                state.observations.append(
                    {"kind": "reported_liquidation", "received_ms": now, **data}
                )
            else:
                raise ValueError(f"unknown event kind {event.kind}")
        except (ValueError, KeyError, TypeError) as exc:
            if event.kind in ("binance_snapshot", "binance_depth", "bybit_depth"):
                book.invalidate()
                metrics.reset_book()
                detectors.reset()
                state.last_error = str(exc)
                raise ResyncRequired(str(exc)) from exc
            raise
        for signal in output:
            self.signals.append(signal)
            if self.journal:
                self.journal.signal(event_id, signal)
        if self.journal and self.journal.pending >= 100:
            self.journal.flush()
        return output

    def view(self, now=None, *, include_history=True, depth=50):
        now = self.now if now is None else now
        markets = {}
        for key, state in self.states.items():
            book = state.book
            fresh = book.ready and now - book.updated_ms <= self.config.stale_ms
            markets[key] = {
                "instrument": asdict(book.instrument),
                "quality": {
                    "book_ready": book.ready,
                    "fresh": fresh,
                    "book_age_ms": now - book.updated_ms if book.updated_ms else None,
                    "book_epoch": book.epoch,
                    "last_update_id": book.last_id,
                    "sequence_guarantee": "pu-chain"
                    if book.instrument.venue == "binance"
                    else "ordered WS; monotonic IDs, no consecutive-ID guarantee",
                    "tape_connected": state.tape_connected,
                    "tape_age_ms": now - state.last_trade_ms if state.last_trade_ms else None,
                    "cvd_scope": "observed stream segment since latest market disconnect",
                    "resyncs": state.resyncs,
                    "last_error": state.last_error,
                },
                "observed": {
                    "book": book.view(depth) if fresh else None,
                    "recent": list(state.observations),
                },
                "derived": state.metrics.view(book, now) if fresh else None,
                "modeled_liquidations": state.liquidation_model.view(now),
                "history": list(state.history) if include_history else [],
            }
        return {
            "schema": 1,
            "received_ms": now,
            "event_count": self.event_count,
            "markets": markets,
            "inferred_signals": [s.record() for s in self.signals],
            "cross_venue": cross_venue(self.states.values(), now, stale_ms=self.config.stale_ms),
        }
