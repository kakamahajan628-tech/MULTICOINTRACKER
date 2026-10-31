import asyncio
import json
from aiohttp.test_utils import TestClient, TestServer
from liquidity.alerts import TelegramAlerts
from liquidity.demo import demo_events
from liquidity.engine import Engine
from liquidity.models import Signal
from liquidity.server import create_app


def test_dashboard_api_replay_auth_and_health(tmp_path, monkeypatch):
    path = tmp_path / "demo.jsonl"
    path.write_text("\n".join(json.dumps(e.record()) for e in demo_events(20)), encoding="utf-8")
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-secret-unit-testing-only-32-characters")

    async def exercise():
        engine = Engine()
        client = TestClient(
            TestServer(create_app(engine, mode="replay", replay_path=path, replay_speed=0))
        )
        await client.start_server()
        try:
            response = await client.get("/")
            assert response.status == 200
            assert "Liquidity heatmap" in await response.text()
            assert (await client.get("/api/state")).status == 401
            for _ in range(100):
                response = await client.get(
                    "/api/state",
                    headers={"Authorization": "Bearer test-secret-unit-testing-only-32-characters"},
                )
                state = await response.json()
                if state["replay_finished"]:
                    break
                await asyncio.sleep(0.01)
            assert state["replay_finished"]
            assert len(state["markets"]) == 2
            assert (
                await client.get(
                    "/api/health",
                    headers={"Authorization": "Bearer test-secret-unit-testing-only-32-characters"},
                )
            ).status == 200
            response = await client.get("/static/app.js")
            assert response.status == 200
            assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
        finally:
            await client.close()

    asyncio.run(exercise())


def test_alerts_disabled_without_optin(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    alerts = TelegramAlerts(None)
    alerts.submit([Signal("iceberg_candidate", "binance", "BTCUSDT", 1, {}, 0.8)])
    assert alerts.queue.empty()


def test_alerts_cooldown_and_score_filter(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    alerts = TelegramAlerts(None, True)
    signal = Signal("iceberg_candidate", "binance", "BTCUSDT", 1, {}, 0.8)
    alerts.submit([signal, signal])
    alerts.submit([Signal("low", "binance", "BTCUSDT", 1, {}, 0.3)])
    assert alerts.queue.qsize() == 1
