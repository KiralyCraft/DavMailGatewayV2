from __future__ import annotations

import asyncio
import hmac
import ipaddress
import math
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from importlib.resources import files
from urllib.parse import parse_qsl, urlsplit

from aiohttp import web

from .config import Config
from .additional_sender import AdditionalSender
from .delivery import Dispatcher
from .errors import AuthenticationRequired, Retryable
from .oauth import LoginFlow, TokenManager
from .security import Vault, verify_password
from .smtp import SMTPServer
from .store import Store


@dataclass
class AdminSession:
    csrf: str
    expires: float
    flow: LoginFlow | None = None
    flow_kind: str = "original"


class AdminUI:
    LOGIN_WINDOW_SECONDS = 900
    LOGIN_PER_CLIENT_LIMIT = 5
    LOGIN_GLOBAL_LIMIT = 40

    def __init__(self, config: Config, store: Store, dispatcher: Dispatcher, tokens: TokenManager, smtp: SMTPServer, vault: Vault, additional: AdditionalSender | None = None):
        self.config = config
        self.store = store
        self.dispatcher = dispatcher
        self.tokens = tokens
        self.smtp = smtp
        self.vault = vault
        self.additional = additional
        self.admin = vault.read("admin")
        self.sessions: dict[str, AdminSession] = {}
        self.attempts: dict[str, deque] = defaultdict(deque)
        self.login_lock = asyncio.Semaphore(2)
        self.prefix = urlsplit(config.web.base_url).path.rstrip("/")
        path = lambda suffix: self.prefix + suffix
        self.app = web.Application(middlewares=[self.boundary], client_max_size=32_768)
        self.app.add_routes([
            web.get(path("/"), self.index), web.get(path("/app.js"), self.script), web.get(path("/style.css"), self.style),
            web.post(path("/api/login"), self.login), web.post(path("/api/logout"), self.logout), web.get(path("/api/session"), self.session_info),
            web.get(path("/api/stats"), self.stats), web.get(path("/api/messages"), self.messages),
            web.get(path("/api/messages/{id}/eml"), self.export), web.post(path("/api/messages/{id}/{action}"), self.action),
            web.post(path("/api/control"), self.control), web.post(path("/api/account/login"), self.begin_login),
            web.post(path("/api/account/complete"), self.complete_login), web.post(path("/api/account/disconnect"), self.disconnect),
            web.post(path("/api/additional/login"), self.begin_additional_login), web.post(path("/api/additional/sender"), self.choose_additional_sender),
            web.post(path("/api/additional/disconnect"), self.disconnect_additional),
            web.get(path("/oauth/callback"), self.callback), web.get(path("/health/live"), self.live), web.get(path("/health/ready"), self.ready),
        ])
        if self.prefix:
            self.app.router.add_get(self.prefix, self.redirect_index)

    @property
    def origin(self) -> str:
        base = urlsplit(self.config.web.base_url)
        return f"{base.scheme}://{base.netloc}"

    def client_key(self, request: web.Request) -> str:
        peer = request.remote or "unknown"
        if peer not in self.config.web.trusted_proxy_ips:
            return peer
        forwarded = request.headers.get("X-Forwarded-For", "")
        if not forwarded:
            return peer
        try:
            return str(ipaddress.ip_address(forwarded.rsplit(",", 1)[-1].strip()))
        except ValueError:
            raise web.HTTPBadRequest(text="Invalid forwarded client address")

    @web.middleware
    async def boundary(self, request: web.Request, handler):
        try:
            if request.host.casefold() != urlsplit(self.config.web.base_url).netloc.casefold():
                raise web.HTTPBadRequest(text="Unexpected Host header")
            if self.prefix and request.path != self.prefix and not request.path.startswith(self.prefix + "/"):
                raise web.HTTPNotFound(text="Unknown administration path")
            now = time.time()
            self.sessions = {key: session for key, session in self.sessions.items() if session.expires > now}
            session = self.sessions.get(request.cookies.get("gateway_session", ""))
            public = {self.prefix + path for path in ("/", "/app.js", "/style.css", "/api/login", "/health/live", "/health/ready")}
            if self.prefix:
                public.add(self.prefix)
            if request.path not in public and session is None:
                return self._secure(web.json_response({"error": "Administration login required"}, status=401))
            if request.method not in {"GET", "HEAD"}:
                if request.headers.get("Origin") != self.origin:
                    raise web.HTTPForbidden(text="Same-origin request required")
                if request.content_type != "application/json":
                    raise web.HTTPUnsupportedMediaType(text="Use application/json")
                if request.path != self.prefix + "/api/login" and (session is None or hmac.compare_digest(request.headers.get("X-CSRF-Token", ""), session.csrf) is False):
                    raise web.HTTPForbidden(text="CSRF validation failed")
            request["admin_session"] = session
            response = await handler(request)
        except web.HTTPException as exc:
            response = web.json_response({"error": exc.text or exc.reason}, status=exc.status)
        except (ValueError, AuthenticationRequired) as exc:
            response = web.json_response({"error": str(exc)}, status=400)
        except Retryable as exc:
            response = web.json_response({"error": str(exc)}, status=503)
        except Exception:
            response = web.json_response({"error": "Administrative operation failed; check local service health"}, status=500)
        return self._secure(response)

    def _secure(self, response: web.StreamResponse) -> web.StreamResponse:
        response.headers.update({
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "X-Frame-Options": "DENY",
            "X-Robots-Tag": "noindex, nofollow, noarchive",
        })
        if self.config.web.base_url.startswith("https://"):
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    async def index(self, request):
        return web.Response(body=files("noreply_gateway").joinpath("static/index.html").read_bytes(), content_type="text/html")

    async def redirect_index(self, request):
        return web.Response(status=308, headers={"Location": self.prefix + "/"})

    async def script(self, request):
        return web.Response(body=files("noreply_gateway").joinpath("static/app.js").read_bytes(), content_type="application/javascript")

    async def style(self, request):
        return web.Response(body=files("noreply_gateway").joinpath("static/style.css").read_bytes(), content_type="text/css")

    async def login(self, request):
        now = time.time()
        # Only configured proxy peers may supply the appended client address.
        for key in list(self.attempts):
            while self.attempts[key] and self.attempts[key][0] < now - self.LOGIN_WINDOW_SECONDS:
                self.attempts[key].popleft()
            if not self.attempts[key]:
                del self.attempts[key]
        key = self.client_key(request)
        client_limit = len(self.attempts[key]) >= self.LOGIN_PER_CLIENT_LIMIT
        global_limit = sum(map(len, self.attempts.values())) >= self.LOGIN_GLOBAL_LIMIT
        if client_limit or global_limit:
            oldest = []
            if client_limit:
                oldest.append(self.attempts[key][0])
            if global_limit:
                oldest.append(min(attempts[0] for attempts in self.attempts.values()))
            retry_after = max(1, math.ceil(self.LOGIN_WINDOW_SECONDS - (now - max(oldest))))
            return web.json_response({"error": "Too many sign-in attempts; try again later"}, status=429, headers={"Retry-After": str(retry_after)})
        self.attempts[key].append(now)
        payload = await request.json()
        password = payload.get("password", "")
        if isinstance(password, str) is False:
            raise ValueError("Password must be a string")
        async with self.login_lock:
            verified = await asyncio.to_thread(verify_password, password, self.admin)
        if verified is False:
            return web.json_response({"error": "Invalid administration password"}, status=401)
        if len(self.sessions) >= 64:
            self.sessions.pop(next(iter(self.sessions)))
        identifier = secrets.token_urlsafe(32)
        session = AdminSession(secrets.token_urlsafe(32), now + self.config.web.session_seconds)
        self.sessions[identifier] = session
        response = web.json_response({"csrf": session.csrf})
        response.set_cookie("gateway_session", identifier, httponly=True, secure=self.config.web.base_url.startswith("https://"), samesite="Lax", max_age=self.config.web.session_seconds, path=self.prefix + "/")
        return response

    async def logout(self, request):
        self.sessions.pop(request.cookies.get("gateway_session", ""), None)
        response = web.json_response({"ok": True})
        response.del_cookie("gateway_session", path=self.prefix + "/")
        return response

    async def session_info(self, request):
        return web.json_response({"csrf": request["admin_session"].csrf})

    async def stats(self, request):
        result = await self.store.stats()
        result.update({
            "account": self.tokens.status(), "sender": self.config.account.sender,
            "login_username": self.config.account.username, "backend": self.config.account.backend,
            "client_id": self.config.account.client_id, "tenant_id": self.config.account.tenant_id,
            "redirect_uri": self.config.account.redirect_uri,
            "save_in_sent": self.config.account.save_in_sent, "nat_marker": self.config.account.nat_marker,
            "recipient_policy": self.config.account.recipient_policy,
            "paused": self.dispatcher.paused, "rate": self.dispatcher.rate,
            "cooldown_remaining": max(0, self.dispatcher.cooldown_until - time.time()),
            "upstream_inflight": self.dispatcher.active,
            "smtp_connections": len(self.smtp.connections), "smtp_counters": dict(self.smtp.counters),
            "smtp_buffer_bytes": self.smtp.buffered, "smtp_port": self.smtp.port,
            "smtp_host": self.config.smtp.host, "allowed_networks": self.config.smtp.allowed_networks,
            "max_queue_messages": self.config.queue.max_messages, "max_queue_bytes": self.config.queue.max_bytes,
            "recipient_limit_24h": self.config.delivery.recipient_limit_24h,
            "additional": self.additional.status() if self.additional else None,
        })
        return web.json_response(result)

    async def messages(self, request):
        status = request.query.get("status", "")
        if status not in {"", "queued", "sending", "submitted", "retry", "failed", "uncertain", "cancelled"}:
            raise ValueError("Unknown message status")
        before = float(request.query["before"]) if "before" in request.query else None
        if before is not None and math.isfinite(before) is False:
            raise ValueError("Invalid pagination cursor")
        return web.json_response(await self.store.messages(status, before=before, before_id=request.query.get("before_id", "")))

    async def export(self, request):
        data = await self.store.export(request.match_info["id"])
        if data is None:
            raise web.HTTPNotFound(text="Message body is unavailable; submitted/cancelled bodies are removed")
        return web.Response(body=data, content_type="message/rfc822", headers={"Content-Disposition": 'attachment; filename="queued-message.eml"'})

    async def action(self, request):
        payload = await request.json()
        await self.store.action(request.match_info["id"], request.match_info["action"], payload.get("acknowledge_duplicate") is True)
        return web.json_response({"ok": True})

    async def control(self, request):
        payload = await request.json()
        if "rate" in payload:
            if isinstance(payload["rate"], bool):
                raise ValueError("Invalid rate")
            await self.dispatcher.set_rate(float(payload["rate"]))
        if "paused" in payload:
            if isinstance(payload["paused"], bool) is False:
                raise ValueError("paused must be a boolean")
            await self.dispatcher.pause(payload["paused"])
        return web.json_response({"ok": True})

    async def begin_login(self, request):
        if not self.config.account.sender:
            raise ValueError("No legacy sender is configured")
        url, flow = self.tokens.begin_login()
        request["admin_session"].flow = flow
        request["admin_session"].flow_kind = "original"
        return web.json_response({"authorization_url": url, "redirect_uri": self.config.account.redirect_uri})

    async def begin_additional_login(self, request):
        if self.additional is None:
            raise ValueError("Additional sending account is unavailable")
        url, flow = self.additional.tokens.begin_login()
        request["admin_session"].flow = flow
        request["admin_session"].flow_kind = "additional"
        return web.json_response({"authorization_url": url, "redirect_uri": self.additional.tokens.config.account.redirect_uri})

    async def _finish(self, request, response: dict):
        session = request["admin_session"]
        flow, session.flow = session.flow, None
        if flow is None:
            raise ValueError("Start a new Microsoft login in this browser session")
        if session.flow_kind == "additional":
            await self.additional.tokens.complete_login(flow, response)
            await self.additional.completed_login()
        else:
            await self.tokens.complete_login(flow, response)
        # Login does not silently undo an operator-requested pause or backoff.

    async def complete_login(self, request):
        payload = await request.json()
        url = payload.get("redirect_url", "")
        if isinstance(url, str) is False or len(url) > 24_000:
            raise ValueError("Invalid redirect URL")
        parsed = urlsplit(url)
        session = request["admin_session"]
        redirect_uri = self.additional.tokens.config.account.redirect_uri if session.flow_kind == "additional" else self.config.account.redirect_uri
        expected = urlsplit(redirect_uri)
        if (parsed.scheme, parsed.netloc, parsed.path) != (expected.scheme, expected.netloc, expected.path):
            raise ValueError("Paste the complete final URL from the configured Microsoft redirect page")
        items = parse_qsl(parsed.query, keep_blank_values=True)
        if len({key for key, value in items}) != len(items):
            raise ValueError("Duplicate OAuth callback parameters")
        await self._finish(request, dict(items))
        return web.json_response({"ok": True, "additional": self.additional.status() if self.additional else None})

    async def choose_additional_sender(self, request):
        if self.additional is None:
            raise ValueError("Additional sending account is unavailable")
        payload = await request.json()
        await self.additional.choose(payload.get("mode"), payload.get("custom_sender", ""))
        return web.json_response({"ok": True, "additional": self.additional.status()})

    async def disconnect_additional(self, request):
        if self.additional is None:
            raise ValueError("Additional sending account is unavailable")
        await self.additional.disconnect()
        return web.json_response({"ok": True, "additional": self.additional.status()})

    async def callback(self, request):
        session = request["admin_session"]
        redirect_uri = self.additional.tokens.config.account.redirect_uri if session.flow_kind == "additional" else self.config.account.redirect_uri
        if redirect_uri != self.config.web.base_url + "/oauth/callback":
            raise ValueError("This callback is not the configured redirect URI")
        if len(set(request.query)) != len(list(request.query.items())):
            raise ValueError("Duplicate OAuth callback parameters")
        await self._finish(request, dict(request.query))
        return web.Response(status=303, headers={"Location": self.prefix + "/"})

    async def disconnect(self, request):
        if not self.config.account.sender:
            raise ValueError("No legacy sender is configured")
        await self.dispatcher.pause(True)
        await self.tokens.disconnect()
        return web.json_response({"ok": True})

    async def live(self, request):
        return web.json_response({"status": "alive"})

    async def ready(self, request):
        stats = await self.store.stats()
        account = self.tokens.status()
        additional = self.additional.status() if self.additional else None
        original_ready = bool(self.config.account.sender and account["credential_present"] and not account["needs_login"])
        additional_ready = bool(additional and additional["selected_sender"] and additional["account"]["credential_present"] and not additional["account"]["needs_login"])
        ready = self.store.healthy and self.store.accepting and self.dispatcher.paused is False and (original_ready or additional_ready) and stats["counters"].get("retained_messages", 0) < self.config.queue.max_messages and stats["counters"].get("retained_bytes", 0) < self.config.queue.max_bytes and stats["disk_free_bytes"] >= self.config.queue.min_free_bytes
        return web.json_response({"status": "ready" if ready else "not_ready"}, status=200 if ready else 503)
