"""OAuth sign-in: PKCE, encrypted token storage, and no built-in vendor client IDs."""

import asyncio
import base64
import hashlib
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.fernet import Fernet
from test_keys import signed_in

from openring import keys, oauth
from openring.oauth import DEFAULT_CLIENT_IDS, OAuthManager, OAuthProvider, OAuthToken

from test_api import DASH


class MemoryStore:
    def __init__(self):
        self.data = {}

    def get_setting(self, key):
        return self.data.get(key)

    def set_setting(self, key, value):
        if value is None:
            self.data.pop(key, None)
        else:
            self.data[key] = value


@pytest.fixture
def manager():
    return OAuthManager(Fernet.generate_key(), MemoryStore())


def token(hours=1, **extra):
    return OAuthToken(OAuthProvider.OPENAI, "at", "Bearer", datetime.now() + timedelta(hours=hours), **extra)


def test_no_vendor_client_ids_are_built_in():
    # Signing in as Claude Code, Codex or Grok CLI would break those vendors' terms.
    assert not any(DEFAULT_CLIENT_IDS.values())
    assert not any("client_id" in c for c in oauth.PROVIDER_CONFIGS.values())


def test_pkce_pair_is_s256(manager):
    verifier, challenge = manager.generate_pkce_pair()
    assert 43 <= len(verifier) <= 128
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    assert challenge == expected and manager.verify_pkce(verifier, challenge)
    assert not manager.verify_pkce(verifier + "x", challenge)


def test_authorization_url_carries_challenge_and_state(manager):
    url = manager.generate_authorization_url(OAuthProvider.OPENAI, "my-client", "http://127.0.0.1:8000/oauth/callback",
                                             state="s1", code_challenge="ch")
    q = parse_qs(urlsplit(url).query)
    assert url.startswith("https://auth.openai.com/oauth/authorize?")
    assert q["client_id"] == ["my-client"] and q["state"] == ["s1"]
    assert q["code_challenge"] == ["ch"] and q["code_challenge_method"] == ["S256"]
    assert "originator" not in q


def test_client_id_comes_from_settings(manager):
    class Settings:
        anthropic_client_id = "own-client"

    assert manager.get_client_id("anthropic", Settings()) == "own-client"
    assert not manager.get_client_id("anthropic", None)


def test_tokens_are_encrypted_and_expire(manager):
    manager.store_token(OAuthProvider.OPENAI, token(refresh_token="rt", extra_data={"a": 1}))
    raw = manager.store.get_setting("oauth_token:openai")
    assert "at" not in raw and "rt" not in raw
    got = manager.get_token(OAuthProvider.OPENAI)
    assert (got.access_token, got.refresh_token, got.extra_data) == ("at", "rt", {"a": 1})

    manager.store_token(OAuthProvider.OPENAI, token(hours=-1))
    assert manager.get_token(OAuthProvider.OPENAI) is None
    assert manager.store.get_setting("oauth_token:openai") is None

    manager.store.set_setting("oauth_token:openai", "garbage")
    assert manager.get_token(OAuthProvider.OPENAI) is None
    assert manager.store.get_setting("oauth_token:openai") is None


def test_exchange_and_refresh_post_the_right_form(manager, monkeypatch):
    sent = []

    def handle(request):
        sent.append(parse_qs(request.content.decode()))
        return httpx.Response(200, json={"access_token": "new", "expires_in": 3600, "refresh_token": "r2"})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handle)))
    got = asyncio.run(manager.exchange_code(OAuthProvider.OPENAI, "cid", "", "code", "http://127.0.0.1/cb", "ver"))
    assert got.access_token == "new" and not got.is_expired()
    assert sent[0]["code_verifier"] == ["ver"] and "client_secret" not in sent[0]

    asyncio.run(manager.refresh_token(OAuthProvider.OPENAI, "cid", "", "r1"))
    assert sent[1]["grant_type"] == ["refresh_token"] and "client_secret" not in sent[1]


def test_exchange_failure_is_an_oauth_error(manager, monkeypatch):
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(lambda r: httpx.Response(400, text="bad"))))
    with pytest.raises(oauth.OAuthError, match="400"):
        asyncio.run(manager.exchange_code(OAuthProvider.OPENAI, "cid", "", "c", "http://127.0.0.1/cb", "v"))


def test_oauth_button_only_with_own_client(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_CLIENT_ID", raising=False)
    found = keys._connectors()
    assert found["openrouter"].get("oauth") and not found["anthropic"].get("oauth")
    monkeypatch.setenv("ANTHROPIC_CLIENT_ID", "own-client")
    assert keys._connectors()["anthropic"].get("oauth")


def test_authorize_and_callback_round_trip(monkeypatch):
    client = signed_in()
    r = client.get("/oauth/authorize", params={"provider": "anthropic", "redirect_uri": ""}, headers=DASH)
    assert r.status_code == 400 and "client ID" in r.json()["detail"]

    client = signed_in(anthropic_client_id="own-client")
    r = client.get("/oauth/authorize", params={"provider": "anthropic", "redirect_uri": "https://evil.example/cb"},
                   headers=DASH)
    assert r.status_code == 400  # redirect not allowed

    r = client.get("/oauth/authorize", params={"provider": "anthropic", "redirect_uri": ""}, headers=DASH)
    assert r.status_code == 200
    state = r.json()["state"]
    assert parse_qs(urlsplit(r.json()["authorization_url"]).query)["client_id"] == ["own-client"]

    async def fake_exchange(self, **kw):
        assert kw["client_id"] == "own-client" and kw["code_verifier"]
        return OAuthToken(kw["provider"], "at", "Bearer", datetime.now() + timedelta(hours=1))

    monkeypatch.setattr(OAuthManager, "exchange_code", fake_exchange)
    # The provider's redirect is cross-site: no dashboard header, no SameSite=Strict cookie. The state is the guard.
    cookies = dict(client.cookies)
    client.cookies.clear()
    callback = {"provider": "anthropic", "code": "c"}
    assert client.get("/oauth/callback", params={**callback, "state": "guess"}).status_code == 400
    assert client.get("/oauth/callback", params={**callback, "provider": "openai", "state": state}).status_code == 400
    r = client.get("/oauth/authorize", params={"provider": "anthropic", "redirect_uri": ""}, headers=DASH)
    assert r.status_code == 401
    client.cookies.update(cookies)
    state = client.get("/oauth/authorize", params={"provider": "anthropic", "redirect_uri": ""},
                       headers=DASH).json()["state"]
    client.cookies.clear()
    r = client.get("/oauth/callback", params={**callback, "state": state})
    assert r.status_code == 200 and r.json()["success"]
    client.cookies.update(cookies)
    assert client.get("/oauth/anthropic/status", headers=DASH).status_code == 200
    # A state works once.
    r = client.get("/oauth/callback", params={"provider": "anthropic", "code": "c", "state": state}, headers=DASH)
    assert r.status_code == 400
