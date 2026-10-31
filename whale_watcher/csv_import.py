"""Import public Hyperliquid watch candidates; CSV figures remain historical claims."""

import argparse
import csv
import hashlib
import io
import json
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path

from dotenv import load_dotenv

from .core import EVM, Store, now

HEADERS = (
    "address",
    "label",
    "coin",
    "direction",
    "position_value_usd",
    "unrealized_pnl_usd",
    "leverage",
)
GROUP = "Futures Unclassified"


def parse_candidates(data, source_name="uploaded.csv"):
    try:
        return _parse_candidates(data, source_name)
    except (UnicodeError, csv.Error):
        raise ValueError("Invalid UTF-8 or malformed CSV") from None


def _parse_candidates(data, source_name):
    if len(data) > 5_000_000:
        raise ValueError("CSV exceeds 5 MB")
    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig"), newline=""), strict=True)
    if tuple(reader.fieldnames or ()) != HEADERS:
        raise ValueError("Unexpected CSV columns")
    rows, seen = [], set()
    digest = hashlib.sha256(data).hexdigest()
    for line, raw in enumerate(reader, 2):
        if None in raw or any(value is None for value in raw.values()):
            raise ValueError(f"Line {line}: missing or extra CSV fields")
        item = {key: value.strip() for key, value in raw.items()}
        if not any(item.values()):
            continue
        address = item["address"].lower()
        if not EVM.fullmatch(address):
            raise ValueError(f"Line {line}: expected a full 20-byte EVM account address")
        coin = item["coin"].upper()
        from .public_markets import symbol_name

        symbol_name(coin)
        if item["direction"].lower() not in ("long", "short"):
            raise ValueError(f"Line {line}: direction must be long or short")
        if item["label"].startswith(("=", "+", "-", "@")) or len(item["label"]) > 160:
            raise ValueError(f"Line {line}: invalid label")
        for key in ("position_value_usd", "unrealized_pnl_usd", "leverage"):
            try:
                value = Decimal(item[key])
            except InvalidOperation:
                raise ValueError(f"Line {line}: invalid {key}") from None
            if not value.is_finite() or value.copy_abs() > Decimal("1e30"):
                raise ValueError(f"Line {line}: non-finite or excessive {key}")
            if key != "unrealized_pnl_usd" and value < 0 or key == "leverage" and value == 0:
                raise ValueError(f"Line {line}: invalid {key}")
        key = (address, coin)
        if key in seen:
            raise ValueError(f"Line {line}: duplicate address/coin candidate")
        seen.add(key)
        if len(rows) >= 1000:
            raise ValueError("Maximum 1,000 watch candidates")
        identity = "|".join((GROUP, "hyperliquid", address, "perpetual:" + coin, "hyperliquid"))
        rows.append(
            {
                "id": hashlib.sha256(identity.encode()).hexdigest()[:24],
                "address": address,
                "chain": "hyperliquid",
                "platform": "hyperliquid",
                "coin": coin,
                "asset": "perpetual:" + coin,
                "decimals": 0,
                "rank": len(rows) + 1,
                "label": item["label"] or f"{coin} candidate #{len(rows) + 1}",
                "group": GROUP,
                "classification": "historical_candidate; profitability_unverified",
                "import_snapshot": {
                    **item,
                    "source_file": Path(source_name).name,
                    "source_sha256": digest,
                    "source_row": line,
                    "observed_at": None,
                    "status": "user_supplied_historical_claim; timestamp_unknown",
                },
            }
        )
    return rows


def append_candidates(store, rows):
    result = store.append_wallets(
        rows,
        state_updates={
            "next_scan_at": 0,
            "paused": False,
            "last_csv_import": {
                "at": now(),
                "candidates": len(rows),
                "source_sha256": rows[0]["import_snapshot"]["source_sha256"] if rows else None,
            },
        },
    )
    return {
        **result,
        "group": GROUP,
        "note": "CSV values are user-supplied historical claims, never current position evidence.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--env-file", default=".env.whale")
    parser.add_argument(
        "--apply", action="store_true", help="Append candidates to the configured database"
    )
    args = parser.parse_args()
    rows = parse_candidates(args.path.read_bytes(), args.path.name)
    result = {
        "validated": len(rows),
        "coins": sorted({r["coin"] for r in rows}),
        "mode": "validation_only",
    }
    if args.apply:
        load_dotenv(args.env_file)
        Path("runtime").mkdir(exist_ok=True)
        store = Store(os.getenv("WHALE_DATABASE_URL", "sqlite:///runtime/whale.sqlite3"))
        try:
            result.update(append_candidates(store, rows), mode="append")
        finally:
            store.engine.dispose()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
