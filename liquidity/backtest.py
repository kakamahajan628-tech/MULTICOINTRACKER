"""Causal taker-only research harness; orders execute on future observed books."""

from dataclasses import asdict, dataclass
from .book import ResyncRequired
from .engine import Engine


def walk_book(book, side, base_qty):
    remaining, quote = base_qty, 0.0
    for p, q in book.top("ask" if side == "buy" else "bid", 100000):
        taken = min(remaining, float(q))
        quote += taken * float(p)
        remaining -= taken
        if remaining <= 1e-12:
            return quote / base_qty
    return None


@dataclass
class BacktestConfig:
    market: str = "binance:BTCUSDT"
    notional: float = 1000
    latency_ms: int = 250
    holding_ms: int = 5000
    fee_bps: float = 4
    slippage_bps: float = 1
    imbalance_threshold: float = 0.15
    require_flow: bool = True


def backtest(events, config=None):
    c = config or BacktestConfig()
    if c.notional <= 0 or min(c.latency_ms, c.holding_ms, c.fee_bps, c.slippage_bps) < 0:
        raise ValueError("invalid backtest parameters")
    engine, rows = Engine(), []
    pending, position = None, None
    last_exit, invalidated = -(10**18), 0
    for event in events:
        try:
            engine.handle(event)
        except ResyncRequired:
            pass
        now = engine.now
        state = engine.states.get(c.market)
        if state is None:
            continue
        book = state.book
        fresh = book.ready and now - book.updated_ms <= engine.config.stale_ms
        if not fresh:
            if pending or position:
                invalidated += 1
            pending, position = None, None
            continue
        metric = state.metrics.view(book, now)
        if (
            position
            and now >= position["due"]
            and event.key == c.market
            and event.kind.endswith("depth")
        ):
            exit_side = "sell" if position["side"] == "buy" else "buy"
            exit_price = walk_book(book, exit_side, position["qty"])
            if exit_price is None:
                invalidated += 1
                position = None
                continue
            direction = 1 if position["side"] == "buy" else -1
            exit_price *= 1 - direction * c.slippage_bps / 10000
            gross = direction * (exit_price - position["entry_price"]) * position["qty"]
            fees = (exit_price + position["entry_price"]) * position["qty"] * c.fee_bps / 10000
            rows.append(
                {
                    "entry_ms": position["entry_ms"],
                    "exit_ms": now,
                    "signal_ms": position["signal_ms"],
                    "side": position["side"],
                    "entry_price": position["entry_price"],
                    "exit_price": exit_price,
                    "base_qty": position["qty"],
                    "gross_quote": gross,
                    "fees_quote": fees,
                    "net_quote": gross - fees,
                }
            )
            position, last_exit = None, now
        if (
            pending
            and now >= pending["due"]
            and event.key == c.market
            and event.kind.endswith("depth")
        ):
            qty = c.notional / metric["mid"]
            entry_price = walk_book(book, pending["side"], qty)
            if entry_price is None:
                invalidated += 1
            else:
                direction = 1 if pending["side"] == "buy" else -1
                entry_price *= 1 + direction * c.slippage_bps / 10000
                position = {
                    "side": pending["side"],
                    "qty": qty,
                    "entry_price": entry_price,
                    "entry_ms": now,
                    "signal_ms": pending["signal_ms"],
                    "due": now + c.holding_ms + c.latency_ms,
                }
            pending = None
        if not pending and not position and now - last_exit >= c.holding_ms:
            imbalance = metric["imbalance_top20"]
            direction = 1 if imbalance >= 0 else -1
            flow_ok = (
                metric["ofi_10s_base"] * direction > 0 and metric["delta_5s_base"] * direction > 0
            )
            if abs(imbalance) >= c.imbalance_threshold and (flow_ok or not c.require_flow):
                pending = {
                    "side": "buy" if direction > 0 else "sell",
                    "signal_ms": now,
                    "due": now + c.latency_ms,
                }
    net = sum(x["net_quote"] for x in rows)
    return {
        "configuration": asdict(c),
        "completed_trades": len(rows),
        "net_quote": net,
        "invalidated_on_gaps_or_insufficient_depth": invalidated,
        "unclosed_at_end": bool(position or pending),
        "trades": rows,
        "limitations": "Taker depth walk with fees, future-book latency and extra slippage; "
        "no market impact feedback, funding, passive queue fills or profitability claim. "
        "Gap-invalidated and end-open trades are excluded; inspect counts for selection bias.",
    }
