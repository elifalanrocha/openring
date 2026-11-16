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
    expires = datetime.now() + timedelta(hours=hours)
    return OAuthToken(OAuthProvider.OPENAI, "secret-access-token", "Bearer", expires, **extra)


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
    manager.store_token(OAuthProvider.OPENAI, token(refresh_token="secret-refresh-token", extra_data={"a": 1}))
    raw = manager.store.get_setting("oauth_token:openai")
    assert "secret-access-token" not in raw and "secret-refresh-token" not in raw
    got = manager.get_token(OAuthProvider.OPENAI)
    assert (got.access_token, got.refresh_token, got.extra_data) == (
        "secret-access-token", "secret-refresh-token", {"a": 1})

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


def test_redirect_uri_must_be_the_same_origin_as_public_url():
    client = signed_in(anthropic_client_id="own-client", public_url="https://voice.example.com")

    def allowed(uri):
        r = client.get("/oauth/authorize", params={"provider": "anthropic", "redirect_uri": uri}, headers=DASH)
        return r.status_code == 200

    assert allowed("https://voice.example.com/oauth/callback")
    assert allowed("http://127.0.0.1:8000/oauth/callback")
    # A prefix match would have accepted these.
    assert not allowed("https://voice.example.com.evil.example/cb")
    assert not allowed("https://voice.example.com@evil.example/cb")
    assert not allowed("http://voice.example.com/cb")  # other scheme
    assert not allowed("javascript://localhost/%0aalert(1)")


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


def test_hugging_face_needs_no_app_when_the_server_has_an_https_address(monkeypatch):
    assert oauth.cimd_client_id("https://voice.example.com/") == "https://voice.example.com/.well-known/oauth-cimd"
    assert oauth.cimd_client_id("http://127.0.0.1:8000") == "" and oauth.cimd_client_id("") == ""
    monkeypatch.delenv("HUGGINGFACE_CLIENT_ID", raising=False)
    monkeypatch.setenv("PUBLIC_URL", "https://voice.example.com")
    assert keys._connectors()["huggingface"].get("oauth")
    monkeypatch.setenv("PUBLIC_URL", "")
    assert not keys._connectors()["huggingface"].get("oauth")


def test_signing_in_with_hugging_face_gives_calls_their_key(monkeypatch):
    import os

    monkeypatch.delenv("HF_TOKEN", raising=False)
    asked = []

    def handle(request):
        asked.append(dict(parse_qs(request.content.decode())))
        return httpx.Response(200, json={"access_token": "hf_oauth_abc", "expires_in": 28800,
                                         "refresh_token": "r1", "scope": "inference-api"})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handle)))
    client = signed_in(public_url="https://voice.example.com")
    app = client.app
    document = client.get("/.well-known/oauth-cimd").json()
    assert document["redirect_uris"] == ["https://voice.example.com/oauth/callback"]
    assert document["token_endpoint_auth_method"] == "none"
    started = client.get("/oauth/authorize", params={"provider": "huggingface", "redirect_uri":
                                                     "http://127.0.0.1:8000/oauth/callback"}, headers=DASH).json()
    query = parse_qs(urlsplit(started["authorization_url"]).query)
    assert query["client_id"] == [document["client_id"]] and query["redirect_uri"] == document["redirect_uris"]
    assert "inference-api" in query["scope"][0] and query["code_challenge_method"] == ["S256"]
    done = client.get("/oauth/callback", params={"code": "c1", "state": started["state"]})  # no provider: as redirected
    assert done.status_code == 200 and "window.close()" in done.text
    assert asked[0]["code"] == ["c1"] and asked[0]["client_id"] == [document["client_id"]]
    assert os.environ["HF_TOKEN"] == "hf_oauth_abc"
    assert client.delete("/providers/huggingface/connect", headers=DASH).status_code == 200
    assert not os.environ.get("HF_TOKEN") and app.state.oauth.stored_token(OAuthProvider.HUGGINGFACE) is None


def test_a_token_is_refreshed_before_it_runs_out_and_dropped_when_it_cant_be(manager, monkeypatch):
    def stored(expires_in, refresh):
        manager.store_token(OAuthProvider.HUGGINGFACE, OAuthToken(
            provider=OAuthProvider.HUGGINGFACE, access_token="old", token_type="Bearer",
            expires_at=datetime.now() + timedelta(seconds=expires_in), refresh_token=refresh))

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"access_token": "new", "expires_in": 28800}))))
    stored(28800, "r1")
    assert asyncio.run(manager.keep_fresh(OAuthProvider.HUGGINGFACE, "cid")).access_token == "old"  # still good
    stored(60, "r1")
    fresh = asyncio.run(manager.keep_fresh(OAuthProvider.HUGGINGFACE, "cid"))
    assert fresh.access_token == "new" and fresh.refresh_token == "r1"  # the refresh token is kept
    stored(-10, None)
    assert asyncio.run(manager.keep_fresh(OAuthProvider.HUGGINGFACE, "cid")) is None
    assert manager.stored_token(OAuthProvider.HUGGINGFACE) is None
