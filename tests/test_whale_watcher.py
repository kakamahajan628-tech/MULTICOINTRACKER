import asyncio
import io
from decimal import Decimal

import httpx
import pytest
from openpyxl import Workbook
from fastapi.testclient import TestClient

from whale_watcher.core import GROUPS, HEADERS, Store, parse_excel
from whale_watcher.providers import RPCProvider


def excel_rows(count=1, mutate=None):
    # In-memory input fixtures only, never user-facing workbook authoring.
    book = Workbook()
    book.remove(book.active)
    for group in GROUPS:
        book.create_sheet(group).append(HEADERS)
    for n in range(count):
        row = ["0x" + f"{n + 1:040x}", "ethereum", "ETH", "", n + 1, "native", 18, ""]
        if mutate:
            mutate(row)
        book[GROUPS[0]].append(row)
    output = io.BytesIO()
    book.save(output)
    return output.getvalue()


def test_import_1000_and_stable_identity():
    rows = parse_excel(excel_rows(1000))
    assert len(rows) == len({r["id"] for r in rows}) == 1000
    assert rows[6]["label"] == "Spot Profit Trader #7"
    assert rows[0]["id"] == parse_excel(excel_rows(1))[0]["id"]
    with pytest.raises(ValueError, match="1,000"):
        parse_excel(excel_rows(1001))


def test_invalid_import_and_zero_decimals():
    with pytest.raises(ValueError, match="invalid address"):
        parse_excel(excel_rows(mutate=lambda r: r.__setitem__(0, "not-an-address")))
    with pytest.raises(ValueError, match="formulas"):
        parse_excel(excel_rows(mutate=lambda r: r.__setitem__(3, "=1+1")))
    with pytest.raises(ValueError, match="Native coin"):
        parse_excel(excel_rows(mutate=lambda r: r.__setitem__(2, "USDC")))

    def token(row):
        row[2], row[5], row[6] = "TEST", "0x" + "a" * 40, 0

    assert parse_excel(excel_rows(mutate=token))[0]["decimals"] == 0


def test_baseline_decimal_change_and_persistence(tmp_path):
    url = "sqlite:///" + (tmp_path / "state.sqlite").as_posix()
    store = Store(url)
    row = parse_excel(excel_rows())[0]
    assert store.observe(row, Decimal("0.000000000000000001"), "mock") is None
    assert store.observe(row, Decimal("0.000000000000000001"), "mock") is None
    event = store.observe(row, Decimal("10000.000000000000000001"), "mock")
    assert event["delta"] == "10000.000000000000000000"
    assert event["price_usd"] is None
    assert "unverified" in event["classification"]
    assert store.ingest("signature", {"test": 1})
    assert not store.ingest("signature", {"test": 2})
    store.engine.dispose()
    restored = Store(url)
    assert len(restored.reports()) == 1
    assert restored.evidence() == [{"test": 1}]


def test_rpc_and_error_not_zero():
    async def run():
        def response(request):
            import json

            payload = json.loads(request.content)
            if payload["params"][0] == "bad":
                return httpx.Response(200, json={"error": {"code": -1}})
            return httpx.Response(200, json={"result": hex(10**18)})

        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            provider = RPCProvider(client, {"ethereum": "https://rpc.test"}, rps=10000)
            row = parse_excel(excel_rows())[0]
            assert await provider.balance(row) == 1
            row["address"] = "bad"
            with pytest.raises(ValueError, match="error"):
                await provider.balance(row)

    asyncio.run(run())


