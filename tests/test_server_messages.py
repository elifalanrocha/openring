"""Vapi's server messages: status-update, conversation-update, end-of-call-report, assistant-request."""

import asyncio
import json
import time
import xml.etree.ElementTree as ET

import httpx
import pytest
from fastapi.testclient import TestClient

from openring import server_messages
from openring.api import create_app
from openring.config import Settings
from openring.server_messages import AssistantRequestError, Outbox, request_assistant, server_of, wants
from openring.store import Store

from test_api import ASSISTANT, AUTH

SERVER = {"url": "https://hooks.example.com/vapi", "secret": "s3cret", "headers": {"X-Team": "a"}}


def test_server_of_reads_vapis_server_and_older_server_url():
    assert server_of({"server": SERVER}) == SERVER
    assert server_of({"serverUrl": "https://old.example.com", "serverUrlSecret": "x"}) == {
        "url": "https://old.example.com", "secret": "x"}
    assert server_of({}) is None and server_of(None) is None


def test_assistant_chooses_its_messages():
    assert wants({}, "status-update") and wants({}, "end-of-call-report")
    assert not wants({"serverMessages": ["end-of-call-report"]}, "status-update")
    assert not wants({"serverMessages": []}, "end-of-call-report")


def capture(monkeypatch, delays=None):
    """Swap the HTTP post for a recorder; `delays` holds a delay per message type to test ordering."""
    sent = []

    async def fake_post(server, payload, **_kw):
        kind = payload["message"]["type"]
        await asyncio.sleep((delays or {}).get(kind, 0))
        sent.append((server["url"], payload["message"]))
        return httpx.Response(200)

    monkeypatch.setattr(server_messages, "post", fake_post)
    return sent


def test_outbox_delivers_in_order_without_holding_the_call(monkeypatch):
    sent = capture(monkeypatch, delays={"status-update": 0.2})
    assistant = {"server": SERVER}

    async def scenario():
        box = Outbox(assistant, lambda: {"id": "c1"})
        started = time.monotonic()
        box.send("status-update", status="in-progress")
        box.send("conversation-update", messages=[{"role": "user", "message": "hi"}])
        assert time.monotonic() - started < 0.1  # sending never waits for the server
        await box.flush()

    asyncio.run(scenario())
    assert [m["type"] for _, m in sent] == ["status-update", "conversation-update"]
    assert sent[0][1]["status"] == "in-progress" and sent[0][1]["call"] == {"id": "c1"}
    assert sent[1][1]["messages"] == [{"role": "user", "message": "hi"}]


def test_outbox_skips_what_the_assistant_did_not_ask_for(monkeypatch):
    sent = capture(monkeypatch)

    async def scenario():
        box = Outbox({"server": SERVER, "serverMessages": ["end-of-call-report"]}, lambda: {})
        box.send("status-update", status="in-progress")
        await box.flush()
        box = Outbox({}, lambda: {})  # no server at all
        box.send("status-update", status="in-progress")
        await box.flush()

    asyncio.run(scenario())
    assert sent == []


def test_send_uses_the_servers_headers_and_secret():
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(200)

    async def scenario():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        await server_messages.post(SERVER, server_messages.message("status-update", {"id": "c1"}, status="ended"),
                                   client=client)

    asyncio.run(scenario())
    assert seen[0].headers["X-Vapi-Secret"] == "s3cret" and seen[0].headers["X-Team"] == "a"
    body = json.loads(seen[0].content)["message"]
    assert body["type"] == "status-update" and body["status"] == "ended" and body["timestamp"] > 0


def ask(responder):
    async def scenario():
        client = httpx.AsyncClient(transport=httpx.MockTransport(responder))
        return await request_assistant(SERVER, {"id": "c1", "customer": {"number": "+5511"}}, {"number": "+1555"},
                                       client=client)

    return asyncio.run(scenario())


