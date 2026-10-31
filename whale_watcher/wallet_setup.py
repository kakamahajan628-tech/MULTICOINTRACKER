"""Configure one neutral public Solana wallet without modifying classified cohorts."""

import argparse
import asyncio
import json
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv

from .core import Store, now
from .portfolio import neutral_solana_watch
from .providers import RPCProvider


async def configure(address, label=""):
    row = neutral_solana_watch(address, label)
    endpoint = os.getenv("WHALE_RPC_SOLANA", "https://api.mainnet.solana.com")
    async with httpx.AsyncClient(timeout=30) as client:
        rpc = RPCProvider(client, {"solana": endpoint}, rps=1)
        result = await rpc.rpc(
            "solana",
            "getAccountInfo",
            [address, {"encoding": "jsonParsed", "commitment": "finalized"}],
        )
    account = result.get("value")
    if (
        not account
        or account.get("owner") != "11111111111111111111111111111111"
        or account.get("executable")
        or account.get("space", 0) != 0
    ):
        raise ValueError(
            "Expected an existing normal System Program account; token/program accounts are not wallet watches"
        )
    Path("runtime").mkdir(exist_ok=True)
    store = Store(os.getenv("WHALE_DATABASE_URL", "sqlite:///runtime/whale.sqlite3"))
    try:
        result = store.append_wallets(
            [row],
            state_updates={
                "paused": False,
                "interval": 600,
                "reporting_version": 2,
                "next_scan_at": 0,
            },
        )
        if not result["added"]:
            return {"status": "already_watched", "address": address, "chain": "solana"}
        return {
            "status": "added",
            "address": address,
            "chain": "solana",
            "network": "mainnet-beta",
            "watch_id": row["id"],
            "group": row["group"],
            "token_discovery": True,
            "checked_at": now(),
            "finalized_slot": result["context"]["slot"],
            "note": "Restart a running app after this offline setup command. No ownership or profitability claim.",
        }
    finally:
        store.engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("address")
    parser.add_argument("--label", default="")
    parser.add_argument("--env-file", default=".env.whale")
    args = parser.parse_args()
    load_dotenv(args.env_file)
    print(json.dumps(asyncio.run(configure(args.address, args.label)), indent=2))


if __name__ == "__main__":
    main()
