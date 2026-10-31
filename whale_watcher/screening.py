"""Six completed scans -> hourly screen; four non-overlapping hourly screens -> 4h."""

import asyncio
import hashlib
import json
import os
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo
from decimal import Decimal, localcontext
from .core import GROUPS, now
from .risk import number
from .public_markets import summarize_markets
from .liquidity_context import summarize_context

PHASES = (
    "accumulation",
    "distribution",
    "sideways",
    "harvesting",
    "liquidation",
    "short_squeeze",
    "long_squeeze",
    "deleveraging",
    "markup",
    "markdown",
    "manipulation",
    "rugpull",
)
SET_KEYS = (
    "wallet_ids",
    "watch_ids",
    "comparable_spot_wallets",
    "accumulating_wallets",
    "distributing_wallets",
    "profitable_close_wallets",
    "liquidated_wallets",
    "near_risk_wallets",
    "short_covering_wallets",
    "long_reducing_wallets",
    "short_liquidated_wallets",
    "long_liquidated_wallets",
    "tx_ids",
    "fill_ids",
)


def identity(value):
    return hashlib.sha256(value.encode()).hexdigest()[:24]


def asset_identity(row, links=None):
    if row["group"].startswith("Spot"):
        return row["chain"] + ":" + row.get("asset", "unknown-contract:" + row["coin"])
    venue = row.get("platform", "unknown").lower() or "unknown"
    key = venue + ":" + row["coin"]
    links = (
        links if links is not None else json.loads(os.getenv("WHALE_SCREEN_ASSET_LINKS_JSON", "{}"))
    )
    return links.get(key, "perp:" + key)


def blank_features(asset_id, symbol):
    return {
        "asset_id": asset_id,
        "symbols": [symbol],
        **{key: [] for key in SET_KEYS},
        "cohorts": {
            g: {"wallet_ids": [], "states": {}, "observations": 0, "observed": 0} for g in GROUPS
        },
        "observations": 0,
        "observed": 0,
        "comparable_spot_observations": 0,
        "source_changes": 0,
        "net_spot_quantity": "0",
        "near_risk_observations": 0,
        "actions": {},
        "event_actions": {},
        "prices": [],
        "gaps": [],
        "market_samples": [],
        "liquidity_samples": [],
    }


