"""Memory per caller (`callerMemory`): an assistant is told what was said on a number's last calls with it."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from openring import memory
from openring.api import create_app
from openring.config import Settings
from openring.store import Store

from test_api import ASSISTANT, AUTH
from test_call_flow import _handshake, _stream_params

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
ANA, BRUNO = "+5511999990001", "+5511999990002"
REMEMBERS = {"id": "a1", "callerMemory": {"enabled": True}}


def _past(store, name, number, day, lines, assistant="a1", status="ended"):
    """A call from the past, as the store keeps it (the store picks the id; `name` is how the tests tell them apart)."""
    messages = [{"role": role, "message": text} for role, text in lines]
    return store.create("calls", {"name": name, "assistantId": assistant, "customer": {"number": number},
                                  "status": status, "startedAt": f"2026-11-{day:02d}T10:00:00Z", "messages": messages})


def _names(store, ids):
    return [store.get("calls", call_id)["name"] for call_id in ids]


def test_memory_is_off_unless_the_assistant_asks():
    assert memory.settings_of({}) is None and memory.settings_of(None) is None
    assert memory.settings_of({"callerMemory": False}) is None
    assert memory.settings_of({"callerMemory": {"calls": 5}}) is None  # no "enabled": not asked for
    assert memory.settings_of({"callerMemory": {"enabled": "yes"}}) is None
    assert memory.settings_of({"callerMemory": True}) == (3, 4000)
    assert memory.settings_of({"callerMemory": {"enabled": True, "calls": 2, "maxCharacters": 900}}) == (2, 900)
    # Out of range or not a number: the nearest limit, or the default.
    assert memory.settings_of({"callerMemory": {"enabled": True, "calls": 999, "maxCharacters": 1}}) == (10, 200)
    assert memory.settings_of({"callerMemory": {"enabled": True, "calls": "many", "maxCharacters": True}}) == (3, 4000)


def test_an_assistant_is_told_the_callers_last_calls_with_it_oldest_first():
    store = Store(":memory:")
    for n, day in enumerate((1, 5, 9, 12), start=1):
        _past(store, f"c{n}", ANA, day, [("user", f"It's Ana, visit number {n}"), ("assistant", "Welcome back")])
    _past(store, "other-caller", BRUNO, 10, [("user", "It's Bruno")])
    _past(store, "other-assistant", ANA, 11, [("user", "Wrong department")], assistant="a2")
    _past(store, "still-on", ANA, 12, [("user", "Half a call")], status="in-progress")
    _past(store, "said-nothing", ANA, 12, [])
    now = {"id": "now", "assistantId": "a1", "customer": {"number": ANA}}

    text, ids = memory.recall(store, now, REMEMBERS, now=NOW)
    assert _names(store, ids) == ["c4", "c3", "c2"]  # the last three, and nobody else's
    assert text.startswith(memory.INTRO)
    assert [line for line in text.splitlines() if line.startswith("Caller:")] == [
        "Caller: It's Ana, visit number 2", "Caller: It's Ana, visit number 3", "Caller: It's Ana, visit number 4"]
    assert "Call on 2026-11-12 (yesterday):" in text and "Call on 2026-11-05 (8 days ago):" in text
    assert "You: Welcome back" in text and "Bruno" not in text and "Wrong department" not in text

    assert memory.recall(store, now, {"id": "a1"}, now=NOW) is None  # the assistant didn't ask
    brunos = memory.recall(store, {**now, "customer": {"number": BRUNO}}, REMEMBERS, now=NOW)[1]
    assert _names(store, brunos) == ["other-caller"]
    assert memory.recall(store, {**now, "customer": {"number": "+15550000000"}}, REMEMBERS, now=NOW) is None
    assert memory.recall(store, {**now, "customer": {}}, REMEMBERS, now=NOW) is None  # a browser call has no number
    assert memory.recall(store, {"id": "now", "customer": {"number": ANA}}, REMEMBERS, now=NOW) is None  # transient


def test_a_call_never_remembers_itself_and_a_deleted_call_is_forgotten():
    store = Store(":memory:")
    earlier = _past(store, "c1", ANA, 2, [("user", "Book me for Friday")])
    now = _past(store, "now", ANA, 13, [("user", "Hello again")])
    assert memory.recall(store, now, REMEMBERS, now=NOW)[1] == [earlier["id"]]
    store.delete("calls", earlier["id"])
    assert memory.recall(store, now, REMEMBERS, now=NOW) is None


def test_the_memory_keeps_what_was_said_not_the_tools_and_fits_its_size():
    store = Store(":memory:")
    past = _past(store, "c1", ANA, 3, [("user", "My order is late. " * 60), ("assistant", "It arrives on Friday.")])
    store.update("calls", past["id"], {"messages": [
        *past["messages"],
        {"role": "tool_calls", "toolCalls": [{"function": {"name": "lookup", "arguments": '{"secret": "k-123"}'}}]},
        {"role": "tool_call_result", "toolCallId": "t1", "result": "internal note: k-123"},
        {"role": "user", "message": "  Thanks,\n  bye  "}]})
    wanted = {"id": "a1", "callerMemory": {"enabled": True, "maxCharacters": 200}}

    text, _ids = memory.recall(store, {"id": "now", "assistantId": "a1", "customer": {"number": ANA}}, wanted, now=NOW)
    said = text.split("\n\n", 1)[1]
    assert "k-123" not in text and "lookup" not in text
    assert said.endswith("Caller: Thanks, bye")  # the end of the call, on one line
    assert "It arrives on Friday." in said and "[...]" in said
    assert len(said) < 200 + 60


def test_a_squads_calls_are_remembered_by_the_squad():
    store = Store(":memory:")
    past = store.create("calls", {"squadId": "s1", "customer": {"number": ANA}, "status": "ended",
                                  "startedAt": "2026-11-10T10:00:00Z", "messages": [{"role": "user", "message": "Hi"}]})
    now = {"id": "now", "squadId": "s1", "customer": {"number": ANA}}
    assert memory.recall(store, now, REMEMBERS, now=NOW)[1] == [past["id"]]
    assert memory.recall(store, {**now, "squadId": "s2"}, REMEMBERS, now=NOW) is None


def test_a_number_is_matched_whole_and_as_data():
    store = Store(":memory:")
    _past(store, "c1", ANA, 2, [("user", "Hi")])
    for odd in ("' OR 1=1 --", "%", ANA[:-1], ANA + "0", ANA + "' --"):
        assert store.calls_of(odd) == []
    assert [c["name"] for c in store.calls_of(ANA)] == ["c1"]


@pytest.fixture
def fake_bot(monkeypatch):
    """Swap the live pipeline for a stub that keeps each call's plan and hangs up."""
    runs = []

    async def run_call(websocket, *, plan, call_id, **_):
        runs.append((call_id, plan))
        await websocket.close()

    monkeypatch.setattr("openring.bot.twilio_transport", lambda websocket, *_: websocket)
    monkeypatch.setattr("openring.bot.run_call", run_call)
    return runs


