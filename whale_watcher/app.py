import asyncio
import hmac
import os
import time
import uuid
import logging
import json
from decimal import Decimal
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.gzip import GZipMiddleware

from .core import Store, parse_excel, now
from .sources import OfficialSources
from .free_sources import FreePriorityProvider, FuturesProvider
from .reporting import (
    make_cycle,
    spot_evidence,
    position_state,
    holding_state,
    telegram_digest,
    risk_digest,
)
from .recovery import Recovery
from .screening import ScreeningService, screen_digest
from .screen_ai import ScreenAI
from .public_markets import PublicMarkets, market_digest, symbol_name
from .liquidity_service import LiquidityService
from .liquidity_context import correlate_cycle, liquidity_digest
from .portfolio import inventory_summary
from .csv_import import parse_candidates, append_candidates
from liquidity.security import DashboardAuth, private_http_logging
from .diagnostics import failure_code
from .orders import OrderStudy, orders_digest, summarize_orders

store = None
client = None
provider = None
futures_provider = None
recovery = None
screening = None
public_markets = None
liquidity_service = None
screen_wake = asyncio.Event()
scan_lock = asyncio.Lock()
import_lock = asyncio.Lock()
tg_lock = asyncio.Lock()
wake = asyncio.Event()
manual_scan = False
login_guard = None


def auth(request: Request):
    if login_guard is None:
        raise HTTPException(503, "Authentication is not initialized")
    retry = login_guard.authenticate(
        request.headers.get("authorization", ""),
        request.client.host if request.client else "unknown",
    )
    if retry > 0:
        raise HTTPException(
            429, "Too many failed authentication attempts", headers={"Retry-After": str(retry)}
        )
    if retry < 0:
        raise HTTPException(401, "Authentication required", headers={"WWW-Authenticate": "Basic"})


