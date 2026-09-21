#!/usr/bin/env python3
"""Render the real frontend with in-memory HTTP fixtures, without browser networking.

Development-only optional dependency: playwright plus a local Chromium binary.
"""
import argparse
import json
import asyncio
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from aiohttp import web
from playwright.async_api import async_playwright

from noreply_gateway.config import Config
from noreply_gateway.delivery import Dispatcher
from noreply_gateway.message import prepare_message
from noreply_gateway.oauth import TokenManager
from noreply_gateway.security import Vault, hash_password
from noreply_gateway.smtp import SMTPServer
from noreply_gateway.store import Store
from noreply_gateway.web import AdminUI


async def run(args):
    with tempfile.TemporaryDirectory(prefix="gateway-ui-offline-") as temporary:
        config = Config(data_dir=Path(temporary))
        config.smtp.port = 0
        config.queue.min_free_bytes = 0
        vault = Vault(config.data_dir, create=True)
        vault.write("admin", hash_password("offline-screenshot-password"))
        tokens = TokenManager(config, vault, None)
        tokens.last_error = "Demo only: no Microsoft login or live sending performed."
        store = Store(config)
        await store.start()
        smtp = SMTPServer(config, store)
        await smtp.start()
        dispatcher = Dispatcher(config, store, None)
        await dispatcher.pause(True)
        subjects = ["Course registration confirmation", "Infrastructure health summary", "Research submission notification", "Account activation instructions"]
        for index in range(120):
            raw = (f"From: _NAT_notifications@example.test\r\nTo: test-recipient@example.test\r\nSubject: {subjects[index % len(subjects)]}\r\n\r\nSynthetic demo body.\r\n").encode()
            await store.submit(prepare_message(raw, ["test-recipient@example.test"], "", str(uuid.uuid4()), config))
        # Claim while unpaused, then freeze. Nothing invokes a real backend.
        await store.set_setting("paused", "false")
        for index in range(117):
            item = await store.claim()
            status = "submitted" if index < 110 else "uncertain" if index == 110 else "failed" if index == 111 else "retry"
            error = "" if status == "submitted" else "Synthetic example: response interrupted; inspect before retry" if status == "uncertain" else "Synthetic example: recipient rejected" if status == "failed" else "Synthetic example: temporary throttling; retry scheduled"
            await store.finish(item["id"], status, error, delay=300 if status == "retry" else 0)
        await dispatcher.pause(True)
        ui = AdminUI(config, store, dispatcher, tokens, smtp, vault)
        runner = web.AppRunner(ui.app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        config.web.base_url = "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])
        errors = []
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(executable_path=args.chromium, headless=True, args=["--no-sandbox"])
                page = await browser.new_page(viewport={"width": 1440, "height": 1200}, device_scale_factor=1)
                page.on("pageerror", lambda error: errors.append(str(error)))
                # Render locally in about:blank; all API responses are fixtures.
                # This does not navigate to a blocked browser-network endpoint.
                root = Path(__file__).resolve().parents[1] / "src/noreply_gateway/static"
                html = (root / "index.html").read_text().replace('<link rel="stylesheet" href="/style.css">', '').replace('<script src="/app.js" defer></script>', '')
                await page.set_content(html)
                await page.add_style_tag(content=(root / "style.css").read_text())
                stats = json.loads((await ui.stats(None)).text)
                records = await store.messages()
                await page.evaluate("""fixtures => {
                    let loggedIn = false;
                    window.fetch = async (address, options = {}) => {
                        const url = new URL(address, 'https://offline.example.test');
                        const path = url.pathname;
                        let body = {}, status = 200;
                        if (path === '/api/login') { loggedIn = true; body = {csrf: 'offline-csrf'}; }
                        else if (!loggedIn) { status = 401; body = {error: 'Administration login required'}; }
                        else if (path === '/api/session') { body = {csrf: 'offline-csrf'}; }
                        else if (path === '/api/stats') { body = fixtures.stats; }
                        else if (path === '/api/messages') { const filter = url.searchParams.get('status'); body = fixtures.records.filter(row => !filter || row.status === filter); }
                        else if (path === '/api/account/login') { body = {authorization_url: 'https://login.microsoftonline.com/common/oauth2/v2.0/authorize?code_challenge=offline-preview', redirect_uri: fixtures.stats.redirect_uri}; }
                        else { status = 404; body = {error: 'Not a frontend preview endpoint'}; }
                        return new Response(JSON.stringify(body), {status, headers: {'Content-Type': 'application/json'}});
                    };
                }""", {"stats": stats, "records": records})
                await page.add_script_tag(content=(root / "app.js").read_text())
                await page.locator("#password").fill("offline-screenshot-password")
                await page.locator('#login-form button').click()
                await page.locator("#accepted").filter(has_text="120").wait_for()
                await page.locator("#filter").select_option("uncertain")
                await page.wait_for_timeout(400)
                assert await page.locator("#message-rows tr").count() == 1
                assert "uncertain" in await page.locator("#message-rows").inner_text()
                await page.locator("#account-login").click()
                await page.locator("#oauth-panel").wait_for(state="visible")
                assert "login.microsoftonline.com" in await page.locator("#authorization-link").get_attribute("href")
                # Collapse the login panel again for the overview screenshot.
                await page.locator("#oauth-panel").evaluate("node => node.hidden = true")
                await page.screenshot(path=str(args.output), full_page=True)
                await page.set_viewport_size({"width": 390, "height": 844})
                await page.wait_for_timeout(100)
                overflow = await page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
                if overflow:
                    raise RuntimeError("Mobile layout overflows the viewport")
                await page.screenshot(path=str(args.output.with_name("ui-mobile-preview.png")), full_page=True)
                await browser.close()
            if errors:
                raise RuntimeError("Browser JavaScript errors: " + repr(errors))
            print("Offline frontend smoke passed: admin login, stats, queue filtering, OAuth link, desktop/mobile layout; zero JS exceptions.")
        finally:
            await runner.cleanup()
            await smtp.close()
            await store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chromium", default="/usr/bin/chromium")
    parser.add_argument("--output", type=Path, default=Path("docs/ui-preview.png"))
    asyncio.run(run(parser.parse_args()))
