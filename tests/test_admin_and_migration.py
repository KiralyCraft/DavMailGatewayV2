import base64
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import aiohttp
import pytest
from aiohttp import web
from yarl import URL

from noreply_gateway.cli import config_text, main
from noreply_gateway.additional_sender import AdditionalSender
from noreply_gateway.config import Config, load_config
from noreply_gateway.delivery import Dispatcher
from noreply_gateway.migration import apply_davmail, imported_record, read_properties
from noreply_gateway.oauth import TokenManager
from noreply_gateway.security import InstanceLock, Vault, hash_password
from noreply_gateway.smtp import SMTPServer
from noreply_gateway.web import AdminUI


@pytest.fixture
async def admin(config, store, vault):
    vault.write("admin", hash_password("test-only-admin-password"))
    tokens = TokenManager(config, vault, None)
    dispatcher = Dispatcher(config, store, None)
    smtp = SMTPServer(config, store)
    await smtp.start()
    ui = AdminUI(config, store, dispatcher, tokens, smtp, vault)
    runner = web.AppRunner(ui.app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    config.web.base_url = "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])
    try:
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as session:
            yield session, config.web.base_url, ui
    finally:
        await runner.cleanup()
        await smtp.close()


@pytest.fixture
async def prefixed_admin(config, store, vault):
    config.web.base_url = "http://127.0.0.1/internal/mailgateway"
    config.web.trusted_proxy_ips = ["192.0.2.2"]
    vault.write("admin", hash_password("test-only-admin-password"))
    tokens = TokenManager(config, vault, None)
    dispatcher = Dispatcher(config, store, None)
    smtp = SMTPServer(config, store)
    await smtp.start()
    ui = AdminUI(config, store, dispatcher, tokens, smtp, vault)
    runner = web.AppRunner(ui.app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    config.web.base_url = "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1]) + "/internal/mailgateway"
    try:
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as session:
            yield session, config.web.base_url, ui
    finally:
        await runner.cleanup()
        await smtp.close()


async def login(session, url):
    parsed = urlsplit(url)
    origin = parsed.scheme + "://" + parsed.netloc
    response = await session.post(url + "/api/login", json={"password": "test-only-admin-password"}, headers={"Origin": origin})
    assert response.status == 200
    assert response.cookies["gateway_session"]["httponly"]
    assert response.cookies["gateway_session"]["samesite"] == "Lax"
    return {"X-CSRF-Token": (await response.json())["csrf"], "Origin": origin}


async def test_prefixed_admin_routes_and_authentication(prefixed_admin):
    session, url, ui = prefixed_admin
    origin = urlsplit(url).scheme + "://" + urlsplit(url).netloc
    response = await session.get(url, allow_redirects=False)
    assert response.status == 308
    assert response.headers["Location"] == "/internal/mailgateway/"
    response = await session.get(url + "/")
    assert response.status == 200
    html = await response.text()
    assert 'href="style.css"' in html and 'src="app.js"' in html
    for path in ("/app.js", "/style.css"):
        assert (await session.get(url + path)).status == 200
    assert (await session.get(url + "/api/stats")).status == 401
    assert (await session.get(origin + "/api/stats")).status == 404
    response = await session.post(url + "/api/login", json={"password": "test-only-admin-password"}, headers={"Origin": url})
    assert response.status == 403
    headers = await login(session, url)
    assert session.cookie_jar.filter_cookies(URL(url + "/")).get("gateway_session") is not None
    assert (await session.get(url + "/api/stats")).status == 200
    assert (await session.post(url + "/api/control", json={"paused": True}, headers={"Origin": origin})).status == 403
    assert (await session.post(url + "/api/control", json={"paused": True}, headers=headers)).status == 200
    response = await session.post(url + "/api/logout", json={}, headers=headers)
    assert response.status == 200
    assert response.cookies["gateway_session"]["path"] == "/internal/mailgateway/"
    assert (await session.get(url + "/api/stats")).status == 401
    assert ui.client_key(SimpleNamespace(remote="192.0.2.2", headers={"X-Forwarded-For": "198.51.100.9, 203.0.113.4"})) == "203.0.113.4"
    assert ui.client_key(SimpleNamespace(remote="192.0.2.5", headers={"X-Forwarded-For": "198.51.100.9"})) == "192.0.2.5"


async def test_additional_sender_choice_requires_admin_and_keeps_original_account(admin, vault, config):
    session, url, ui = admin
    ui.additional = AdditionalSender(config, vault, None, None)
    html = await (await session.get(url + "/")).text()
    assert 'id="sender-dialog"' in html
    assert 'id="default-sender"' in html
    assert 'id="oauth-status"' in html and 'id="sender-status"' in html
    assert 'id="additional-identity"' in html
    assert 'id="additional-default"' in html
    assert 'id="additional-from"' in html
    assert 'id="additional-delivery"' in html
    assert "Microsoft sending account" in html
    assert 'id="legacy-route"' not in html
    assert (await session.post(url + "/api/additional/sender", json={"mode": "default"})).status == 401
    headers = await login(session, url)
    assert (await session.post(url + "/api/additional/sender", json={"mode": "default"}, headers=headers)).status == 400
    vault.write("account", {"refresh_token": "original-offline-token"})
    ui.additional.tokens.record.update(username="person@example.test", default_sender="person@example.test", refresh_token="additional-offline-token")
    await ui.additional.completed_login()
    response = await session.post(url + "/api/additional/sender", json={"mode": "custom", "custom_sender": "shared@example.test"}, headers=headers)
    assert response.status == 200
    selected = (await response.json())["additional"]
    assert selected["default_sender"] == "person@example.test"
    assert selected["selected_sender"] == "shared@example.test"
    assert vault.read("account")["refresh_token"] == "original-offline-token"


def test_prefixed_config_validation(config):
    config.smtp.port = 1025
    config.web.base_url = "https://gateway.example.org/internal/mailgateway/"
    config.web.trusted_proxy_ips = ["192.0.2.2"]
    config.validate()
    assert config.web.base_url == "https://gateway.example.org/internal/mailgateway"
    for invalid in ("https://gateway.example.org/internal//mailgateway", "https://gateway.example.org/internal/../mailgateway", "https://gateway.example.org/internal/mailgateway?x=1"):
        config.web.base_url = invalid
        with pytest.raises(ValueError):
            config.validate()
    config.web.base_url = "https://gateway.example.org/internal/mailgateway"
    config.web.trusted_proxy_ips = ["not-an-ip"]
    with pytest.raises(ValueError):
        config.validate()


async def test_admin_authentication_csrf_origin_host(admin):
    session, url, ui = admin
    response = await session.get(url + "/api/stats")
    assert response.status == 401
    response = await session.post(url + "/api/login", json={"password": "test-only-admin-password"})
    assert response.status == 403
    response = await session.get(url + "/", headers={"Host": "attacker.example"})
    assert response.status == 400
    response = await session.post(url + "/api/login", json={"password": "wrong"}, headers={"Origin": url})
    assert response.status == 401
    headers = await login(session, url)
    assert (await session.get(url + "/api/stats")).status == 200
    assert (await session.post(url + "/api/control", json={"paused": True}, headers={"Origin": url})).status == 403
    assert (await session.post(url + "/api/control", json={"paused": True}, headers=headers)).status == 200
    assert ui.dispatcher.paused
    assert (await session.post(url + "/api/logout", json={}, headers=headers)).status == 200
    assert (await session.get(url + "/api/stats")).status == 401


async def test_static_assets_headers_and_health(admin):
    session, url, ui = admin
    for path in ("/", "/app.js", "/style.css"):
        response = await session.get(url + path)
        assert response.status == 200
        assert len(await response.read()) > 100
        assert response.headers["Referrer-Policy"] == "no-referrer"
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["X-Robots-Tag"] == "noindex, nofollow, noarchive"
    assert (await session.get(url + "/health/live")).status == 200
    assert (await session.get(url + "/health/ready")).status == 503


async def test_ui_stats_does_not_expose_tokens(admin):
    session, url, ui = admin
    await login(session, url)
    ui.tokens.record["refresh_token"] = "VERY-PRIVATE-TEST-REFRESH"
    ui.tokens.access_token = "VERY-PRIVATE-TEST-ACCESS"
    response = await session.get(url + "/api/stats")
    text = await response.text()
    assert "VERY-PRIVATE-TEST" not in text
    assert "credential_present" in text


async def test_ui_queue_actions_and_eml(admin, store, message):
    session, url, ui = admin
    headers = await login(session, url)
    identifier = await store.submit(message())
    await store.claim()
    await store.finish(identifier, "uncertain")
    assert (await session.post(url + f"/api/messages/{identifier}/retry", json={}, headers=headers)).status == 400
    assert (await session.get(url + f"/api/messages/{identifier}/eml")).status == 200
    assert (await session.post(url + f"/api/messages/{identifier}/retry", json={"acknowledge_duplicate": True}, headers=headers)).status == 200
    assert (await session.post(url + f"/api/messages/{identifier}/cancel", json={}, headers=headers)).status == 200
    assert (await session.get(url + f"/api/messages/{identifier}/eml")).status == 404


async def test_oauth_flow_bound_to_admin_session_and_consumed(admin):
    session, url, ui = admin
    headers = await login(session, url)
    response = await session.post(url + "/api/account/login", json={}, headers=headers)
    assert response.status == 200
    assert "code_challenge=" in (await response.json())["authorization_url"]
    invalid = ui.config.account.redirect_uri + "?code=OFFLINE&state=wrong"
    response = await session.post(url + "/api/account/complete", json={"redirect_url": invalid}, headers=headers)
    assert response.status == 400
    assert "state mismatch" in await response.text()
    response = await session.post(url + "/api/account/complete", json={"redirect_url": invalid}, headers=headers)
    assert response.status == 400
    assert "Start a new" in await response.text()


async def test_additional_oauth_uses_its_own_gateway_callback(admin, vault):
    session, url, ui = admin
    ui.config.additional.client_id = "11111111-2222-3333-4444-555555555555"
    ui.config.additional.redirect_uri = url + "/oauth/callback"
    ui.additional = AdditionalSender(ui.config, vault, None, None)
    headers = await login(session, url)
    started = await session.post(url + "/api/additional/login", json={}, headers=headers)
    assert started.status == 200
    payload = await started.json()
    assert payload["redirect_uri"] == url + "/oauth/callback"
    assert payload["authorization_url"] != ""
    flow = next(iter(ui.sessions.values())).flow
    assert flow is not None

    async def complete_login(value, response):
        assert value is flow
        assert response == {"code": "OFFLINE", "state": flow.state}
        ui.additional.tokens.record.update(username="person@example.test", default_sender="person@example.test", refresh_token="offline")
    ui.additional.tokens.complete_login = complete_login
    callback = await session.get(url + "/oauth/callback", params={"code": "OFFLINE", "state": flow.state}, allow_redirects=False)
    assert callback.status == 303
    assert callback.headers["Location"] == "/"
    assert (await session.get(url + "/oauth/callback", params={"code": "OFFLINE", "state": flow.state})).status == 400


async def test_senderless_gateway_is_ready_with_selected_microsoft_account(admin, vault):
    session, url, ui = admin
    ui.config.account.sender = ""
    ui.additional = AdditionalSender(ui.config, vault, None, None)
    ui.additional.tokens.record.update(username="person@example.test", default_sender="person@example.test", refresh_token="offline")
    await ui.additional.choose("default")
    assert (await session.get(url + "/health/ready")).status == 200
    await ui.dispatcher.pause(True)
    assert (await session.get(url + "/health/ready")).status == 503


async def test_remote_login_rate_limit(admin):
    session, url, ui = admin
    for index in range(6):
        response = await session.post(url + "/api/login", json={"password": "wrong"}, headers={"Origin": url})
        assert response.status == (401 if index < 5 else 429)
        if index == 5:
            assert 1 <= int(response.headers["Retry-After"]) <= 900


async def test_trusted_proxy_login_limit_uses_last_client_address(admin):
    session, url, ui = admin
    ui.config.web.trusted_proxy_ips = ["127.0.0.1"]
    for index in range(6):
        response = await session.post(url + "/api/login", json={"password": "wrong"}, headers={"Origin": url, "X-Forwarded-For": "192.0.2.1, 198.51.100.8"})
        assert response.status == (401 if index < 5 else 429)
    response = await session.post(url + "/api/login", json={"password": "wrong"}, headers={"Origin": url, "X-Forwarded-For": "192.0.2.1, 198.51.100.9"})
    assert response.status == 401


def test_java_properties_escapes_and_continuation(tmp_path):
    path = tmp_path / "source.properties"
    path.write_bytes(b"# comment\nx\\:key : hello\\nworld\nlong=one\\\n  two\nunicode=\\u0041\n")
    assert read_properties(path) == {"x:key": "hello\nworld", "long": "onetwo", "unicode": "A"}


def test_migration_maps_only_send_settings(config):
    properties = {"davmail.smtpEmbeddedUsername": "gateway@example.test", "davmail.allowRemote": "true", "davmail.smtpEmbeddedPassword": "unused", "davmail.smtpPort": "2525", "davmail.oauth.tenantId": "", "davmail.oauth.gateway@example.test.refreshToken": base64.b64encode(b"OFFLINE-MIGRATED-TOKEN").decode()}
    apply_davmail(config, properties)
    assert config.smtp.host == "127.0.0.1"
    assert config.smtp.port == 2525
    assert imported_record(config, properties)["refresh_token"] == "OFFLINE-MIGRATED-TOKEN"
    config.account.backend = "graph"
    with pytest.raises(ValueError):
        imported_record(config, properties)


def test_cli_init_check_reset_and_no_overwrite(tmp_path, monkeypatch):
    path = tmp_path / "gateway.toml"
    monkeypatch.setenv("TEST_ADMIN_PASSWORD", "offline-test-password")
    assert main(["init", "--config", str(path), "--admin-password-env", "TEST_ADMIN_PASSWORD"]) == 0
    config = load_config(path)
    assert config.smtp.port == 1025
    assert (config.data_dir.stat().st_mode & 0o777) == 0o700
    assert (config.data_dir / "vault.key").stat().st_mode & 0o777 == 0o600
    assert main(["check", "--config", str(path)]) == 0
    assert main(["init", "--config", str(path), "--admin-password-env", "TEST_ADMIN_PASSWORD"]) == 1
    monkeypatch.setenv("TEST_ADMIN_PASSWORD", "another-test-password")
    assert main(["reset-admin", "--config", str(path), "--admin-password-env", "TEST_ADMIN_PASSWORD"]) == 0


def test_second_process_lock_is_denied(config):
    first = InstanceLock(config.data_dir)
    try:
        with pytest.raises(RuntimeError):
            InstanceLock(config.data_dir)
    finally:
        first.close()


@pytest.mark.parametrize("change", [lambda c: setattr(c.smtp, "allowed_networks", ["0.0.0.0/0"]), lambda c: setattr(c.web, "base_url", "http://admin.example.net"), lambda c: setattr(c.account, "ews_url", "http://outlook.office365.com/EWS/Exchange.asmx"), lambda c: setattr(c.delivery, "messages_per_second", float("nan")), lambda c: setattr(c.delivery, "workers", 5)])
def test_unsafe_config_rejected(config, change):
    config.smtp.port = 1025
    change(config)
    with pytest.raises(ValueError):
        config.validate()


def test_config_roundtrip(config, tmp_path):
    config.smtp.port = 1025
    path = tmp_path / "gateway.toml"
    path.write_text(config_text(config))
    reread = load_config(path)
    assert reread.account == config.account
    assert reread.additional == config.additional
    assert reread.smtp == config.smtp
