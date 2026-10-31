import asyncio
import json
from datetime import datetime, timedelta, timezone
import httpx
import pytest
from whale_watcher.core import Store, GROUPS
from whale_watcher.screening import (
    ScreeningService,
    features_from_cycles,
    build_screen,
    screen_digest,
)
from whale_watcher.screen_ai import ScreenAI, validate_answer


def cycle(index, revision=1, missing=False):
    start = datetime(2026, 10, 9, tzinfo=timezone.utc) + timedelta(minutes=(index - 1) * 10)
    rows = {}
    for n, group in enumerate(GROUPS):
        row = {
            "id": "watch-" + str(n),
            "label": "Trader " + str(n),
            "address": "0x" + str(n + 1) * 40,
            "chain": "ethereum",
            "coin": "ETH",
            "asset": "native",
            "platform": "hyperliquid" if n >= 2 else "",
            "group": group,
            "state": "unavailable" if missing else "accumulating" if n < 2 else "holding",
            "amount": None if missing else str(100 + index),
            "previous": None if missing else str(99 + index),
            "previous_observed_at": (start + timedelta(seconds=1)).isoformat(),
            "actions": [{"action": "swap_in", "hash": "same-tx"}] if n < 2 and not missing else [],
            "price_usd": "100" if n < 2 else None,
            "price_source": "test",
            "realized_pnl": None,
            "fills": [],
        }
        rows[group] = {"rows": [row]}
    return {
        "id": "cycle-" + str(index),
        "started_at": (start + timedelta(minutes=10)).isoformat(),
        "completed_at": (start + timedelta(minutes=10, seconds=5)).isoformat(),
        "window_start": start.isoformat(),
        "window_end": (start + timedelta(minutes=10)).isoformat(),
        "watch_revision": revision,
        "screen_schema": 1,
        "reports": rows,
    }


def test_six_scans_aggregate_unique_traders_and_all_cohorts(monkeypatch):
    monkeypatch.setenv("WHALE_SCREEN_ASSET_LINKS_JSON", '{"hyperliquid:ETH":"ethereum:native"}')
    report = build_screen([cycle(i) for i in range(1, 7)], "1h", 1)
    assert report["actual_span_minutes"] == 60 and len(report["scan_ids"]) == 6
    item = report["assets"][0]
    assert item["metrics"]["unique_traders"] == 4  # Not 4*6.
    assert item["metrics"]["wallet_observations"] == 24
    assert item["metrics"]["verified_tx_count"] == 2  # Repeated hash per wallet is one event.
    assert item["metrics"]["net_spot_quantity"] == "12"
    assert item["phase"] == "accumulation"
    assert all(item["metrics"]["cohorts"][g]["unique_traders"] == 1 for g in GROUPS)
    assert (
        next(h for h in item["hypotheses"] if h["phase"] == "rugpull")["estimated_probability_pct"]
        is None
    )


def test_same_ticker_different_contracts_and_source_switch_exclusion():
    c = cycle(1)
    c["reports"]["Spot Profit"]["rows"][0]["asset"] = "0x" + "a" * 40
    c["reports"]["Spot Loss"]["rows"][0]["asset"] = "0x" + "b" * 40
    c["reports"]["Spot Profit"]["rows"][0]["balance_source_changed"] = True
    items = features_from_cycles([c])
    assert len([f for f in items if not f["asset_id"].startswith("perp:")]) == 2
    first = next(f for f in items if f["asset_id"].endswith("a" * 40))
    assert first["net_spot_quantity"] == "0" and first["source_changes"] == 1


def test_auto_every_six_then_four_hours_restart_and_revision(tmp_path, monkeypatch):
    monkeypatch.setenv("WHALE_SCREEN_ASSET_LINKS_JSON", '{"hyperliquid:ETH":"ethereum:native"}')

    async def run():
        store = Store("sqlite:///" + (tmp_path / "screens.sqlite").as_posix())
        store.put("watch_revision", 1)
        service = ScreeningService(store)
        for i in range(1, 25):
            store.save_cycle(cycle(i))
            await service.advance()
            assert len(store.screen_history("1h", 1, 10)) == i // 6
        hourly = store.screen_history("1h", 1, 10)
        four = store.screen_history("4h", 1, 10)
        assert len(hourly) == 4 and len(four) == 1
        assert len(four[0]["scan_ids"]) == len(set(four[0]["scan_ids"])) == 24
        assert four[0]["actual_span_minutes"] == 240
        assert four[0]["assets"][0]["metrics"]["unique_traders"] == 4
        assert len(four[0]["assets"][0]["features"]["tx_ids"]) == 2
        assert four[0]["assets"][0]["metrics"]["verified_actions"]["swap_in"] == 2
        assert await ScreeningService(store).advance() == 0
        store.put("watch_revision", 2)
        assert not (await ScreeningService(store).requested("1h"))["ready"]

    asyncio.run(run())


def test_legacy_cycles_do_not_block_new_screening_and_missing_coverage(tmp_path):
    async def run():
        store = Store("sqlite:///" + (tmp_path / "legacy.sqlite").as_posix())
        store.put("watch_revision", 1)
        for i in range(1, 7):
            old = cycle(i)
            old.pop("screen_schema")
            store.save_cycle(old)
        for i in range(7, 13):
            store.save_cycle(cycle(i, missing=True))
        service = ScreeningService(store)
        await service.advance()
        report = await service.requested("1h")
        assert report["ready"] and all(r["phase"] == "insufficient_data" for r in report["assets"])
        assert "traders 4" not in screen_digest(
            report
        )  # Contracts/venues separately grouped without links.

    asyncio.run(run())


