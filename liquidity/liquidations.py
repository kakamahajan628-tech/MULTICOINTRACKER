"""Scenario bands from public OI/mark observations, not a map of actual positions."""

from dataclasses import dataclass
import math


@dataclass
class Cohort:
    created_ms: int
    entry: float
    notional: float


class LiquidationModel:
    def __init__(self):
        self.cohorts = []
        self.last_oi = None
        self.latest = None

    def observe(self, mark, oi_quote, now):
        if not all(math.isfinite(x) and x > 0 for x in (mark, oi_quote)):
            raise ValueError("invalid OI/mark observation")
        # OI is one-sided notional. Splitting it in half would double-discount a
        # long/short model: each reported contract has both a long and a short.
        if self.last_oi is None:
            self.cohorts.append(Cohort(now, mark, oi_quote))
        elif oi_quote > self.last_oi:
            self.cohorts.append(Cohort(now, mark, oi_quote - self.last_oi))
        else:
            ratio = oi_quote / self.last_oi
            for cohort in self.cohorts:
                cohort.notional *= ratio
        self.last_oi = oi_quote
        self.latest = (mark, oi_quote, now)
        # Bound retained scenario cohorts; collapse oldest with weighted entry.
        if len(self.cohorts) > 1000:
            old = self.cohorts[:500]
            total = sum(c.notional for c in old)
            self.cohorts[:500] = [
                Cohort(old[-1].created_ms, sum(c.entry * c.notional for c in old) / total, total)
            ]

    def view(self, now):
        if not self.latest:
            return {"classification": "modeled", "bands": [], "status": "no OI observations"}
        mark, oi, timestamp = self.latest
        output = []
        weights = ((5, 0.15), (10, 0.25), (25, 0.30), (50, 0.20), (100, 0.10))
        for leverage, weight in weights:
            for side in ("long", "short"):
                rows = []
                for c in self.cohorts:
                    age_days = max(0, now - c.created_ms) / 86400000
                    half_life = max(0.5, 50 / leverage)
                    size = c.notional * weight * 2 ** (-age_days / half_life)
                    # Simplified isolated linear position, no maintenance deduction.
                    # MMR and entry spread are scenario ranges, not confidence intervals.
                    centers = [
                        c.entry * (1 - 1 / leverage) / (1 - mmr)
                        if side == "long"
                        else c.entry * (1 + 1 / leverage) / (1 + mmr)
                        for mmr in (0.004, 0.01)
                    ]
                    low, high = min(centers) * 0.985, max(centers) * 1.015
                    if (side == "long" and high >= mark) or (side == "short" and low <= mark):
                        continue
                    rows.append((low, high, size))
                if rows:
                    total = sum(x[2] for x in rows)
                    output.append(
                        {
                            "side": side,
                            "leverage": leverage,
                            "price_low": min(x[0] for x in rows),
                            "price_high": max(x[1] for x in rows),
                            "scenario_quote_notional": total,
                        }
                    )
        return {
            "classification": "modeled",
            "bands": output,
            "observed_oi_quote": oi,
            "observed_mark": mark,
            "observation_age_ms": now - timestamp,
            "stale": now - timestamp > 120000,
            "uncertainty": "Scenario envelopes, not statistical confidence intervals.",
            "assumptions": "Initial OI entry=first mark; leverage prior 5/10/25/50/100x; "
            "MMR 0.4%-1%; entry +/-1.5%; isolated linear margin; age decay. "
            "Both position sides share one-sided OI budget; do not sum as unique exposure. "
            "OI USD changes also reflect price; entries/leverage/collateral are unknown. "
            "No claim that traded-through bands mean liquidation.",
        }