async def scan():
    if scan_lock.locked():
        return
    async with scan_lock:
        wallets, scan_watch_revision = store.wallet_snapshot()
        order_study = (
            OrderStudy(
                store,
                futures_provider,
                max_orders=int(os.getenv("WHALE_MAX_ORDERS_PER_WALLET", "200")),
                status_budget=int(os.getenv("WHALE_ORDER_STATUS_BUDGET", "20")),
            )
            if os.getenv("WHALE_ORDERS_ENABLED", "true").lower() == "true" and futures_provider
            else None
        )
        started = now()
        liquidity_context = (
            liquidity_service.compact_snapshot()
            if liquidity_service
            else {"mode": "unavailable", "markets": {}, "inferred_signals": []}
        )
        window_start = (store.get("latest_cycle") or {}).get("window_end", started)
        # Source events and fills use this cutoff; observations retain their own times.
        window_end = started
        evidence = store.evidence_window(window_start, window_end)
        errors, changed, observations, pending_snapshots = [], 0, [], []
        if hasattr(provider, "begin_cycle"):
            await provider.begin_cycle(wallets)
        if futures_provider:
            futures_provider.begin_cycle()
        sem = asyncio.Semaphore(8)

        async def one(row):
            nonlocal changed
            async with sem:
                previous = None
                try:
                    is_future = row["group"].startswith("Futures")
                    prefix = "position:" if is_future else "snapshot:"
                    previous = store.get(prefix + row["id"])
                    if is_future:
                        if not futures_provider:
                            raise ValueError("Futures adapter unavailable")
                        result = await futures_provider.observe(row, window_start, window_end)
                    elif hasattr(provider, "observe"):
                        result = await provider.observe(row)
                    else:
                        result = {
                            "amount": str(await provider.balance(row)),
                            "source": "finalized_rpc",
                        }
                    if is_future and order_study:
                        try:
                            result["orders"] = await order_study.capture(row, result, previous)
                        except Exception as exc:
                            result["orders"] = {
                                "status": "unavailable",
                                "failure_code": failure_code("order_study", exc),
                            }
                    amount = Decimal(result["amount"])
                    if not amount.is_finite() or (not is_future and amount < 0):
                        raise ValueError("Invalid wallet/position quantity")
                    old = Decimal(previous["amount"]) if previous else None
                    state = position_state(old, amount) if is_future else holding_state(old, amount)
                    observation = {
                        k: row[k]
                        for k in (
                            "id",
                            "label",
                            "address",
                            "group",
                            "chain",
                            "coin",
                            "rank",
                            "platform",
                            "asset",
                        )
                    }
                    observation.update(result)
                    if row.get("import_snapshot"):
                        observation["import_snapshot"] = row["import_snapshot"]
                    observation.update(
                        {
                            "state": state,
                            "observed_at": now(),
                            "previous": str(old) if old is not None else None,
                            "actions": []
                            if is_future
                            else spot_evidence(row, evidence, window_start, window_end),
                            "realized_pnl": result.get("realized_pnl"),
                            "liquidated": result.get("liquidated"),
                        }
                    )
                    observation["balance_source_changed"] = bool(
                        previous and previous.get("source") != result["source"]
                    )
                    observation["previous_observed_at"] = previous.get("at") if previous else None
                    if is_future and result.get("liquidated") is True and previous:
                        legs = previous.get("risk_legs", [])
                        observation["liquidation_side"] = (
                            legs[0].get("side") if len(legs) == 1 else previous.get("direction")
                        )
                    pending_snapshots.append(
                        {
                            "row": row,
                            "amount": amount,
                            "future": is_future,
                            "previous": previous,
                            "at": observation["observed_at"],
                            "result": result,
                        }
                    )
                    if old is not None and old != amount:
                        changed += 1
                    observations.append(observation)
                except Exception as exc:
                    code = failure_code("wallet_observe", exc)
                    # Avoid leaking RPC URLs/API credentials from HTTP exceptions.
                    reason = "Account credentials/platform missing, endpoint unavailable, invalid metadata or free budget exhausted"
                    if code.startswith(("internal_error", "schema_error")):
                        reason = (
                            "Adapter implementation/schema error; inspect the safe failure code"
                        )
                    errors.append({"label": row["label"], "reason": reason, "failure_code": code})
                    observations.append(
                        {
                            **{
                                k: row[k]
                                for k in (
                                    "id",
                                    "label",
                                    "address",
                                    "group",
                                    "chain",
                                    "coin",
                                    "rank",
                                    "platform",
                                    "asset",
                                )
                            },
                            "state": "unavailable",
                            "reason": reason,
                            "amount": None,
                            "actions": [],
                            "last_known": previous,
                            "freshness": "current_data_unavailable",
                            "failure_code": code,
                        }
                    )

        await asyncio.gather(*(one(row) for row in wallets))
        # Commit successful snapshots together; one failed source retains its old snapshot.
        store.save_scan_observations(pending_snapshots)
        # Inventory is opt-in per wallet, with two program queries shared across all mints.
        # An inventory error must not turn a successful native balance into an error.
        portfolios = []
        seen_portfolios = set()
        for row in wallets:
            if (
                not row.get("discover_tokens")
                or row["chain"] != "solana"
                or row["address"] in seen_portfolios
            ):
                continue
            seen_portfolios.add(row["address"])
            key = "portfolio:" + row["address"]
            previous_inventory = store.get(key)
            try:
                current = await provider.inventory(row["address"])
                summary = inventory_summary(current, previous_inventory)
                store.put(key, current)
                summary.update(watch_id=row["id"], status="observed")
            except Exception as exc:
                code = failure_code("token_inventory", exc)
                summary = {
                    "failure_code": code,
                    "watch_id": row["id"],
                    "address": row["address"],
                    "status": "unavailable",
                    "last_success_at": (previous_inventory or {}).get("observed_at"),
                    "reason": "Complete token inventory unavailable; previous inventory preserved",
                }
            store.put("portfolio_status:" + row["address"], summary)
            portfolios.append(summary)
        market_data = {"snapshots": [], "status": "adapter_unavailable"}
        if public_markets:
            try:
                market_data = await public_markets.collect(
                    wallets, store.get("screen_asset_links"), store.get("market_asset_links")
                )
            except Exception as exc:
                market_data = {
                    "snapshots": [],
                    "status": "collection_failed; wallet_results_preserved",
                    "failure_code": failure_code("public_markets", exc),
                }
        completed = now()
        if recovery:
            recovery.flush()
        observations.sort(key=lambda r: (r["group"], r["rank"], r["coin"]))
        cycle = make_cycle(
            uuid.uuid4().hex,
            started,
            completed,
            window_start,
            observations,
            getattr(provider, "attempts", []),
        )
        cycle["portfolios"] = portfolios
        cycle["window_end"] = window_end
        cycle["watch_revision"] = scan_watch_revision
        cycle["screen_schema"] = 1
        cycle["public_markets"] = market_data
        cycle["liquidity"] = liquidity_context
        cycle["wallet_liquidity"] = correlate_cycle(
            cycle,
            liquidity_context,
            store.get("screen_asset_links"),
            store.get("market_asset_links"),
        )
        cycle["interval_seconds"] = store.get("interval", 600)
        cycle["scan_duration_seconds"] = round(
            (
                __import__("datetime").datetime.fromisoformat(completed)
                - __import__("datetime").datetime.fromisoformat(started)
            ).total_seconds(),
            2,
        )
        cycle["schedule_overrun"] = cycle["scan_duration_seconds"] > cycle["interval_seconds"]
        messages = (
            telegram_digest(cycle)
            if os.getenv("WHALE_AUTO_REPORTS", "true").lower() == "true"
            and os.getenv("WHALE_TELEGRAM_OWNER_ID")
            else []
        )
        if messages and os.getenv("WHALE_RISK_REPORT_TELEGRAM", "true").lower() == "true":
            messages.append(risk_digest(cycle))
        if (
            messages
            and market_data["snapshots"]
            and os.getenv("WHALE_MARKET_AUTO_REPORTS", "true").lower() == "true"
        ):
            messages.append(market_digest(market_data["snapshots"]))
        if messages and os.getenv("WHALE_LIQUIDITY_AUTO_REPORTS", "true").lower() == "true":
            messages.append(liquidity_digest(liquidity_context, cycle["wallet_liquidity"]))
        if messages and os.getenv("WHALE_ORDER_AUTO_REPORTS", "true").lower() == "true":
            messages.append(orders_digest(cycle))
        store.save_cycle(cycle, messages)
        store.put(
            "last_scan",
            {
                "at": completed,
                "entries": len(wallets),
                "changed": changed,
                "errors": errors,
                "cycle_id": cycle["id"],
            },
        )
        screen_wake.set()


