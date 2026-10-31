"""Bounded real collector test; only synthetic wallet fixtures, no Telegram or account keys."""

import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

from liquidity.book import ResyncRequired
from liquidity.engine import Engine
from liquidity.storage import read_events
from whale_watcher.liquidity_service import LiquidityService


class FixtureStore:
    def get(self, key, default=None):
        if key == "wallets":
            # 1,000 synthetic entries sharing one native ETH asset.
            return [
                {
                    "id": str(i),
                    "group": "Spot Profit",
                    "chain": "ethereum",
                    "asset": "native",
                    "coin": "ETH",
                    "address": "0x" + f"{i + 1:040x}",
                    "platform": "",
                    "label": f"Fixture {i}",
                    "rank": i + 1,
                }
                for i in range(1000)
            ]
        return default


async def main(seconds=45):
    path = Path("reports/integrated-live.sqlite")
    if path.exists():
        raise RuntimeError("Archive reports/integrated-live.sqlite before a new run")
    os.environ.update(
        WHALE_LIQUIDITY_ENABLED="true",
        WHALE_LIQUIDITY_MODE="live",
        WHALE_LIQUIDITY_ALERTS="false",
        WHALE_LIQUIDITY_JOURNAL=str(path),
        WHALE_LIQUIDITY_MAX_MARKETS="2",
    )
    service = LiquidityService(FixtureStore())
    await service.start()
    await asyncio.sleep(seconds)
    snapshot = service.snapshot()
    active = list(service.tasks)
    await service.close()
    replay = Engine()
    gaps = []
    for event in read_events(path):
        try:
            replay.handle(event)
        except ResyncRequired as exc:
            gaps.append(str(exc))

    def compare(engine):
        return {
            k: {
                "book": v.book.view(100000),
                "id": v.book.last_id,
                "cvd": str(v.metrics.cvd_base),
                "quote_cvd": str(v.metrics.cvd_quote),
            }
            for k, v in engine.states.items()
        }

    equal = compare(service.engine) == compare(replay)
    report = {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "fixture_wallet_entries": 1000,
        "active_shared_feeds": active,
        "seconds": seconds,
        "events": service.engine.event_count,
        "quality": {k: v["quality"] for k, v in snapshot["markets"].items()},
        "replay_gaps": gaps,
        "replay_matches_collector": equal,
        "external_messages_sent": 0,
        "passed": equal
        and len(snapshot["markets"]) == 2
        and not gaps
        and all(v["quality"]["fresh"] for v in snapshot["markets"].values()),
    }
    Path("reports/integrated-live.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