def features_from_cycles(cycles, links=None):
    items = {}
    seen_observations = set()
    for cycle in cycles:
        for group, report in cycle["reports"].items():
            for row in report["rows"]:
                key = asset_identity(row, links)
                f = items.setdefault(key, blank_features(key, row["coin"]))
                if row["coin"] not in f["symbols"]:
                    f["symbols"].append(row["coin"])
                wallet = identity(row["chain"] + ":" + row["address"])
                watch = row.get("id") or identity(group + ":" + wallet + ":" + key)
                if wallet not in f["wallet_ids"]:
                    f["wallet_ids"].append(wallet)
                if watch not in f["watch_ids"]:
                    f["watch_ids"].append(watch)
                cohort = f["cohorts"][group]
                if wallet not in cohort["wallet_ids"]:
                    cohort["wallet_ids"].append(wallet)
                cohort["observations"] += 1
                cohort["states"][row["state"]] = cohort["states"].get(row["state"], 0) + 1
                cohort["observed"] += int(row["state"] != "unavailable")
                market_scope = (
                    "spot" if group.startswith("Spot") else "perp:" + row.get("platform", "unknown")
                )
                marker = (cycle["id"], wallet, key, market_scope)
                if marker in seen_observations:
                    continue
                seen_observations.add(marker)
                f["observations"] += 1
                if row["state"] == "unavailable":
                    continue
                f["observed"] += 1

                def add(field, features=f, identity=wallet):
                    if identity not in features[field]:
                        features[field].append(identity)

                f["source_changes"] += int(row.get("balance_source_changed", False))
                amount, previous = number(row.get("amount")), number(row.get("previous"))
                comparable = (
                    amount is not None
                    and previous is not None
                    and not row.get("balance_source_changed")
                )
                at = row.get("previous_observed_at")
                if at and at < cycles[0].get("window_start", cycles[0]["started_at"]):
                    comparable = False
                if group.startswith("Spot") and comparable:
                    add("comparable_spot_wallets")
                    f["comparable_spot_observations"] += 1
                    with localcontext() as context:
                        context.prec = 100
                        delta = amount - previous
                        f["net_spot_quantity"] = str(Decimal(f["net_spot_quantity"]) + delta)
                    if delta > 0:
                        add("accumulating_wallets")
                    if delta < 0:
                        add("distributing_wallets")
                if group.startswith("Futures"):
                    if row.get("liquidated") is True:
                        add("liquidated_wallets")
                        side = (
                            row.get("liquidation_side") or row.get("side") or row.get("direction")
                        )
                        if side in ("long", "short"):
                            add(side + "_liquidated_wallets")
                    if comparable and abs(amount) < abs(previous):
                        add("short_covering_wallets" if previous < 0 else "long_reducing_wallets")
                        pnl = number(row.get("realized_pnl"))
                        if pnl is not None and pnl > 0:
                            add("profitable_close_wallets")
                    if row.get("risk_band") in ("near", "critical", "threshold_crossed"):
                        f["near_risk_observations"] += 1
                        add("near_risk_wallets")
                for action in row.get("actions", []):
                    if not action.get("hash"):
                        continue
                    event_id = identity(
                        row["chain"]
                        + ":"
                        + row["address"]
                        + ":"
                        + action["hash"]
                        + ":"
                        + action["action"]
                    )
                    if event_id not in f["tx_ids"]:
                        f["tx_ids"].append(event_id)
                        f["event_actions"][event_id] = action["action"]
                        f["actions"][action["action"]] = f["actions"].get(action["action"], 0) + 1
                for fill in row.get("fills", []):
                    info = fill.get("info") or fill
                    fill_id = next(
                        (
                            info.get(field)
                            for field in ("tid", "tradeId", "id", "orderId")
                            if info.get(field) is not None
                        ),
                        None,
                    )
                    if fill_id is not None:
                        fill_key = identity(
                            str(row.get("platform"))
                            + ":"
                            + row["address"]
                            + ":"
                            + row["coin"]
                            + ":"
                            + str(fill_id)
                        )
                        if fill_key not in f["fill_ids"]:
                            f["fill_ids"].append(fill_key)
                price = (
                    number(row.get("price_usd"))
                    if group.startswith("Spot")
                    else number(row.get("mark_price"))
                )
                source = row.get("price_source") if group.startswith("Spot") else row.get("source")
                if (
                    price is not None
                    and price > 0
                    and not any(p["cycle_id"] == cycle["id"] for p in f["prices"])
                ):
                    f["prices"].append(
                        {
                            "cycle_id": cycle["id"],
                            "at": cycle["completed_at"],
                            "value": str(price),
                            "source": source,
                            "basis": "spot_reference" if group.startswith("Spot") else "venue_mark",
                        }
                    )
        for snapshot in cycle.get("public_markets", {}).get("snapshots", []):
            for asset_id in snapshot.get("asset_associations", {}):
                if asset_id not in items:
                    continue
                if snapshot["collected_at"] < cycles[0].get(
                    "window_start", cycles[0]["started_at"]
                ):
                    continue
                f = items[asset_id]
                for market in snapshot["venues"]:
                    if not market["fields"]:
                        continue
                    marker = (market["venue"], market["instrument"], market["observed_at"])
                    if any(
                        (m["venue"], m["instrument"], m["observed_at"]) == marker
                        for m in f["market_samples"]
                    ):
                        continue
                    f["market_samples"].append(
                        {
                            k: market[k]
                            for k in ("venue", "instrument", "observed_at", "fields", "field_times")
                        }
                    )
        for association in cycle.get("wallet_liquidity", {}).get("assets", []):
            if association["asset_id"] in items:
                items[association["asset_id"]]["liquidity_samples"].append(association)
    return list(items.values())