async def run_schedule_step():
    global manual_scan
    interval = store.get("interval", 600)
    due = store.get("next_scan_at", 0)
    scheduled = not store.get("paused", True) and time.time() >= due
    if manual_scan or scheduled:
        manual_scan = False
        anchor = time.time()
        try:
            await scan()
        except Exception as exc:
            store.put(
                "scan_failure",
                {
                    "at": now(),
                    "reason": "Scan failed; database/provider configuration needs review",
                    "failure_code": failure_code("scheduled_scan", exc),
                },
            )
        next_due = anchor + interval
        while next_due <= time.time():
            next_due += interval
        store.put("next_scan_at", next_due)
    delay = (
        max(0.1, store.get("next_scan_at", time.time() + interval) - time.time())
        if not store.get("paused", True)
        else interval
    )
    try:
        await asyncio.wait_for(wake.wait(), timeout=delay)
    except asyncio.TimeoutError:
        pass
    wake.clear()


async def runner():
    while True:
        try:
            await run_schedule_step()
        except Exception:
            logging.warning("Whale scheduler unavailable; retrying in 30 seconds")
            await asyncio.sleep(30)


async def delivery_runner():
    while True:
        token, owner = os.getenv("WHALE_TELEGRAM_TOKEN"), os.getenv("WHALE_TELEGRAM_OWNER_ID")
        try:
            if token and owner:
                for item in store.pending_deliveries():
                    try:
                        response = await client.post(
                            f"https://api.telegram.org/bot{token}/sendMessage",
                            json={"chat_id": owner, "text": item["text"]},
                        )
                        response.raise_for_status()
                        if not response.json().get("ok"):
                            raise ValueError("Telegram rejected report")
                        store.delivered(item["id"])
                        await asyncio.sleep(1.1)
                    except Exception:
                        store.put(
                            "report_delivery_error",
                            {
                                "at": now(),
                                "reason": "Telegram delivery failed; pending report will retry",
                            },
                        )
                        break
        except Exception:
            logging.warning("Whale report queue unavailable; retrying")
        await asyncio.sleep(30)


async def screening_runner():
    while True:
        screen_wake.clear()
        try:
            for _ in range(8):
                if not await screening.advance():
                    break
            try:
                await asyncio.wait_for(screen_wake.wait(), timeout=600)
            except asyncio.TimeoutError:
                pass
        except Exception:
            logging.warning("Screening aggregation unavailable; retrying in 30 seconds")
            await asyncio.sleep(30)