def answer(item, phase="accumulation", probability=60):
    return {
        "dominant_phase": phase,
        "summary": "The supplied tracked sample supports a tentative phase assessment.",
        "hypotheses": [
            {
                "phase": h["phase"],
                "estimated_probability_pct": probability if h["eligible"] else None,
                "evidence_ids": h["evidence_ids"],
                "reason": "Supported by the supplied evidence.",
            }
            for h in item["hypotheses"]
        ],
    }


def test_ai_rejects_unsupported_rugpull_and_trade_instruction():
    item = build_screen([cycle(i) for i in range(1, 7)], "1h", 1)["assets"][0]
    data = answer(item)
    next(h for h in data["hypotheses"] if h["phase"] == "rugpull")["estimated_probability_pct"] = 90
    with pytest.raises(ValueError):
        validate_answer(data, item)
    data = answer(item)
    data["summary"] = "Buy now with leverage"
    with pytest.raises(ValueError):
        validate_answer(data, item)


def test_all_ai_providers_consensus_headers_and_no_wallets(tmp_path, monkeypatch):
    monkeypatch.setenv("WHALE_SCREEN_ASSET_LINKS_JSON", '{"hyperliquid:ETH":"ethereum:native"}')
    for provider in ("GROQ", "GEMINI", "OPENROUTER"):
        monkeypatch.setenv("WHALE_SCREEN_" + provider + "_KEY", "SECRET-" + provider)
        monkeypatch.setenv(
            "WHALE_SCREEN_" + provider + "_MODEL",
            "openrouter/free" if provider == "OPENROUTER" else "test-model",
        )

    async def run():
        item = build_screen([cycle(i) for i in range(1, 7)], "1h", 1)["assets"][0]
        calls = []

        def handler(request):
            calls.append(request)
            assert (
                "Trader" not in request.content.decode()
                and "0x" + 40 * "1" not in request.content.decode()
            )
            if request.url.host == "generativelanguage.googleapis.com":
                assert request.headers["x-goog-api-key"] == "SECRET-GEMINI"
                return httpx.Response(
                    200,
                    json={
                        "candidates": [
                            {
                                "finishReason": "STOP",
                                "content": {"parts": [{"text": json.dumps(answer(item))}]},
                            }
                        ]
                    },
                )
            assert request.headers["Authorization"].startswith("Bearer SECRET-")
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"finish_reason": "stop", "message": {"content": json.dumps(answer(item))}}
                    ]
                },
            )

        store = Store("sqlite:///" + (tmp_path / "ai.sqlite").as_posix())
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await ScreenAI(client, store).analyze(item)
            assert result["status"] == "consensus" and result["valid_providers"] == 3
            assert result["consensus_phase"] == "accumulation" and len(calls) == 3
            assert (
                next(h for h in result["hypotheses"] if h["phase"] == "rugpull")[
                    "estimated_probability_pct"
                ]
                is None
            )

    asyncio.run(run())


def test_ai_missing_keys_no_network_and_provider_disagreement(tmp_path, monkeypatch):
    async def run():
        item = build_screen([cycle(i) for i in range(1, 7)], "1h", 1)["assets"][0]
        store = Store("sqlite:///" + (tmp_path / "missing.sqlite").as_posix())
        for provider in ("GROQ", "GEMINI", "OPENROUTER"):
            monkeypatch.delenv("WHALE_SCREEN_" + provider + "_KEY", raising=False)
        async with httpx.AsyncClient() as client:
            ai = ScreenAI(client, store)
            assert (await ai.analyze(item))["status"] == "awaiting_provider_keys_and_models"
            monkeypatch.setenv("WHALE_SCREEN_GROQ_KEY", "x")
            monkeypatch.setenv("WHALE_SCREEN_GROQ_MODEL", "test")
            monkeypatch.setenv("WHALE_SCREEN_GEMINI_KEY", "x")
            monkeypatch.setenv("WHALE_SCREEN_GEMINI_MODEL", "test")

            async def vote(provider, text, item):
                return {
                    "provider": provider,
                    "status": "validated",
                    "answer": answer(item, "accumulation" if provider == "groq" else "sideways"),
                }

            ai.provider_vote = vote
            assert (await ai.analyze(item))["status"] == "disagreement"

    asyncio.run(run())


def test_command_and_private_api_progress_and_mapping(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from whale_watcher import app as module

    monkeypatch.setenv("WHALE_DATABASE_URL", "sqlite:///" + (tmp_path / "api.sqlite").as_posix())
    monkeypatch.setenv("WHALE_ADMIN_TOKEN", "screen-test-unit-testing-only-32-characters")
    monkeypatch.delenv("RENDER", raising=False)
    with TestClient(module.app) as client:
        assert client.post("/api/screens/1h").status_code == 401
        auth = ("whale", "screen-test-unit-testing-only-32-characters")
        assert client.post("/api/screens/1h", auth=auth).json()["required"] == 6
        assert client.portal.call(module.command, "/report screen4h")["report"].startswith(
            "SCREEN 4h | collecting"
        )
        result = client.portal.call(module.command, "/screenlink hyperliquid:ETH ethereum:native")
        assert result["to"] == "ethereum:native"
        revision = module.store.get("watch_revision")
        client.portal.call(module.command, "/screenlink hyperliquid:ETH ethereum:native")
        assert module.store.get("watch_revision") == revision


def test_same_address_spot_and_perp_are_separate_observations(monkeypatch):
    monkeypatch.setenv("WHALE_SCREEN_ASSET_LINKS_JSON", '{"hyperliquid:ETH":"ethereum:native"}')
    c = cycle(1)
    for group in GROUPS:
        c["reports"][group]["rows"][0]["address"] = "0x" + "a" * 40
    item = features_from_cycles([c])[0]
    assert len(item["wallet_ids"]) == 1 and item["observations"] == 2
