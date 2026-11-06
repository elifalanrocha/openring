import pytest
from fastapi.testclient import TestClient

from openring.api import create_app
from openring.config import Settings
from openring.netguard import UnsafeURL, check_url
from openring.store import Store
from openring.tools import ToolError, check_tool

from test_api import ASSISTANT, AUTH


def make_client() -> TestClient:
    settings = Settings(
        api_key="test-key",
        public_url="https://ring.example.com",
        db_path=":memory:",
        twilio_auth_token="tok",
    )
    return TestClient(create_app(settings, Store(":memory:")))


def test_netguard_blocks_cloud_metadata():
    with pytest.raises(UnsafeURL):
        check_url("http://169.254.169.254/latest/meta-data/")
    with pytest.raises(UnsafeURL):
        check_url("http://[fe80::1]/metadata")


def test_api_request_rejects_credential_id():
    valid = {"type": "apiRequest", "name": "test", "method": "GET", "url": "https://example.com"}
    check_tool(valid)
    with_cred = {**valid, "credentialId": "secret"}
    with pytest.raises(ToolError):
        check_tool(with_cred)


def test_campaign_name_xss_reflected(monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "test-deepgram")
    client = make_client()
    a = client.post("/assistant", json=ASSISTANT, headers=AUTH).json()
    pn = client.post("/phone-number", json={"number": "+15551234567", "assistantId": a["id"]}, headers=AUTH).json()
    payload = {
        "name": "<script>alert('xss')</script>",
        "phoneNumberId": pn["id"],
        "assistantId": a["id"],
        "customers": [{"number": "+15551234567"}],
    }
    resp = client.post("/campaign", json=payload, headers=AUTH)
    assert resp.status_code < 500, resp.text
    assert "<script>" not in resp.text


def test_route_max_latency_is_enforced():
    client = make_client()
    base = {"name": "Test Route", "models": [{"provider": "openai", "model": "gpt-4o"}], "cooldownMinutes": 5}
    too_slow = client.post("/model-route", json={**base, "maxLatencyMs": 60000}, headers=AUTH)
    assert too_slow.status_code == 400
    ok = client.post("/model-route", json={**base, "maxLatencyMs": 30000}, headers=AUTH)
    assert ok.status_code == 201
    measured = client.post(f"/model-route/{ok.json()['id']}/measure", headers=AUTH)
    assert measured.status_code == 200
    assert len(measured.json()["measured"]) == 1