@asynccontextmanager
async def lifespan(app):
    global \
        store, \
        client, \
        provider, \
        futures_provider, \
        recovery, \
        screening, \
        public_markets, \
        liquidity_service, \
        screen_wake, \
        scan_lock, \
        import_lock, \
        tg_lock, \
        wake, \
        manual_scan, \
        login_guard
    private_http_logging()
    login_guard = DashboardAuth(os.getenv("WHALE_ADMIN_TOKEN", ""), "WHALE_ADMIN_TOKEN")
    scan_lock, import_lock, tg_lock = asyncio.Lock(), asyncio.Lock(), asyncio.Lock()
    wake, manual_scan = asyncio.Event(), False
    screen_wake = asyncio.Event()
    url = os.getenv("WHALE_DATABASE_URL", "sqlite:///runtime/whale.sqlite3")
    if os.getenv("RENDER") and not url.startswith(
        ("postgres://", "postgresql://", "postgresql+psycopg://")
    ):
        raise RuntimeError("Render requires external durable PostgreSQL: WHALE_DATABASE_URL")
    Path("runtime").mkdir(exist_ok=True)
    store = Store(url)
    if store.get("reporting_version", 0) < 2:
        store.put("interval", 600)
        store.put("reporting_version", 2)
    client = httpx.AsyncClient(timeout=25)
    recovery = Recovery(store)
    provider = FreePriorityProvider(client, store, recovery)
    futures_provider = FuturesProvider(client, store, recovery)
    screening = ScreeningService(store, ScreenAI(client, store, recovery))
    public_markets = PublicMarkets(client, store, recovery)
    liquidity_service = LiquidityService(store)
    liquidity_task = asyncio.create_task(liquidity_service.run())
    task = asyncio.create_task(runner())
    delivery_task = asyncio.create_task(delivery_runner())
    screen_task = asyncio.create_task(screening_runner())
    try:
        yield
    finally:
        for worker in (task, delivery_task, screen_task, liquidity_task):
            worker.cancel()
        try:
            async with asyncio.timeout(10):
                await asyncio.gather(
                    task, delivery_task, screen_task, liquidity_task, return_exceptions=True
                )
                await asyncio.gather(futures_provider.close(), client.aclose())
        except TimeoutError:
            logging.error("Service shutdown exceeded its 10-second deadline")
            raise RuntimeError("Service shutdown exceeded its deadline") from None
        finally:
            liquidity_service = None
            public_markets = None
            login_guard = None
            store.engine.dispose()


app = FastAPI(title="Whale Watcher + Liquidity Intelligence", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1000, compresslevel=4)
app.mount(
    "/liquidity-static",
    StaticFiles(directory=Path(__file__).with_name("liquidity_static")),
    name="liquidity-static",
)


@app.get("/liquidity", response_class=HTMLResponse, dependencies=[Depends(auth)])
async def liquidity_dashboard():
    return (
        Path(__file__)
        .with_name("liquidity_static")
        .joinpath("index.html")
        .read_text(encoding="utf-8")
    )


@app.get("/api/liquidity", dependencies=[Depends(auth)])
async def liquidity_api():
    return (
        liquidity_service.snapshot()
        if liquidity_service
        else {"mode": "unavailable", "markets": {}}
    )


@app.get("/api/liquidity/state", dependencies=[Depends(auth)])
async def liquidity_chart_api(market: str = "", since: int = 0):
    return (
        liquidity_service.snapshot(history_market=market, since_ms=max(0, since))
        if liquidity_service
        else {"mode": "unavailable", "markets": {}}
    )


@app.get("/health")
def health():
    return {"ok": True, "service": "whale-watcher", "scan_running": scan_lock.locked()}


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(auth)])
def dashboard():
    return Path(__file__).with_name("dashboard.html").read_text(encoding="utf-8")


@app.get("/api/orders", dependencies=[Depends(auth)])
def order_report_api(coin: str = "", account: str = "", limit: int = 100, before_id: int = 0):
    cycle = store.get("latest_cycle")
    if not cycle:
        return {"status": "awaiting_scan", "history": []}
    rows = [row for report in cycle["reports"].values() for row in report["rows"]]
    rows += cycle.get("neutral_watch", {}).get("rows", [])
    rows = [
        r
        for r in rows
        if r["group"].startswith("Futures")
        and (not coin or r["coin"].upper() == coin.upper())
        and (not account or r["address"].lower() == account.lower())
    ]
    history = store.order_history(coin.upper(), account.lower(), limit, before_id)
    return {
        "completed_at": cycle["completed_at"],
        "summary": summarize_orders(rows),
        "wallets": [
            {
                "id": r["id"],
                "address": r["address"],
                "coin": r["coin"],
                "label": r["label"],
                "state": r["state"],
                "orders": r.get("orders", {"status": "disabled_or_unavailable"}),
            }
            for r in rows
        ],
        "history": history,
        "next_before_id": history[-1]["storage_id"]
        if len(history) == max(1, min(200, limit))
        else None,
    }


@app.get("/api/portfolios/{watch_id}", dependencies=[Depends(auth)])
def portfolio_api(
    watch_id: str, offset: int = 0, limit: int = 100, search: str = "", positive: bool = True
):
    row = next(
        (r for r in store.get("wallets", []) if r["id"] == watch_id and r.get("discover_tokens")),
        None,
    )
    if row is None:
        raise HTTPException(404, "Portfolio watch not found")
    inventory = store.get("portfolio:" + row["address"])
    if inventory is None:
        raise HTTPException(404, "Inventory not yet observed")
    holdings = [
        r
        for r in inventory["holdings"]
        if (not positive or int(r["raw_amount"]) > 0) and search.lower() in r["mint"].lower()
    ]
    offset, limit = max(0, offset), max(1, min(200, limit))
    return {
        **{k: v for k, v in inventory.items() if k != "holdings"},
        "refresh": store.get("portfolio_status:" + row["address"]),
        "matched": len(holdings),
        "offset": offset,
        "limit": limit,
        "holdings": holdings[offset : offset + limit],
    }


