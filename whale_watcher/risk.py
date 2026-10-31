"""Snapshot liquidation distance and evidence-based loss exits."""

import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext


def number(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def liquidation_risk(side, mark, liquidation, observed_at=None, as_of=None):
    critical = number(os.getenv("WHALE_LIQ_CRITICAL_PCT", "2"))
    near = number(os.getenv("WHALE_LIQ_NEAR_PCT", "5"))
    if critical is None or near is None or not 0 < critical <= near:
        raise ValueError("Liquidation bands require 0 < critical <= near")
    result = {
        "risk_band": "unknown",
        "distance_pct": None,
        "mark_price": None,
        "liquidation_price": None,
        "side": side,
        "risk_observed_at": observed_at,
        "critical_threshold_pct": str(critical),
        "near_threshold_pct": str(near),
    }
    if observed_at:
        try:
            reference = datetime.fromisoformat(as_of) if as_of else datetime.now(timezone.utc)
            age = (reference - datetime.fromisoformat(observed_at)).total_seconds()
            if age < -30 or age > int(os.getenv("WHALE_RISK_MAX_AGE_SECONDS", "900")):
                result["risk_reason"] = "stale_or_invalid_snapshot_time"
                return result
        except (ValueError, TypeError):
            result["risk_reason"] = "invalid_snapshot_time"
            return result
    mark, liquidation = number(mark), number(liquidation)
    if (
        side not in ("long", "short")
        or mark is None
        or liquidation is None
        or mark <= 0
        or liquidation <= 0
    ):
        result["risk_reason"] = "missing_side_mark_or_liquidation_price"
        return result
    with localcontext() as context:
        context.prec = 100
        distance = (mark - liquidation if side == "long" else liquidation - mark) / mark * 100
    result.update(
        mark_price=str(mark), liquidation_price=str(liquidation), distance_pct=str(distance)
    )
    result["risk_band"] = (
        "threshold_crossed"
        if distance <= 0
        else "critical"
        if distance <= critical
        else "near"
        if distance <= near
        else "outside_band"
    )
    result["risk_reason"] = "snapshot_distance; not proof or prediction of liquidation"
    return result


def summarize_legs(legs, observed_at=None, as_of=None):
    assessed = [
        {
            **leg,
            **liquidation_risk(
                leg.get("side"),
                leg.get("mark_price"),
                leg.get("liquidation_price"),
                leg.get("risk_observed_at") or observed_at,
                as_of,
            ),
        }
        for leg in legs
    ]
    priority = {"threshold_crossed": 0, "critical": 1, "near": 2, "outside_band": 3, "unknown": 4}
    valid = [leg for leg in assessed if leg["risk_band"] != "unknown"]
    worst = (
        min(valid, key=lambda leg: (priority[leg["risk_band"]], Decimal(leg["distance_pct"])))
        if valid
        else liquidation_risk(None, None, None)
    )
    return {
        **worst,
        "risk_legs": assessed,
        "risk_coverage": "complete"
        if assessed and len(valid) == len(assessed)
        else "partial"
        if valid
        else "unknown",
    }


def loss_closes(row):
    """Negative closed fills remain visible even if other profitable fills net them out."""
    if row.get("state") == "unavailable":
        return []
    if row.get("fill_coverage") == "partial_or_updated_orders":
        return []
    fills = row.get("fills", [])
    losses = []
    for fill in fills:
        data = fill.get("info") or fill
        pnl = next(
            (
                number(data[field])
                for field in ("closedPnl", "realizedPnl", "fillPnl")
                if field in data
            ),
            None,
        )
        if pnl is not None and pnl < 0:
            losses.append(
                {
                    "id": data.get("tid")
                    or data.get("execId")
                    or data.get("tradeId")
                    or data.get("id")
                    or data.get("orderId"),
                    "realized_pnl": str(pnl),
                    "exit_price": data.get("px")
                    or data.get("execPrice")
                    or data.get("price")
                    or data.get("fillPx")
                    or data.get("avgExitPrice"),
                    "closed_size": data.get("sz")
                    or data.get("qty")
                    or data.get("fillSz")
                    or data.get("qty"),
                    "closed_at": data.get("time")
                    or data.get("fillTime")
                    or data.get("updatedTime")
                    or fill.get("timestamp"),
                    "basis": "source_reported_closed_fill_or_order; fees/funding treatment venue-specific",
                }
            )
    if (
        not losses
        and number(row.get("realized_pnl")) is not None
        and number(row["realized_pnl"]) < 0
    ):
        losses.append(
            {
                "id": None,
                "realized_pnl": row["realized_pnl"],
                "basis": "verified_window_total",
                "exit_price": None,
            }
        )
    return losses
