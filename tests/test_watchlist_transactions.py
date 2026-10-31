from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3

import pytest
from sqlalchemy import select

from whale_watcher.core import Store


def row(identity):
    return {
        "id": identity,
        "chain": "solana",
        "address": identity,
        "coin": "SOL",
        "group": "Spot Unclassified",
    }


def test_legacy_migration_preserves_order_metadata_and_removes_blob(tmp_path):
    path = tmp_path / "legacy.sqlite"
    rows = [row("b"), row("a")]
    with sqlite3.connect(path) as con:
        con.execute("CREATE TABLE whale_state (key VARCHAR(100) PRIMARY KEY, value TEXT)")
        con.execute("INSERT INTO whale_state VALUES (?,?)", ("wallets", json.dumps(rows)))
    store = Store("sqlite:///" + path.as_posix())
    assert store.get("wallets") == rows
    with store.engine.connect() as con:
        assert (
            con.execute(select(store.state.c.value).where(store.state.c.key == "wallets")).scalar()
            is None
        )
        assert len(con.execute(select(store.wallets)).all()) == 2
    store.engine.dispose()
    restored = Store("sqlite:///" + path.as_posix())
    assert restored.get("wallets") == rows
    restored.engine.dispose()


def test_concurrent_appends_do_not_lose_rows_or_revision(tmp_path):
    store = Store("sqlite:///" + (tmp_path / "concurrent.sqlite").as_posix())

    def append(i):
        return store.append_wallets([row(f"wallet-{i}")])["added"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(append, range(40))) == 40
    assert len(store.get("wallets")) == 40
    assert store.get("watch_revision") == 40
    assert store.append_wallets([row("wallet-0")])["added"] == 0
    assert store.get("watch_revision") == 40
    store.engine.dispose()


def test_concurrent_capacity_limit_is_atomic(tmp_path):
    store = Store("sqlite:///" + (tmp_path / "capacity.sqlite").as_posix())
    store.put("wallets", [row(str(i)) for i in range(999)])

    def append(identity):
        try:
            return store.append_wallets([row(identity)])["added"]
        except ValueError:
            return 0

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(append, ("new-a", "new-b"))) == 1
    assert len(store.get("wallets")) == 1000
    store.engine.dispose()


def test_invalid_batch_does_not_partially_replace_and_stale_blob_fails_closed(tmp_path):
    path = tmp_path / "atomic.sqlite"
    store = Store("sqlite:///" + path.as_posix())
    store.put("wallets", [row("original")])
    with pytest.raises(ValueError):
        store.replace_wallets([row("dup"), row("dup")])
    assert store.get("wallets") == [row("original")]
    with store.engine.begin() as con:
        con.execute(store.state.insert().values(key="wallets", value="[]"))
    store.engine.dispose()
    with pytest.raises(RuntimeError, match="old app"):
        Store("sqlite:///" + path.as_posix())


def test_scan_snapshot_batch_commits_once_and_failure_is_atomic(tmp_path):
    from decimal import Decimal
    from sqlalchemy import event

    store = Store("sqlite:///" + (tmp_path / "batch.sqlite").as_posix())
    updates = [
        {
            "row": {**row(str(i)), "label": "wallet"},
            "amount": Decimal("1"),
            "future": False,
            "previous": None,
            "at": "2026-10-29T00:00:00+00:00",
            "result": {"source": "test"},
        }
        for i in range(100)
    ]
    commits = []
    event.listen(store.engine, "commit", lambda connection: commits.append(1))
    assert store.save_scan_observations(updates) == []
    assert len(commits) == 1
    assert store.get("snapshot:99")["amount"] == "1"
    for update in updates:
        update["previous"] = store.get("snapshot:" + update["row"]["id"])
        update["amount"] = Decimal("2")
    updates[-1]["amount"] = Decimal("NaN")
    with pytest.raises(ValueError):
        store.save_scan_observations(updates)
    assert store.get("snapshot:0")["amount"] == "1" and store.reports() == []
    store.engine.dispose()


def test_mid_scan_append_does_not_relabel_old_membership_as_new_revision(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from whale_watcher import app as module

    monkeypatch.setenv(
        "WHALE_DATABASE_URL", "sqlite:///" + (tmp_path / "revision.sqlite").as_posix()
    )
    wallet = {
        "id": "one",
        "chain": "solana",
        "address": "11111111111111111111111111111112",
        "coin": "SOL",
        "asset": "native",
        "platform": "",
        "group": "Spot Unclassified",
        "label": "test",
        "rank": 1,
    }

    class Provider:
        attempts = []

        async def observe(self, row):
            module.store.append_wallets(
                [{**wallet, "id": "two", "address": "11111111111111111111111111111113"}]
            )
            return {"amount": "1", "source": "mock"}

    with TestClient(module.app) as client:
        module.store.replace_wallets([wallet], bump_revision=True)
        monkeypatch.setattr(module, "provider", Provider())
        monkeypatch.setattr(module, "public_markets", None)
        client.portal.call(module.scan)
        cycle = module.store.get("latest_cycle")
        assert cycle["watch_revision"] == 1 and cycle["coverage"]["entries"] == 1
        assert module.store.get("watch_revision") == 2 and len(module.store.get("wallets")) == 2