@app.get("/api/status", dependencies=[Depends(auth)])
def status():
    return {
        "paused": store.get("paused", True),
        "interval_seconds": store.get("interval", 600),
        "capacity": 1000,
        "wallets": [
            {
                **r,
                "snapshot": store.get(
                    ("position:" if r["group"].startswith("Futures") else "snapshot:") + r["id"]
                ),
            }
            for r in store.get("wallets", [])
        ],
        "last_scan": store.get("last_scan"),
        "reports": store.reports(),
        "cycle": store.get("latest_cycle"),
        "next_scan_at": store.get("next_scan_at"),
        "pending_reports": len(store.pending_deliveries()),
    }


async def import_data(data):
    rows = parse_excel(data)
    async with import_lock:
        if scan_lock.locked():
            raise ValueError("Scan running; retry import when complete")
        store.replace_wallets(rows, bump_revision=True)
        if rows:
            store.put("paused", False)
            store.put("next_scan_at", time.time() + 600)
            wake.set()
    return {
        "imported": len(rows),
        "mode": "replace_watchlist",
        "unique_addresses": len({(r["chain"], r["address"]) for r in rows}),
    }


async def limited_body(request, maximum=5_000_000):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > maximum:
            raise HTTPException(413, "Upload too large")
    return bytes(body)


@app.post("/api/import", dependencies=[Depends(auth)])
async def excel(request: Request):
    try:
        return await import_data(await limited_body(request))
    except Exception as exc:
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(
            400, str(exc) if isinstance(exc, ValueError) else "Invalid XLSX workbook"
        ) from None


@app.post("/api/import/csv", dependencies=[Depends(auth)])
async def import_csv(request: Request):
    try:
        rows = parse_candidates(await limited_body(request))
        async with import_lock:
            if scan_lock.locked():
                raise HTTPException(409, "Scan running; retry after completion")
            result = append_candidates(store, rows)
            wake.set()
        return result
    except (ValueError, UnicodeError) as exc:
        raise HTTPException(400, str(exc)) from None


@app.post("/api/control/{action}", dependencies=[Depends(auth)])
async def control(action: str):
    return await command("/" + action)


