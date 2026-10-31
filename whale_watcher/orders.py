"""Read-only order evidence, kept distinct from snapshots, executions and intent estimates."""

import asyncio
from collections import Counter
from decimal import Decimal, localcontext
from functools import wraps
import hashlib
import json

from .core import now
from .diagnostics import failure_code
from .risk import number


def exact_quantities(function):
    @wraps(function)
    def call(*args, **kwargs):
        with localcontext() as context:
            context.prec = 100
            return function(*args, **kwargs)

    return call


def required_number(value, *, positive=False):
    result = number(value)
    if result is None or (positive and result <= 0):
        raise ValueError("Order quantity/price is missing or invalid")
    return result


@exact_quantities
def intent(order, signed_position, hedged=False):
    if hedged:
        return "unknown_hedged_position"
    if order["reduce_only"] is True:
        return "reduce_only_instruction"
    if order["remaining_base"] is None:
        return "unknown_dynamic_quantity"
    quantity = Decimal(order["remaining_base"])
    side = 1 if order["side"] == "buy" else -1
    if not signed_position:
        return "open_long_candidate" if side > 0 else "open_short_candidate"
    if signed_position * side > 0:
        return "increase_position_candidate"
    return (
        "reverse_position_candidate"
        if quantity > abs(signed_position)
        else "reduce_position_candidate"
    )


@exact_quantities
def normalize_hyperliquid_orders(payload, coin):
    if not isinstance(payload, list):
        raise ValueError("Incomplete open-order response")
    rows, seen = [], set()
    for item in payload:
        if item.get("coin") != coin:
            continue
        oid = str(item["oid"])
        if oid in seen or not oid.isdigit():
            raise ValueError("Duplicate/invalid order ID")
        seen.add(oid)
        side = {"B": "buy", "A": "sell"}.get(item["side"])
        if side is None:
            raise ValueError("Invalid order side")
        size = required_number(item["sz"])
        original = required_number(item.get("origSz", item["sz"]))
        if size < 0 or original < 0:
            raise ValueError("Negative order size")
        price = required_number(item["limitPx"])
        if price < 0:
            raise ValueError("Invalid order price")
        rows.append(
            {
                "order_id": oid,
                "coin": coin,
                "side": side,
                "remaining_base": str(size) if size else None,
                "quantity_status": "reported_remaining"
                if size
                else "zero_or_dynamic_quantity_unknown",
                "original_base": str(original) if original else None,
                "limit_price": str(price),
                "display_notional_quote": str(price * size) if price and size else None,
                "reduce_only": item.get("reduceOnly")
                if isinstance(item.get("reduceOnly"), bool)
                else None,
                "conditional": item.get("isTrigger")
                if isinstance(item.get("isTrigger"), bool)
                else None,
                "position_tpsl": item.get("isPositionTpsl"),
                "order_type": item.get("orderType"),
                "trigger_price": item.get("triggerPx"),
                "trigger_condition": item.get("triggerCondition"),
                "source_created_ms": item.get("timestamp"),
                "basis": "venue_reported_open_order",
            }
        )
    return rows


@exact_quantities
def normalize_ccxt_orders(payload, coin, symbol, multiplier):
    if not isinstance(payload, list):
        raise ValueError("Incomplete authorized order response")
    multiplier = required_number(multiplier, positive=True)
    rows = []
    for item in payload:
        if item.get("symbol") != symbol:
            continue
        remaining = required_number(item.get("remaining"), positive=True) * multiplier
        original = required_number(item.get("amount"), positive=True) * multiplier
        price = number(item.get("price"))
        if item.get("side") not in ("buy", "sell") or item.get("id") is None:
            raise ValueError("Invalid normalized order identity/side")
        rows.append(
            {
                "order_id": str(item["id"]),
                "coin": coin,
                "side": item["side"],
                "remaining_base": str(remaining),
                "original_base": str(original),
                "limit_price": str(price) if price is not None else None,
                "display_notional_quote": str(price * remaining)
                if price is not None and price > 0
                else None,
                "reduce_only": item.get("reduceOnly"),
                "conditional": item.get("triggerPrice") is not None,
                "trigger_price": item.get("triggerPrice"),
                "order_type": item.get("type"),
                "source_created_ms": item.get("timestamp"),
                "basis": "authorized_venue_open_order",
            }
        )
    if len({row["order_id"] for row in rows}) != len(rows):
        raise ValueError("Duplicate order ID")
    return rows


