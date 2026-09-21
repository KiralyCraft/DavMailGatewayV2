from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from noreply_gateway.additional_sender import AdditionalAuthenticationRequired, AdditionalSender
from noreply_gateway.message import MessageRejected, prepare_message
from noreply_gateway.oauth import account_fingerprint


@pytest.fixture
def additional(config, vault):
    return AdditionalSender(config, vault, None, None)


async def connect_offline(additional, username="person@example.test", address="person@example.test"):
    manager = additional.tokens
    _, flow = manager.begin_login()
    async def exchange(url, data=None):
        return {"id_token": "offline-id-token", "access_token": "offline-access", "refresh_token": "offline-refresh", "expires_in": 3600}
    async def verify(token, nonce):
        assert nonce == flow.nonce
        return {"preferred_username": username}
    async def profile(token):
        assert token == "offline-access"
        return address
    manager._read_json = exchange
    manager._verify_id_token = verify
    manager._profile_address = profile
    result = await manager.complete_login(flow, {"code": "offline-code", "state": flow.state})
    await additional.completed_login()
    return result


def test_additional_login_requests_shared_send_and_profile(additional):
    url, flow = additional.tokens.begin_login()
    query = parse_qs(urlsplit(url).query)
    scopes = set(query["scope"][0].split())
    assert {"Mail.Send", "Mail.Send.Shared", "User.Read", "offline_access"} <= scopes
    assert "login_hint" not in query
    assert query["state"] == [flow.state]


async def test_profile_address_is_read_from_microsoft(additional):
    class Body:
        async def iter_chunked(self, size):
            yield b'{"mail":"primary@example.test","userPrincipalName":"login@example.test"}'
    class Response:
        status = 200
        content = Body()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
    class Session:
        def get(self, url, *, headers, allow_redirects):
            assert url == "https://graph.microsoft.com/v1.0/me?$select=mail,userPrincipalName"
            assert headers == {"Authorization": "Bearer offline-access"}
            assert allow_redirects is False
            return Response()
    additional.tokens.session = Session()
    assert await additional.tokens._profile_address("offline-access") == "primary@example.test"


async def test_default_address_comes_from_microsoft_and_is_not_editable(config, vault, additional):
    vault.write("account", {"fingerprint": account_fingerprint(config), "username": config.account.sender, "refresh_token": "original-refresh"})
    assert await connect_offline(additional, address="primary@example.test") == "primary@example.test"
    assert additional.status()["choice_needed"]
    assert additional.status()["default_sender"] == "primary@example.test"
    assert vault.read("account")["refresh_token"] == "original-refresh"
    assert await additional.choose("default", "ignored@example.test") == "primary@example.test"
    assert additional.selected_sender == "primary@example.test"
    assert AdditionalSender(config, vault, None, None).selected_sender == "primary@example.test"
    assert additional.tokens.record["username"] == "person@example.test"


async def test_custom_sender_is_admin_selected_and_smtp_restricted(config, vault, additional):
    await connect_offline(additional)
    with pytest.raises(ValueError):
        await additional.choose("custom", "not an email")
    with pytest.raises(ValueError):
        await additional.choose("arbitrary", "other@example.test")
    assert await additional.choose("custom", "shared@example.test") == "shared@example.test"
    assert additional.selected_sender == "shared@example.test"
    assert AdditionalSender(config, vault, None, None).selected_sender == "shared@example.test"
    def prepare(sender, allowed=""):
        raw = f"From: {sender}\r\nTo: to@example.test\r\nSubject: test\r\n\r\nbody\r\n".encode()
        return prepare_message(raw, ["to@example.test"], "smtp@example.test", "offline-id", config, allowed)
    with pytest.raises(MessageRejected):
        prepare("shared@example.test")
    assert b"From: shared@example.test" in prepare("shared@example.test", additional.selected_sender).mime
    assert b"From: gateway@example.test" in prepare("gateway@example.test", additional.selected_sender).mime
    with pytest.raises(MessageRejected):
        prepare("other@example.test", additional.selected_sender)


def test_additional_graph_size_limit_applies_even_with_larger_original_limit(config):
    config.smtp.max_message_bytes = 3_000_000
    body = b"x" * 2_500_001
    raw = b"From: shared@example.test\r\nTo: to@example.test\r\n\r\n" + body
    with pytest.raises(MessageRejected, match="2,500,000"):
        prepare_message(raw, ["to@example.test"], "smtp@example.test", "offline-id", config, "shared@example.test")


async def test_backend_routes_by_queued_mime_even_after_choice_changes(config, vault, additional):
    await connect_offline(additional)
    await additional.choose("custom", "shared@example.test")
    sent = []
    async def original(message):
        sent.append("original")
    async def delegated(message):
        sent.append("delegated")
    additional.original_backend = SimpleNamespace(send=original)
    additional.backend = SimpleNamespace(send=delegated)
    def queued(sender):
        raw = f"From: {sender}\r\nTo: to@example.test\r\n\r\nbody\r\n".encode()
        prepared = prepare_message(raw, ["to@example.test"], "smtp@example.test", "offline-id", config, additional.selected_sender)
        return {"id": prepared.id, "mime": prepared.mime}
    old_custom = queued("shared@example.test")
    await additional.send(queued("gateway@example.test"))
    await additional.choose("default")
    await additional.send(old_custom)
    assert sent == ["original", "delegated"]
    await additional.tokens.disconnect()
    with pytest.raises(AdditionalAuthenticationRequired):
        await additional.send(old_custom)