async def command(text):
    global manual_scan
    parts = text.split()
    name = parts[0].split("@")[0]
    if name == "/liquidity" and len(parts) > 1 and parts[1].lower() in ("on", "off"):
        if not liquidity_service:
            raise ValueError("Liquidity service unavailable")
        return await liquidity_service.set_enabled(parts[1].lower() == "on")
    if name == "/liquidity" or (name == "/report" and len(parts) > 1 and parts[1] == "liquidity"):
        selector = (
            parts[2]
            if name == "/report" and len(parts) > 2
            else parts[1]
            if name == "/liquidity" and len(parts) > 1
            else None
        )
        if selector:
            selector = symbol_name(selector)
        context = (
            liquidity_service.snapshot()
            if liquidity_service
            else {"mode": "unavailable", "markets": {}}
        )
        correlations = (store.get("latest_cycle") or {}).get("wallet_liquidity")
        return {"report": liquidity_digest(context, correlations, selector)}
    if name == "/marketlink":
        if len(parts) != 3 or scan_lock.locked():
            raise ValueError(
                "/marketlink CHAIN:ASSET_OR_perp:VENUE:SYMBOL EXCHANGE_BASE_SYMBOL; retry after running scan"
            )
        asset_id, symbol = parts[1], symbol_name(parts[2])
        from .screening import asset_identity

        known = {
            asset_identity(r, store.get("screen_asset_links")) for r in store.get("wallets", [])
        }
        if asset_id not in known:
            raise ValueError(
                "Use an exact asset ID from the imported watchlist or screening report"
            )
        links = store.get(
            "market_asset_links", json.loads(os.getenv("WHALE_MARKET_ASSET_LINKS_JSON", "{}"))
        )
        if links.get(asset_id) == symbol:
            return {"linked": asset_id, "market_symbol": symbol, "changed": False}
        links[asset_id] = symbol
        store.put("market_asset_links", links)
        store.put("watch_revision", store.get("watch_revision", 0) + 1)
        return {
            "linked": asset_id,
            "market_symbol": symbol,
            "new_window": "Collect six fresh scans after mapping change",
        }
    if name == "/market" or (name == "/report" and len(parts) > 1 and parts[1] == "market"):
        selector = (
            parts[2]
            if name == "/report" and len(parts) > 2
            else parts[1]
            if name == "/market" and len(parts) > 1
            else None
        )
        snapshots = (
            [await public_markets.snapshot(symbol_name(selector))]
            if selector
            else (store.get("latest_cycle") or {}).get("public_markets", {}).get("snapshots", [])
        )
        return {
            "report": market_digest(snapshots)
            if snapshots
            else "No public market snapshot yet. Use /market ETH or run a scan."
        }
    if name == "/screenlink":
        if len(parts) != 3 or scan_lock.locked():
            raise ValueError(
                "/screenlink VENUE:SYMBOL CHAIN:CONTRACT_OR_native; retry after any running scan"
            )
        from .birdeye import Birdeye

        if ":" not in parts[1] or ":" not in parts[2]:
            raise ValueError("Use explicit venue:symbol and chain:asset IDs")
        chain, asset = parts[2].split(":", 1)
        venue, symbol = parts[1].split(":", 1)
        if venue.lower() not in ("hyperliquid", "binance", "bybit", "okx") or not symbol:
            raise ValueError("Use a supported venue and exact perpetual symbol")
        future_key = venue.lower() + ":" + symbol
        Birdeye.validate(chain, None if asset == "native" else asset)
        target = chain + ":" + (asset if chain == "solana" or asset == "native" else asset.lower())
        links = store.get(
            "screen_asset_links", json.loads(os.getenv("WHALE_SCREEN_ASSET_LINKS_JSON", "{}"))
        )
        if links.get(future_key) == target:
            return {"linked": future_key, "to": target, "changed": False}
        links[future_key] = target
        store.put("screen_asset_links", links)
        store.put("watch_revision", store.get("watch_revision", 0) + 1)
        return {
            "linked": future_key,
            "to": target,
            "new_window": "Collect six fresh scans after mapping change",
        }
    if name in ("/screen", "/screen1h", "/screen4h") or (
        name == "/report" and (len(parts) < 2 or parts[1] != "orders")
    ):
        if name == "/report":
            if len(parts) < 2 or parts[1] not in ("screen", "screen1h", "screen4h"):
                raise ValueError(
                    "/report screen [COIN_OR_ASSET_ID] or /report screen4h [COIN_OR_ASSET_ID]"
                )
            report_name = parts[1]
            selector = parts[2] if len(parts) > 2 else None
        else:
            report_name = name[1:]
            selector = parts[1] if len(parts) > 1 else None
        report = await screening.requested("4h" if report_name == "screen4h" else "1h")
        return {"report": screen_digest(report, selector)}
    if name in ("/pause", "/resume"):
        store.put("paused", name == "/pause")
        if name == "/resume":
            store.put("next_scan_at", time.time())
        wake.set()
        return {"paused": name == "/pause"}
    if name == "/scan":
        if scan_lock.locked():
            return {"scan": "already running"}
        manual_scan = True
        wake.set()
        return {"scan": "queued"}
    if name == "/interval" and len(parts) == 2:
        seconds = int(parts[1])
        if not 60 <= seconds <= 86400:
            raise ValueError("Interval: 60..86400 seconds")
        store.put("interval", seconds)
        store.put("next_scan_at", time.time() + seconds)
        wake.set()
        return {"interval_seconds": seconds}
    if name == "/orders" or (name == "/report" and len(parts) > 1 and parts[1] == "orders"):
        latest = store.get("latest_cycle")
        offset = 2 if name == "/report" else 1
        return (
            {"report": orders_digest(latest, parts[offset] if len(parts) > offset else None)}
            if latest
            else {"status": "Awaiting first scan"}
        )
    if name == "/reports":
        latest = store.get("latest_cycle")
        return (
            {
                "cycle": latest["completed_at"],
                "reports": [
                    {k: report[k] for k in ("title", "entries", "counts")}
                    for report in latest["reports"].values()
                ],
            }
            if latest
            else {"status": "No completed reporting cycle yet"}
        )
    if name == "/wallets":
        return [
            {k: r[k] for k in ("label", "chain", "coin", "group")}
            for r in store.get("wallets", [])[:15]
        ]
    if name == "/risk":
        latest = store.get("latest_cycle")
        return (
            {"report": risk_digest(latest)}
            if latest
            else {"status": "No completed reporting cycle yet"}
        )
    if name == "/status":
        return {
            "paused": store.get("paused", True),
            "entries": len(store.get("wallets", [])),
            "last_scan": store.get("last_scan"),
        }
    if name == "/sources":
        from .sources import source_activation

        return [
            {
                "name": source["name"],
                "state": source["state"],
                "last_success": source.get("last_success"),
            }
            for source in source_activation(
                recovery.health if recovery else store.get("source_health", {})
            )
        ]
    if name == "/source" and len(parts) >= 3:
        adapter = OfficialSources(client, store, recovery)
        if parts[1] == "birdeye":
            if len(parts) < 4:
                raise ValueError("/source birdeye KIND CHAIN [TOKEN_OR_WALLET] [COUNT]")
            result = await birdeye_query(
                parts[2],
                parts[3],
                parts[4] if len(parts) > 4 else None,
                int(parts[5]) if len(parts) > 5 else 100,
            )
        elif parts[1] == "dune":
            result = await adapter.dune(parts[2])
            store.put("source:dune:" + str(int(parts[2])), result)
        elif parts[1] == "nansen":
            row = next((r for r in store.get("wallets", []) if r["id"] == parts[2]), None)
            if row is None:
                raise ValueError("Use watch ID from dashboard/API status")
            kind = parts[3] if len(parts) > 3 else "balances"
            if kind not in ("balances", "labels"):
                raise ValueError("Nansen kind: balances or labels")
            result = await adapter.nansen(row, kind)
            store.put("source:nansen:" + row["id"] + ":" + kind, result)
        else:
            raise ValueError("Available on-demand adapters: birdeye, nansen, dune")
        return result
    return {
        "commands": "/orders [COIN]; /report orders [COIN]; /liquidity [BASE_SYMBOL|on|off]; /report liquidity [BASE_SYMBOL]; /market [BASE_SYMBOL]; /report market [BASE_SYMBOL]; /marketlink ASSET_ID BASE_SYMBOL; /screen [COIN_OR_ASSET_ID]; /screen4h [COIN_OR_ASSET_ID]; /report screen|screen4h; /screenlink VENUE:SYMBOL CHAIN:ASSET; /status /wallets /scan /pause /resume /interval SECONDS /reports /risk /sources; /source birdeye KIND CHAIN [ADDRESS] [COUNT]; /source nansen WATCH_ID [balances|labels]; /source dune QUERY_ID; send XLSX to replace watchlist",
        "automatic_reports": "scan summaries, public exchange market context, six-scan hourly and four-hourly screening when enabled",
        "dashboard": os.getenv("WHALE_PUBLIC_URL", ""),
    }


