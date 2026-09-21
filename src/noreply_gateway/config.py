from __future__ import annotations

import ipaddress
import math
import re
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from urllib.parse import urlsplit


@dataclass
class SMTPConfig:
    host: str = "127.0.0.1"
    port: int = 1025
    hostname: str = "noreply-gateway.local"
    allowed_networks: list[str] = field(default_factory=lambda: ["127.0.0.0/8", "::1/128"])
    max_connections: int = 128
    max_message_bytes: int = 2_000_000
    max_recipients: int = 500
    max_header_bytes: int = 131_072
    max_buffer_bytes: int = 64 * 1024 * 1024
    command_timeout: float = 60.0
    data_timeout: float = 120.0
    tls_cert: str = ""
    tls_key: str = ""


@dataclass
class WebConfig:
    host: str = "127.0.0.1"
    port: int = 8080
    base_url: str = "http://127.0.0.1:8080"
    session_seconds: int = 28_800
    trusted_proxy_ips: list[str] = field(default_factory=list)


@dataclass
class AccountConfig:
    sender: str = "noreply@example.org"
    login_username: str = ""
    backend: str = "ews"
    tenant_id: str = "common"
    client_id: str = "facd6cff-a294-4415-b59f-c5b01937d7bd"
    redirect_uri: str = "https://login.microsoftonline.com/common/oauth2/nativeclient"
    ews_url: str = "https://outlook.office365.com/EWS/Exchange.asmx"
    save_in_sent: bool = True
    client_secret_env: str = ""
    nat_marker: str = "_NAT_"
    recipient_policy: str = "davmail_union"
    allowed_recipient_domains: list[str] = field(default_factory=list)

    @property
    def username(self) -> str:
        return self.login_username or self.sender

    @property
    def scope(self) -> str:
        resource_scope = "https://graph.microsoft.com/Mail.Send" if self.backend == "graph" else "https://outlook.office365.com/EWS.AccessAsUser.All"
        return "openid profile offline_access " + resource_scope


@dataclass
class QueueConfig:
    max_messages: int = 100_000
    max_bytes: int = 5 * 1024**3
    min_free_bytes: int = 256 * 1024**2
    batch_size: int = 128
    batch_seconds: float = 0.005
    pending_submissions: int = 1024
    history_days: int = 7


@dataclass
class DeliveryConfig:
    workers: int = 4
    messages_per_second: float = 0.5
    recipient_limit_24h: int = 10_000
    max_attempts: int = 20
    max_age_hours: float = 72.0
    request_timeout: float = 60.0
    connect_timeout: float = 10.0
    retry_base_seconds: float = 15.0
    retry_max_seconds: float = 3600.0
    shutdown_seconds: float = 70.0


