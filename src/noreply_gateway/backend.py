from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from email.parser import BytesHeaderParser
from email.utils import parsedate_to_datetime

import aiohttp
from defusedxml import ElementTree

from .config import Config
from .http import ResponseTooLarge, read_limited
from .errors import AuthenticationRequired, Permanent, Retryable, Uncertain
from .oauth import TokenManager


def retry_after(value: str | None) -> float:
    if value is None:
        return 0.0
    try:
        delay = float(value)
        return delay if 0 <= delay < float("inf") else 0.0
    except ValueError:
        try:
            return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
        except (ValueError, TypeError, OverflowError):
            return 0.0


def ews_request(mime: bytes, save_in_sent: bool) -> bytes:
    disposition = "SendAndSaveCopy" if save_in_sent else "SendOnly"
    folder = '<m:SavedItemFolderId><t:DistinguishedFolderId Id="sentitems"/></m:SavedItemFolderId>' if save_in_sent else ""
    header = BytesHeaderParser().parsebytes(mime.partition(b"\r\n\r\n")[0] + b"\r\n\r\n")
    item_class = "<t:ItemClass>REPORT.IPM.Note.IPNRN</t:ItemClass>" if header.get_content_type() == "multipart/report" else ""
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        'xmlns:m="http://schemas.microsoft.com/exchange/services/2006/messages" '
        'xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types">'
        '<s:Header><t:RequestServerVersion Version="Exchange2013_SP1"/></s:Header>'
        '<s:Body><m:CreateItem MessageDisposition="' + disposition + '">' + folder +
        '<m:Items><t:Message><t:MimeContent CharacterSet="UTF-8">' + base64.b64encode(mime).decode("ascii") +
        '</t:MimeContent>' + item_class + '</t:Message></m:Items></m:CreateItem></s:Body></s:Envelope>'
    ).encode("utf-8")


class MicrosoftBackend:
    def __init__(self, config: Config, tokens: TokenManager, session: aiohttp.ClientSession):
        self.config = config
        self.tokens = tokens
        self.session = session
        self.endpoint = "https://graph.microsoft.com/v1.0/me/sendMail" if config.account.backend == "graph" else config.account.ews_url

    async def send(self, message: dict) -> None:
        token = await self.tokens.get_token()
        graph = self.config.account.backend == "graph"
        payload = base64.b64encode(message["mime"]) if graph else ews_request(message["mime"], self.config.account.save_in_sent)
        headers = {
            "Authorization": "Bearer " + token,
            "Content-Type": "text/plain" if graph else "text/xml; charset=utf-8",
            "X-AnchorMailbox": self.config.account.sender,
            "User-Agent": "noreply-mcs-gateway/0.1.0",
            "client-request-id": message["id"],
            "return-client-request-id": "true",
        }
        if graph is False:
            headers["SOAPAction"] = '"http://schemas.microsoft.com/exchange/services/2006/messages/CreateItem"'
        try:
            async with self.session.post(self.endpoint, data=payload, headers=headers, allow_redirects=False) as response:
                try:
                    content = await read_limited(response.content)
                except ResponseTooLarge:
                    raise Uncertain("Oversized upstream response; submission outcome unknown")
                delay = retry_after(response.headers.get("Retry-After"))
                if response.status == 401:
                    await self.tokens.rejected_token(token)
                if response.status == 403:
                    raise AuthenticationRequired("Upstream access denied; verify consent, account permissions, and service availability")
                if response.status == 429:
                    raise Retryable("Upstream HTTP 429 throttling", delay=delay, global_cooldown=True)
                if graph and response.status == 202:
                    self.tokens.accepted_token()
                    return
                if graph is False and content.lstrip().startswith(b"<"):
                    self._interpret_ews(response.status, content, delay)
                    self.tokens.accepted_token()
                    return
                if response.status >= 500:
                    raise Uncertain(f"Upstream HTTP {response.status}; submission outcome unknown")
                if 300 <= response.status < 500:
                    code = ""
                    if graph:
                        try:
                            code = str(json.loads(content).get("error", {}).get("code", ""))
                        except (ValueError, AttributeError):
                            pass
                    code = re.sub(r"[^A-Za-z0-9_.-]", "", code)[:100]
                    raise Permanent(f"Upstream HTTP {response.status} {code}".strip())
                raise Uncertain("Unexpected upstream response; submission outcome unknown")
        except (aiohttp.ClientConnectorError, aiohttp.ConnectionTimeoutError):
            raise Retryable("Upstream connection could not be established") from None
        except (aiohttp.ClientError, asyncio.TimeoutError):
            # A timeout or lost response after a POST is not proof of rejection.
            raise Uncertain("Connection interrupted during submission; review before retry") from None

    def _interpret_ews(self, status: int, content: bytes, delay: float) -> None:
        try:
            root = ElementTree.fromstring(content)
        except Exception:
            raise Uncertain("Unparseable EWS response; submission outcome unknown") from None
        messages = root.findall(".//{http://schemas.microsoft.com/exchange/services/2006/messages}CreateItemResponseMessage")
        if len(messages) != 1:
            raise Uncertain("Unexpected EWS response structure; submission outcome unknown")
        item = messages[0]
        code = item.findtext("{http://schemas.microsoft.com/exchange/services/2006/messages}ResponseCode", "")
        if status == 200 and item.get("ResponseClass") == "Success" and code == "NoError":
            return
        for value in root.iter():
            if value.get("Name") == "BackOffMilliseconds":
                try:
                    delay = max(delay, float(value.text or "0") / 1000)
                except ValueError:
                    pass
        safe_code = re.sub(r"[^A-Za-z0-9_]", "", code)[:100]
        if code in {"ErrorServerBusy", "ErrorExceededConnectionCount", "ErrorMailboxStoreUnavailable", "ErrorInsufficientResources", "ErrorInternalServerTransientError"}:
            raise Retryable("EWS " + safe_code, delay=delay, global_cooldown=True)
        if code in {"ErrorAccessDenied", "ErrorInvalidClientSecurityContext", "ErrorInvalidUserSid"}:
            raise AuthenticationRequired("EWS " + safe_code)
        if code in {"ErrorQuotaExceeded", "ErrorMessageSubmissionBlocked", "ErrorSendQuotaExceeded"}:
            raise Retryable("EWS " + safe_code, delay=max(delay, 3600), global_cooldown=True)
        if code in {"ErrorTimeoutExpired", "ErrorInternalServerError", ""}:
            raise Uncertain("EWS " + (safe_code or "unknown error") + "; review before retry")
        raise Permanent("EWS " + safe_code)