@exact_quantities
def normalize_fills(fills, coin):
    from .reporting import position_state

    rows, seen = [], set()
    for fill in fills:
        if fill.get("coin") != coin:
            continue
        identity = str(
            fill.get("tid") or hashlib.sha256(json.dumps(fill, sort_keys=True).encode()).hexdigest()
        )
        if identity in seen:
            continue
        seen.add(identity)
        quantity = required_number(fill["sz"], positive=True)
        price = required_number(fill["px"], positive=True)
        sign = {"B": Decimal(1), "A": Decimal(-1)}.get(fill["side"])
        if sign is None:
            raise ValueError("Invalid fill side")
        before = number(fill.get("startPosition"))
        after = before + quantity * sign if before is not None else None
        rows.append(
            {
                "fill_id": identity,
                "order_id": str(fill["oid"]),
                "coin": coin,
                "side": "buy" if sign > 0 else "sell",
                "base_quantity": str(quantity),
                "price": str(price),
                "source_time_ms": int(fill["time"]),
                "reported_direction": fill.get("dir"),
                "position_before_fill": str(before) if before is not None else None,
                "position_after_fill": str(after) if after is not None else None,
                "position_condition": position_state(before, after)
                if after is not None
                else "unknown",
                "closed_pnl": str(required_number(fill["closedPnl"]))
                if fill.get("closedPnl") is not None
                else None,
                "fee": str(required_number(fill["fee"])) if fill.get("fee") is not None else None,
                "fee_currency": fill.get("feeToken"),
                "basis": "venue_reported_execution",
            }
        )
    return sorted(rows, key=lambda row: (row["source_time_ms"], row["fill_id"]))


def unique_fills(fills):
    seen, result = set(), []
    for fill in fills:
        identity = (
            str(fill["tid"])
            if fill.get("tid") is not None
            else hashlib.sha256(json.dumps(fill, sort_keys=True).encode()).hexdigest()
        )
        if identity not in seen:
            seen.add(identity)
            result.append(fill)
    return result


