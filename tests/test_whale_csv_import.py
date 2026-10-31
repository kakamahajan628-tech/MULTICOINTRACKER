import pytest
from fastapi.testclient import TestClient

from whale_watcher.core import Store
from whale_watcher.csv_import import HEADERS, append_candidates, parse_candidates
from whale_watcher.portfolio import neutral_solana_watch
from whale_watcher.reporting import make_cycle


def data(address=None, pnl="500", leverage="10"):
    address = address or "0x" + "a" * 40
    return (
        ",".join(HEADERS) + "\n" + f"{address},source label,ZEC,long,1000,{pnl},{leverage}\n"
    ).encode()


def test_csv_preserves_untrusted_historical_figures_and_neutral_futures_identity():
    rows = parse_candidates(data(), "wallets.csv")
    row = rows[0]
    assert row["group"] == "Futures Unclassified" and row["chain"] == "hyperliquid"
    assert row["import_snapshot"]["unrealized_pnl_usd"] == "500"
    assert row["import_snapshot"]["observed_at"] is None
    assert "amount" not in row and "realized_pnl" not in row
    assert row["id"] == parse_candidates(data().replace(b"source label", b"new label"))[0]["id"]


@pytest.mark.parametrize(
    "payload",
    [
        data(address="0xabc"),
        data(pnl="NaN"),
        data(pnl="Infinity"),
        data(leverage="0"),
        data(leverage="-1"),
        data().replace(b"source label", b"=HYPERLINK(1)"),
        data() + data().split(b"\n")[1] + b"\n",
        data().replace(b"coin", b"unknown"),
        data() + b"extra,fields\n",
        b'"unterminated',
        data(pnl="1e999999999"),
    ],
)
def test_bad_csv_is_rejected(payload):
    with pytest.raises(ValueError):
        parse_candidates(payload)


def test_append_is_atomic_idempotent_and_preserves_solana_watch(tmp_path):
    store = Store("sqlite:///" + (tmp_path / "import.sqlite").as_posix())
    sol = neutral_solana_watch("11111111111111111111111111111112")
    store.put("wallets", [sol])
    rows = parse_candidates(data())
    assert append_candidates(store, rows)["added"] == 1
    assert append_candidates(store, rows)["added"] == 0
    assert store.get("wallets")[0] == sol and store.get("watch_revision") == 1
    before = store.get("wallets")
    too_many = [dict(rows[0], address="0x" + f"{i:040x}") for i in range(1000)]
    with pytest.raises(ValueError):
        append_candidates(store, too_many)
    assert store.get("wallets") == before
    store.engine.dispose()


def test_neutral_futures_have_risk_evidence_without_profit_cohort_classification():
    row = {
        **parse_candidates(data())[0],
        "state": "baseline",
        "amount": "10",
        "actions": [],
        "risk_legs": [
            {
                "side": "long",
                "mark_price": "100",
                "liquidation_price": "99",
                "risk_observed_at": "2026-10-29T00:00:00+00:00",
            }
        ],
    }
    cycle = make_cycle(
        "c",
        "2026-10-29T00:00:00+00:00",
        "2026-10-29T00:00:10+00:00",
        "2026-10-28T23:50:00+00:00",
        [row],
    )
    assert cycle["neutral_watch"]["rows"][0]["risk_band"] == "critical"
    assert len(cycle["risk_report"]["near_liquidation"]) == 1
    assert all(report["entries"] == 0 for report in cycle["reports"].values())


def test_csv_api_auth_and_append_preserve_existing_watch(tmp_path, monkeypatch):
    monkeypatch.setenv("WHALE_DATABASE_URL", "sqlite:///" + (tmp_path / "api.sqlite").as_posix())
    monkeypatch.setenv("WHALE_ADMIN_TOKEN", "test-secret-unit-testing-only-32-characters")
    monkeypatch.setenv("WHALE_LIQUIDITY_ENABLED", "false")
    monkeypatch.delenv("RENDER", raising=False)
    from whale_watcher import app as module

    with TestClient(module.app) as client:
        sol = neutral_solana_watch("11111111111111111111111111111112")
        module.store.put("wallets", [sol])
        assert client.post("/api/import/csv", content=data()).status_code == 401
        auth = ("whale", "test-secret-unit-testing-only-32-characters")

        # Keep the scheduler asleep while exercising import semantics.
        async def no_scan():
            await __import__("asyncio").Event().wait()

        monkeypatch.setattr(module, "scan", no_scan)
        result = client.post("/api/import/csv", content=data(), auth=auth)
        assert result.status_code == 200 and result.json()["added"] == 1
        assert len(module.store.get("wallets")) == 2
        assert client.post("/api/import/csv", content=b"broken", auth=auth).status_code == 400
        assert len(module.store.get("wallets")) == 2
