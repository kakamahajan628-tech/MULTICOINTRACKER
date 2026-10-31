"""Capture real feed reconstruction and compare it with journal replay."""

import asyncio
import json
from pathlib import Path
import sys
import time
import aiohttp
from liquidity.book import ResyncRequired
from liquidity.engine import Engine
from liquidity.feeds import LiveFeed
from liquidity.storage import Journal, read_events


async def smoke(seconds=45):
    directory = Path("reports")
    directory.mkdir(exist_ok=True)
    path = directory / "live-smoke.sqlite"
    if path.exists():
        raise ValueError("use a new reports/live-smoke.sqlite path for each smoke run")
    journal, gaps = Journal(path), []
    engine = Engine(journal)

    def emit(event):
        try:
            engine.handle(event)
        except ResyncRequired as exc:
            gaps.append(str(exc))
            raise

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
        tasks = [
            asyncio.create_task(LiveFeed(session, v, "BTCUSDT", emit).run())
            for v in ("binance", "bybit")
        ]
        await asyncio.sleep(seconds)
        state = engine.view(time.time_ns() // 1_000_000)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    journal.close()
    replay = Engine()
    replay_gaps = []
    for event in read_events(path):
        try:
            replay.handle(event)
        except ResyncRequired as exc:
            replay_gaps.append(str(exc))

    def comparable(e):
        return {
            k: {
                "book": v.book.view(1000),
                "ready": v.book.ready,
                "last_id": v.book.last_id,
                "cvd": str(v.metrics.cvd_base),
            }
            for k, v in e.states.items()
        }

    equal = comparable(engine) == comparable(replay)
    report = {
        "duration_seconds": seconds,
        "events": engine.event_count,
        "markets": {k: v["quality"] for k, v in state["markets"].items()},
        "reconstruction_gaps": gaps,
        "replay_gaps": replay_gaps,
        "replay_matches_live_state": equal,
        "passed": len(state["markets"]) == 2
        and all(v["quality"]["fresh"] for v in state["markets"].values())
        and equal,
    }
    (directory / "live-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    if not report["passed"]:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(smoke())
