import asyncio
import base64
import json
import time
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from noreply_gateway.errors import AuthenticationRequired, Retryable
from noreply_gateway.oauth import TokenManager, account_fingerprint


@pytest.fixture
def manager(config, vault):
    return TokenManager(config, vault, None)


@pytest.fixture
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def token_for(manager, signing_key, changes=None):
    tenant = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    now = int(time.time())
    claims = {"aud": manager.config.account.client_id, "iss": f"https://login.microsoftonline.com/{tenant}/v2.0", "tid": tenant, "sub": "offline-subject", "exp": now + 3600, "iat": now, "nonce": "expected-nonce", "preferred_username": manager.config.account.username}
    claims.update(changes or {})
    key = jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key(), as_dict=True)
    key.update(kid="offline-test-key", use="sig")
    manager.metadata = {"issuer": "https://login.microsoftonline.com/{tenantid}/v2.0", "jwks_uri": "https://login.microsoftonline.com/common/discovery/v2.0/keys"}
    manager.jwks = {"keys": [key]}
    return jwt.encode(claims, signing_key, algorithm="RS256", headers={"kid": "offline-test-key"})


async def test_id_token_signature_and_identity(manager, signing_key):
    token = token_for(manager, signing_key)
    assert (await manager._verify_id_token(token, "expected-nonce"))["preferred_username"] == manager.config.account.username


@pytest.mark.parametrize("changes", [{"nonce": "wrong"}, {"aud": "different-app"}, {"preferred_username": "attacker@example.net"}, {"exp": 1}, {"iss": "https://evil.example.net"}, {"tid": "not-a-uuid"}])
async def test_id_token_invalid_claims_rejected(manager, signing_key, changes):
    with pytest.raises(AuthenticationRequired):
        await manager._verify_id_token(token_for(manager, signing_key, changes), "expected-nonce")


async def test_forged_signature_rejected(manager, signing_key):
    token = token_for(manager, signing_key)
    header, payload, signature = token.split(".")
    raw = bytearray(base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)))
    raw[0] ^= 1
    forged = header + "." + payload + "." + base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    with pytest.raises(AuthenticationRequired):
        await manager._verify_id_token(forged, "expected-nonce")


def test_pkce_login_url(manager):
    url, flow = manager.begin_login()
    query = parse_qs(urlsplit(url).query)
    assert urlsplit(url).hostname == "login.microsoftonline.com"
    assert query["code_challenge_method"] == ["S256"]
    assert query["state"] == [flow.state]
    assert query["nonce"] == [flow.nonce]
    assert flow.verifier not in url
    assert "offline_access" in query["scope"][0]


async def test_oauth_state_and_expiry_rejected_before_network(manager):
    _, flow = manager.begin_login()
    with pytest.raises(AuthenticationRequired):
        await manager.complete_login(flow, {"code": "fake", "state": "wrong"})
    flow.expires = 1
    with pytest.raises(AuthenticationRequired):
        await manager.complete_login(flow, {"code": "fake", "state": flow.state})


async def test_refresh_serialized_and_rotated_encrypted(config, vault, monkeypatch):
    vault.write("account", {"fingerprint": account_fingerprint(config), "refresh_token": "OFFLINE-OLD", "mode": "legacy_v1"})
    manager = TokenManager(config, vault, None)
    calls = []
    async def exchange(url, data=None):
        calls.append((url, dict(data)))
        await asyncio.sleep(.005)
        return {"access_token": "OFFLINE-ACCESS", "refresh_token": "OFFLINE-ROTATED", "expires_in": 3600}
    monkeypatch.setattr(manager, "_read_json", exchange)
    assert await asyncio.gather(*(manager.get_token() for _ in range(20))) == ["OFFLINE-ACCESS"] * 20
    assert len(calls) == 1
    assert calls[0][0].endswith("/oauth2/token")
    assert calls[0][1]["resource"] == "https://outlook.office365.com/"
    assert vault.read("account")["refresh_token"] == "OFFLINE-ROTATED"
    assert b"OFFLINE-ROTATED" not in (config.data_dir / "account.enc").read_bytes()
    assert "OFFLINE-ACCESS" not in json.dumps(manager.status())


async def test_v2_login_validates_before_storing(manager, signing_key, monkeypatch):
    _, flow = manager.begin_login()
    token = token_for(manager, signing_key, {"nonce": flow.nonce})
    async def exchange(url, data=None):
        assert data["grant_type"] == "authorization_code"
        assert data["code_verifier"] == flow.verifier
        return {"id_token": token, "access_token": "OFFLINE-ACCESS", "refresh_token": "OFFLINE-REFRESH", "expires_in": 3600}
    monkeypatch.setattr(manager, "_read_json", exchange)
    await manager.complete_login(flow, {"code": "OFFLINE-CODE", "state": flow.state})
    assert manager.record["mode"] == "v2"
    assert manager.vault.read("account")["refresh_token"] == "OFFLINE-REFRESH"


async def test_multiple_401_for_one_generation_do_not_force_login(manager):
    manager.access_token = "old"
    for _ in range(4):
        with pytest.raises(Retryable):
            await manager.rejected_token("old")
    assert manager.needs_login is False
    manager.access_token = "new"
    with pytest.raises(Retryable):
        await manager.rejected_token("old")
    assert manager.access_token == "new"
    with pytest.raises(AuthenticationRequired):
        await manager.rejected_token("new")


def test_changed_account_configuration_invalidates_cache(config, vault):
    vault.write("account", {"fingerprint": account_fingerprint(config), "refresh_token": "OFFLINE-REFRESH"})
    config.account.backend = "graph"
    manager = TokenManager(config, vault, None)
    assert manager.status()["credential_present"] is False


async def test_cannot_send_until_rotated_token_is_persisted(manager, monkeypatch):
    def fail(name, record):
        raise OSError("simulated disk failure")
    monkeypatch.setattr(manager.vault, "write", fail)
    with pytest.raises(Retryable):
        await manager._save_result({"access_token": "OFFLINE-ACCESS", "refresh_token": "OFFLINE-ROTATED"}, "v2")
    assert manager.access_token == ""
    assert manager.record["refresh_token"] == "OFFLINE-ROTATED"
