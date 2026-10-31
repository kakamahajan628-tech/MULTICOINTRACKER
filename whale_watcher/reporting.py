"""Evidence-aware cohort reports. Holdings, realized PnL and liquidation stay distinct."""

import json
import os
from collections import Counter
from decimal import Decimal, localcontext
from .core import GROUPS
from .risk import summarize_legs, loss_closes, number

REPORT_STYLES = {
    "Spot Profit": ("Profit cohort · Disposal & booking ledger", "disposal_ledger"),
    "Spot Loss": ("Loss cohort · Accumulation & exit matrix", "accumulation_matrix"),
    "Futures Profit": ("Profit futures · Position lifecycle digest", "position_lifecycle"),
    "Futures Loss": ("Loss futures · Exposure & liquidation monitor", "exposure_monitor"),
}
SPOT_ACTIONS = (
    "buy_against_usdt",
    "sell_against_usdt",
    "swap_to_usdt",
    "swap_to_other_coin",
    "swap_in",
    "transfer_to_exchange",
    "transfer_out",
    "transfer_in",
    "liquidity_add",
    "liquidity_remove",
)


def holding_state(previous, current):
    if previous is None:
        return "baseline" if current else "nil_baseline"
    if previous > 0 and current == 0:
        return "holding_cleared"
    if current < previous:
        return "partial_reduction"
    if current > previous:
        return "accumulating"
    return "holding" if current else "nil"


def position_state(previous, current):
    if previous is None:
        return "baseline" if current else "nil_baseline"
    if previous and not current:
        return "full_close"
    if previous * current < 0:
        return "direction_flip"
    if abs(current) < abs(previous):
        return "partial_close"
    if abs(current) > abs(previous):
        return "position_increase"
    return "holding" if current else "nil"


def spot_evidence(row, records, start, end):
    """Only successful, wallet-attributed, provider-parsed events get trade labels."""
    quote_assets = json.loads(os.getenv("WHALE_QUOTE_ASSETS_JSON", "{}"))
    exchanges = json.loads(os.getenv("WHALE_EXCHANGE_ADDRESSES_JSON", "{}"))
    actions = []
    for record in records:
        raw = record.get("raw", {})
        if (
            record.get("source") != "helius"
            or row["chain"] != "solana"
            or row["address"] not in record.get("matched_addresses", [])
        ):
            continue
        # Late receipt from an older transaction belongs to its event window, not this cycle.
        from datetime import datetime, timezone

        timestamp = raw.get("timestamp")
        if not isinstance(timestamp, (int, float)):
            continue
        at = datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
        if not start < at <= end or raw.get("transactionError"):
            continue
        transfers = (
            raw.get("nativeTransfers", [])
            if row["asset"] == "native"
            else raw.get("tokenTransfers", [])
        )
        transfers = [
            t for t in transfers if row["asset"] == "native" or t.get("mint") == row["asset"]
        ]
        with localcontext() as context:
            context.prec = 100
            net = Decimal(0)
            exchange_out = False
            for t in transfers:
                amount = (
                    Decimal(str(t.get("amount", 0))) / Decimal(10**9)
                    if row["asset"] == "native"
                    else Decimal(str(t.get("tokenAmount", 0)))
                )
                if t.get("toUserAccount") == row["address"]:
                    net += amount
                if t.get("fromUserAccount") == row["address"]:
                    net -= amount
                    exchange_out |= "solana:" + str(t.get("toUserAccount")) in exchanges
        if not net:
            continue
        kind = raw.get("type")
        action = None
        if kind == "SWAP":
            opposite = []
            for t in raw.get("tokenTransfers", []):
                if t.get("mint") == row["asset"]:
                    continue
                if (net < 0 and t.get("toUserAccount") == row["address"]) or (
                    net > 0 and t.get("fromUserAccount") == row["address"]
                ):
                    opposite.append(t.get("mint"))
            if not opposite:
                # Native SOL consideration is another asset, never assumed to be USDT.
                if any(
                    (net < 0 and t.get("toUserAccount") == row["address"])
                    or (net > 0 and t.get("fromUserAccount") == row["address"])
                    for t in raw.get("nativeTransfers", [])
                ):
                    opposite = ["native"]
            if not opposite:
                continue
            usdt = any(quote_assets.get("solana:" + str(mint)) == "USDT" for mint in opposite)
            action = (
                ("swap_to_usdt" if usdt else "swap_to_other_coin")
                if net < 0
                else ("buy_against_usdt" if usdt else "swap_in")
            )
        elif kind in ("ADD_LIQUIDITY", "REMOVE_LIQUIDITY"):
            action = "liquidity_add" if kind == "ADD_LIQUIDITY" else "liquidity_remove"
        elif kind in ("TRANSFER", "TOKEN_TRANSFER"):
            action = (
                ("transfer_to_exchange" if exchange_out else "transfer_out")
                if net < 0
                else "transfer_in"
            )
        if action:
            actions.append(
                {
                    "action": action,
                    "amount": str(abs(net)),
                    "hash": raw.get("signature"),
                    "at": at,
                    "source": "helius",
                    "confidence": "provider_parsed",
                    "execution_price_usd": None,
                }
            )
    return actions


