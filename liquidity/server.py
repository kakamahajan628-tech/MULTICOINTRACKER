import asyncio
import os
from pathlib import Path
import time
from aiohttp import web
from .alerts import TelegramAlerts
from .book import ResyncRequired
from .demo import run_demo
from .feeds import LiveFeed
from .security import DashboardAuth, is_loopback, private_http_logging


def create_app(
    engine,
    mode="demo",
    symbols=("BTCUSDT",),
    venues=("binance", "bybit"),
    telegram=False,
    replay_path=None,
    replay_speed=1,
    allow_unauthenticated=False,
):
    private_http_logging()
    token = os.getenv("DASHBOARD_TOKEN", "")
    if allow_unauthenticated and (mode != "demo" or token):
        raise ValueError(
            "Unauthenticated access is only allowed for an explicit token-free synthetic demo"
        )
    guard = None if allow_unauthenticated else DashboardAuth(token, "DASHBOARD_TOKEN", "bearer")

    @web.middleware
    async def authenticate(request, handler):
        if request.path.startswith("/api/"):
            if guard is None:
                if not is_loopback(request.remote):
                    raise web.HTTPForbidden(text="Synthetic demo is loopback-only")
            else:
                retry = guard.authenticate(request.headers.get("Authorization", ""), request.remote)
                if retry > 0:
                    raise web.HTTPTooManyRequests(headers={"Retry-After": str(retry)})
                if retry < 0:
                    raise web.HTTPUnauthorized(headers={"WWW-Authenticate": "Bearer"})
        response = await handler(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
            "img-src 'self' data:; frame-ancestors 'none'"
        )
        return response

    app = web.Application(middlewares=[authenticate])
    runtime = {}
    runtime["replay_finished"] = False
    runtime["runtime_error"] = ""
    runtime["requested_markets"] = (
        [f"{v}:{s}" for v in venues for s in symbols]
        if mode == "live"
        else ["binance:BTCUSDT", "bybit:BTCUSDT"]
    )

    async def state(request):
        now = time.time_ns() // 1_000_000 if mode in ("live", "demo") else engine.now
        value = engine.view(now)
        value.update(
            {
                "mode": mode,
                "replay_finished": runtime["replay_finished"],
                "requested_markets": runtime["requested_markets"],
            }
        )
        return web.json_response(value)

    async def health(request):
        now = time.time_ns() // 1_000_000 if mode in ("live", "demo") else engine.now
        view = engine.view(now)
        readiness = {k: v["quality"]["fresh"] for k, v in view["markets"].items()}
        healthy = all(readiness.get(k, False) for k in runtime["requested_markets"])
        if runtime["runtime_error"]:
            healthy = False
        return web.json_response(
            {
                "healthy": healthy,
                "mode": mode,
                "markets": readiness,
                "events": engine.event_count,
                "error": runtime["runtime_error"],
                "alerts_dropped": runtime["alerts"].dropped,
                "alerts_failures": runtime["alerts"].failures,
            },
            status=200 if healthy else 503,
        )

    async def index(request):
        return web.FileResponse(Path(__file__).parent / "static" / "index.html")

    app.router.add_get("/", index)
    app.router.add_get("/api/state", state)
    app.router.add_get("/api/health", health)
    app.router.add_static("/static/", Path(__file__).parent / "static")

    async def lifecycle(application):
        import aiohttp
        from .storage import read_events

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            alerts = TelegramAlerts(session, telegram)
            runtime["alerts"] = alerts

            def emit(event):
                signals = engine.handle(event)
                alerts.submit(signals)

            async def replay():
                previous = None
                for event in read_events(replay_path):
                    if previous is not None and replay_speed > 0:
                        await asyncio.sleep(
                            max(0, event.received_ms - previous) / 1000 / replay_speed
                        )
                    try:
                        emit(event)
                    except ResyncRequired:
                        pass
                    previous = event.received_ms
                    await asyncio.sleep(0)
                runtime["replay_finished"] = True

            async def flush():
                while True:
                    await asyncio.sleep(0.5)
                    if engine.journal:
                        engine.journal.flush()

            async def guarded(coroutine):
                try:
                    await coroutine
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    runtime["runtime_error"] = f"{type(exc).__name__}: {str(exc)[:180]}"
                    raise

            tasks = [asyncio.create_task(guarded(flush()))]
            if alerts.enabled:
                tasks.append(asyncio.create_task(guarded(alerts.worker())))
            if mode == "live":
                tasks += [
                    asyncio.create_task(guarded(LiveFeed(session, v, s, emit).run()))
                    for v in venues
                    for s in symbols
                ]
            elif mode == "demo":
                tasks.append(asyncio.create_task(guarded(run_demo(emit))))
            elif mode == "replay":
                tasks.append(asyncio.create_task(guarded(replay())))
            yield
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if engine.journal:
                engine.journal.flush()

    app.cleanup_ctx.append(lifecycle)
    return app
