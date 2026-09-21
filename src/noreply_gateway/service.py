from __future__ import annotations

import asyncio
import logging
import signal

import aiohttp
from aiohttp import web

from .backend import MicrosoftBackend
from .additional_sender import AdditionalSender
from .config import Config
from .delivery import Dispatcher
from .oauth import TokenManager
from .security import InstanceLock, Vault
from .smtp import SMTPServer
from .store import Store
from .web import AdminUI


async def serve(config: Config) -> None:
    lock = InstanceLock(config.data_dir)
    store = Store(config)
    runner = smtp = dispatcher = session = None
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    signals = (signal.SIGINT, signal.SIGTERM)
    try:
        vault = Vault(config.data_dir)
        if not vault.read("admin"):
            raise ValueError("No administration password is configured; run init or reset-admin")
        timeout = aiohttp.ClientTimeout(total=config.delivery.request_timeout, connect=config.delivery.connect_timeout)
        connector = aiohttp.TCPConnector(limit=config.delivery.workers + 4, limit_per_host=config.delivery.workers + 2, ttl_dns_cache=300)
        session = aiohttp.ClientSession(timeout=timeout, connector=connector, trust_env=False)
        await store.start()
        tokens = TokenManager(config, vault, session)
        additional = AdditionalSender(config, vault, session, MicrosoftBackend(config, tokens, session))
        dispatcher = Dispatcher(config, store, additional)
        smtp = SMTPServer(config, store, additional)
        ui = AdminUI(config, store, dispatcher, tokens, smtp, vault, additional)
        runner = web.AppRunner(ui.app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, config.web.host, config.web.port).start()
        await smtp.start()
        await dispatcher.start()
        for item in signals:
            loop.add_signal_handler(item, stopped.set)
        logging.getLogger(__name__).info("SMTP %s:%s; administration %s; backend %s", config.smtp.host, smtp.port, config.web.base_url, config.account.backend)
        await stopped.wait()
    finally:
        for item in signals:
            loop.remove_signal_handler(item)
        # Stop admission before draining the in-flight delivery attempts.
        try:
            if smtp is not None:
                await smtp.close()
            if runner is not None:
                await runner.cleanup()
            if dispatcher is not None and dispatcher.tasks:
                await dispatcher.close()
        finally:
            try:
                await store.close()
            finally:
                if session is not None:
                    await session.close()
                lock.close()