class OrderStudy:
    def __init__(self, store, provider, *, max_orders=200, status_budget=20):
        self.store, self.provider = store, provider
        self.max_orders = min(1000, max(1, max_orders))
        self.status_budget = min(100, max(0, status_budget))
        self.cache = {}
        self.status_queries = 0

    async def capture(self, row, position, previous):
        key = (row.get("platform"), row["address"], row["coin"])
        if key not in self.cache:
            self.cache[key] = asyncio.create_task(self._capture(row, position, previous))
        return await self.cache[key]

    async def raw_orders(self, row):
        platform = row.get("platform")
        if platform == "hyperliquid" and hasattr(self.provider, "info"):
            dex = row["coin"].split(":")[0] if ":" in row["coin"] else ""
            key = ("open_orders", row["address"], dex)
            if key not in self.provider.cache:
                self.provider.cache[key] = asyncio.create_task(
                    self.provider.info(
                        {"type": "frontendOpenOrders", "user": row["address"], "dex": dex}
                    )
                )
            return normalize_hyperliquid_orders(await self.provider.cache[key], row["coin"])
        exchange = getattr(self.provider, "exchanges", {}).get(str(platform) + ":" + row["address"])
        if exchange is None or not hasattr(exchange, "fetch_open_orders"):
            raise ValueError(
                "Orders require a supported public or authorized read-only account adapter"
            )
        symbol = row["coin"] + "/USDT:USDT"
        payload = await self.provider.recovery.run(
            platform + "_orders", lambda: exchange.fetch_open_orders(symbol)
        )
        return normalize_ccxt_orders(
            payload, row["coin"], symbol, exchange.markets[symbol].get("contractSize", 1)
        )

    async def order_status(self, row, oid):
        if row.get("platform") != "hyperliquid":
            return None  # No cross-venue substitution or guessed CCXT cancellation status.
        response = await self.provider.info(
            {"type": "orderStatus", "user": row["address"], "oid": int(oid)}
        )
        if response.get("status") == "unknownOid":
            return None
        record = response["order"]
        order = record["order"]
        if (
            response.get("status") != "order"
            or str(order["oid"]) != oid
            or order["coin"] != row["coin"]
        ):
            raise ValueError("Order status identity mismatch")
        return {"venue_status": record["status"], "source_status_ms": record["statusTimestamp"]}

    async def _capture(self, row, position, previous):
        from .reporting import position_state

        identity = row.get("platform", "") + ":" + row["address"] + ":" + row["coin"]
        snapshot_key = "orders:" + hashlib.sha256(identity.encode()).hexdigest()[:32]
        old = self.store.get(snapshot_key)
        at = now()
        report = {
            "source": row.get("platform"),
            "address": row["address"],
            "coin": row["coin"],
            "observed_at": at,
            "status": "unavailable",
            "open_orders": None,
            "position_amount": position["amount"],
            "position_source_observed_at": position.get("source_observed_at"),
            "fills": [],
            "changes": [],
            "fill_coverage": position.get("fill_coverage", "unavailable"),
        }
        comparable = (
            previous is not None
            and previous.get("source") == position.get("source")
            and position.get("direction") != "hedged"
        )
        before = number(previous.get("amount")) if comparable else None
        after = required_number(position["amount"])
        report["position_condition"] = position_state(before, after)
        report["position_comparison"] = (
            "comparable_snapshot" if comparable else "baseline_or_source_change_or_hedged"
        )
        events = []
        try:
            fills = (
                normalize_fills(position.get("fills", []), row["coin"])
                if row.get("platform") == "hyperliquid"
                else []
            )
            fees = {}
            for fill in fills:
                if fill["fee"] is not None and fill["fee_currency"]:
                    currency = fill["fee_currency"]
                    fees[currency] = str(Decimal(fees.get(currency, "0")) + Decimal(fill["fee"]))
                events.append(
                    {"kind": "fill", "identity": "fill:" + fill["fill_id"], "evidence": fill}
                )
            report.update(
                fills=fills[-50:],
                fills_total=len(fills),
                fills_display_truncated=len(fills) > 50,
                fees_by_currency=fees,
            )
        except (ValueError, KeyError, TypeError) as exc:
            report["fill_parse_failure"] = failure_code("order_fills", exc)
        try:
            orders = await self.raw_orders(row)
            at = now()
            report["observed_at"] = at
            total_orders = len(orders)
            complete = len(orders) <= self.max_orders
            orders = orders[: self.max_orders]
            for order in orders:
                order["inferred_effect"] = intent(
                    order, after, position.get("direction") == "hedged"
                )
            current = {order["order_id"]: order for order in orders}
            prior = (
                {order["order_id"]: order for order in old["open_orders"]}
                if old and old.get("complete")
                else {}
            )
            compare_orders = bool(old and old.get("complete") and complete)
            for oid, order in current.items():
                kind = (
                    "order_baseline"
                    if not compare_orders
                    else "order_appeared"
                    if oid not in prior
                    else "order_changed"
                    if {k: v for k, v in order.items() if k != "inferred_effect"}
                    != {k: v for k, v in prior[oid].items() if k != "inferred_effect"}
                    else None
                )
                if kind:
                    events.append(
                        {"kind": kind, "identity": kind + ":" + oid + ":" + at, "evidence": order}
                    )
            disappeared = sorted(set(prior) - set(current)) if compare_orders else []
            unresolved = 0
            for index, oid in enumerate(disappeared):
                evidence = {
                    "order_id": oid,
                    "basis": "not_in_latest_open_order_snapshot",
                    "venue_status": None,
                }
                if index < 5 and self.status_queries < self.status_budget:
                    self.status_queries += 1
                    try:
                        confirmed = await self.order_status(row, oid)
                        if confirmed:
                            evidence.update(confirmed, basis="venue_order_status_response")
                    except Exception as exc:
                        evidence["status_failure"] = failure_code("order_status", exc)
                unresolved += evidence["venue_status"] is None
                events.append(
                    {
                        "kind": "order_disappeared",
                        "identity": "gone:" + oid + ":" + at,
                        "evidence": evidence,
                    }
                )
            report.update(
                status="observed",
                open_orders=orders,
                open_count=total_orders,
                complete=complete,
                snapshot_comparison="comparable" if compare_orders else "baseline_or_truncated",
                unresolved_disappearances=unresolved,
            )
            if comparable and before != after:
                events.append(
                    {
                        "kind": "position_snapshot_change",
                        "identity": "position:" + at,
                        "evidence": {
                            "before": str(before),
                            "after": str(after),
                            "condition": report["position_condition"],
                            "basis": "net_position_snapshot_comparison",
                        },
                    }
                )
            report["changes"] = [
                {"kind": event["kind"], **event["evidence"]}
                for event in events
                if event["kind"] != "fill"
            ][:100]
            self.store.save_order_study(
                snapshot_key,
                {"open_orders": orders, "complete": complete, "observed_at": at},
                identity,
                events,
                at,
            )
        except Exception as exc:
            report.update(
                failure_code=failure_code("open_orders", exc),
                last_success_at=(old or {}).get("observed_at"),
            )
            # Executions remain durable even when the open-order endpoint fails.
            self.store.save_order_study(
                None, None, identity, [e for e in events if e["kind"] == "fill"], at
            )
        report["caveat"] = (
            "Polling can miss orders placed and removed between scans. Disappearance is not cancellation or fill. "
            "Order effects are inferred; executions and position snapshots remain separate. Fees/funding are not netted into closed P&L."
        )
        return report