def merge_features(hourlies):
    combined = {}
    for report in hourlies:
        for item in report["assets"]:
            incoming = item["features"]
            key = incoming["asset_id"]
            f = combined.setdefault(key, blank_features(key, incoming["symbols"][0]))
            for field in SET_KEYS + ("symbols", "gaps"):
                f[field] = list(dict.fromkeys(f[field] + incoming[field]))
            for field in (
                "observations",
                "observed",
                "comparable_spot_observations",
                "source_changes",
                "near_risk_observations",
            ):
                f[field] += incoming[field]
            with localcontext() as context:
                context.prec = 100
                f["net_spot_quantity"] = str(
                    Decimal(f["net_spot_quantity"]) + Decimal(incoming["net_spot_quantity"])
                )
            for g in GROUPS:
                target, source = f["cohorts"][g], incoming["cohorts"][g]
                target["wallet_ids"] = list(
                    dict.fromkeys(target["wallet_ids"] + source["wallet_ids"])
                )
                for field in ("observations", "observed"):
                    target[field] += source[field]
                target["states"] = dict(Counter(target["states"]) + Counter(source["states"]))
            # Automatic hourly blocks never overlap; transaction IDs still protect repeats.
            f["event_actions"].update(incoming.get("event_actions", {}))
            f["actions"] = dict(Counter(f["event_actions"].values()))
            known = {p["cycle_id"] for p in f["prices"]}
            f["prices"] += [p for p in incoming["prices"] if p["cycle_id"] not in known]
            known_markets = {
                (p["venue"], p["instrument"], p["observed_at"]) for p in f["market_samples"]
            }
            f["market_samples"] += [
                p
                for p in incoming.get("market_samples", [])
                if (p["venue"], p["instrument"], p["observed_at"]) not in known_markets
            ]
            known_liquidity = {x["cycle_id"] for x in f["liquidity_samples"]}
            f["liquidity_samples"] += [
                x
                for x in incoming.get("liquidity_samples", [])
                if x["cycle_id"] not in known_liquidity
            ]
    return list(combined.values())


