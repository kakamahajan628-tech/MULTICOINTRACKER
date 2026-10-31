"""Explainable candidates, never claims of hidden order identity or spoofing intent."""

from collections import deque
from dataclasses import dataclass
from statistics import median
from .models import Signal


@dataclass
class DetectorConfig:
    wall_multiple: float = 5.0
    wall_min_quote: float = 100000.0
    wall_persist_ms: int = 5000
    wall_vanish_fraction: float = 0.2
    absorption_min_quote: float = 50000.0
    absorption_max_range_bps: float = 2.0
    iceberg_ratio: float = 3.0
    cooldown_ms: int = 10000
    stale_ms: int = 3000


class Detectors:
    def __init__(self, instrument, config=None):
        self.instrument = instrument
        self.config = config or DetectorConfig()
        self.walls = {}
        self.mids = deque(maxlen=100000)
        self.levels = {}
        self.refills = {}
        self.pulls = {"bid": deque(), "ask": deque()}
        self.cooldowns = {}

    def reset(self):
        self.walls.clear()
        self.mids.clear()
        self.levels.clear()
        self.refills.clear()
        self.pulls = {"bid": deque(), "ask": deque()}
        self.cooldowns.clear()

    def emit(self, kind, now, evidence, score, key=""):
        tag = (kind, key)
        if now - self.cooldowns.get(tag, -(10**18)) < self.config.cooldown_ms:
            return None
        self.cooldowns[tag] = now
        return Signal(
            kind,
            self.instrument.venue,
            self.instrument.symbol,
            now,
            evidence,
            round(min(1, max(0, score)), 3),
        )

    def on_book(self, book, metrics, now, tape_fresh):
        c, out = self.config, []
        bid, _, ask, _ = book.bbo()
        mid = float((bid + ask) / 2)
        self.mids.append((now, mid))
        while self.mids and self.mids[0][0] < now - 5000:
            self.mids.popleft()
        current = {}
        for side in ("bid", "ask"):
            visible = book.top(side, 100)
            baseline = median([float(p * q) for p, q in visible]) if visible else 0
            for p, q in visible:
                key = (side, p)
                current[key] = q
                old = self.levels.get(key)
                if old is not None and q > old and tape_fresh:
                    matches = [
                        t
                        for t in metrics.trades
                        if t[0] >= now - 1000
                        and t[1] == p
                        and t[3] == ("sell" if side == "bid" else "buy")
                    ]
                    if matches:
                        times = self.refills.setdefault(key, deque())
                        if not times or now - times[-1] >= 100:
                            times.append(now)
                        while times and times[0] < now - 5000:
                            times.popleft()
                notional = float(p * q)
                abnormal = baseline > 0 and notional >= max(
                    c.wall_min_quote, c.wall_multiple * baseline
                )
                if abnormal and key not in self.walls:
                    self.walls[key] = {"since": now, "peak": q, "reported": False}
                wall = self.walls.get(key)
                if (
                    wall
                    and not abnormal
                    and q > wall["peak"] * type(q)(str(c.wall_vanish_fraction))
                ):
                    # A level must remain abnormal continuously to count as persistent.
                    del self.walls[key]
                    wall = None
                if wall:
                    wall["peak"] = max(q, wall["peak"])
                    if (
                        abnormal
                        and now - wall["since"] >= c.wall_persist_ms
                        and not wall["reported"]
                    ):
                        wall["reported"] = True
                        signal = self.emit(
                            "persistent_wall_candidate",
                            now,
                            {
                                "side": side,
                                "price": str(p),
                                "quote_notional": notional,
                                "age_ms": now - wall["since"],
                                "median_quote": baseline,
                            },
                            0.65,
                            str(key),
                        )
                        if signal:
                            out.append(signal)
        # Only levels still in the subscribed coverage can be classified as vanished.
        for key, wall in list(self.walls.items()):
            side, p = key
            levels = book.top(side, 100)
            in_range = levels and min(x[0] for x in levels) <= p <= max(x[0] for x in levels)
            qty = current.get(key, 0)
            if not in_range:
                del self.walls[key]
                continue
            if qty <= wall["peak"] * type(wall["peak"])(str(c.wall_vanish_fraction)):
                executed = sum(
                    float(q)
                    for t, price, q, aggressor in metrics.trades
                    if price == p
                    and t >= wall["since"]
                    and aggressor == ("sell" if side == "bid" else "buy")
                )
                fraction = min(1, executed / float(wall["peak"]))
                signal = self.emit(
                    "vanishing_wall_candidate",
                    now,
                    {
                        "side": side,
                        "price": str(p),
                        "age_ms": now - wall["since"],
                        "executed_fraction_lower_bound": fraction,
                        "attribution": "execution/cancellation/replacement cannot be fully separated",
                    },
                    0.55,
                    str(key),
                )
                if signal:
                    out.append(signal)
                if tape_fresh and fraction < 0.1 and now - wall["since"] < 5000:
                    self.pulls[side].append(now)
                del self.walls[key]
        for side, pulls in self.pulls.items():
            while pulls and pulls[0] < now - 60000:
                pulls.popleft()
            if len(pulls) >= 3 and tape_fresh:
                signal = self.emit(
                    "spoof_risk_candidate",
                    now,
                    {
                        "side": side,
                        "rapid_low_execution_vanishes_60s": len(pulls),
                        "alternative_causes": "legitimate repricing, cancellations, missing tape",
                    },
                    min(0.9, 0.35 + 0.1 * len(pulls)),
                    side,
                )
                if signal:
                    out.append(signal)
        self.levels = current
        # Purge levels leaving the tracked range to bound memory on moving markets.
        self.refills = {k: v for k, v in self.refills.items() if k in current}
        return out

    def on_trade(self, book, metrics, now):
        if len(self.mids) < 2 or self.mids[-1][0] - self.mids[0][0] < 1000:
            return []
        prices = [x[1] for x in self.mids if x[0] >= now - 5000]
        if not prices:
            return []
        spread = (max(prices) - min(prices)) / prices[-1] * 10000
        if spread > self.config.absorption_max_range_bps:
            return []
        out = []
        for side, passive in (("sell", "bid"), ("buy", "ask")):
            trades = [x for x in metrics.trades if x[0] >= now - 5000 and x[3] == side]
            quote = sum(float(p * q) for _, p, q, _ in trades)
            if quote < self.config.absorption_min_quote:
                continue
            signal = self.emit(
                "absorption_candidate",
                now,
                {
                    "aggressor": side,
                    "passive_side": passive,
                    "executed_quote_5s": quote,
                    "mid_range_bps_5s": spread,
                },
                0.6,
                passive,
            )
            if signal:
                out.append(signal)
            # Repeated actual replenishment at the executed price is required.
            for p, displayed in book.top(passive, 10):
                executed = sum(float(q) for _, price, q, _ in trades if price == p)
                ratio = executed / float(displayed) if displayed else 0
                refills = sum(t >= now - 5000 for t in self.refills.get((passive, p), []))
                if ratio >= self.config.iceberg_ratio and refills >= 2:
                    signal = self.emit(
                        "iceberg_candidate",
                        now,
                        {
                            "side": passive,
                            "price": str(p),
                            "executed_to_displayed": ratio,
                            "replenishments_5s": refills,
                            "mid_range_bps_5s": spread,
                            "alternative": "multiple independent limit orders may replenish the level",
                        },
                        min(0.85, 0.45 + refills * 0.1),
                        f"{passive}:{p}",
                    )
                    if signal:
                        out.append(signal)
        return out