def make_cycle(cycle_id, started, completed, window_start, observations, source_status=None):
    from .orders import summarize_orders

    observations = [dict(r) for r in observations]
    for row in observations:
        if row["group"].startswith("Futures"):
            risk = summarize_legs(row.get("risk_legs", []), row.get("observed_at"), completed)
            if row["state"] == "unavailable":
                risk.update(
                    risk_band="unknown",
                    distance_pct=None,
                    risk_reason="current_position_unavailable",
                )
            elif number(row.get("amount")) == 0 and not row.get("risk_legs"):
                risk.update(
                    risk_band="not_applicable", distance_pct=None, risk_reason="no_open_position"
                )
            row.update(risk)
            row["loss_exit_evidence"] = loss_closes(row)
    reports = {}
    for group in GROUPS:
        title, layout = REPORT_STYLES[group]
        rows = [r for r in observations if r["group"] == group]
        counters = Counter(r["state"] for r in rows)
        all_actions = {
            action: len(
                {
                    r["address"]
                    for r in rows
                    if any(e["action"] == action for e in r.get("actions", []))
                }
            )
            for action in SPOT_ACTIONS
        }
        realized_profit = [
            r for r in rows if r.get("realized_pnl") is not None and Decimal(r["realized_pnl"]) > 0
        ]
        realized_loss = [
            r for r in rows if r.get("realized_pnl") is not None and Decimal(r["realized_pnl"]) < 0
        ]
        report = {
            "group": group,
            "title": title,
            "layout": layout,
            "entries": len(rows),
            "unique_addresses": len({(r["chain"], r["address"]) for r in rows}),
            "counts": dict(counters),
            "action_wallet_counts": all_actions,
            "rows": rows,
            "subreports": {},
            "by_coin": {},
        }
        if group.startswith("Spot"):
            report["subreports"] = {
                "accumulation": [r for r in rows if r["state"] == "accumulating"],
                "partial_reductions": [r for r in rows if r["state"] == "partial_reduction"],
                "holding_cleared": [r for r in rows if r["state"] == "holding_cleared"],
                "profit_booked" if group == "Spot Profit" else "loss_booked": realized_profit
                if group == "Spot Profit"
                else realized_loss,
                "booking_review": [
                    r
                    for r in rows
                    if r["state"] == "holding_cleared" and r.get("realized_pnl") is None
                ],
                "market_distribution_signals": [
                    r
                    for r in rows
                    if any(
                        a["action"] in ("sell_against_usdt", "swap_to_usdt", "swap_to_other_coin")
                        for a in r.get("actions", [])
                    )
                ],
                "transaction_activity": [r for r in rows if r.get("actions")],
                "unavailable": [r for r in rows if r["state"] == "unavailable"],
            }
        else:
            report["subreports"] = {
                "holding": [
                    r
                    for r in rows
                    if r["state"] in ("holding", "baseline") and Decimal(r.get("amount", "0")) != 0
                ],
                "partial_closes": [r for r in rows if r["state"] == "partial_close"],
                "full_closes": [r for r in rows if r["state"] == "full_close"],
                "nil_positions": [
                    r for r in rows if r["state"] in ("nil", "nil_baseline", "full_close")
                ],
                "profit_digest": realized_profit,
                "loss_digest": realized_loss,
                "liquidated": [r for r in rows if r.get("liquidated") is True],
                "near_liquidation": sorted(
                    [
                        r
                        for r in rows
                        if r["risk_band"] in ("near", "critical", "threshold_crossed")
                    ],
                    key=lambda r: Decimal(r["distance_pct"]),
                ),
                "critical_liquidation": [
                    r for r in rows if r["risk_band"] in ("critical", "threshold_crossed")
                ],
                "risk_unknown": [r for r in rows if r["risk_band"] == "unknown"],
                "closed_at_loss": [r for r in rows if r["loss_exit_evidence"]],
                "partial_close_at_loss": [
                    r for r in rows if r["state"] == "partial_close" and r["loss_exit_evidence"]
                ],
                "full_close_at_loss": [
                    r for r in rows if r["state"] == "full_close" and r["loss_exit_evidence"]
                ],
                "unavailable": [r for r in rows if r["state"] == "unavailable"],
            }
        for coin in sorted({r["coin"] for r in rows}):
            subset = [r for r in rows if r["coin"] == coin]
            report["by_coin"][coin] = {
                "entries": len(subset),
                "unique_addresses": len({r["address"] for r in subset}),
                "counts": dict(Counter(r["state"] for r in subset)),
            }
        report["order_summary"] = summarize_orders(rows)
        reports[group] = report
    futures_rows = [r for r in observations if r["group"].startswith("Futures")]
    risk_report = {
        "title": "Liquidation proximity & realized loss exits",
        "near_liquidation": sorted(
            [
                r
                for r in futures_rows
                if r["risk_band"] in ("near", "critical", "threshold_crossed")
            ],
            key=lambda r: Decimal(r["distance_pct"]),
        ),
        "closed_at_loss": [r for r in futures_rows if r["loss_exit_evidence"]],
        "risk_unknown": [r for r in futures_rows if r["risk_band"] == "unknown"],
        "critical_threshold_pct": os.getenv("WHALE_LIQ_CRITICAL_PCT", "2"),
        "near_threshold_pct": os.getenv("WHALE_LIQ_NEAR_PCT", "5"),
    }
    return {
        "id": cycle_id,
        "started_at": started,
        "completed_at": completed,
        "window_start": window_start,
        "interval_seconds": 600,
        "reports": reports,
        "source_status": source_status or [],
        "risk_report": risk_report,
        "order_flow": summarize_orders(observations),
        "neutral_watch": {
            "title": "Neutral wallet watch · P&L unverified",
            "rows": [
                r
                for r in observations
                if r["group"] in ("Spot Unclassified", "Futures Unclassified")
            ],
        },
        "coverage": {
            "entries": len(observations),
            "observed": sum(r["state"] != "unavailable" for r in observations),
        },
        "note": "Cohort labels are user supplied. Holding clear is not proof of booked PnL; nil position is not proof of liquidation.",
    }


