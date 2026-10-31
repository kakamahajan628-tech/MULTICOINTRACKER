"""Read-only Solana inventory. Token balances never establish value or trading intent."""

import hashlib
from decimal import Decimal, localcontext

from .core import SOL, now

TOKEN_PROGRAMS = {
    "spl_token": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "token_2022": "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
}
NEUTRAL_GROUP = "Spot Unclassified"


def neutral_solana_watch(address, label=""):
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    if not isinstance(address, str) or not SOL.fullmatch(address):
        raise ValueError("Invalid Solana public address")
    number = 0
    for char in address:
        number = number * 58 + alphabet.index(char)
    decoded_size = (number.bit_length() + 7) // 8 + len(address) - len(address.lstrip("1"))
    if decoded_size != 32:
        raise ValueError("Solana public key must decode to 32 bytes")
    identity = "|".join((NEUTRAL_GROUP, "solana", address, "native", ""))
    return {
        "id": hashlib.sha256(identity.encode()).hexdigest()[:24],
        "address": address,
        "chain": "solana",
        "coin": "SOL",
        "asset": "native",
        "decimals": 9,
        "platform": "",
        "rank": 1,
        "label": label or "Solana " + address[:6] + "…" + address[-4:],
        "group": NEUTRAL_GROUP,
        "classification": "pnl_unverified",
        "discover_tokens": True,
    }


async def read_inventory(rpc, address):
    holdings, slots = [], {}
    for name, program in TOKEN_PROGRAMS.items():
        result = await rpc(
            "solana",
            "getTokenAccountsByOwner",
            [
                address,
                {"programId": program},
                {"encoding": "jsonParsed", "commitment": "finalized"},
            ],
        )
        slots[name] = result["context"]["slot"]
        amounts, seen = {}, set()
        for item in result["value"]:
            if item["pubkey"] in seen:
                raise ValueError("Duplicate token account in RPC response")
            seen.add(item["pubkey"])
            account = item["account"]
            parsed = account["data"]["parsed"]
            info = parsed["info"]
            if (
                account["owner"] != program
                or parsed["type"] != "account"
                or info["owner"] != address
            ):
                raise ValueError("RPC token account owner/program mismatch")
            token = info["tokenAmount"]
            decimals, raw = int(token["decimals"]), int(token["amount"])
            if not 0 <= decimals <= 255 or not 0 <= raw < 2**64:
                raise ValueError("Invalid token quantity metadata")
            mint = info["mint"]
            row = amounts.setdefault(
                mint,
                {
                    "mint": mint,
                    "program": name,
                    "decimals": decimals,
                    "raw_amount": 0,
                    "accounts": 0,
                    "states": {},
                },
            )
            if row["decimals"] != decimals:
                raise ValueError("Conflicting decimals for one mint")
            row["raw_amount"] += raw
            row["accounts"] += 1
            state = info["state"]
            row["states"][state] = row["states"].get(state, 0) + 1
        with localcontext() as context:
            context.prec = 100
            for row in amounts.values():
                row["amount"] = str(Decimal(row["raw_amount"]).scaleb(-row["decimals"]))
                row["raw_amount"] = str(row["raw_amount"])
        holdings.extend(amounts.values())
    holdings.sort(key=lambda row: (row["mint"], row["program"]))
    return {
        "address": address,
        "chain": "solana",
        "network": "mainnet-beta",
        "observed_at": now(),
        "commitment": "finalized",
        "context_slots": slots,
        "complete": True,
        "token_accounts": sum(r["accounts"] for r in holdings),
        "positive_mints": sum(int(r["raw_amount"]) > 0 for r in holdings),
        "mint_count": len(holdings),
        "holdings": holdings,
        "total_value_usd": None,
        "valuation_status": "not_valued",
        "scope": "SPL Token and Token-2022 accounts. Excludes DeFi/staked/compressed assets and transaction history. "
        "Raw token quantities use mint decimals; extension-scaled UI amounts may differ. "
        "Balances may include unwanted airdrops, frozen assets or NFTs. No token quality, price or P&L is inferred.",
    }


def inventory_summary(current, previous=None):
    summary = {k: v for k, v in current.items() if k != "holdings"}
    summary["baseline"] = previous is None
    changes = []
    if previous is not None:
        before = {(r["program"], r["mint"]): r for r in previous["holdings"]}
        after = {(r["program"], r["mint"]): r for r in current["holdings"]}
        for key in sorted(before.keys() | after.keys()):
            old, new = before.get(key), after.get(key)
            old_raw, new_raw = (
                (old or {}).get("raw_amount", "0"),
                (new or {}).get("raw_amount", "0"),
            )
            if old_raw != new_raw or (old or {}).get("states", {}) != (new or {}).get("states", {}):
                changes.append(
                    {
                        "program": key[0],
                        "mint": key[1],
                        "before": (old or {}).get("amount", "0"),
                        "after": (new or {}).get("amount", "0"),
                        "classification": "balance_or_account_state_change",
                    }
                )
    summary.update(
        changed_mints=len(changes),
        changes=changes[:100],
        changes_truncated=len(changes) > 100,
        comparison_note="Balance changes do not prove swaps, profit, sales or liquidation.",
    )
    return summary