def test_assistant_request_answers():
    seen = []

    def by_id(request):
        seen.append(json.loads(request.content)["message"])
        return httpx.Response(200, json={"assistantId": "a-1"})

    assert ask(by_id) == {"assistantId": "a-1"}
    assert seen[0]["type"] == "assistant-request" and seen[0]["phoneNumber"] == {"number": "+1555"}
    assert seen[0]["customer"] == {"number": "+5511"}
    transient = {"name": "Night shift", "model": {"provider": "groq", "model": "openai/gpt-oss-20b"}}
    assert ask(lambda r: httpx.Response(200, json={"assistant": transient})) == {"assistant": transient}
    for responder, reason in [
        (lambda r: httpx.Response(200, json={"error": "closed today"}), "assistant-request-returned-error"),
        (lambda r: httpx.Response(200, text="not json"), "assistant-request-returned-invalid-json"),
        (lambda r: httpx.Response(200, json={}), "assistant-request-returned-no-assistant"),
        (lambda r: httpx.Response(500), "assistant-request-failed"),
    ]:
        with pytest.raises(AssistantRequestError) as err:
            ask(responder)
        assert err.value.ended_reason == reason


def inbound_app(monkeypatch, number_fields):
    for k in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.setenv(k, "fake")
    store = Store(":memory:")
    settings = Settings(api_key="test-key", public_url="https://x.test", db_path=":memory:",
                        validate_twilio_signature=False)
    client = TestClient(create_app(settings, store))
    a = client.post("/assistant", json=ASSISTANT, headers=AUTH).json()
    client.post("/phone-number", json={"number": "+15551234567", **number_fields}, headers=AUTH)
    return client, store, a


def _ring(client):
    twiml = client.post("/twilio/voice", data={"To": "+15551234567", "From": "+5511999", "CallSid": "CA1"}).text
    root = ET.fromstring(twiml)
    stream = root.find("./Connect/Stream")
    params = {p.get("name"): p.get("value") for p in stream.findall("Parameter")} if stream is not None else None
    return root, params


def test_inbound_call_asks_the_numbers_server_for_an_assistant(monkeypatch):
    client, store, a = inbound_app(monkeypatch, {"server": SERVER})
    asked = []

    async def fake_request(server, call, number):
        asked.append((server["url"], call["customer"], number["number"]))
        return {"assistantId": a["id"]}

    monkeypatch.setattr("openring.api.request_assistant", fake_request)
    _, params = _ring(client)
    assert asked == [(SERVER["url"], {"number": "+5511999"}, "+15551234567")]
    assert params["assistantId"] == a["id"]
    assert store.get("calls", params["callId"])["assistantId"] == a["id"]


def test_inbound_call_with_a_transient_assistant(monkeypatch):
    client, store, _ = inbound_app(monkeypatch, {"server": SERVER})
    transient = {**ASSISTANT, "name": "Night shift"}

    async def fake_request(*_a):
        return {"assistant": transient}

    monkeypatch.setattr("openring.api.request_assistant", fake_request)
    _, params = _ring(client)
    assert set(params) == {"callId"}  # nothing to look up: the assistant lives in the call record
    call = store.get("calls", params["callId"])
    assert call["assistant"]["name"] == "Night shift" and call["assistantId"] is None


def test_inbound_call_when_the_server_cannot_say(monkeypatch):
    client, store, _ = inbound_app(monkeypatch, {"server": SERVER})

    async def failing(*_a):
        raise AssistantRequestError("assistant-request-returned-error", "closed today")

    monkeypatch.setattr("openring.api.request_assistant", failing)
    root, params = _ring(client)
    assert params is None and root.find("./Say") is not None
    call = store.list("calls")[0]
    assert (call["status"], call["endedReason"]) == ("ended", "assistant-request-returned-error")

    async def unknown(*_a):
        return {"assistantId": "nope"}

    monkeypatch.setattr("openring.api.request_assistant", unknown)
    _ring(client)
    assert store.list("calls")[0]["endedReason"] == "assistant-request-returned-unknown-assistant"