@app.get("/api/screens", dependencies=[Depends(auth)])
def screen_history_api():
    revision = store.get("watch_revision", 0)
    return {
        "hourly": store.screen_history("1h", revision, 4),
        "four_hourly": store.screen_history("4h", revision, 4),
    }


@app.get("/api/markets", dependencies=[Depends(auth)])
def public_market_history_api():
    return (store.get("latest_cycle") or {}).get(
        "public_markets", {"snapshots": [], "status": "awaiting_scan"}
    )


@app.post("/api/markets/{symbol}", dependencies=[Depends(auth)])
async def public_market_api(symbol: str):
    try:
        return await public_markets.snapshot(symbol_name(symbol))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@app.post("/api/screens/{kind}", dependencies=[Depends(auth)])
async def screen_api(kind: str, selector: str | None = None):
    if kind not in ("1h", "4h"):
        raise HTTPException(400, "Screen kind: 1h or 4h")
    report = await screening.requested(kind)
    if selector and report.get("ready"):
        report = {
            **report,
            "assets": [
                r
                for r in report["assets"]
                if r["asset_id"].lower() == selector.lower()
                or selector.upper() in [s.upper() for s in r["symbols"]]
            ],
        }
    return report


async def birdeye_query(kind, chain, address=None, count=100):
    from .birdeye import Birdeye

    adapter = getattr(provider, "birdeye", None) or Birdeye(client, store, recovery)
    if kind == "price":
        return await adapter.price(chain, address or "")
    return await adapter.research(kind, chain, address, count)


@app.post("/api/birdeye/{kind}", dependencies=[Depends(auth)])
async def birdeye_api(
    kind: str, chain: str = "solana", address: str | None = None, count: int = 100
):
    try:
        return await birdeye_query(kind, chain, address, count)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@app.get("/api/sources", dependencies=[Depends(auth)])
def sources(include_evidence: bool = False):
    from .sources import source_activation

    return {
        "platforms": source_activation(
            recovery.health if recovery else store.get("source_health", {})
        ),
        "health": recovery.health if recovery else store.get("source_health", {}),
        "evidence": store.evidence() if include_evidence else [],
    }


@app.get("/api/reports", dependencies=[Depends(auth)])
def report_history():
    return {"cycles": store.report_history()}