def summarize_orders(observations):
    seen, rows = set(), []
    for row in observations:
        if not row["group"].startswith("Futures"):
            continue
        identity = (row.get("platform"), row["address"], row["coin"])
        if identity in seen:
            continue
        seen.add(identity)
        rows.append(row)
    observed = [row for row in rows if row.get("orders", {}).get("status") == "observed"]
    return {
        "wallet_coin_pairs": len(rows),
        "orders_observed": len(observed),
        "orders_unavailable": len(rows) - len(observed),
        "open_orders": sum(row["orders"]["open_count"] for row in observed),
        "position_conditions": dict(
            Counter(row.get("orders", {}).get("position_condition", row["state"]) for row in rows)
        ),
        "by_coin": {
            coin: {
                "wallet_coin_pairs": sum(r["coin"] == coin for r in rows),
                "open_orders": sum(
                    r["orders"]["open_count"] for r in observed if r["coin"] == coin
                ),
            }
            for coin in sorted({r["coin"] for r in rows})
        },
        "note": "Open orders are instructions, not executions. Condition counts describe the observed watchlist.",
    }


def orders_digest(cycle, coin=None):
    rows = [r for report in cycle["reports"].values() for r in report["rows"]]
    rows += cycle.get("neutral_watch", {}).get("rows", [])
    if coin:
        rows = [row for row in rows if row["coin"].upper() == coin.upper()]
    summary = summarize_orders(rows)
    return (
        "ORDERS & POSITION CONDITIONS\n"
        + cycle["completed_at"]
        + "\n"
        + f"Observed {summary['orders_observed']}/{summary['wallet_coin_pairs']} wallet/coin pairs · open orders {summary['open_orders']}\n"
        + "Conditions: "
        + json.dumps(summary["position_conditions"])
        + "\n"
        + "Order intent, executed fills and net position changes are separate evidence. No complete intracycle history claim."
    )[:3900]
