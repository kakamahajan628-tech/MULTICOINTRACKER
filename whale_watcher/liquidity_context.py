"""Join verified asset mappings to public market context, never to CEX order identity."""

from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation
from .public_markets import market_targets


def milliseconds(value):
    return int(datetime.fromisoformat(value).timestamp() * 1000)


def correlate_cycle(cycle, context, links=None, asset_links=None):
    rows = [row for report in cycle["reports"].values() for row in report["rows"]]
    rows += cycle.get("neutral_watch", {}).get("rows", [])
    targets, unmapped = market_targets(rows, links, asset_links)
    output = []
    for symbol, associations in targets.items():
        for asset_id, mapping_basis in associations.items():
            from .screening import asset_identity

            matching = [r for r in rows if asset_identity(r, links) == asset_id]
            comparable, increasing, decreasing, delta, seen = 0, 0, 0, Decimal(0), set()
            for row in matching:
                identity = (row["chain"], row["address"])
                if identity in seen or not row["group"].startswith("Spot"):
                    continue
                seen.add(identity)
                if row.get("state") == "unavailable" or row.get("balance_source_changed"):
                    continue
                try:
                    before, after = (
                        Decimal(str(row.get("previous"))),
                        Decimal(str(row.get("amount"))),
                    )
                    if not before.is_finite() or not after.is_finite():
                        continue
                except (InvalidOperation, TypeError):
                    continue
                previous_at = row.get("previous_observed_at")
                if previous_at and previous_at < cycle["window_start"]:
                    continue
                comparable += 1
                change = after - before
                increasing += change > 0
                decreasing += change < 0
                delta += change
            markets = {}
            signals = []
            synthetic = context.get("mode") == "demo"
            for venue in ("binance", "bybit"):
                key = venue + ":" + symbol + "USDT"
                state = context.get("markets", {}).get(key)
                if state is None:
                    continue
                quality = state["quality"]
                metrics = state.get("derived") if quality.get("fresh") else None
                tape_fresh = (
                    quality.get("tape_connected")
                    and quality.get("tape_age_ms") is not None
                    and quality["tape_age_ms"] <= context.get("stale_ms", 3000)
                )
                markets[venue] = {
                    "quality": quality,
                    "derived": metrics,
                    "tape_fresh": bool(tape_fresh),
                }
                if quality.get("fresh") and tape_fresh and not synthetic:
                    cutoff = context.get("received_ms", 0)
                    signals += [
                        s
                        for s in context.get("inferred_signals", [])
                        if s["venue"] == venue
                        and s["symbol"] == symbol + "USDT"
                        and cutoff - 120000 <= s["received_ms"] <= cutoff
                    ]
            candidates = []
            if comparable and not synthetic:
                for signal in signals:
                    passive = signal["evidence"].get("passive_side", signal["evidence"].get("side"))
                    if signal["kind"] in ("absorption_candidate", "iceberg_candidate"):
                        if delta > 0 and passive == "bid":
                            candidates.append("wallet_increase_with_bid_absorption_candidate")
                        if delta < 0 and passive == "ask":
                            candidates.append("wallet_decrease_with_ask_absorption_candidate")
                    if signal["kind"] == "spoof_risk_candidate":
                        candidates.append("wallet_activity_with_rapid_wall_withdrawals_candidate")
            output.append(
                {
                    "asset_id": asset_id,
                    "symbol": symbol,
                    "mapping_basis": mapping_basis,
                    "classification": "inferred_association",
                    "cycle_id": cycle["id"],
                    "captured_ms": context.get("received_ms"),
                    "mode": context.get("mode"),
                    "wallet_evidence": {
                        "comparable_spot_wallets": comparable,
                        "increasing_wallets": increasing,
                        "decreasing_wallets": decreasing,
                        "net_spot_quantity": str(delta),
                        "meaning": "balance change, not a confirmed trade",
                    },
                    "markets": markets,
                    "signal_counts": dict(Counter(s["kind"] for s in signals)),
                    "candidates": sorted(set(candidates)),
                    "status": "synthetic_excluded"
                    if synthetic
                    else "context_available"
                    if any(v["derived"] for v in markets.values())
                    else "market_unavailable",
                    "temporal_scope": "Market snapshot at scan start; wallet observations have individual "
                    "timestamps. Five/ten-second metrics and last-120s candidates "
                    "are samples, not continuous coverage of the wallet scan window.",
                    "caveat": "Co-occurrence does not identify a wallet's CEX orders or prove causation.",
                }
            )
    return {
        "assets": output,
        "unmapped_assets": unmapped,
        "captured_ms": context.get("received_ms"),
        "schema": 1,
    }


def summarize_context(samples):
    # Keep one sample per completed wallet scan. A 4h summary must not sum CVD levels
    # or OFI snapshots and mislabel the result as four-hour executed flow.
    unique = {s["cycle_id"]: s for s in samples}
    ordered = sorted(unique.values(), key=lambda x: x.get("captured_ms") or 0)
    if not ordered:
        return {"samples": 0, "status": "unavailable", "candidate_counts": {}, "latest_markets": {}}
    return {
        "samples": len(ordered),
        "status": ordered[-1]["status"],
        "first_captured_ms": ordered[0]["captured_ms"],
        "last_captured_ms": ordered[-1]["captured_ms"],
        "candidate_counts": dict(Counter(c for s in ordered for c in s["candidates"])),
        "latest_markets": ordered[-1]["markets"],
        "aggregation": "sample counts and latest per-venue metrics; CVD/OFI levels are not summed",
        "classification": "inferred_association",
        "causal_attribution": False,
    }


def liquidity_digest(context, correlations=None, selector=None):
    lines = [
        "LIQUIDITY CONTEXT | " + context.get("mode", "unavailable").upper(),
        "Observed L2 / derived flow / inferred candidates / modeled bands are separate.",
    ]
    for key, market in context.get("markets", {}).items():
        if selector and key.split(":")[1] != selector + "USDT":
            continue
        q, m = market["quality"], market.get("derived")
        if not q.get("fresh") or m is None:
            lines.append(key + ": unavailable/stale; no liquidity conclusion.")
            continue
        lines.append(
            f"{key}: mid {m['mid']:.6g} USDT | spread {m['spread_bps']:.3f} bps | "
            f"CVD {m['cvd_base']:.6g} base (segment {m['cvd_epoch']}) | "
            f"OFI 10s {m['ofi_10s_base']:.6g} base"
        )
    for asset in (correlations or {}).get("assets", []):
        if selector and asset["symbol"] != selector:
            continue
        wallet = asset["wallet_evidence"]
        lines.append(
            f"{asset['symbol']} [{asset['asset_id']}]: {wallet['comparable_spot_wallets']} "
            f"comparable wallets | balance delta {wallet['net_spot_quantity']} | "
            + (", ".join(asset["candidates"]) or asset["status"])
        )
    if context.get("deferred_markets"):
        lines.append("Feed budget deferred: " + ", ".join(context["deferred_markets"]))
    lines.append(
        "Uncalibrated candidates; no hidden-order identity, guaranteed direction or liquidation prediction."
    )
    return "\n".join(lines)[:3900]
