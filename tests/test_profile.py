"""What an assistant costs per minute and how fast it answers, layer by layer (`GET /assistant/{id}/profile`)."""

from fastapi.testclient import TestClient

from openring import stats
from openring.api import create_app
from openring.config import Settings
from openring.providers import resolve_plan
from openring.store import Store

from test_api import ASSISTANT, AUTH

LOCAL = {"transcriber": {"provider": "whisper", "model": "base", "language": "en"},
         "model": {"provider": "ollama", "model": "llama3.2"}, "voice": {"provider": "kokoro", "voiceId": "af_heart"}}


def _call(assistant_id, seconds, stt, llm, tts, turns, **more):
    return {"assistantId": assistant_id, "status": "ended", "durationSeconds": seconds,
            "costBreakdown": {"stt": stt, "llm": llm, "tts": tts, "total": stt + llm + tts, **more},
            "artifact": {"performanceMetrics": {"turns": turns}}}


def test_an_assistant_without_calls_gets_an_estimate_and_no_made_up_latency():
    free = stats.profile([], resolve_plan(LOCAL, check_keys=False))
    assert free["measured"] is False and free["costPerMinute"] == {"stt": 0.0, "llm": 0.0, "tts": 0.0}
    assert free["totalPerMinute"] == 0 and free["unpriced"] == []
    assert set(free["latency"].values()) == {None}  # nothing was measured: nothing is claimed
    assert free["assumes"] == stats.ESTIMATE

    paid = stats.profile([], resolve_plan(ASSISTANT, check_keys=False))
    assert paid["measured"] is False
    known = [v for v in paid["costPerMinute"].values() if v is not None]
    assert known and all(v >= 0 for v in known) and paid["totalPerMinute"] == round(sum(known), 5)
    assert len(paid["unpriced"]) == 3 - len(known)  # a layer with no list price is named, never counted as free


def test_an_assistant_with_calls_is_measured_on_them():
    turn = {"endpointingLatency": 200, "transcriberLatency": 100, "modelLatency": 400, "voiceLatency": 300,
            "turnLatency": 1000}
    calls = [_call("a1", 60, 0.006, 0.001, 0.0, [turn, {**turn, "modelLatency": 600, "turnLatency": 1200}]),
             _call("a1", 60, 0.006, 0.003, 0.0, [], unpriced=["phone line (telnyx)"]),
             {"assistantId": "a1", "status": "in-progress", "durationSeconds": 30, "costBreakdown": {"stt": 9}},
             {"assistantId": "a1", "status": "ended", "durationSeconds": 0, "costBreakdown": {"stt": 9}}]
    got = stats.profile(calls, resolve_plan(LOCAL, check_keys=False))
    assert got["measured"] is True and got["calls"] == 2 and got["minutes"] == 2.0
    assert got["costPerMinute"] == {"stt": 0.006, "llm": 0.002, "tts": 0.0} and got["totalPerMinute"] == 0.008
    assert got["latency"]["modelLatency"] == 500 and got["latency"]["turnLatency"] == 1100
    assert got["unpriced"] == ["phone line (telnyx)"]


def test_the_profile_is_per_assistant_and_behind_the_key():
    store = Store(":memory:")
    client = TestClient(create_app(Settings(api_key="test-key", public_url="", db_path=":memory:"), store))
    mine = client.post("/assistant", json=LOCAL, headers=AUTH).json()
    other = client.post("/assistant", json=LOCAL, headers=AUTH).json()
    store.create("calls", _call(other["id"], 120, 0.5, 0.5, 0.5, []))

    assert client.get(f"/assistant/{mine['id']}/profile", headers=AUTH).json()["measured"] is False
    theirs = client.get(f"/assistant/{other['id']}/profile", headers=AUTH).json()
    assert theirs["measured"] is True and theirs["totalPerMinute"] == 0.75
    assert client.get(f"/assistant/{mine['id']}/profile").status_code == 401
    assert client.get("/assistant/nobody/profile", headers=AUTH).status_code == 404