def _ring(client, number):
    twiml = client.post("/twilio/voice", data={"To": "+15551234567", "From": number, "CallSid": f"CA{number}"}).text
    return _stream_params(twiml)


def test_a_phone_call_starts_with_the_callers_memory(monkeypatch, fake_bot):
    for k in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.setenv(k, "fake")
    store = Store(":memory:")
    settings = Settings(api_key="test-key", public_url="https://x.test", db_path=":memory:",
                        validate_twilio_signature=False)
    client = TestClient(create_app(settings, store))
    a = client.post("/assistant", json={**ASSISTANT, "callerMemory": {"enabled": True}}, headers=AUTH).json()
    client.post("/phone-number", json={"number": "+15551234567", "assistantId": a["id"]}, headers=AUTH)
    assert a["callerMemory"] == {"enabled": True}  # kept as sent, next to Vapi's fields

    def call(number):
        params = _ring(client, number)
        with client.websocket_connect("/ws/twilio") as ws:
            _handshake(ws, params)
            try:
                ws.receive_text()
            except Exception:  # the stub hangs up
                pass
        return params["callId"], fake_bot[-1][1]

    first, plan = call(ANA)
    assert not any(memory.INTRO in m["content"] for m in plan.messages)  # nothing to remember yet
    assert "rememberedCallIds" not in store.get("calls", first)
    store.update("calls", first, {"status": "ended", "messages": [
        {"role": "user", "message": "A table for two on Friday, under Ana"}, {"role": "assistant", "message": "Done"}]})

    second, plan = call(ANA)
    told = plan.messages[-1]
    assert told["role"] == "system" and told["content"].startswith(memory.INTRO)
    assert "Caller: A table for two on Friday, under Ana" in told["content"]
    assert store.get("calls", second)["rememberedCallIds"] == [first]

    _third, plan = call(BRUNO)  # someone else gets none of it
    assert not any("Ana" in m["content"] for m in plan.messages)

    client.patch(f"/assistant/{a['id']}", json={"callerMemory": {"enabled": False}}, headers=AUTH)
    _fourth, plan = call(ANA)
    assert not any(memory.INTRO in m["content"] for m in plan.messages)
