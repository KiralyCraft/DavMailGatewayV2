from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import uuid
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit

import aiohttp
import jwt

from .config import Config
from .http import ResponseTooLarge, read_limited
from .errors import AuthenticationRequired, Retryable
from .security import Vault


@dataclass
class LoginFlow:
    state: str
    nonce: str
    verifier: str
    expires: float


def account_fingerprint(config: Config) -> str:
    values = (config.account.sender.casefold(), config.account.username.casefold(), config.account.backend, config.account.tenant_id, config.account.client_id, config.account.redirect_uri)
    if config.account.send_shared:
        values += ("Mail.Send.Shared",)
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


class TokenManager:
    """One mailbox, serialized refresh, encrypted rotating credentials.

    Imported credentials retain DavMail's v1 resource-based refresh request.
    New browser logins use authorization code + S256 PKCE + state + nonce and
    cryptographically verified ID tokens. No Microsoft password is collected.
    """

    def __init__(self, config: Config, vault: Vault, session: aiohttp.ClientSession, *, record_name: str = "account", allow_any_username: bool = False):
        self.config = config
        self.vault = vault
        self.session = session
        self.record_name = record_name
        self.allow_any_username = allow_any_username
        self.lock = asyncio.Lock()
        self.record = vault.read(record_name)
        self.access_token = ""
        self.expires = 0.0
        self.rejections = 0
        self.rejected_digest = ""
        self.last_error = ""
        self.needs_login = False
        self.metadata = None
        self.jwks = None
        if self.record and self.record.get("fingerprint") != account_fingerprint(config):
            self.record = {}
            self.last_error = "Account configuration changed; sign in again"

    def status(self) -> dict:
        return {
            "credential_present": bool(self.record.get("refresh_token")),
            "needs_login": self.needs_login or self.record.get("refresh_token") is None,
            "username": self.record.get("username", self.config.account.username),
            "credential_origin": self.record.get("mode", "none"),
            "last_refresh": self.record.get("last_refresh"),
            "last_error": self.last_error,
            "access_expires_at": self.expires or None,
        }

    def begin_login(self) -> tuple[str, LoginFlow]:
        flow = LoginFlow(secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(64), time.time() + 600)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(flow.verifier.encode()).digest()).rstrip(b"=").decode()
        parameters = {
            "client_id": self.config.account.client_id,
            "response_type": "code",
            "redirect_uri": self.config.account.redirect_uri,
            "response_mode": "query",
            "scope": self.config.account.scope,
            "state": flow.state,
            "nonce": flow.nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "prompt": "select_account",
        }
        if not self.allow_any_username:
            parameters["login_hint"] = self.config.account.username
        url = "https://login.microsoftonline.com/" + self.config.account.tenant_id + "/oauth2/v2.0/authorize?" + urlencode(parameters)
        return url, flow

    async def _read_json(self, url: str, data: dict | None = None) -> dict:
        try:
            async with self.session.request("POST" if data is not None else "GET", url, data=data, allow_redirects=False) as response:
                try:
                    content = await read_limited(response.content)
                except ResponseTooLarge:
                    raise AuthenticationRequired("Oversized identity provider response")
                if response.status in (429, 500, 502, 503, 504):
                    raise Retryable("Identity provider temporarily unavailable", delay=30, global_cooldown=True)
                try:
                    result = json.loads(content)
                except (ValueError, UnicodeError):
                    raise AuthenticationRequired("Invalid identity provider response") from None
                if isinstance(result, dict) is False:
                    raise AuthenticationRequired("Invalid identity provider response")
                if response.status >= 300 or "error" in result:
                    code = re.sub(r"[^A-Za-z0-9_.-]", "", str(result.get("error", "identity_request_failed")))[:100]
                    raise AuthenticationRequired("Microsoft login: " + code)
                return result
        except (aiohttp.ClientError, asyncio.TimeoutError):
            raise Retryable("Identity provider connection failed", delay=30, global_cooldown=True) from None

    async def _verify_id_token(self, token: str, nonce: str) -> dict:
        if self.metadata is None:
            self.metadata = await self._read_json("https://login.microsoftonline.com/" + self.config.account.tenant_id + "/v2.0/.well-known/openid-configuration")
        jwks_url = self.metadata.get("jwks_uri", "")
        parsed = urlsplit(jwks_url)
        if parsed.scheme != "https" or parsed.hostname != "login.microsoftonline.com" or parsed.username is not None:
            raise AuthenticationRequired("Unexpected identity provider key endpoint")
        try:
            header = jwt.get_unverified_header(token)
            unverified = jwt.decode(token, options={"verify_signature": False})
            tenant = str(uuid.UUID(unverified["tid"]))
            if header.get("alg") != "RS256":
                raise ValueError("Unexpected algorithm")
            issuer = self.metadata["issuer"].replace("{tenantid}", tenant)
            for attempt in range(2):
                if self.jwks is None or attempt == 1:
                    self.jwks = await self._read_json(jwks_url)
                matching = [key for key in self.jwks.get("keys", []) if key.get("kid") == header.get("kid") and key.get("use", "sig") == "sig" and key.get("kty") == "RSA"]
                if matching:
                    break
            if len(matching) != 1:
                raise ValueError("Unknown signing key")
            key = jwt.PyJWK.from_dict(matching[0], algorithm="RS256").key
            claims = jwt.decode(token, key, algorithms=["RS256"], audience=self.config.account.client_id, issuer=issuer, leeway=60, options={"require": ["exp", "iat", "iss", "aud", "sub", "tid", "nonce"]})
            if hmac.compare_digest(str(claims["nonce"]), nonce) is False:
                raise ValueError("Nonce mismatch")
            username = claims.get("preferred_username") or claims.get("upn") or claims.get("unique_name")
            if isinstance(username, str) is False or (not self.allow_any_username and username.casefold() != self.config.account.username.casefold()):
                raise ValueError("The signed-in account does not match account.login_username")
            return claims
        except (jwt.PyJWTError, KeyError, TypeError, ValueError):
            raise AuthenticationRequired("ID token validation failed, or the signed-in account does not match the configured login username") from None

    async def _profile_address(self, access_token: str) -> str:
        from .message import mailbox
        try:
            async with self.session.get("https://graph.microsoft.com/v1.0/me?$select=mail,userPrincipalName", headers={"Authorization": "Bearer " + access_token}, allow_redirects=False) as response:
                content = await read_limited(response.content)
                if response.status != 200:
                    raise AuthenticationRequired("Could not read the signed-in Microsoft address")
                profile = json.loads(content)
                address = profile.get("mail") or profile.get("userPrincipalName")
                return mailbox(address)
        except (aiohttp.ClientError, asyncio.TimeoutError, ResponseTooLarge, ValueError, TypeError, AttributeError):
            raise AuthenticationRequired("Could not read a valid address from the signed-in Microsoft account") from None

    async def complete_login(self, flow: LoginFlow, response: dict[str, str]) -> str:
        if flow.expires < time.time() or hmac.compare_digest(response.get("state", ""), flow.state) is False:
            raise AuthenticationRequired("Expired login or OAuth state mismatch")
        if "error" in response or not response.get("code"):
            raise AuthenticationRequired("Microsoft login was not completed")
        async with self.lock:
            data = {
                "client_id": self.config.account.client_id,
                "grant_type": "authorization_code",
                "code": response["code"],
                "redirect_uri": self.config.account.redirect_uri,
                "scope": self.config.account.scope,
                "code_verifier": flow.verifier,
            }
            self._add_client_secret(data)
            result = await self._read_json("https://login.microsoftonline.com/" + self.config.account.tenant_id + "/oauth2/v2.0/token", data)
            claims = await self._verify_id_token(result.get("id_token", ""), flow.nonce)
            if not result.get("refresh_token"):
                raise AuthenticationRequired("No refresh token returned; offline_access consent is required")
            address = await self._profile_address(result["access_token"]) if self.allow_any_username else self.config.account.username
            await self._save_result(result, "v2", username=claims.get("preferred_username") or claims.get("upn") or claims.get("unique_name"), default_sender=address)
            self.needs_login = False
            self.last_error = ""
            self.rejections = 0
            self.rejected_digest = ""
            return address

    def _add_client_secret(self, data: dict) -> None:
        variable = self.config.account.client_secret_env
        if variable:
            secret = os.environ.get(variable)
            if not secret:
                raise AuthenticationRequired("Configured OAuth client-secret environment variable is missing")
            data["client_secret"] = secret

    async def _save_result(self, result: dict, mode: str, *, username: str | None = None, default_sender: str | None = None) -> None:
        token = result.get("access_token")
        if isinstance(token, str) is False or token == "" or str(result.get("token_type", "Bearer")).casefold() != "bearer":
            raise AuthenticationRequired("Token endpoint did not return a bearer access token")
        record = {
            "fingerprint": account_fingerprint(self.config),
            "mode": mode,
            "username": username or self.record.get("username", self.config.account.username),
            "default_sender": default_sender or self.record.get("default_sender", self.config.account.sender),
            "refresh_token": result.get("refresh_token", self.record.get("refresh_token")),
            "last_refresh": time.time(),
        }
        if not record["refresh_token"]:
            raise AuthenticationRequired("No refresh credential is available")
        # Retain a rotated token in memory even if the filesystem temporarily
        # fails. Never submit mail until credential persistence succeeds.
        self.record = record
        try:
            await asyncio.to_thread(self.vault.write, self.record_name, record)
        except OSError:
            raise Retryable("Cannot persist renewed account credentials", delay=30, global_cooldown=True) from None
        self.access_token = token
        self.expires = time.time() + min(max(float(result.get("expires_in", 3600)), 1), 86400)

    async def get_token(self) -> str:
        async with self.lock:
            if self.needs_login or not self.record.get("refresh_token"):
                raise AuthenticationRequired(self.last_error or "Sign in to the configured Microsoft account")
            if self.access_token and self.expires > time.time() + 60:
                return self.access_token
            mode = self.record.get("mode", "v2")
            data = {"client_id": self.config.account.client_id, "grant_type": "refresh_token", "refresh_token": self.record["refresh_token"]}
            if mode == "legacy_v1":
                data.update(resource="https://outlook.office365.com/", redirect_uri=self.config.account.redirect_uri)
                path = "/oauth2/token"
            else:
                data["scope"] = self.config.account.scope
                path = "/oauth2/v2.0/token"
            self._add_client_secret(data)
            try:
                result = await self._read_json("https://login.microsoftonline.com/" + self.config.account.tenant_id + path, data)
                await self._save_result(result, mode)
            except AuthenticationRequired as exc:
                self.last_error = str(exc)
                self.needs_login = True
                raise
            return self.access_token

    async def rejected_token(self, rejected: str) -> None:
        async with self.lock:
            # Concurrent requests can all reject the same stale token. Count
            # rejected credential generations, not HTTP responses.
            if self.access_token and self.access_token != rejected:
                raise Retryable("An older access token was rejected", delay=3, global_cooldown=True)
            digest = hashlib.sha256(rejected.encode()).hexdigest()
            if digest != self.rejected_digest:
                self.rejected_digest = digest
                self.rejections += 1
            self.access_token = ""
            self.expires = 0
            if self.rejections > 1:
                self.needs_login = True
                self.last_error = "Microsoft rejected renewed credentials; sign in again"
                raise AuthenticationRequired(self.last_error)
        raise Retryable("Microsoft requested a fresh access token", delay=3, global_cooldown=True)

    def accepted_token(self) -> None:
        self.rejections = 0
        self.rejected_digest = ""

    async def disconnect(self) -> None:
        async with self.lock:
            await asyncio.to_thread(self.vault.write, self.record_name, {})
            self.record = {}
            self.access_token = ""
            self.expires = 0
            self.needs_login = True
            self.last_error = "Account disconnected locally"
