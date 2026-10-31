"""Bounded retry, circuit cooldown and persisted source health. No secrets in diagnostics."""

import asyncio
import os
import time
from datetime import datetime, timezone, timedelta
import httpx
from .core import now


class Recovery:
    def __init__(self, store):
        self.store = store
        self.health = store.get("source_health", {})
        self.locks = {}

    async def run(self, source, operation, budget=None, attempts=3):
        async with self.locks.setdefault(source, asyncio.Lock()):
            status = self.health.setdefault(source, {"state": "enabled_unverified", "failures": 0})
            if status.get("retry_at", 0) > time.time():
                raise ValueError("Source cooling down or budget unavailable")
            for attempt in range(attempts):
                try:
                    if budget:
                        period, cost, limit = budget
                        if cost:
                            self.store.reserve_budget(source, period, cost, limit)
                    result = await operation()
                    status.update(
                        state="active", failures=0, last_success=now(), retry_at=0, reason=None
                    )
                    return result
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    expected_names = (
                        "AuthenticationError",
                        "PermissionDenied",
                        "BadSymbol",
                        "NetworkError",
                        "RequestTimeout",
                        "RateLimitExceeded",
                        "ExchangeNotAvailable",
                        "DDoSProtection",
                    )
                    if (
                        not isinstance(exc, (ValueError, httpx.HTTPError, OSError))
                        and type(exc).__name__ not in expected_names
                    ):
                        status.update(
                            state="internal_error",
                            last_failure=now(),
                            reason="internal_adapter_error",
                            error_type=type(exc).__name__,
                        )
                        raise  # Do not relabel programming/schema errors as an upstream outage.
                    code = (
                        exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                    )
                    exhausted = isinstance(exc, ValueError) and "free budget exhausted" in str(exc)
                    fatal = code in (400, 401, 402, 403, 404, 422) or type(exc).__name__ in (
                        "AuthenticationError",
                        "PermissionDenied",
                        "BadSymbol",
                    )
                    transient = isinstance(exc, (httpx.TransportError, httpx.HTTPStatusError)) and (
                        code is None or code == 429 or code >= 500
                    )
                    transient |= type(exc).__name__ in (
                        "NetworkError",
                        "RequestTimeout",
                        "RateLimitExceeded",
                        "ExchangeNotAvailable",
                        "DDoSProtection",
                    )
                    reason = (
                        "budget_exhausted"
                        if exhausted
                        else "access_or_request_rejected"
                        if fatal
                        else "rate_limited"
                        if code == 429
                        else "upstream_unavailable"
                    )
                    if transient and attempt + 1 < attempts:
                        delay = min(10, 2**attempt)
                        if code == 429:
                            try:
                                delay = min(
                                    10,
                                    max(
                                        delay, float(exc.response.headers.get("retry-after", delay))
                                    ),
                                )
                            except ValueError:
                                pass
                        await asyncio.sleep(delay)
                        continue
                    status["failures"] = status.get("failures", 0) + 1
                    cooldown = int(os.getenv("WHALE_SOURCE_COOLDOWN_SECONDS", "120"))
                    if exhausted and (budget or hasattr(exc, "period")):
                        current = datetime.now(timezone.utc)
                        period = getattr(exc, "period", budget[0] if budget else "")
                        if len(period) == 7:
                            reset = (current.replace(day=28) + timedelta(days=4)).replace(
                                day=1, hour=0, minute=0, second=0, microsecond=0
                            )
                        else:
                            reset = (current + timedelta(days=1)).replace(
                                hour=0, minute=0, second=0, microsecond=0
                            )
                        cooldown = max(1, reset.timestamp() - time.time())
                    status.update(
                        state="budget_exhausted"
                        if exhausted
                        else "awaiting_access"
                        if fatal
                        else "cooldown",
                        retry_at=time.time() + cooldown,
                        last_failure=now(),
                        reason=reason,
                    )
                    raise ValueError(source + ": " + reason) from None
            raise ValueError("Source unavailable")

    def flush(self):
        self.store.put("source_health", self.health)
