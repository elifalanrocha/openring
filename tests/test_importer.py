from fastapi.testclient import TestClient

from openring.api import create_app
from openring.config import Settings
from openring.store import Store

AUTH = {"Authorization": "Bearer test-key"}

def make_client() -> TestClient:
    settings = Settings(
        api_key="test-key",
        public_url="https://ring.example.com",
        db_path=":memory:",
        twilio_auth_token="tok",
    )
    return TestClient(create_app(settings, Store(":memory:")))

def test_importer_endpoint(monkeypatch):
    class MockResponse:
        def __init__(self, status_code, json_data):
            self.status_code = status_code
            self._json = json_data
        def json(self):
            return self._json

    async def mock_get(client, base_url, path, params=None):
        if "assistant" in path:
            return MockResponse(200, [{
                "id": "vapi-asst-1",
                "name": "Support Bot",
                "model": {"provider": "openai", "model": "gpt-4o", "messages": [{"role": "system", "content": "Help"}]},
                "voice": {"provider": "11labs", "voiceId": "v1"},
                "transcriber": {"provider": "deepgram", "model": "nova-2"}
            }])
        return MockResponse(200, [])

    async def mock_import(base_url, api_key, store, *, client=None):
        store.create("assistants", {
            "name": "Support Bot",
            "model": {"provider": "openai", "model": "gpt-4o", "messages": [{"role": "system", "content": "Help"}]},
            "voice": {"provider": "11labs", "voiceId": "v1"},
            "transcriber": {"provider": "deepgram", "model": "nova-2"},
            "importedFrom": {"baseUrl": base_url, "id": "vapi-asst-1"}
        })
        return {"assistants": 1, "tools": 0, "numbers": 0, "squads": 0, "problems": {}}

    monkeypatch.setattr("openring.api.import_account", mock_import)

    client = make_client()
    r = client.post("/import", json={"baseUrl": "https://vapi.example.com", "apiKey": "fake-key"}, headers=AUTH)
    assert r.status_code == 200
    data = r.json()
    assert data["assistants"] == 1

    assts = client.get("/assistant", headers=AUTH).json()
    assert len(assts) == 1
    assert assts[0]["name"] == "Support Bot"
    assert assts[0]["importedFrom"] == {"baseUrl": "https://vapi.example.com", "id": "vapi-asst-1"}
