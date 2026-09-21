import asyncio
import base64
from contextlib import asynccontextmanager

import aiohttp
import pytest
from aiohttp import web
from defusedxml import ElementTree

from noreply_gateway.backend import MicrosoftBackend, ews_request, retry_after
from noreply_gateway.delivery import Dispatcher
from noreply_gateway.errors import AuthenticationRequired, Permanent, Retryable, Uncertain


def ews_response(code="NoError", backoff=None):
    extra = f'<m:MessageXml><t:Value Name="BackOffMilliseconds">{backoff}</t:Value></m:MessageXml>' if backoff is not None else ""
    return ('<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" xmlns:m="http://schemas.microsoft.com/exchange/services/2006/messages" xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types"><s:Body><m:CreateItemResponse><m:ResponseMessages><m:CreateItemResponseMessage ResponseClass="' + ("Success" if code == "NoError" else "Error") + '"><m:ResponseCode>' + code + '</m:ResponseCode>' + extra + '</m:CreateItemResponseMessage></m:ResponseMessages></m:CreateItemResponse></s:Body></s:Envelope>').encode()


class FakeTokens:
    def __init__(self):
        self.accepted = 0

    async def get_token(self):
        return "OFFLINE-TEST-TOKEN"

    async def rejected_token(self, rejected):
        raise Retryable("refresh", delay=3, global_cooldown=True)

    def accepted_token(self):
        self.accepted += 1


@asynccontextmanager
async def endpoint(handler):
    app = web.Application(client_max_size=5 * 1024 * 1024)
    app.router.add_post("/send", handler)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        yield "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1]) + "/send"
    finally:
        await runner.cleanup()


@pytest.mark.parametrize("backend", ["ews", "graph"])
async def test_live_loopback_mime_submission(config, message, backend):
    config.account.backend = backend
    prepared = message(sender="_NAT_source@example.test", recipients=["secret@example.test"])
    async def handler(request):
        assert request.headers["Authorization"] == "Bearer OFFLINE-TEST-TOKEN"
        assert request.headers["client-request-id"] == prepared.id
        payload = await request.read()
        if backend == "graph":
            assert request.content_type == "text/plain"
            decoded = base64.b64decode(payload, validate=True)
            return_code, body = 202, b""
        else:
            parsed = ElementTree.fromstring(payload)
            decoded = base64.b64decode(parsed.findtext(".//{http://schemas.microsoft.com/exchange/services/2006/types}MimeContent"))
            assert b'MessageDisposition="SendAndSaveCopy"' in payload
            return_code, body = 200, ews_response()
        assert decoded == prepared.mime
        return web.Response(status=return_code, body=body)
    async with endpoint(handler) as url, aiohttp.ClientSession() as session:
        tokens = FakeTokens()
        instance = MicrosoftBackend(config, tokens, session)
        instance.endpoint = url
        await instance.send({"id": prepared.id, "mime": prepared.mime})
        assert tokens.accepted == 1


@pytest.mark.parametrize("code,error,headers", [(429, Retryable, {"Retry-After": "7200"}), (500, Uncertain, {}), (403, AuthenticationRequired, {}), (401, Retryable, {}), (400, Permanent, {})])
async def test_http_error_classification(config, message, code, error, headers):
    config.account.backend = "graph"
    async def handler(request):
        await request.read()
        return web.Response(status=code, body=b'{"error":{"code":"TestError"}}', headers=headers)
    async with endpoint(handler) as url, aiohttp.ClientSession() as session:
        instance = MicrosoftBackend(config, FakeTokens(), session)
        instance.endpoint = url
        m = message()
        with pytest.raises(error) as exc:
            await instance.send({"id": m.id, "mime": m.mime})
        if code == 429:
            assert exc.value.delay == 7200
            assert exc.value.global_cooldown


@pytest.mark.parametrize("code,error", [("ErrorServerBusy", Retryable), ("ErrorSendQuotaExceeded", Retryable), ("ErrorInvalidRecipients", Permanent), ("ErrorAccessDenied", AuthenticationRequired), ("ErrorTimeoutExpired", Uncertain)])
def test_ews_error_codes(config, code, error):
    instance = MicrosoftBackend(config, FakeTokens(), None)
    with pytest.raises(error) as exc:
        instance._interpret_ews(200, ews_response(code, 9000), 0)
    if error is Retryable:
        assert exc.value.delay >= 9


async def test_chunked_ews_response_read_in_full(config, message):
    async def handler(request):
        await request.read()
        response = web.StreamResponse(status=200, headers={"Content-Type": "text/xml"})
        await response.prepare(request)
        for chunk in (ews_response()[:100], ews_response()[100:]):
            await response.write(chunk)
            await asyncio.sleep(.005)
        await response.write_eof()
        return response
    async with endpoint(handler) as url, aiohttp.ClientSession() as session:
        instance = MicrosoftBackend(config, FakeTokens(), session)
        instance.endpoint = url
        m = message()
        await instance.send({"id": m.id, "mime": m.mime})


async def test_lost_response_is_uncertain(config, message):
    async def handler(request):
        await request.read()
        request.transport.close()
        return web.Response(status=202)
    config.account.backend = "graph"
    async with endpoint(handler) as url, aiohttp.ClientSession() as session:
        instance = MicrosoftBackend(config, FakeTokens(), session)
        instance.endpoint = url
        m = message()
        with pytest.raises(Uncertain):
            await instance.send({"id": m.id, "mime": m.mime})


def test_ews_send_only_and_report_type():
    result = ews_request(b"Content-Type: multipart/report; report-type=disposition-notification\r\n\r\nx\r\n", False)
    assert b'MessageDisposition="SendOnly"' in result
    assert b"SavedItemFolderId" not in result
    assert b"REPORT.IPM.Note.IPNRN" in result


@pytest.mark.parametrize("value,expected", [(None, 0), ("garbage", 0), ("inf", 0), ("NaN", 0), ("-4", 0), ("100", 100)])
def test_retry_after_numeric(value, expected):
    assert retry_after(value) == expected


@pytest.mark.parametrize("error,status,paused", [(Retryable("busy", delay=7200, global_cooldown=True), "retry", False), (Permanent("bad recipients"), "failed", False), (Uncertain("lost reply"), "uncertain", False), (AuthenticationRequired("login"), "retry", True)])
async def test_dispatcher_records_outcomes_and_backoff(config, store, message, error, status, paused):
    class Backend:
        async def send(self, item):
            raise error
    await store.submit(message())
    attempt = await store.claim()
    instance = Dispatcher(config, store, Backend())
    await instance._attempt(attempt)
    row = (await store.messages())[0]
    assert row["status"] == status
    assert instance.paused == paused
    if isinstance(error, Retryable):
        assert row["next_attempt"] >= row["updated"] + 7200
        assert float((await store.settings())["cooldown_until"]) >= row["updated"] + 7200