def assess_features(features, kind):
    f = features
    coverage = 100 * f["observed"] / max(1, f["observations"])
    comparable = len(f["comparable_spot_wallets"])
    acc = 100 * len(f["accumulating_wallets"]) / max(1, comparable)
    dist = 100 * len(f["distributing_wallets"]) / max(1, comparable)
    prices = f["prices"]
    price_change = price_range = None
    if len(prices) >= 2 and len({(p["source"], p["basis"]) for p in prices}) == 1:
        values = [Decimal(p["value"]) for p in prices]
        price_change = float((values[-1] - values[0]) / values[0] * 100)
        price_range = float((max(values) - min(values)) / values[0] * 100)
    enough = (
        coverage >= float(os.getenv("WHALE_SCREEN_MIN_COVERAGE_PCT", "70")) and f["observed"] > 0
    )
    criteria = {
        "accumulation": (
            enough and comparable > 0 and acc >= 20 and Decimal(f["net_spot_quantity"]) > 0,
            min(95, acc),
            ["spot_quantity_increase", "net_spot_quantity"],
        ),
        "distribution": (
            enough and comparable > 0 and dist >= 20 and Decimal(f["net_spot_quantity"]) < 0,
            min(95, dist),
            ["spot_quantity_reduction", "net_spot_quantity"],
        ),
        "sideways": (
            enough
            and len(prices) >= 3
            and price_range is not None
            and price_range <= float(os.getenv("WHALE_SCREEN_SIDEWAYS_RANGE_PCT", "1.5")),
            60,
            ["reference_price_range"],
        ),
        "harvesting": (
            enough and bool(f["profitable_close_wallets"]),
            min(90, 45 + len(f["profitable_close_wallets"]) * 5),
            ["realized_profit_with_position_reduction"],
        ),
        "liquidation": (
            enough and bool(f["liquidated_wallets"]),
            min(95, 60 + len(f["liquidated_wallets"]) * 5),
            ["venue_confirmed_liquidation"],
        ),
        "short_squeeze": (
            enough
            and price_change is not None
            and price_change >= 3
            and len(f["short_covering_wallets"]) >= 2
            and bool(f["short_liquidated_wallets"]),
            50,
            ["price_rise", "short_covering", "short_liquidation"],
        ),
        "long_squeeze": (
            enough
            and price_change is not None
            and price_change <= -3
            and len(f["long_reducing_wallets"]) >= 2
            and bool(f["long_liquidated_wallets"]),
            50,
            ["price_fall", "long_reduction", "long_liquidation"],
        ),
        "deleveraging": (
            enough and len(f["short_covering_wallets"]) + len(f["long_reducing_wallets"]) >= 2,
            55,
            ["futures_exposure_reduction"],
        ),
        "markup": (
            enough
            and price_change is not None
            and price_change >= 3
            and bool(f["accumulating_wallets"]),
            55,
            ["price_rise", "spot_quantity_increase"],
        ),
        "markdown": (
            enough
            and price_change is not None
            and price_change <= -3
            and bool(f["distributing_wallets"]),
            55,
            ["price_fall", "spot_quantity_reduction"],
        ),
        "manipulation": (False, 0, []),
        "rugpull": (False, 0, []),
    }
    hypotheses = [
        {
            "phase": phase,
            "eligible": bool(gate),
            "support_score": round(score, 2) if gate else None,
            "score_kind": "uncalibrated_heuristic_support",
            "evidence_ids": refs if gate else [],
            "estimated_probability_pct": None,
        }
        for phase, (gate, score, refs) in criteria.items()
    ]
    eligible = sorted(
        [h for h in hypotheses if h["eligible"]], key=lambda h: h["support_score"], reverse=True
    )
    phase = (
        eligible[0]["phase"]
        if eligible
        else "insufficient_data"
        if not enough
        else "mixed_or_unclassified"
    )
    public_markets = summarize_markets(f.get("market_samples", []))
    gaps = [
        "token_security/deployer_evidence_required_for_rugpull",
        "independent_market/coordination_evidence_required_for_manipulation",
    ]
    if not any(
        "open_interest_base" in p["latest"] and "funding_rate" in p["latest"]
        for p in public_markets
    ):
        gaps.append("market_open_interest_and_funding_not_available")
    if public_markets:
        gaps.append("public_exchange_aggregates_do_not_identify_tracked_wallet_positions")
    if sum(bool(f["cohorts"][g]["observations"]) for g in GROUPS) < 4:
        gaps.append("not_all_four_cohorts_mapped_to_this_asset")
    if price_change is None:
        gaps.append("comparable_price_series_missing_or_source_changed")
    if f["source_changes"]:
        gaps.append("source_changed_balance_deltas_excluded")
    metrics = {
        "unique_traders": len(f["wallet_ids"]),
        "unique_watch_entries": len(f["watch_ids"]),
        "wallet_observations": f["observations"],
        "observed_wallet_observations": f["observed"],
        "coverage_pct": round(coverage, 2),
        "accumulating_wallet_pct": round(acc, 2),
        "distributing_wallet_pct": round(dist, 2),
        "net_spot_quantity": f["net_spot_quantity"],
        "verified_tx_count": len(f["tx_ids"]),
        "unique_fill_or_order_records": len(f["fill_ids"]),
        "price_change_pct": price_change,
        "price_range_pct": price_range,
        "near_liquidation_observations": f["near_risk_observations"],
        "liquidated_wallets": len(f["liquidated_wallets"]),
        "near_liquidation_wallets": len(f["near_risk_wallets"]),
        "verified_actions": f["actions"],
        "short_covering_wallets": len(f["short_covering_wallets"]),
        "long_reducing_wallets": len(f["long_reducing_wallets"]),
        "profitable_close_wallets": len(f["profitable_close_wallets"]),
        "public_markets": public_markets,
        "liquidity_context": summarize_context(f.get("liquidity_samples", [])),
        "cohorts": {
            g: {
                "unique_traders": len(v["wallet_ids"]),
                "observations": v["observations"],
                "observed": v["observed"],
                "states": v["states"],
            }
            for g, v in f["cohorts"].items()
        },
    }
    return {
        "asset_id": f["asset_id"],
        "symbols": f["symbols"],
        "phase": phase,
        "decision_status": "tentative_sample_assessment"
        if sum(bool(f["cohorts"][g]["observations"]) for g in GROUPS) == 4
        else "limited_cohort_assessment",
        "metrics": metrics,
        "hypotheses": hypotheses,
        "gaps": gaps,
        "features": f,
        "ai": {"status": "pending_or_disabled"},
    }