def test_api_auth_atomic_import_and_helius(tmp_path, monkeypatch):
    monkeypatch.setenv("WHALE_DATABASE_URL", "sqlite:///" + (tmp_path / "api.sqlite").as_posix())
    monkeypatch.setenv("WHALE_ADMIN_TOKEN", "secret-test-unit-testing-only-32-characters")
    monkeypatch.setenv("WHALE_HELIUS_WEBHOOK_SECRET", "helius-secret")
    monkeypatch.delenv("RENDER", raising=False)
    from whale_watcher import app as module

    with TestClient(module.app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/api/status").status_code == 401
        auth = ("whale", "secret-test-unit-testing-only-32-characters")
        assert client.post("/api/import", content=excel_rows(2), auth=auth).json()["imported"] == 2
        assert client.post("/api/import", content=b"broken", auth=auth).status_code == 400
        assert len(client.get("/api/status", auth=auth).json()["wallets"]) == 2
        assert client.get("/", auth=auth).status_code == 200
        wallet = {"address": "A" * 44, "chain": "solana"}
        module.store.put("wallets", [wallet])
        event = [
            {"signature": "S" * 88, "accountData": [{"account": "A" * 44}], "type": "TRANSFER"}
        ]
        assert client.post("/helius/webhook", json=event).status_code == 403
        headers = {"authorization": "helius-secret"}
        assert client.post("/helius/webhook", json=event, headers=headers).json()["accepted"] == 1
        assert client.post("/helius/webhook", json=event, headers=headers).json()["accepted"] == 0


def test_scan_1000_mocked_provider_and_failure_preserves_balance(tmp_path):
    from whale_watcher import app as module

    async def run():
        module.store = Store("sqlite:///" + (tmp_path / "batch.sqlite").as_posix())
        rows = parse_excel(excel_rows(1000))
        module.store.put("wallets", rows)

        class MockProvider:
            amount = Decimal(1)
            fail = False

            async def balance(self, row):
                if self.fail and row["id"] == rows[0]["id"]:
                    raise ValueError("unavailable")
                return self.amount

        mock = MockProvider()
        module.provider = mock
        await module.scan()
        assert module.store.get("last_scan")["entries"] == 1000
        assert module.store.reports() == []
        mock.amount, mock.fail = Decimal(2), True
        await module.scan()
        state = module.store.get("last_scan")
        assert state["changed"] == 999 and len(state["errors"]) == 1
        assert module.store.get("snapshot:" + rows[0]["id"])["amount"] == "1"

    asyncio.run(run())


def test_official_sources_retain_completeness_and_timestamps(monkeypatch):
    from whale_watcher.sources import OfficialSources

    monkeypatch.setenv("WHALE_NANSEN_KEY", "test-key")
    monkeypatch.setenv("WHALE_DUNE_KEY", "test-key")

    async def run():
        def response(request):
            if request.url.host == "api.nansen.ai":
                assert request.headers["apiKey"] == "test-key"
                return httpx.Response(
                    200, json={"data": [{"value_usd": 5}], "pagination": {"is_last_page": True}}
                )
            return httpx.Response(
                200,
                json={
                    "state": "QUERY_STATE_COMPLETED",
                    "next_offset": 1000,
                    "result": {"rows": []},
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            adapter = OfficialSources(client)
            balances = await adapter.nansen(parse_excel(excel_rows())[0])
            assert balances["complete"] and balances["retrieved_at"]
            assert balances["data"][0]["value_usd"] == 5
            assert not (await adapter.dune(123))["complete"]

    asyncio.run(run())


def test_telegram_requires_secret_owner_and_private_chat(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "WHALE_DATABASE_URL", "sqlite:///" + (tmp_path / "telegram.sqlite").as_posix()
    )
    monkeypatch.setenv("WHALE_WEBHOOK_SECRET", "test-secret")
    monkeypatch.setenv("WHALE_TELEGRAM_OWNER_ID", "123")
    monkeypatch.delenv("RENDER", raising=False)
    from whale_watcher import app as module

    with TestClient(module.app) as client:
        update = {
            "update_id": 1,
            "message": {
                "from": {"id": 999},
                "chat": {"id": 999, "type": "private"},
                "text": "/resume",
            },
        }
        assert client.post("/telegram/webhook", json=update).status_code == 403
        headers = {"x-telegram-bot-api-secret-token": "test-secret"}
        assert client.post("/telegram/webhook", json=update, headers=headers).status_code == 200
        assert module.store.get("paused", True)
        update["message"]["from"]["id"] = 123
        update["message"]["chat"]["type"] = "group"
        assert client.post("/telegram/webhook", json=update, headers=headers).status_code == 200
        assert module.store.get("paused", True)
