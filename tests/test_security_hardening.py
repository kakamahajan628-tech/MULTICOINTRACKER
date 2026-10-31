import asyncio
import base64
import logging
from aiohttp.test_utils import TestClient, TestServer
from fastapi.testclient import TestClient as ASGIClient
import pytest
from liquidity.engine import Engine
from liquidity.security import DashboardAuth, FailureLimiter, private_http_logging
from liquidity.server import create_app

TOKEN = "unit-security-token-at-least-32-characters"


def test_missing_short_placeholder_and_unicode_config_tokens_fail_closed(monkeypatch):
    for token in ("", "short", "replace-with-a-long-random-password", "é" * 40):
        monkeypatch.setenv("DASHBOARD_TOKEN", token)
        with pytest.raises(ValueError, match="32"):
            create_app(Engine())
    monkeypatch.delenv("DASHBOARD_TOKEN")
    with pytest.raises(ValueError):
        create_app(Engine(), mode="live", allow_unauthenticated=True)
    assert create_app(Engine(), mode="demo", allow_unauthenticated=True)


def test_limiter_expires_without_evicting_blocked_clients():
    clock = [0]
    limiter = FailureLimiter(limit=2, window=60, capacity=2, clock=lambda: clock[0])
    assert limiter.failure("a") == limiter.failure("a") == 0
    assert limiter.failure("a") == 60
    assert limiter.failure("b") == 0
    assert limiter.failure("c") == 60
    assert len(limiter.clients) == 2 and "a" in limiter.clients
    clock[0] = 61
    assert limiter.failure("a") == 0
    assert len(limiter.clients) == 1


def test_malformed_unicode_and_wrong_scheme_never_crash_authentication():
    bearer = DashboardAuth(TOKEN, "TOKEN", "bearer")
    for header in ("Bearer é", "Bearer 😀", "Basic abc", "", "Bearer"):
        assert bearer.authenticate(header, "peer") == -1
    assert bearer.authenticate("Bearer nope", "peer") > 0
    assert bearer.authenticate("Bearer " + TOKEN, "peer") == 0
    basic = DashboardAuth(TOKEN, "TOKEN")
    assert basic.authenticate("Basic é", "peer") == -1
    good = base64.b64encode(("whale:" + TOKEN).encode()).decode()
    assert basic.authenticate("Basic " + good, "peer") == 0


def test_standalone_http_auth_throttle_and_unicode(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", TOKEN)

    async def exercise():
        client = TestClient(TestServer(create_app(Engine())))
        await client.start_server()
        try:
            codes = [
                (await client.get("/api/state", headers={"Authorization": "Bearer é"})).status
                for _ in range(6)
            ]
            assert codes == [401] * 5 + [429]
            response = await client.get("/api/state", headers={"Authorization": "Bearer nope"})
            assert int(response.headers["Retry-After"]) > 0
            assert (
                await client.get("/api/state", headers={"Authorization": "Bearer " + TOKEN})
            ).status == 200
        finally:
            await client.close()

    asyncio.run(exercise())


def test_main_app_throttles_all_bad_headers_and_valid_login_still_works(tmp_path, monkeypatch):
    from whale_watcher import app as module

    monkeypatch.setenv("WHALE_ADMIN_TOKEN", TOKEN)
    monkeypatch.setenv("WHALE_DATABASE_URL", "sqlite:///" + (tmp_path / "auth.sqlite").as_posix())
    with ASGIClient(module.app) as client:
        codes = [
            client.get("/api/status", headers={"X-Forwarded-For": str(i)}).status_code
            for i in range(6)
        ]
        assert codes == [401] * 5 + [429]
        assert client.get("/api/status", auth=("whale", TOKEN)).status_code == 200
    monkeypatch.delenv("WHALE_ADMIN_TOKEN")
    with pytest.raises(ValueError, match="WHALE_ADMIN_TOKEN"):
        with ASGIClient(module.app):
            pass


def test_http_request_info_logs_are_suppressed(caplog):
    logging.getLogger("httpx").setLevel(logging.INFO)
    private_http_logging()
    logging.getLogger("httpx").info("POST https://api.telegram.org/botSECRET/sendMessage")
    assert "SECRET" not in caplog.text
    assert logging.getLogger("httpx").level == logging.WARNING


def test_webhooks_reject_nonascii_secrets_without_server_error(tmp_path, monkeypatch):
    from whale_watcher import app as module

    monkeypatch.setenv("WHALE_DATABASE_URL", "sqlite:///" + (tmp_path / "hooks.sqlite").as_posix())
    monkeypatch.setenv("WHALE_WEBHOOK_SECRET", "known-secret")
    monkeypatch.setenv("WHALE_HELIUS_WEBHOOK_SECRET", "known-secret")
    with ASGIClient(module.app) as client:
        for path, header in (
            ("/helius/webhook", b"authorization"),
            ("/telegram/webhook", b"x-telegram-bot-api-secret-token"),
        ):
            assert (
                client.post(path, json=[], headers=[(header, "é".encode("utf-8"))]).status_code
                == 403
            )


def test_telegram_reply_failure_never_exposes_bot_token(tmp_path, monkeypatch, caplog):
    import httpx
    from whale_watcher import app as module

    token = "BOT_TOKEN_MUST_NOT_APPEAR"
    monkeypatch.setenv(
        "WHALE_DATABASE_URL", "sqlite:///" + (tmp_path / "telegram.sqlite").as_posix()
    )
    monkeypatch.setenv("WHALE_WEBHOOK_SECRET", "webhook-secret")
    monkeypatch.setenv("WHALE_TELEGRAM_OWNER_ID", "123")
    monkeypatch.setenv("WHALE_TELEGRAM_TOKEN", token)
    with ASGIClient(module.app) as client:
        original = module.client
        fake = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
        monkeypatch.setattr(module, "client", fake)
        body = {
            "update_id": 1,
            "message": {
                "from": {"id": 123},
                "chat": {"id": 123, "type": "private"},
                "text": "/status",
            },
        }
        response = client.post(
            "/telegram/webhook",
            json=body,
            headers={"x-telegram-bot-api-secret-token": "webhook-secret"},
        )
        assert response.status_code == 502
        assert token not in response.text and token not in caplog.text
        client.portal.call(fake.aclose)
        monkeypatch.setattr(module, "client", original)
