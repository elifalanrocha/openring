"""Versions of an assistant and traffic split between them (versions.py, OpenRing's own)."""

import pytest
from fastapi.testclient import TestClient

from openring import versions
from openring.api import create_app
from openring.config import Settings
from openring.store import Store

from test_api import ASSISTANT, AUTH
from test_call_flow import _close_code, _stream_params


@pytest.fixture
def server(monkeypatch):
    for key in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.setenv(key, "fake")
    store = Store(":memory:")
    client = TestClient(create_app(Settings(api_key="test-key", public_url="https://x.test", db_path=":memory:",
                                            validate_twilio_signature=False), store))
    assistant = client.post("/assistant", json={**ASSISTANT, "firstMessage": "Hello, one."}, headers=AUTH).json()
    return client, store, assistant


def test_a_version_keeps_the_assistant_as_it_was_and_can_be_restored(server):
    client, store, a = server
    v1 = client.post(f"/assistant/{a['id']}/version", json={"name": "Before the new greeting"}, headers=AUTH).json()
    assert v1["number"] == 1 and v1["assistant"]["firstMessage"] == "Hello, one." and "id" not in v1["assistant"]
    client.patch(f"/assistant/{a['id']}", json={"firstMessage": "Hello, two.", "backgroundSound": "office"},
                 headers=AUTH)
    v2 = client.post(f"/assistant/{a['id']}/version", headers=AUTH).json()
    assert (v2["number"], v2["name"]) == (2, "Version 2")
    listed = client.get(f"/assistant/{a['id']}/version", headers=AUTH).json()
    assert [v["number"] for v in listed["versions"]] == [1, 2]
    back = client.post(f"/assistant/{a['id']}/version/{v1['id']}/restore", headers=AUTH).json()
    assert back["firstMessage"] == "Hello, one." and back.get("backgroundSound") is None and back["id"] == a["id"]
    other = client.post("/assistant", json=ASSISTANT, headers=AUTH).json()
    assert client.post(f"/assistant/{other['id']}/version/{v1['id']}/restore", headers=AUTH).status_code == 404


def test_a_traffic_plan_is_checked(server):
    client, _, a = server
    v1 = client.post(f"/assistant/{a['id']}/version", headers=AUTH).json()
    other = client.post("/assistant", json=ASSISTANT, headers=AUTH).json()
    foreign = client.post(f"/assistant/{other['id']}/version", headers=AUTH).json()

    def plan(shares):
        return client.patch(f"/assistant/{a['id']}", json={"trafficPlan": {"versions": shares}}, headers=AUTH)

    assert plan([{"versionId": v1["id"], "weight": 30}]).status_code == 200
    assert plan([{"versionId": v1["id"], "weight": 130}]).status_code == 400
    assert plan([{"versionId": v1["id"], "weight": 60}, {"versionId": v1["id"], "weight": 60}]).status_code == 400
    assert plan([{"versionId": foreign["id"], "weight": 10}]).status_code == 400
    assert plan("half").status_code == 400
    # a version with traffic can't be deleted from under it
    assert client.delete(f"/assistant/{a['id']}/version/{v1['id']}", headers=AUTH).status_code == 400


def test_calls_are_shared_out_by_weight_and_keep_who_answered():
    store = Store(":memory:")
    a = store.create("assistants", {"name": "a", "firstMessage": "now"})
    old = versions.save(store, {**a, "firstMessage": "then"})
    a = store.update("assistants", a["id"], {"trafficPlan": {"versions": [{"versionId": old["id"], "weight": 25}]}})
    answers, label = versions.pick(a, store, roll=10)
    assert (answers["firstMessage"], label, answers["id"]) == ("then", 1, a["id"])
    assert versions.pick(a, store, roll=25)[1] == "current" and versions.pick(a, store, roll=99)[1] == "current"
    picked = [versions.pick(a, store)[1] for _ in range(2000)]
    assert 380 < picked.count(1) < 620  # about a quarter


def test_a_call_gets_one_version_for_good(server, monkeypatch):
    client, store, a = server
    client.patch(f"/assistant/{a['id']}", json={"firstMessage": "Hello, two."}, headers=AUTH)
    old = store.create("assistant_versions", {"assistantId": a["id"], "number": 1, "name": "v1",
                                              "assistant": {**ASSISTANT, "firstMessage": "Hello, one."}})
    client.patch(f"/assistant/{a['id']}", json={"trafficPlan": {"versions": [{"versionId": old["id"], "weight": 100}]}},
                 headers=AUTH)
    client.post("/phone-number", json={"number": "+15551234567", "assistantId": a["id"]}, headers=AUTH)
    runs = []

    async def run_call(websocket, *, plan, call_id, **_):
        runs.append(plan.assistant.get("firstMessage"))
        await websocket.close()

    monkeypatch.setattr("openring.bot.twilio_transport", lambda websocket, *_: websocket)
    monkeypatch.setattr("openring.bot.run_call", run_call)
    params = _stream_params(client.post("/twilio/voice", data={"To": "+15551234567", "From": "+5511",
                                                                "CallSid": "CA1"}).text)
    _close_code(client, params)
    call = store.get("calls", params["callId"])
    assert call["assistantVersion"] == 1 and runs == ["Hello, one."]  # the saved version answered, not the current one


def test_versions_are_compared_on_the_calls_they_answered():
    store = Store(":memory:")

    def call(version, success, cost, seconds):
        return {"assistantId": "a", "status": "ended", "assistantVersion": version, "cost": cost,
                "durationSeconds": seconds, **({"analysis": {"successEvaluation": success}} if success else {})}

    calls = [call(1, "true", 0.02, 60), call(1, "false", 0.04, 120), call("current", "true", 0.01, 30),
             call(None, None, 0.03, 90), {"assistantId": "b", "status": "ended"}]
    got = versions.results(store, "a", calls)
    assert got[0] == {"version": 1, "calls": 2, "successRate": 0.5, "judgedCalls": 2, "averageCost": 0.03,
                      "averageSeconds": 90.0}
    assert got[1]["version"] == "current" and got[1]["calls"] == 2 and got[1]["successRate"] == 1.0
