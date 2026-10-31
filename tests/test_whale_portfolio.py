import asyncio
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from whale_watcher.core import GROUPS
from whale_watcher.liquidity_context import correlate_cycle
from whale_watcher.portfolio import (
    TOKEN_PROGRAMS,
    inventory_summary,
    neutral_solana_watch,
    read_inventory,
)
from whale_watcher.reporting import make_cycle
from whale_watcher.screening import features_from_cycles

ADDRESS = "11111111111111111111111111111112"


def token(account, mint, program, raw, decimals=9, owner=ADDRESS):
    return {
        "pubkey": account,
        "account": {
            "owner": program,
            "data": {
                "parsed": {
                    "type": "account",
                    "info": {
                        "mint": mint,
                        "owner": owner,
                        "state": "initialized",
                        "tokenAmount": {"amount": str(raw), "decimals": decimals},
                    },
                }
            },
        },
    }


def test_neutral_address_validation_and_stable_identity():
    row = neutral_solana_watch(ADDRESS)
    assert row["id"] == neutral_solana_watch(ADDRESS, "label")["id"]
    assert row["group"] not in GROUPS and row["classification"] == "pnl_unverified"
    for invalid in ("O" * 44, "1" * 33, "z" * 44, "0x" + "a" * 40, "abc"):
        with pytest.raises(ValueError):
            neutral_solana_watch(invalid)


def test_inventory_batches_all_mints_and_uses_exact_raw_quantities():
    calls = []

    async def rpc(chain, method, params):
        calls.append(params)
        assert chain == "solana" and method == "getTokenAccountsByOwner"
        assert params[2] == {"encoding": "jsonParsed", "commitment": "finalized"}
        program = params[1]["programId"]
        items = (
            [
                token("a", "mint1", program, 2**64 - 1),
                token("b", "mint1", program, 1),
                token("c", "zero", program, 0),
            ]
            if program == TOKEN_PROGRAMS["spl_token"]
            else [token("d", "mint2", program, 7, 0)]
        )
        return {"context": {"slot": 10 + len(calls)}, "value": items}

    result = asyncio.run(read_inventory(rpc, ADDRESS))
    assert len(calls) == 2 and result["token_accounts"] == 4 and result["positive_mints"] == 2
    assert result["holdings"][0]["amount"] == "18446744073.709551616"
    assert result["holdings"][1]["amount"] == "7"
    assert result["total_value_usd"] is None and result["complete"]
    assert result["context_slots"] == {"spl_token": 11, "token_2022": 12}


@pytest.mark.parametrize("problem", ["owner", "duplicates", "partial", "decimals"])
def test_inventory_rejects_incomplete_or_inconsistent_results(problem):
    async def rpc(chain, method, params):
        program = params[1]["programId"]
        if problem == "partial" and program == TOKEN_PROGRAMS["token_2022"]:
            raise ValueError("RPC failed")
        item = token("a", "mint", program, 5)
        if problem == "owner":
            item["account"]["data"]["parsed"]["info"]["owner"] = "different"
        items = [item]
        if problem == "duplicates":
            items.append(item)
        if problem == "decimals":
            items.append(token("b", "mint", program, 5, 6))
        return {"context": {"slot": 1}, "value": items}

    with pytest.raises(ValueError):
        asyncio.run(read_inventory(rpc, ADDRESS))


def inventory(count=250):
    return {
        "address": ADDRESS,
        "chain": "solana",
        "observed_at": "2026-10-29T00:00:00+00:00",
        "token_accounts": count,
        "positive_mints": count,
        "mint_count": count,
        "complete": True,
        "holdings": [
            {
                "mint": f"mint{i:04}",
                "program": "spl_token",
                "amount": "1",
                "raw_amount": "1",
                "states": {"initialized": 1},
            }
            for i in range(count)
        ],
    }


def test_inventory_changes_are_bounded_and_not_classified_as_trades():
    before, after = inventory(), inventory()
    for row in after["holdings"]:
        row.update(raw_amount="2", amount="2")
    assert inventory_summary(before)["baseline"]
    summary = inventory_summary(after, before)
    assert summary["changed_mints"] == 250 and len(summary["changes"]) == 100
    assert summary["changes_truncated"]
    assert {r["classification"] for r in summary["changes"]} == {"balance_or_account_state_change"}
    assert "holdings" not in summary


def test_neutral_balances_keep_coverage_and_liquidity_without_profit_cohorts():
    row = {
        **neutral_solana_watch(ADDRESS),
        "amount": "2",
        "previous": "1",
        "state": "accumulating",
        "actions": [],
    }
    cycle = make_cycle(
        "id",
        "2026-10-29T00:10:00+00:00",
        "2026-10-29T00:10:10+00:00",
        "2026-10-29T00:00:00+00:00",
        [row],
    )
    assert cycle["coverage"] == {"entries": 1, "observed": 1}
    assert all(report["entries"] == 0 for report in cycle["reports"].values())
    assert cycle["neutral_watch"]["rows"] == [row]
    assert features_from_cycles([cycle]) == []
    context = {"mode": "live", "markets": {}, "received_ms": 1}
    association = correlate_cycle(cycle, context)["assets"][0]
    assert association["symbol"] == "SOL"
    assert association["wallet_evidence"]["net_spot_quantity"] == "1"


def test_scan_preserves_native_balance_and_last_inventory_on_partial_failure(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "WHALE_DATABASE_URL", "sqlite:///" + (tmp_path / "portfolio.sqlite").as_posix()
    )
    monkeypatch.setenv("WHALE_ADMIN_TOKEN", "test-secret-unit-testing-only-32-characters")
    monkeypatch.setenv("WHALE_LIQUIDITY_ENABLED", "false")
    monkeypatch.setenv("WHALE_AUTO_REPORTS", "false")
    monkeypatch.delenv("RENDER", raising=False)
    from whale_watcher import app as module

    fail = False

    class Provider:
        attempts = []

        async def observe(self, row):
            return {"amount": "0.01", "source": "test_finalized_rpc"}

        async def inventory(self, address):
            if fail:
                raise ValueError("Token-2022 query failed")
            return deepcopy(inventory())

    with TestClient(module.app) as client:
        monkeypatch.setattr(module, "provider", Provider())
        monkeypatch.setattr(module, "public_markets", None)
        watch = neutral_solana_watch(ADDRESS)
        module.store.put("wallets", [watch])
        client.portal.call(module.scan)
        auth = ("whale", "test-secret-unit-testing-only-32-characters")
        path = "/api/portfolios/" + watch["id"]
        assert client.get(path).status_code == 401
        page = client.get(path, params={"offset": 200, "limit": 10000}, auth=auth).json()
        assert page["limit"] == 200 and len(page["holdings"]) == 50 and page["matched"] == 250
        assert (
            len(client.get(path, params={"search": "mint0001"}, auth=auth).json()["holdings"]) == 1
        )
        fail = True
        client.portal.call(module.scan)
        status = client.get("/api/status", auth=auth).json()
        assert status["cycle"]["neutral_watch"]["rows"][0]["state"] == "holding"
        assert status["cycle"]["portfolios"][0]["status"] == "unavailable"
        preserved = client.get(path, auth=auth).json()
        assert (
            preserved["refresh"]["status"] == "unavailable" and preserved["positive_mints"] == 250
        )
        assert status["cycle"]["coverage"]["observed"] == 1
