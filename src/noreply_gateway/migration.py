from __future__ import annotations

import base64
import re
from pathlib import Path

from .config import Config
from .oauth import account_fingerprint


def _unescape(value: str) -> str:
    def replace(match):
        text = match.group(1)
        if text.startswith("u"):
            return chr(int(text[1:], 16))
        return {"t": "\t", "r": "\r", "n": "\n", "f": "\f"}.get(text, text)
    return re.sub(r"\\(u[0-9a-fA-F]{4}|.)", replace, value)


def read_properties(path: Path) -> dict[str, str]:
    """Read Java Properties' ISO-8859-1/escaped representation, not executable code."""
    result, continuation = {}, ""
    for physical in path.read_bytes().decode("iso-8859-1").splitlines():
        line = continuation + physical.lstrip(" \t\f")
        trailing = len(line) - len(line.rstrip("\\"))
        if trailing % 2:
            continuation = line[:-1]
            continue
        continuation = ""
        if line == "" or line[0] in "#!":
            continue
        escaped, index = False, len(line)
        for n, char in enumerate(line):
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char in "=:\t \f":
                index = n
                break
        key = line[:index]
        rest = line[index:].lstrip(" \t\f")
        if rest.startswith(("=", ":")):
            rest = rest[1:].lstrip(" \t\f")
        result[_unescape(key)] = _unescape(rest)
    if continuation:
        raise ValueError("Incomplete continued Java property")
    return result


def apply_davmail(config: Config, properties: dict[str, str]) -> None:
    sender = properties.get("davmail.smtpEmbeddedUsername", "").strip()
    if sender == "":
        raise ValueError("DavMail properties have no smtpEmbeddedUsername")
    config.account.sender = sender
    config.account.backend = "ews"
    for source, target in (("davmail.oauth.tenantId", "tenant_id"), ("davmail.oauth.clientId", "client_id"), ("davmail.oauth.redirectUri", "redirect_uri"), ("davmail.url", "ews_url")):
        if properties.get(source, "").strip():
            setattr(config.account, target, properties[source].strip())
    if properties.get("davmail.smtpPort", "").strip():
        config.smtp.port = int(properties["davmail.smtpPort"])
    config.account.save_in_sent = properties.get("davmail.smtpSaveInSent", "true").lower() == "true"
    # Never import allowRemote, passwords, other protocols, or logging paths.


def imported_record(config: Config, properties: dict[str, str]) -> dict:
    if config.account.backend != "ews":
        raise ValueError("Legacy DavMail token import is EWS-only; use a fresh Graph login")
    key = "davmail.oauth." + config.account.username.lower() + ".refreshToken"
    value = properties.get(key, "")
    if value == "":
        raise ValueError("No refresh token property for the configured sending account")
    try:
        token = base64.b64decode(value, validate=True).decode("utf-8")
    except (ValueError, UnicodeError):
        raise ValueError("Refresh token is not in the legacy DavMail patch's base64 format") from None
    if token == "" or re.search(r"[\x00-\x20\x7f]", token):
        raise ValueError("Invalid decoded refresh token")
    return {"fingerprint": account_fingerprint(config), "mode": "legacy_v1", "username": config.account.username, "refresh_token": token, "last_refresh": None}
