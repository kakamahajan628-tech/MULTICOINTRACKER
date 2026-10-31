"""Loopback-only synthetic preview. Does not read .env files or contact wallet APIs."""

import asyncio
from contextlib import asynccontextmanager
from decimal import Decimal
import os
from pathlib import Path
import sys
import secrets

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
os.environ.update(
    WHALE_LIQUIDITY_ENABLED="true",
    WHALE_LIQUIDITY_MODE="demo",
    WHALE_LIQUIDITY_ALERTS="false",
    WHALE_LIQUIDITY_JOURNAL="",
    WHALE_AUTO_REPORTS="false",
    WHALE_MARKET_AUTO_REPORTS="false",
    WHALE_SCREEN_AUTO_REPORTS="false",
    WHALE_TELEGRAM_OWNER_ID="",
    WHALE_DATABASE_URL="sqlite:///runtime/synthetic-demo.sqlite3",
)
os.environ.pop("RENDER", None)
os.environ["WHALE_ADMIN_TOKEN"] = secrets.token_urlsafe(32)
import uvicorn  # noqa: E402 -- environment/root are initialized before the synthetic app
import whale_watcher.app as module  # noqa: E402 -- synthetic environment first

original = module.lifespan


class SyntheticWallets:
    attempts = []

    async def observe(self, row):
        return {
            "amount": str(Decimal("10") + Decimal(row["rank"])),
            "source": "synthetic_demo",
            "price_usd": "60000",
            "value_usd": "660000",
        }


@asynccontextmanager
async def demo_lifespan(app):
    async with original(app):
        module.provider = SyntheticWallets()
        module.public_markets = None
        module.store.put("paused", True)
        module.store.put(
            "wallets",
            [
                {
                    "id": f"demo-{i}",
                    "group": "Spot Profit" if i < 3 else "Spot Loss",
                    "chain": "ethereum",
                    "coin": "BTC",
                    "asset": "0x" + "b" * 40,
                    "decimals": 8,
                    "address": "0x" + f"{i + 1:040x}",
                    "label": f"SYNTHETIC Trader {i + 1}",
                    "rank": i + 1,
                    "platform": "",
                }
                for i in range(6)
            ],
        )
        module.store.put("market_asset_links", {"ethereum:0x" + "b" * 40: "BTC"})
        await asyncio.sleep(0.1)
        await module.scan()
        yield


module.app.router.lifespan_context = demo_lifespan
# There are only synthetic fixtures here. Production app routes retain Basic auth.
module.app.dependency_overrides[module.auth] = lambda: None
if __name__ == "__main__":
    print("SYNTHETIC preview: http://127.0.0.1:8341 (no production credentials)")
    uvicorn.run(module.app, host="127.0.0.1", port=8341, log_level="warning")