def telegram_digest(cycle):
    messages = []
    for group, report in cycle["reports"].items():
        counts = report["counts"]
        if group == "Spot Profit":
            body = f"DISPOSAL LEDGER | cleared {counts.get('holding_cleared', 0)} | reduced {counts.get('partial_reduction', 0)}\nProfit booked {len(report['subreports']['profit_booked'])} | booking review {len(report['subreports']['booking_review'])}"
        elif group == "Spot Loss":
            body = f"ACCUMULATION MATRIX | increasing {counts.get('accumulating', 0)} | exits {counts.get('holding_cleared', 0)}\nLoss booked {len(report['subreports']['loss_booked'])} | partial reductions {counts.get('partial_reduction', 0)}"
        elif group == "Futures Profit":
            body = f"POSITION DIGEST | holding {len(report['subreports']['holding'])} → partial {counts.get('partial_close', 0)} → full {counts.get('full_close', 0)}\nProfit fills {len(report['subreports']['profit_digest'])} | nil {len(report['subreports']['nil_positions'])}"
        else:
            body = f"EXPOSURE MONITOR | open {len(report['subreports']['holding'])} | nil {len(report['subreports']['nil_positions'])}\nPartial {counts.get('partial_close', 0)} | full {counts.get('full_close', 0)} | confirmed liquidated {len(report['subreports']['liquidated'])}"
        if group.startswith("Futures"):
            body += f"\nNear liquidation {len(report['subreports']['near_liquidation'])} | critical {len(report['subreports']['critical_liquidation'])} | loss exits {len(report['subreports']['closed_at_loss'])} | risk unknown {len(report['subreports']['risk_unknown'])}"
        messages.append(
            f"{report['title']}\n{cycle['completed_at']}\n{body}\nEntries {report['entries']} | unavailable {counts.get('unavailable', 0)}\nDetails: {os.getenv('WHALE_PUBLIC_URL', 'dashboard')}\nBooking and liquidation need source evidence."
        )
    return messages


def risk_digest(cycle):
    report = cycle.get("risk_report", {})
    near, losses = report.get("near_liquidation", []), report.get("closed_at_loss", [])
    lines = [
        "LIQUIDATION PROXIMITY & LOSS EXITS",
        cycle["completed_at"],
        f"Near/critical {len(near)} | loss exits {len(losses)} | risk unknown {len(report.get('risk_unknown', []))}",
    ]
    for row in near[:5]:
        lines.append(
            f"{row['label'][:80]} · {row['coin']} {row.get('side')} · distance {Decimal(row['distance_pct']):.3f}% · {row['risk_band']}"
        )
    for row in losses[:5]:
        values = ", ".join(e["realized_pnl"] for e in row["loss_exit_evidence"][:3])
        lines.append(
            f"{row['label'][:80]} · {row['coin']} · negative closes {values} · {row['state']}"
        )
    lines.append(
        "Snapshot thresholds are not liquidation predictions. Loss fills can occur even when the window net PnL is positive."
    )
    lines.append("Details: " + os.getenv("WHALE_PUBLIC_URL", "dashboard"))
    return "\n".join(lines)[:3900]