def test_number_with_an_assistant_never_asks(monkeypatch):
    client, _, a = inbound_app(monkeypatch, {"server": SERVER})
    client.patch(f"/phone-number/{client.get('/phone-number', headers=AUTH).json()[0]['id']}",
                 json={"assistantId": a["id"]}, headers=AUTH)

    async def must_not_ask(*_a):
        raise AssertionError("asked the server although the number has an assistant")

    monkeypatch.setattr("openring.api.request_assistant", must_not_ask)
    _, params = _ring(client)
    assert params["assistantId"] == a["id"]


def test_outbound_call_with_a_transient_assistant(monkeypatch):
    from openring import twilio

    async def create_call(*_a, **_kw):
        return "CA-out-1"

    monkeypatch.setattr(twilio, "create_call", create_call)
    client, store, _ = inbound_app(monkeypatch, {})
    n = client.get("/phone-number", headers=AUTH).json()[0]
    settings_client = TestClient(client.app)
    client.app.state.settings.twilio_account_sid = "AC1"
    client.app.state.settings.twilio_auth_token = "tok"
    body = {"phoneNumberId": n["id"], "customer": {"number": "+5511912345678"},
            "assistant": {**ASSISTANT, "name": "One-off"}}
    r = settings_client.post("/call", json=body, headers=AUTH)
    assert r.status_code == 201, r.text
    call = r.json()
    assert call["assistant"]["name"] == "One-off" and "assistantId" not in call
    bad = {**body, "assistant": {"model": {"provider": "nope"}}}
    assert settings_client.post("/call", json=bad, headers=AUTH).status_code == 400
    # Answered: the stream carries only the call id.
    twiml = settings_client.post(f"/twilio/voice?callId={call['id']}", data={"CallSid": "CA-out-1"}).text
    stream = ET.fromstring(twiml).find("./Connect/Stream")
    assert {p.get("name") for p in stream.findall("Parameter")} == {"callId"}