@dataclass
class Config:
    data_dir: Path = Path("./state")
    smtp: SMTPConfig = field(default_factory=SMTPConfig)
    web: WebConfig = field(default_factory=WebConfig)
    account: AccountConfig = field(default_factory=AccountConfig)
    queue: QueueConfig = field(default_factory=QueueConfig)
    delivery: DeliveryConfig = field(default_factory=DeliveryConfig)

    def validate(self) -> None:
        from .message import mailbox
        mailbox(self.account.sender)
        mailbox(self.account.username)
        if self.account.backend not in {"ews", "graph"}:
            raise ValueError("account.backend must be ews or graph")
        if self.account.recipient_policy not in {"davmail_union", "envelope_strict"}:
            raise ValueError("Invalid recipient_policy")
        if self.account.nat_marker == "" or re.search(r"[\r\n\x00]", self.account.nat_marker):
            raise ValueError("Invalid NAT marker")
        if re.fullmatch(r"[A-Za-z0-9.-]+", self.smtp.hostname) is None:
            raise ValueError("Invalid SMTP hostname")
        if re.fullmatch(r"[A-Za-z0-9.-]+", self.account.tenant_id) is None:
            raise ValueError("Invalid tenant_id")
        if re.fullmatch(r"[0-9a-fA-F-]{36}", self.account.client_id) is None:
            raise ValueError("client_id must be an application UUID")
        networks = [ipaddress.ip_network(x) for x in self.smtp.allowed_networks]
        if len(networks) == 0 or any(x.prefixlen == 0 for x in networks):
            raise ValueError("Explicit, non-world SMTP client CIDRs are required")
        for domain in self.account.allowed_recipient_domains:
            if re.fullmatch(r"[A-Za-z0-9.-]+", domain) is None:
                raise ValueError("Recipient domains must be plain DNS names")
        for port in (self.smtp.port, self.web.port):
            if not 1 <= port <= 65535:
                raise ValueError("Ports must be in 1..65535")
        for value in (self.smtp.max_connections, self.smtp.max_message_bytes, self.smtp.max_recipients, self.smtp.max_header_bytes, self.smtp.max_buffer_bytes, self.queue.max_messages, self.queue.max_bytes, self.queue.batch_size, self.queue.pending_submissions, self.queue.history_days, self.delivery.workers, self.delivery.max_attempts, self.web.session_seconds):
            if isinstance(value, bool) or value <= 0:
                raise ValueError("Capacity and count values must be positive")
        for value in (self.smtp.command_timeout, self.smtp.data_timeout, self.queue.batch_seconds, self.delivery.messages_per_second, self.delivery.max_age_hours, self.delivery.request_timeout, self.delivery.connect_timeout, self.delivery.retry_base_seconds, self.delivery.retry_max_seconds, self.delivery.shutdown_seconds):
            if isinstance(value, bool) or math.isfinite(value) is False or value <= 0:
                raise ValueError("Rates and timeouts must be finite and positive")
        if self.delivery.recipient_limit_24h < 0 or self.queue.min_free_bytes < 0:
            raise ValueError("Quota/minimum free space cannot be negative")
        if self.delivery.workers > 4:
            raise ValueError("This single-mailbox gateway allows at most four upstream workers")
        if self.account.backend == "graph" and (self.account.save_in_sent is False or self.smtp.max_message_bytes > 2_500_000):
            raise ValueError("Graph MIME mode requires save_in_sent=true and max_message_bytes<=2500000")
        if bool(self.smtp.tls_cert) != bool(self.smtp.tls_key):
            raise ValueError("Supply both SMTP TLS certificate and key")
        endpoint = urlsplit(self.account.ews_url)
        if endpoint.scheme != "https" or endpoint.hostname is None or endpoint.username is not None:
            raise ValueError("The EWS endpoint must be HTTPS without embedded credentials")
        base = urlsplit(self.web.base_url)
        path = base.path.rstrip("/")
        if base.scheme not in {"http", "https"} or base.hostname is None or base.query or base.fragment or base.username or base.password or base.path.endswith("//") or (path and re.fullmatch(r"/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", path) is None):
            raise ValueError("web.base_url must be an HTTP(S) URL with an optional simple path prefix")
        if base.scheme == "http" and base.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Remote WebUI requires HTTPS through a reverse proxy")
        if not isinstance(self.web.trusted_proxy_ips, list) or any(not isinstance(value, str) for value in self.web.trusted_proxy_ips):
            raise ValueError("web.trusted_proxy_ips must be a list of IP addresses")
        for value in self.web.trusted_proxy_ips:
            ipaddress.ip_address(value)
        redirect = urlsplit(self.account.redirect_uri)
        if redirect.scheme not in {"https", "http"} or redirect.hostname is None or redirect.query or redirect.fragment or redirect.username:
            raise ValueError("Invalid OAuth redirect URI")
        if redirect.scheme == "http" and redirect.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Plain HTTP OAuth callbacks must be loopback")
        self.web.base_url = self.web.base_url.rstrip("/")


def load_config(path: Path) -> Config:
    raw = tomllib.loads(path.read_text())
    unknown = set(raw) - {"data_dir", "smtp", "web", "account", "queue", "delivery"}
    if unknown:
        raise ValueError("Unknown configuration keys: " + ", ".join(sorted(unknown)))
    result = Config(data_dir=Path(raw.get("data_dir", "./state")).expanduser())
    if result.data_dir.is_absolute() is False:
        result.data_dir = (path.resolve().parent / result.data_dir).resolve()
    for name, cls in (("smtp", SMTPConfig), ("web", WebConfig), ("account", AccountConfig), ("queue", QueueConfig), ("delivery", DeliveryConfig)):
        table = raw.get(name, {})
        unknown = set(table) - {f.name for f in fields(cls)}
        if unknown:
            raise ValueError(f"Unknown [{name}] keys: " + ", ".join(sorted(unknown)))
        setattr(result, name, cls(**table))
    result.validate()
    return result