@app.get("/api/reports/latest", dependencies=[Depends(auth)])
def latest_report():
    return store.get("latest_cycle", {"status": "No completed reporting cycle yet"})


@app.post("/helius/webhook")
async def helius(request: Request):
    secret = os.getenv("WHALE_HELIUS_WEBHOOK_SECRET", "")
    if not secret or not hmac.compare_digest(
        request.headers.get("authorization", "").encode("utf-8"), secret.encode("utf-8")
    ):
        raise HTTPException(403)
    import json

    events = json.loads(await limited_body(request, 5_000_000))
    if not isinstance(events, list) or len(events) > 1000:
        raise HTTPException(400, "Expected up to 1,000 enhanced transaction events")
    addresses = {r["address"] for r in store.get("wallets", []) if r["chain"] == "solana"}
    accepted = 0
    for event in events:
        if not isinstance(event, dict):
            raise HTTPException(400, "Invalid event")
        signature = event.get("signature")
        if not isinstance(signature, str) or not 32 <= len(signature) <= 128:
            raise HTTPException(400, "Missing or invalid transaction signature")
        touched = {a.get("account") for a in event.get("accountData", []) if isinstance(a, dict)}
        for transfer in event.get("tokenTransfers", []) + event.get("nativeTransfers", []):
            if isinstance(transfer, dict):
                touched.update((transfer.get("fromUserAccount"), transfer.get("toUserAccount")))
        matched = sorted(addresses.intersection(touched))
        if matched:
            accepted += int(
                store.ingest(
                    "helius:" + signature,
                    {
                        "source": "helius",
                        "received_at": now(),
                        "matched_addresses": matched,
                        "provider_type": event.get("type"),
                        "classification": "provider evidence; trade attribution pending parser validation",
                        "raw": event,
                    },
                )
            )
    return {"accepted": accepted}


@app.post("/telegram/webhook")
async def telegram(request: Request):
    secret = os.getenv("WHALE_WEBHOOK_SECRET", "")
    if not secret or not hmac.compare_digest(
        request.headers.get("x-telegram-bot-api-secret-token", "").encode("utf-8"),
        secret.encode("utf-8"),
    ):
        raise HTTPException(403)
    update = __import__("json").loads(await limited_body(request, 100_000))
    message = update.get("message", {})
    owner = os.getenv("WHALE_TELEGRAM_OWNER_ID", "")
    if (
        not owner
        or str(message.get("from", {}).get("id")) != owner
        or message.get("chat", {}).get("type") != "private"
    ):
        return {"ok": True}
    token = os.getenv("WHALE_TELEGRAM_TOKEN", "")
    if not token:
        raise HTTPException(503, "Telegram token missing")
    async with tg_lock:
        update_id = int(update["update_id"])
        recent = store.get("telegram_updates", [])
        if update_id in recent:
            return {"ok": True}
        try:
            doc = message.get("document")
            if doc:
                if (
                    not doc.get("file_name", "").lower().endswith(".xlsx")
                    or doc.get("file_size", 0) > 5_000_000
                ):
                    raise ValueError("Send XLSX up to 5 MB")
                response = await client.get(
                    f"https://api.telegram.org/bot{token}/getFile",
                    params={"file_id": doc["file_id"]},
                )
                response.raise_for_status()
                info = response.json()
                if not info.get("ok"):
                    raise ValueError("Telegram could not retrieve file")
                async with client.stream(
                    "GET", f"https://api.telegram.org/file/bot{token}/{info['result']['file_path']}"
                ) as download:
                    download.raise_for_status()
                    data = bytearray()
                    async for chunk in download.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > 5_000_000:
                            raise ValueError("Excel exceeds 5 MB")
                result = await import_data(bytes(data))
            else:
                result = await command(message.get("text") or "/help")
            text = (
                result["report"]
                if isinstance(result, dict)
                and set(result) == {"report"}
                and isinstance(result["report"], str)
                else json.dumps(result, ensure_ascii=False, indent=2)
            )
        except Exception as exc:
            text = (
                str(exc)
                if isinstance(exc, ValueError)
                else "Request failed; check workbook/provider configuration"
            )
        try:
            reply = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": message["chat"]["id"], "text": text[:3900]},
            )
            reply.raise_for_status()
            if not reply.json().get("ok"):
                raise ValueError("Telegram rejected reply")
        except (httpx.HTTPError, ValueError, KeyError):
            # HTTPStatusError/transport exceptions can contain the secret bot URL.
            raise HTTPException(502, "Telegram reply unavailable") from None
        store.put("telegram_updates", (recent + [update_id])[-1000:])
    return {"ok": True}
