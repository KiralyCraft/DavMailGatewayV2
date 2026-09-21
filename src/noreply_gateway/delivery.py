from __future__ import annotations

import asyncio
import logging
import random
import time

from .backend import MicrosoftBackend
from .config import Config
from .errors import AuthenticationRequired, Permanent, Retryable, Uncertain
from .store import Store


class Dispatcher:
    def __init__(self, config: Config, store: Store, backend: MicrosoftBackend):
        self.config = config
        self.store = store
        self.backend = backend
        self.paused = False
        self.rate = config.delivery.messages_per_second
        self.cooldown_until = 0.0
        self.stopping = asyncio.Event()
        self.rate_lock = asyncio.Lock()
        self.next_permit = 0.0
        self.tasks: list[asyncio.Task] = []
        self.active = 0

    async def start(self) -> None:
        settings = await self.store.settings()
        self.paused = settings.get("paused", "false") == "true"
        self.rate = float(settings.get("rate", str(self.rate)))
        self.cooldown_until = float(settings.get("cooldown_until", "0"))
        self.tasks = [asyncio.create_task(self._worker(), name=f"delivery-{n}") for n in range(self.config.delivery.workers)]
        self.tasks.append(asyncio.create_task(self._housekeeping(), name="queue-housekeeping"))

    async def pause(self, paused: bool) -> None:
        await self.store.set_setting("paused", "true" if paused else "false")
        self.paused = paused

    async def set_rate(self, rate: float) -> None:
        if not 0.01 <= rate <= 1000:
            raise ValueError("Rate must be between 0.01 and 1000 messages/second")
        await self.store.set_setting("rate", str(rate))
        self.rate = rate

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self.stopping.wait(), timeout=max(0.001, seconds))
        except asyncio.TimeoutError:
            pass

    async def _permit(self) -> None:
        async with self.rate_lock:
            await self._sleep(max(0, self.next_permit - time.monotonic()))
            self.next_permit = time.monotonic() + 1 / self.rate

    async def _worker(self) -> None:
        while self.stopping.is_set() is False:
            try:
                if self.paused or self.store.healthy is False or self.cooldown_until > time.time():
                    await self._sleep(0.5)
                    continue
                await self._permit()
                if self.stopping.is_set() or self.paused or self.cooldown_until > time.time():
                    continue
                message = await self.store.claim()
                if message is None:
                    await self._sleep(0.1)
                    continue
                self.active += 1
                try:
                    await self._attempt(message)
                finally:
                    self.active -= 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.store.healthy = False
                logging.getLogger(__name__).error("Delivery supervisor halted intake and delivery (%s)", type(exc).__name__)
                await self._sleep(1)

    async def _attempt(self, message: dict) -> None:
        status, error, delay, cooldown = "submitted", "", 0.0, False
        try:
            await self.backend.send(message)
        except AuthenticationRequired as exc:
            await self.pause(True)
            status, error, delay = "retry", str(exc), 60.0
        except Retryable as exc:
            exponent = min(message["attempts"] - 1, 16)
            backoff = min(self.config.delivery.retry_max_seconds, self.config.delivery.retry_base_seconds * 2**exponent)
            delay = max(exc.delay, backoff * random.uniform(1.0, 1.2))
            cooldown = exc.global_cooldown
            status, error = "retry", str(exc)
            if message["attempts"] >= self.config.delivery.max_attempts:
                status = "failed"
                error += "; maximum attempts reached"
        except Permanent as exc:
            status, error = "failed", str(exc)
        except Uncertain as exc:
            status, error = "uncertain", str(exc)
        except asyncio.CancelledError:
            await asyncio.shield(self.store.finish(message["id"], "uncertain", "Shutdown interrupted an attempt; review before retry"))
            raise
        except Exception as exc:
            status, error = "uncertain", "Unexpected backend error (" + type(exc).__name__ + "); review before retry"
            self.store.healthy = False
        if cooldown:
            self.cooldown_until = max(self.cooldown_until, time.time() + delay)
        await self.store.finish(message["id"], status, error, delay, cooldown)

    async def _housekeeping(self) -> None:
        while self.stopping.is_set() is False:
            await self._sleep(60)
            if self.stopping.is_set():
                break
            try:
                await self.store.housekeeping()
            except Exception as exc:
                self.store.healthy = False
                logging.getLogger(__name__).error("Queue maintenance failed (%s)", type(exc).__name__)

    async def close(self) -> None:
        self.stopping.set()
        done, pending = await asyncio.wait(self.tasks, timeout=self.config.delivery.shutdown_seconds)
        for task in pending:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