def build_screen(inputs, kind, revision, links=None):
    ids = [r["id"] for r in inputs]
    policy = {
        "version": 3,
        "liquidity_context": "sampled_context_only; no_wallet_order_attribution",
        "public_market_context": "same_venue_series_only; wallet_evidence_gates_unchanged",
        "coverage_pct": os.getenv("WHALE_SCREEN_MIN_COVERAGE_PCT", "70"),
        "sideways_range_pct": os.getenv("WHALE_SCREEN_SIDEWAYS_RANGE_PCT", "1.5"),
        "asset_links": links
        if links is not None
        else json.loads(os.getenv("WHALE_SCREEN_ASSET_LINKS_JSON", "{}")),
        "ai": {
            p: {
                "model": os.getenv("WHALE_SCREEN_" + p.upper() + "_MODEL", ""),
                "configured": bool(os.getenv("WHALE_SCREEN_" + p.upper() + "_KEY")),
            }
            for p in ("groq", "gemini", "openrouter")
        },
    }
    report_id = identity(json.dumps([kind, revision, ids, policy], sort_keys=True))
    start = inputs[0].get("window_start", inputs[0].get("started_at"))
    end = inputs[-1].get("window_end", inputs[-1]["completed_at"])
    seconds = max(0, (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds())
    features = features_from_cycles(inputs, links) if kind == "1h" else merge_features(inputs)
    assets = [assess_features(f, kind) for f in features]
    for asset in assets:
        asset["metrics"].update(
            nominal_window=kind,
            actual_span_minutes=round(seconds / 60, 2),
            scan_count=len(inputs) if kind == "1h" else sum(len(r["scan_ids"]) for r in inputs),
        )
    return {
        "id": report_id,
        "kind": kind,
        "revision": revision,
        "ready": True,
        "completed_at": now(),
        "window_start": start,
        "window_end": end,
        "input_ids": ids,
        "scan_ids": ids if kind == "1h" else [s for r in inputs for s in r["scan_ids"]],
        "actual_span_minutes": round(seconds / 60, 2),
        "nominal_hours": 1 if kind == "1h" else 4,
        "window_basis": "six_completed_scans"
        if kind == "1h"
        else "four_completed_nonoverlapping_hourly_reports",
        "assets": assets,
        "criteria_policy": policy,
        "note": "Tracked sample only; AI percentages are uncalibrated estimates, not verified market probabilities.",
    }


def screen_digest(report, selector=None):
    if not report.get("ready"):
        return f"SCREEN {report['kind']} | collecting {report.get('available', 0)}/{report['required']} inputs. No completed screening window yet."
    rows = report["assets"]
    if selector:
        rows = [
            r
            for r in rows
            if r["asset_id"].lower() == selector.lower()
            or selector.upper() in [s.upper() for s in r["symbols"]]
        ]

    def local(value):
        return (
            datetime.fromisoformat(value)
            .astimezone(ZoneInfo("Asia/Kolkata"))
            .strftime("%d %b %H:%M IST")
        )

    lines = [
        f"SCREEN {report['kind']} | {local(report['window_start'])} → {local(report['window_end'])}",
        f"{len(report['scan_ids'])} scans | actual span {report['actual_span_minutes']} minutes",
    ]
    for r in rows[:8]:
        m = r["metrics"]
        ai = r.get("ai", {})
        lines.append(
            f"{','.join(r['symbols'])} [{r['asset_id']}]\nPhase: {r['phase']} | traders {m['unique_traders']} | coverage {m['coverage_pct']}%\nAccumulating {m['accumulating_wallet_pct']}% | reducing {m['distributing_wallet_pct']}% | tx {m['verified_tx_count']}\nAI: {ai.get('status')} | {ai.get('consensus_phase', 'no model consensus')}"
        )
        estimates = [
            h for h in ai.get("hypotheses", []) if h.get("estimated_probability_pct") is not None
        ]
        if estimates:
            estimates.sort(key=lambda h: h["estimated_probability_pct"], reverse=True)
            lines.append(
                "AI estimates (uncalibrated): "
                + ", ".join(
                    f"{h['phase']} {h['estimated_probability_pct']}%" for h in estimates[:3]
                )
            )
        lc = m.get("liquidity_context", {})
        if lc.get("samples"):
            lines.append(
                "Liquidity samples: "
                + str(lc["samples"])
                + " | "
                + str(lc["candidate_counts"])
                + " | sampled associations, not wallet order identity"
            )
        supported = [h for h in r["hypotheses"] if h["eligible"]]
        markets = m.get("public_markets", [])
        if markets:
            lines.append(
                "Public market context: "
                + "; ".join(
                    f"{p['venue']} OI Δ {p['open_interest_change_pct']}% | funding {p['latest'].get('funding_rate_pct', '?')}%"
                    for p in markets[:3]
                )
            )
        lines.append(
            "Criteria: " + ", ".join(dict.fromkeys(e for h in supported for e in h["evidence_ids"]))
            if supported
            else "Criteria: insufficient observed evidence"
        )
        lines.append(
            "Rugpull/manipulation: unassessed; extra security/coordination evidence required."
        )
    if selector and not rows:
        lines.append(
            "No matching coin/contract in this window. Use the exact asset ID for ambiguous tickers."
        )
    lines.append(
        "Tentative tracked-sample assessment. Manipulation/rugpull probabilities require extra evidence; no trade instruction."
    )
    return "\n".join(lines)[:3900]


class ScreeningService:
    def __init__(self, store, ai=None):
        self.store, self.ai = store, ai
        self.lock = asyncio.Lock()

    async def prepare(self, inputs, kind, revision):
        report = build_screen(inputs, kind, revision, self.store.get("screen_asset_links"))
        existing = self.store.screen_by_id(report["id"])
        if existing:
            return existing
        if self.ai:
            for index, item in enumerate(report["assets"]):
                item["ai"] = (
                    await self.ai.analyze(item)
                    if index < int(os.getenv("WHALE_SCREEN_MAX_AI_ASSETS", "8"))
                    else {"status": "asset_budget_limit"}
                )
        return report

    async def advance(self):
        async with self.lock:
            created = 0
            revision = self.store.get("watch_revision", 0)
            cursor_key = "screen_cycle_cursor:" + str(revision)
            # Small DB batches avoid loading every historical full scan on free hosting.
            cursor = self.store.get(cursor_key, "")
            cycles = self.store.cycles_after(cursor, 6, revision)
            cycles = [
                c
                for c in cycles
                if c.get("screen_schema") == 1 and c.get("watch_revision", 0) == revision
            ]
            if len(cycles) == 6:
                report = await self.prepare(cycles, "1h", revision)
                self.store.save_screen(
                    report, True, cursor_key, cycles[-1]["completed_at"], self.message(report)
                )
                created += 1
            hourlies = list(reversed(self.store.screen_history("1h", revision, 4, True)))
            four_cursor = self.store.get("screen_hour_cursor:" + str(revision), "")
            if len(hourlies) == 4 and hourlies[0]["completed_at"] > four_cursor:
                report = await self.prepare(hourlies, "4h", revision)
                self.store.save_screen(
                    report,
                    True,
                    "screen_hour_cursor:" + str(revision),
                    hourlies[-1]["completed_at"],
                    self.message(report),
                )
                created += 1
            return created

    def message(self, report):
        return (
            screen_digest(report)
            if os.getenv("WHALE_SCREEN_AUTO_REPORTS", "true").lower() == "true"
            and os.getenv("WHALE_TELEGRAM_OWNER_ID")
            else None
        )

    async def requested(self, kind):
        async with self.lock:
            revision = self.store.get("watch_revision", 0)
            if kind == "1h":
                inputs = [
                    c
                    for c in reversed(self.store.report_history(6))
                    if c.get("screen_schema") == 1 and c.get("watch_revision", 0) == revision
                ]
                required = 6
            else:
                inputs = list(reversed(self.store.screen_history("1h", revision, 4, True)))
                required = 4
            if len(inputs) < required:
                return {
                    "kind": kind,
                    "ready": False,
                    "available": len(inputs),
                    "required": required,
                    "assets": [],
                }
            report = await self.prepare(inputs, kind, revision)
            self.store.save_screen(report)
            return report
