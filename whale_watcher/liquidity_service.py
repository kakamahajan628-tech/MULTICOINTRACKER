"""One continuous collector per venue/coin, shared by all wallet cohorts."""

import asyncio
from collections import deque
import logging
import os
import sqlite3
from pathlib import Path
import time
import tomllib
import aiohttp
from liquidity.book import ResyncRequired
from liquidity.demo import run_demo
from liquidity.engine import Engine
from liquidity.feeds import LiveFeed
from liquidity.storage import Journal
from liquidity.signals import DetectorConfig
from .public_markets import market_targets

LOG = logging.getLogger(__name__)


class LiquidityService:
    def __init__(self, store, *, feed_factory=LiveFeed, clock=None):
        self.store, self.feed_factory = store, feed_factory
        self.clock = clock or (lambda: time.time_ns() // 1_000_000)
        config = DetectorConfig()
        if os.getenv("WHALE_LIQUIDITY_CONFIG"):
            with open(os.environ["WHALE_LIQUIDITY_CONFIG"], "rb") as source:
                config = DetectorConfig(**tomllib.load(source).get("signals", {}))
        self.engine = Engine(config=config)
        self.tasks = {}
        self.session = None
        self.journal = None
        self.fatal_error = ""
        self.pending_alerts = deque(maxlen=100)
        self.cooldowns = {}
        self.alerts_dropped = 0
        self.targets, self.deferred, self.unmapped = [], [], []
        self.enabled = store.get(
            "liquidity_enabled", os.getenv("WHALE_LIQUIDITY_ENABLED", "true").lower() == "true"
        )
        self.mode = os.getenv("WHALE_LIQUIDITY_MODE", "live")
        if self.mode not in ("live", "demo"):
            raise ValueError("WHALE_LIQUIDITY_MODE must be live or demo")
        self.max_markets = min(20, max(1, int(os.getenv("WHALE_LIQUIDITY_MAX_MARKETS", "4"))))

    def emit(self, event):
        if self.fatal_error:
            raise RuntimeError(self.fatal_error)
        try:
            signals = self.engine.handle(event)
        except ResyncRequired:
            raise
        except (OSError, sqlite3.DatabaseError) as exc:
            self.fatal_error = f"Recording unavailable ({type(exc).__name__}); collector stopped"
            raise
        if (
            self.mode != "demo"
            and os.getenv("WHALE_LIQUIDITY_ALERTS", "false").lower() == "true"
            and os.getenv("WHALE_TELEGRAM_OWNER_ID")
        ):
            for signal in signals:
                if signal.score < 0.6:
                    continue
                key = (signal.venue, signal.symbol, signal.kind, signal.evidence.get("side"))
                if event.received_ms - self.cooldowns.get(key, -(10**18)) < 60000:
                    continue
                self.cooldowns[key] = event.received_ms
                if len(self.pending_alerts) == self.pending_alerts.maxlen:
                    self.alerts_dropped += 1
                self.pending_alerts.append(signal)

    async def start(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        self.open_journal()
        await self.reconcile()

    def open_journal(self):
        configured = os.getenv("WHALE_LIQUIDITY_JOURNAL")
        path = (
            configured
            if configured is not None
            else ("" if os.getenv("RENDER") else "runtime/liquidity-events.sqlite")
        )
        if self.enabled and path and self.journal is None:
            self.journal = Journal(path)
            self.engine.journal = self.journal

    async def set_enabled(self, enabled):
        if self.session is None:
            raise ValueError("Liquidity collector is not initialized")
        self.enabled = bool(enabled)
        self.store.put("liquidity_enabled", self.enabled)
        self.open_journal()
        self.pending_alerts.clear()
        await self.reconcile()
        return {"liquidity_enabled": self.enabled, "requested_markets": list(self.targets)}

    async def reconcile(self):
        if self.fatal_error or not self.enabled:
            wanted = []
            self.targets, self.deferred, self.unmapped = [], [], []
        elif self.mode == "demo":
            wanted = ["demo:BTCUSDT"]
            self.targets, self.deferred, self.unmapped = (
                ["binance:BTCUSDT", "bybit:BTCUSDT"],
                [],
                [],
            )
        else:
            targets, self.unmapped = market_targets(
                self.store.get("wallets", []),
                self.store.get("screen_asset_links"),
                self.store.get("market_asset_links"),
            )
            pairs = [
                f"{venue}:{symbol}USDT"
                for symbol in sorted(targets)
                for venue in ("binance", "bybit")
            ]
            wanted, self.deferred = pairs[: self.max_markets], pairs[self.max_markets :]
            self.targets = wanted
        removed = [k for k in self.tasks if k not in wanted]
        for key in removed:
            self.tasks[key].cancel()
        if removed:
            await asyncio.gather(*(self.tasks[k] for k in removed), return_exceptions=True)
        for key in removed:
            if key == "demo:BTCUSDT":
                self.engine.states.clear()
            self.tasks.pop(key)
            # Never compare removed or remapped coins as if still actively subscribed.
            self.engine.states.pop(key, None)
        for key in wanted:
            task = self.tasks.get(key)
            if task and not task.done():
                continue
            if task and task.done():
                try:
                    task.result()
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    LOG.warning("Liquidity collector restart %s (%s)", key, type(exc).__name__)
            if key == "demo:BTCUSDT":
                coroutine = run_demo(self.emit)
            else:
                venue, symbol = key.split(":")
                coroutine = self.feed_factory(self.session, venue, symbol, self.emit).run()
            self.tasks[key] = asyncio.create_task(coroutine, name="liquidity-" + key)

    def snapshot(self, *, history_market=None, since_ms=0):
        now = self.clock()
        result = self.engine.view(now, include_history=False, depth=20)
        if self.fatal_error:
            for value in result["markets"].values():
                value["quality"]["fresh"] = False
                value["derived"] = None
                value["observed"]["book"] = None
        result["inferred_signals"] = [
            s for s in result["inferred_signals"] if now - 120000 <= s["received_ms"] <= now
        ][-48:]
        if history_market is not None:
            selected = history_market or next(iter(self.engine.states), "")
            if selected in result["markets"]:
                result["markets"][selected]["history"] = [
                    h for h in self.engine.states[selected].history if h["t"] > since_ms
                ]
        result.update(
            {
                "mode": self.mode,
                "enabled": self.enabled,
                "requested_markets": list(self.targets),
                "deferred_markets": list(self.deferred),
                "unmapped_assets": list(self.unmapped),
                "fatal_error": self.fatal_error,
                "stale_ms": self.engine.config.stale_ms,
                "alerts_dropped": self.alerts_dropped,
                "journal": str(Path(self.journal_path).name) if self.journal else "disabled",
                "replay_finished": False,
            }
        )
        return result

    @property
    def journal_path(self):
        return os.getenv("WHALE_LIQUIDITY_JOURNAL", "runtime/liquidity-events.sqlite")

    def compact_snapshot(self):
        result = self.snapshot()
        result.pop("cross_venue", None)
        for value in result["markets"].values():
            value.pop("history", None)
            value["observed"].pop("book", None)
        # Raw observations remain in the optional tick journal. Durable scan records
        # contain bounded evidence, metrics, quality and scenario assumptions.
        result["inferred_signals"] = [
            s
            for s in result["inferred_signals"]
            if s["received_ms"] >= result["received_ms"] - 120000
        ][-100:]
        return result

    async def run(self):
        try:
            await self.start()
            while True:
                await asyncio.sleep(1)
                if self.journal:
                    try:
                        self.journal.flush()
                    except Exception as exc:
                        self.fatal_error = f"Recording flush failed ({type(exc).__name__})"
                # A database query per 15 seconds, not per depth/trade event.
                if self.clock() // 15000 != getattr(self, "_refresh_bucket", None):
                    self._refresh_bucket = self.clock() // 15000
                    await self.reconcile()
                self.flush_alerts()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.fatal_error = f"Liquidity unavailable ({type(exc).__name__})"
            LOG.exception("Liquidity service stopped")
        finally:
            await self.close()

    def flush_alerts(self):
        allowed = (
            self.mode != "demo"
            and os.getenv("WHALE_LIQUIDITY_ALERTS", "false").lower() == "true"
            and os.getenv("WHALE_TELEGRAM_OWNER_ID")
        )
        for _ in range(min(3, len(self.pending_alerts))):
            signal = self.pending_alerts.popleft()
            if not allowed:
                continue
            message = (
                f"LIQUIDITY CANDIDATE | {signal.venue} {signal.symbol}\n"
                f"{signal.kind} | heuristic score {signal.score} (not probability)\n"
                f"Evidence: {signal.evidence}\n{signal.caveat}"
            )
            self.store.enqueue_report("liquidity", message[:3900])

    async def close(self):
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        self.tasks.clear()
        if self.session:
            await self.session.close()
            await asyncio.sleep(0.25)
            self.session = None
        if self.journal:
            try:
                self.journal.close()
            finally:
                self.journal = None
                self.engine.journal = None