def test_a_call_reports_its_status_to_the_assistants_server(monkeypatch, quiet_providers):
    sent = capture(monkeypatch)
    for k in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.setenv(k, "fake")
    store = Store(":memory:")
    settings = Settings(api_key="test-key", public_url="https://x.test", db_path=":memory:",
                        validate_twilio_signature=False)
    client = TestClient(create_app(settings, store))
    a = client.post("/assistant", json={**ASSISTANT, "server": SERVER}, headers=AUTH).json()
    client.post("/phone-number", json={"number": "+15551234567", "assistantId": a["id"]}, headers=AUTH)
    _, params = _ring(client)

    with client.websocket_connect("/ws/twilio") as ws:
        ws.send_text(json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"}))
        ws.send_text(json.dumps({"event": "start", "streamSid": "MZ1", "start": {
            "streamSid": "MZ1", "callSid": "CA1", "accountSid": "AC1", "customParameters": params,
            "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1}}}))
        for _ in range(150):  # until the pipeline runs and says so (the first Pipecat import is slow)
            if sent:
                break
            time.sleep(0.1)
        ws.send_text(json.dumps({"event": "stop", "streamSid": "MZ1"}))

    for _ in range(100):
        if any(m["type"] == "end-of-call-report" for _, m in sent):
            break
        time.sleep(0.1)
    kinds = [(m["type"], m.get("status")) for _, m in sent if m["type"] != "conversation-update"]
    assert kinds == [("status-update", "in-progress"), ("status-update", "ended"), ("end-of-call-report", None)]
    report = sent[-1][1]
    assert report["endedReason"] == "customer-ended-call" and report["call"]["id"] == params["callId"]
    assert report["artifact"]["messages"][0]["message"] == ASSISTANT["firstMessage"]
    assert all(url == SERVER["url"] for url, _ in sent)


# --- as the call happens: speech-update, user-interrupted, hang, transcript ---


def _live(monkeypatch, steps, assistant=None, hang_seconds=None, delays=None):
    """Run `steps(live)` on a call's live messages and return what its server got, as (type, message) pairs."""
    sent = capture(monkeypatch, delays=delays)

    async def scenario():
        box = Outbox({"server": SERVER, **(assistant or {})}, lambda: {"id": "c1"})
        live = server_messages.Live(box, hang_seconds=hang_seconds)
        await steps(live)
        live.close()
        await box.flush()

    asyncio.run(scenario())
    return [(m["type"], m) for _, m in sent]


def test_speech_updates_say_who_spoke_and_count_the_turns(monkeypatch):
    async def steps(live):
        live.assistant_speaking(True)   # the greeting
        live.assistant_speaking(True)   # Pipecat says it twice, up and down the pipeline
        live.assistant_speaking(False)
        live.user_speaking(True)
        live.user_speaking(False)
        live.assistant_speaking(True)
        live.assistant_speaking(False)
        live.user_speaking(True)

    got = [(m["role"], m["status"], m["turn"]) for kind, m in _live(monkeypatch, steps) if kind == "speech-update"]
    assert got == [("assistant", "started", 0), ("assistant", "stopped", 0), ("user", "started", 1),
                   ("user", "stopped", 1), ("assistant", "started", 1), ("assistant", "stopped", 1),
                   ("user", "started", 2)]


def test_a_caller_who_talks_over_the_assistant_is_reported(monkeypatch):
    async def steps(live):
        live.user_speaking(True)        # nobody was talking: no interruption
        live.user_speaking(False)
        live.assistant_speaking(True)
        live.user_speaking(True)

    kinds = [kind for kind, _ in _live(monkeypatch, steps)]
    assert kinds.count("user-interrupted") == 1
    assert kinds.index("user-interrupted") > kinds.index("speech-update")
    assert all(m["call"] == {"id": "c1"} for _, m in _live(monkeypatch, steps))


def test_a_caller_left_waiting_is_reported_once(monkeypatch):
    async def unanswered(live):
        live.user_speaking(True)
        live.heard("Is anyone there?", True)
        live.user_speaking(False)
        await asyncio.sleep(0.3)

    async def answered(live):
        live.user_speaking(True)
        live.heard("Hello", True)
        live.user_speaking(False)
        await asyncio.sleep(0.05)
        live.assistant_speaking(True)
        await asyncio.sleep(0.3)

    async def nothing_said(live):  # a noise on the line: nothing was heard, so nobody owes an answer
        live.user_speaking(True)
        live.user_speaking(False)
        await asyncio.sleep(0.3)

    async def still_talking(live):  # the transcriber finishes a sentence while the caller goes on
        live.user_speaking(True)
        live.heard("My number is", True)
        await asyncio.sleep(0.3)

    def hangs(steps, **assistant):
        return [kind for kind, _ in _live(monkeypatch, steps, assistant, hang_seconds=0.1)].count("hang")

    assert hangs(unanswered) == 1
    assert hangs(answered) == hangs(nothing_said) == hangs(still_talking) == 0
    assert hangs(unanswered, serverMessages=["status-update"]) == 0


def test_transcripts_go_only_to_an_assistant_that_asks(monkeypatch):
    async def steps(live):
        live.heard("I'd like", False)
        live.heard("I'd like a table", True)
        live.heard("   ", True)
        live.said("For how many?")

    def transcripts(messages):
        got = _live(monkeypatch, steps, {"serverMessages": messages} if messages is not None else None)
        return [(kind, m["role"], m["transcriptType"], m["transcript"]) for kind, m in got if "transcript" in m]

    assert transcripts(None) == []  # not among Vapi's defaults
    assert transcripts(["transcript"]) == [
        ("transcript", "user", "partial", "I'd like"), ("transcript", "user", "final", "I'd like a table"),
        ("transcript", "assistant", "final", "For how many?")]
    final_only = 'transcript[transcriptType="final"]'
    assert transcripts([final_only]) == [
        (final_only, "user", "final", "I'd like a table"), (final_only, "assistant", "final", "For how many?")]


def test_a_slow_server_loses_partial_transcripts_never_final_ones(monkeypatch):
    async def steps(live):
        for n in range(200):
            live.heard(f"word {n}", False)
        live.heard("the whole sentence", True)

    got = _live(monkeypatch, steps, {"serverMessages": ["transcript"]}, delays={"transcript": 0.001})
    partial = [m for _, m in got if m["transcriptType"] == "partial"]
    assert len(partial) == server_messages.BACKLOG
    assert got[-1][1]["transcript"] == "the whole sentence"


def test_a_call_tells_the_server_what_happens_as_it_happens(monkeypatch):
    """Through the pipeline: the transcriber hears the caller, the assistant greets."""
    from pipecat.frames.frames import TranscriptionFrame

    from test_call_flow import _handshake, _scripted_call, _wait_until_ended

    # Pipecat's own turn-taking says when the caller started and stopped, from what the transcriber heard.
    heard = [TranscriptionFrame("Do you open on Sunday?", "caller", "2026-11-13T12:00:00Z")]
    client, store, params, _said = _scripted_call(monkeypatch, (1.5, 0), on_start=heard, speaking=0.2)
    sent = capture(monkeypatch)  # after the helper, which records them its own way
    assistant = store.list("assistants")[0]
    client.patch(f"/assistant/{assistant['id']}", headers=AUTH, json={
        "server": SERVER, "serverMessages": ["speech-update", "transcript", "user-interrupted", "hang"]})
    with client.websocket_connect("/ws/twilio") as ws:
        _handshake(ws, params)
        _wait_until_ended(store, params["callId"])
    for _ in range(50):
        if any(m.get("role") == "assistant" and m.get("status") == "stopped" for _, m in sent):
            break
        time.sleep(0.1)

    messages = [m for _, m in sent]
    speech = [(m["role"], m["status"]) for m in messages if m["type"] == "speech-update"]
    assert speech.count(("user", "started")) == 1 and speech.count(("user", "stopped")) == 1  # each said once
    assert speech.count(("assistant", "started")) == 1 and speech.count(("assistant", "stopped")) == 1
    transcripts = {(m["role"], m["transcriptType"], m["transcript"]) for m in messages if m["type"] == "transcript"}
    assert transcripts == {("user", "final", "Do you open on Sunday?"),
                           ("assistant", "final", ASSISTANT["firstMessage"])}
    assert all(m["call"]["id"] == params["callId"] for m in messages)
    assert not any(m["type"] in ("status-update", "end-of-call-report") for m in messages)  # not asked for


def test_each_delivery_is_logged_for_its_call_without_query_string_or_payload(monkeypatch):
    from test_api import make_client

    server_messages.DELIVERIES.clear()

    async def answers(server, payload, **_kw):
        if payload["message"]["type"] == "end-of-call-report":
            raise httpx.ConnectError("refused")
        return httpx.Response(503 if payload["message"]["type"] == "hang" else 200)

    monkeypatch.setattr(server_messages, "post", answers)
    assistant = {"server": {"url": "https://hooks.example.com/vapi?token=s3cret"}}

    async def scenario():
        for kind in ("status-update", "hang", "end-of-call-report"):
            await server_messages.send(assistant, kind, {"id": "c1"})
        await server_messages.send(assistant, "status-update", {"id": "other"})

    asyncio.run(scenario())
    log = server_messages.deliveries("c1")
    assert [(d["type"], d["status"], d["error"]) for d in log] == [
        ("status-update", 200, None), ("hang", 503, None), ("end-of-call-report", None, "ConnectError")]
    assert all("s3cret" not in json.dumps(d) and d["url"].startswith("https://hooks.example.com") for d in log)
    assert all(isinstance(d["ms"], int) and d["at"].endswith("Z") for d in log)

    client = make_client()
    call = client.app.state.store.create("calls", {"status": "ended"})
    server_messages.DELIVERIES.append({**log[0], "callId": call["id"]})
    shown = client.get(f"/call/{call['id']}/webhooks", headers=AUTH).json()
    assert [d["type"] for d in shown] == ["status-update"]
    assert client.get(f"/call/{call['id']}/webhooks").status_code == 401
    assert client.get("/call/nobody/webhooks", headers=AUTH).status_code == 404
