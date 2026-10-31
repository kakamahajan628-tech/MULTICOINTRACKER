import asyncio
import logging
import os
import time

LOG = logging.getLogger(__name__)


class TelegramAlerts:
    def __init__(self, session, enabled=False):
        self.session = session
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        self.enabled = enabled and bool(self.token and self.chat_id)
        self.queue = asyncio.Queue(maxsize=100)
        self.sent = {}
        self.dropped = 0
        self.failures = 0

    def submit(self, signals):
        if not self.enabled:
            return
        for signal in signals:
            if signal.score < 0.6:
                continue
            key = (signal.venue, signal.symbol, signal.kind, signal.evidence.get("side"))
            now = time.monotonic()
            if now - self.sent.get(key, -1000) < 60:
                continue
            try:
                self.queue.put_nowait(signal)
                self.sent[key] = now
            except asyncio.QueueFull:
                self.dropped += 1

    async def worker(self):
        while True:
            signal = await self.queue.get()
            text = (
                f"{signal.venue} {signal.symbol}: {signal.kind}\n"
                f"INFERRED | heuristic score {signal.score} (not a probability)\n"
                f"Evidence: {signal.evidence}\n{signal.caveat}"
            )
            for attempt in range(3):
                try:
                    async with self.session.post(
                        f"https://api.telegram.org/bot{self.token}/sendMessage",
                        json={"chat_id": self.chat_id, "text": text[:3900]},
                    ) as response:
                        data = await response.json()
                        if response.status == 429:
                            await asyncio.sleep(
                                min(60, data.get("parameters", {}).get("retry_after", 5))
                            )
                            continue
                        if response.status != 200 or not data.get("ok"):
                            raise ValueError("Telegram rejected alert")
                        break
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.failures += 1
                    # Never log exception URL; it contains the bot token.
                    LOG.warning("Telegram delivery failed, attempt %d", attempt + 1)
                    await asyncio.sleep(2**attempt)
            self.queue.task_done()
