"""End-to-end call lifecycle over a simulated Twilio media stream.

The providers are stubs (`quiet_providers`), so tests stay off the network; they check that the
call is tracked from start to finish and the transcript is saved. Other tests swap the
whole pipeline for a stub to check which media streams are accepted.
"""

import asyncio
import json
import time
import xml.etree.ElementTree as ET

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from openring.api import create_app
from openring.config import Settings
from openring.store import Store

from test_api import ASSISTANT, AUTH


def _answered_call(monkeypatch, **server):
    """A server with one assistant on +15551234567 whose Twilio webhook already answered a call."""
    for k in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.setenv(k, "fake")
    store = Store(":memory:")
    settings = Settings(api_key="test-key", public_url="https://x.test", db_path=":memory:",
                        validate_twilio_signature=False, **server)
    client = TestClient(create_app(settings, store))
    a = client.post("/assistant", json=ASSISTANT, headers=AUTH).json()
    client.post("/phone-number", json={"number": "+15551234567", "assistantId": a["id"]}, headers=AUTH)
    twiml = client.post("/twilio/voice", data={"To": "+15551234567", "From": "+5511", "CallSid": "CA1"}).text
    return client, store, _stream_params(twiml)


def _stream_params(twiml):
    return {p.get("name"): p.get("value") for p in ET.fromstring(twiml).find("./Connect/Stream").findall("Parameter")}


def _handshake(ws, params, line=1):
    ws.send_text(json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"}))
    ws.send_text(json.dumps({"event": "start", "streamSid": f"MZ{line}", "start": {
        "streamSid": f"MZ{line}", "callSid": f"CA{line}", "accountSid": "AC1", "customParameters": params,
        "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1}}}))


def _close_code(client, params) -> int:
    """Open a media stream the way Twilio does and return the code the server closes it with."""
    with client.websocket_connect("/ws/twilio") as ws:
        _handshake(ws, params)
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
    return closed.value.code


@pytest.fixture
def fake_bot(monkeypatch):
    """Swap the live pipeline for a stub that records each call it is asked to run."""
    runs = []

    async def run_call(websocket, *, plan, call_id, **_):
        runs.append((call_id, plan))
        await websocket.close()

    # The stub gets the raw websocket instead of a Pipecat transport, so it can hang up.
    monkeypatch.setattr("openring.bot.twilio_transport", lambda websocket, *_: websocket)
    monkeypatch.setattr("openring.bot.run_call", run_call)
    return runs


def _pipeline_started(monkeypatch):
    """Records the server messages a call sends; the first one means its pipeline is running."""
    import httpx

    from openring import server_messages

    sent = []

    async def record(server, payload, **_kw):
        sent.append(payload["message"])
        return httpx.Response(200)

    monkeypatch.setattr(server_messages, "post", record)  # also keeps the tests off the network
    return sent


def test_call_lifecycle(monkeypatch, quiet_providers):
    sent = _pipeline_started(monkeypatch)
    client, store, params = _answered_call(monkeypatch)

    with client.websocket_connect("/ws/twilio") as ws:
        _handshake(ws, params)
        for _ in range(150):  # the first Pipecat import in a process takes a few seconds
            if sent:
                break
            time.sleep(0.1)
        assert store.get("calls", params["callId"])["status"] == "in-progress"
        ws.send_text(json.dumps({"event": "stop", "streamSid": "MZ1"}))

    call = _wait_until_ended(store, params["callId"])
    assert call["status"] == "ended"
    assert call["messages"][0] == {"role": "assistant", "message": ASSISTANT["firstMessage"]}
    assert call["artifact"]["messages"] == call["messages"]
    assert call["artifact"]["performanceMetrics"]["turnLatencies"] == []  # nobody spoke
    assert set(call["costBreakdown"]) == {"llmPromptTokens", "llmCompletionTokens", "ttsCharacters",
                                          "transcriberAudioSeconds", "stt", "llm", "tts", "line", "total"}
    assert call["cost"] == call["costBreakdown"]["total"]


def test_two_calls_at_once_stay_apart(monkeypatch, quiet_providers):
    """Both run on one event loop, as on a real server, and neither touches the other's call."""
    sent = _pipeline_started(monkeypatch)
    client, store, first = _answered_call(monkeypatch)
    other = client.post("/assistant", json={**ASSISTANT, "firstMessage": "Second line here."}, headers=AUTH).json()
    client.post("/phone-number", json={"number": "+15557654321", "assistantId": other["id"]}, headers=AUTH)
    twiml = client.post("/twilio/voice", data={"To": "+15557654321", "From": "+5522", "CallSid": "CA2"}).text
    second = _stream_params(twiml)
    assert first["callId"] != second["callId"]

    with client, client.websocket_connect("/ws/twilio") as ws2:
        with client.websocket_connect("/ws/twilio") as ws1:
            _handshake(ws1, first)
            _handshake(ws2, second, line=2)
            for _ in range(150):  # the first Pipecat import in a process takes a few seconds
                if {m["call"]["id"] for m in sent} >= {first["callId"], second["callId"]}:
                    break
                time.sleep(0.1)
            assert {m["call"]["id"] for m in sent} >= {first["callId"], second["callId"]}
            ws1.send_text(json.dumps({"event": "stop", "streamSid": "MZ1"}))

        one = _wait_until_ended(store, first["callId"])
        assert one["status"] == "ended"
        assert store.get("calls", second["callId"])["status"] == "in-progress"  # the other caller is still talking
        ws2.send_text(json.dumps({"event": "stop", "streamSid": "MZ2"}))

    two = _wait_until_ended(store, second["callId"])
    assert two["status"] == "ended"
    assert one["messages"] == [{"role": "assistant", "message": ASSISTANT["firstMessage"]}]
    assert two["messages"] == [{"role": "assistant", "message": "Second line here."}]
    assert two["assistantId"] == other["id"] != one["assistantId"]


def test_media_stream_is_single_use(monkeypatch, fake_bot):
    client, _, params = _answered_call(monkeypatch)
    assert _close_code(client, params) == 1000
    assert _close_code(client, params) == 1008  # a second connection for the same call
    assert [call_id for call_id, _ in fake_bot] == [params["callId"]]


def test_media_stream_uses_the_calls_assistant(monkeypatch, fake_bot):
    client, _, params = _answered_call(monkeypatch)
    other = client.post("/assistant", json={**ASSISTANT, "firstMessage": "Not me"}, headers=AUTH).json()
    _close_code(client, {**params, "assistantId": other["id"]})
    assert fake_bot[0][1].first_message == ASSISTANT["firstMessage"]


def test_media_stream_fills_the_calls_variables(monkeypatch, fake_bot):
    client, store, params = _answered_call(monkeypatch)
    client.patch(f"/assistant/{params['assistantId']}", json={"firstMessage": "Oi {{customer.name}}, {{motivo}}."},
                 headers=AUTH)
    store.update("calls", params["callId"], {"customer": {"number": "+5511", "name": "Maria"}, "assistantOverrides": {
        "variableValues": {"motivo": "sua consulta é amanhã"}}})
    _close_code(client, params)
    assert fake_bot[0][1].first_message == "Oi Maria, sua consulta é amanhã."


def test_media_stream_refuses_unknown_call_and_silence(monkeypatch, fake_bot):
    client, _, params = _answered_call(monkeypatch)
    assert _close_code(client, {**params, "callId": "made-up"}) == 1008

    monkeypatch.setattr("openring.api.HANDSHAKE_TIMEOUT", 0.2)
    with client.websocket_connect("/ws/twilio") as ws:
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()  # never sends the handshake
    assert closed.value.code == 1008
    assert fake_bot == []


def _wait_until_ended(store, call_id, seconds=20):
    for _ in range(seconds * 10):
        call = store.get("calls", call_id)
        if call["status"] == "ended":
            return call
        time.sleep(0.1)
    return call


def test_caller_who_hangs_up_at_once_ends_the_call(monkeypatch, quiet_providers):
    """Hanging up while the services are still being built used to leave the call 'in-progress' for good."""
    from openring import bot

    built = bot.build_services

    def slow(plan):
        time.sleep(0.5)  # a local voice model takes a moment to load
        return built(plan)

    monkeypatch.setattr(bot, "build_services", slow)
    _pipeline_started(monkeypatch)
    client, store, params = _answered_call(monkeypatch)
    with client.websocket_connect("/ws/twilio") as ws:
        _handshake(ws, params)
        ws.send_text(json.dumps({"event": "stop", "streamSid": "MZ1"}))
    call = _wait_until_ended(store, params["callId"])
    assert (call["status"], call["endedReason"]) == ("ended", "customer-ended-call")


def test_a_phone_call_records_what_the_line_cost(monkeypatch, quiet_providers):
    """A call to a US Twilio number is priced for the line too: a started minute at Twilio's list price."""
    sent = _pipeline_started(monkeypatch)
    client, store, params = _answered_call(monkeypatch)
    with client.websocket_connect("/ws/twilio") as ws:
        _handshake(ws, params)
        for _ in range(150):  # a call hung up within 50 ms lasted 0.0 s, which Twilio's rounding prices at nothing
            if sent:
                break
            time.sleep(0.1)
        time.sleep(0.2)
        ws.send_text(json.dumps({"event": "stop", "streamSid": "MZ1"}))
    call = _wait_until_ended(store, params["callId"])
    breakdown = call["costBreakdown"]
    assert breakdown["line"] == 0.0129 and breakdown["total"] >= breakdown["line"]
    assert "unpriced" not in breakdown or not any("phone line" in name for name in breakdown["unpriced"])


def test_a_provider_that_stops_working_mid_call_ends_the_call_with_the_reason(monkeypatch, quiet_providers):
    """A rejected key (or no credit, or an outage) left the caller in silence until they hung up, and the call was
    recorded as 'customer-ended-call', a success."""
    from pipecat.frames.frames import StartFrame
    from pipecat.processors.frame_processor import FrameProcessor

    from openring import bot

    class RejectedTranscriber(FrameProcessor):
        openring_price = ("transcriber", "deepgram", "nova-3", "pt-BR")
        openring_label = "deepgram nova-3"

        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            await self.push_frame(frame, direction)
            if isinstance(frame, StartFrame):  # as Deepgram answers a bad key: right when the call starts
                await self.push_error("HTTP 401 (bad.py:12): Invalid credentials", force_treat_as_permanent=True)

    quiet = quiet_providers
    monkeypatch.setattr(bot, "build_services", lambda plan: (RejectedTranscriber(), quiet("model"), quiet("voice")))
    _pipeline_started(monkeypatch)
    client, store, params = _answered_call(monkeypatch)
    with client.websocket_connect("/ws/twilio") as ws:
        _handshake(ws, params)
        call = _wait_until_ended(store, params["callId"])  # the caller stays on the line
    assert call["status"] == "ended"
    assert call["endedReason"] == ("pipeline-error: the transcriber (deepgram nova-3) stopped working: "
                                   "HTTP 401: Invalid credentials")


def test_a_provider_that_fails_to_start_ends_the_call(monkeypatch):
    import asyncio

    from openring import bot
    from openring.providers import ProviderError, resolve_plan

    def broken(_plan):
        raise ProviderError("provider 'cartesia' needs an extra package")

    monkeypatch.setattr(bot, "build_services", broken)
    for k in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.setenv(k, "fake")
    store = Store(":memory:")
    call = store.create("calls", {"type": "webCall", "status": "in-progress"})
    asyncio.run(bot.run_call(object(), plan=resolve_plan(ASSISTANT), call_id=call["id"], store=store))
    call = store.get("calls", call["id"])
    assert call["status"] == "ended" and call["endedReason"].startswith("pipeline-error: provider 'cartesia'")
    assert call["messages"] == []


@pytest.mark.parametrize("limits, reason", [((600, 1.0), "silence-timed-out"), ((1.0, 0), "exceeded-max-duration")])
def test_a_call_ends_itself_when_the_caller_is_silent_or_it_runs_too_long(monkeypatch, quiet_providers, limits, reason):
    """Vapi's silenceTimeoutSeconds and maxDurationSeconds: the call hangs up by itself, with Vapi's endedReason
    (made a second long here; the dashboard and the API keep them at 10 s or more)."""
    from openring import bot

    real = bot.CallLimits
    monkeypatch.setattr(bot, "CallLimits", lambda _duration, _silence, now: real(*limits, now))
    _pipeline_started(monkeypatch)
    client, store, params = _answered_call(monkeypatch)
    started = time.monotonic()
    with client.websocket_connect("/ws/twilio") as ws:
        _handshake(ws, params)
        call = _wait_until_ended(store, params["callId"])  # the caller stays on the line
    assert call["endedReason"] == reason
    assert time.monotonic() - started < 10


class _Said(list):
    """The sentences a call said, and when the last one finished."""

    stopped_at = 0.0


def _scripted_call(monkeypatch, limits, on_start=(), frames=None, speaking=0.0, stuck=False, then=None, **server):
    """A call through stub providers: the transcriber pushes `on_start` when the call starts (and runs `then()`), and
    the voice "says" each sentence in `speaking` seconds, recording it (and its frame, in `frames`). With `stuck`, the
    model never gets past the first sentence. Returns (client, store, params, sentences)."""
    from pipecat.frames.frames import BotStartedSpeakingFrame, BotStoppedSpeakingFrame, StartFrame, TTSSpeakFrame
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

    from openring import bot

    said = _Said()

    class Stub(FrameProcessor):
        def __init__(self, layer):
            super().__init__()
            self.layer, self.openring_label, self.openring_price = layer, layer, (layer, "none", None, None)

        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            await self.push_frame(frame, direction)
            if self.layer == "transcriber" and isinstance(frame, StartFrame):
                for early in on_start:
                    await self.push_frame(early)
                if then:
                    self.create_task(then())
            if self.layer == "model" and stuck and isinstance(frame, TTSSpeakFrame):
                await asyncio.sleep(60)
            if self.layer == "voice" and isinstance(frame, TTSSpeakFrame):
                said.append(frame.text)
                if frames is not None:
                    frames.append(frame)
                await self.push_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
                await asyncio.sleep(speaking)
                await self.push_frame(BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)
                said.stopped_at = time.monotonic()

    real = bot.CallLimits
    monkeypatch.setattr(bot, "CallLimits", lambda _duration, _silence, now: real(*limits, now))
    monkeypatch.setattr(bot, "build_services", lambda plan: (Stub("transcriber"), Stub("model"), Stub("voice")))
    _pipeline_started(monkeypatch)
    return (*_answered_call(monkeypatch, **server), said)


def _working_frames():
    from pipecat.frames.frames import (
        FunctionCallFromLLM,
        FunctionCallsStartedFrame,
        LLMFullResponseStartFrame,
        UserStartedSpeakingFrame,
    )

    tool = FunctionCallFromLLM(function_name="lookup", tool_call_id="t1", arguments={}, context=None)
    return {"caller talking": UserStartedSpeakingFrame(), "model thinking": LLMFullResponseStartFrame(),
            "tool running": FunctionCallsStartedFrame(function_calls=[tool])}


@pytest.mark.parametrize("what", ["caller talking", "model thinking", "tool running"])
def test_silence_never_ends_a_call_while_someone_is_working(monkeypatch, what):
    frame = _working_frames()[what]
    client, store, params, _said = _scripted_call(monkeypatch, (2.0, 0.5), on_start=[frame])
    with client.websocket_connect("/ws/twilio") as ws:
        _handshake(ws, params)
        call = _wait_until_ended(store, params["callId"])
    assert call["endedReason"] == "exceeded-max-duration"  # the silence limit (0.5 s) never fired


def test_the_caller_gets_the_whole_silence_limit_after_the_assistant_stops_talking(monkeypatch):
    """A greeting or an answer longer than the limit must not hang up the moment it ends."""
    client, store, params, said = _scripted_call(monkeypatch, (600, 1.0), speaking=1.5)
    with client.websocket_connect("/ws/twilio") as ws:
        _handshake(ws, params)
        call = _wait_until_ended(store, params["callId"])
    assert call["endedReason"] == "silence-timed-out"
    assert time.monotonic() - said.stopped_at >= 0.9


def test_a_call_past_its_limit_ends_even_if_a_provider_hangs(monkeypatch):
    from openring import bot

    monkeypatch.setattr(bot, "END_GRACE_SECONDS", 0.5)
    client, store, params, _said = _scripted_call(monkeypatch, (0.5, 0), stuck=True)
    with client.websocket_connect("/ws/twilio") as ws:
        _handshake(ws, params)
        call = _wait_until_ended(store, params["callId"], seconds=10)
    assert call["status"] == "ended" and call["endedReason"] == "exceeded-max-duration"


def test_a_limit_keeps_the_reason_of_a_call_already_ending(monkeypatch):
    """A voicemail message that outlasts the limit is still a voicemail, with no goodbye recorded after it."""
    client, store, params, said = _scripted_call(monkeypatch, (0.5, 0), speaking=1.5)
    assistant = store.list("assistants")[0]
    store.update("assistants", assistant["id"], {"voicemailMessage": "Please call us back.", "endCallMessage": "Bye."})
    store.update("calls", params["callId"], {"answeredBy": "machine_end_beep"})
    with client.websocket_connect("/ws/twilio") as ws:
        _handshake(ws, params)
        call = _wait_until_ended(store, params["callId"])
    assert call["endedReason"] == "voicemail" and said == ["Please call us back."]


def test_pipecats_own_idle_limit_waits_for_the_silence_limit_and_says_why(monkeypatch):
    """Pipecat cancels a call with no activity after 5 minutes: an hour of allowed silence must get its hour."""
    from pipecat.pipeline import task

    real, idle = task.PipelineTask, []

    def patient(*args, **kwargs):
        idle.append(kwargs["idle_timeout_secs"])
        return real(*args, **{**kwargs, "idle_timeout_secs": 0.5})

    monkeypatch.setattr(task, "PipelineTask", patient)
    client, store, params, _said = _scripted_call(monkeypatch, (600, 0))
    store.update("assistants", store.list("assistants")[0]["id"], {"silenceTimeoutSeconds": 3600})
    with client.websocket_connect("/ws/twilio") as ws:
        _handshake(ws, params)
        call = _wait_until_ended(store, params["callId"])
    assert idle == [3660]
    assert call["endedReason"] == "silence-timed-out"  # not "customer-ended-call", which reads as a normal call


def _transfer_on_start(monkeypatch, limits, redirect, interrupted=False):
    """A call whose assistant asks for a transfer as soon as it starts. Returns _scripted_call's and the hang-ups."""
    from pipecat.serializers.twilio import TwilioFrameSerializer

    from openring import bot, tools, twilio

    controls, hang_ups = {}, []

    async def transfer():
        if interrupted:  # Pipecat cancels a tool's handler when the caller speaks
            class Params:
                arguments, tool_call_id = {"destination": "+15557654321"}, "t1"

                @staticmethod
                async def result_callback(_result, properties=None):
                    pass

            tool = {"type": "transferCall", "destinations": [{"type": "number", "number": "+15557654321"}]}
            running = asyncio.ensure_future(tools.make_handler(tool, controls["call"])(Params()))
            await asyncio.sleep(0.2)
            running.cancel()
            return
        try:
            await controls["call"].transfer("+15557654321", None)
        except twilio.TwilioError:
            pass

    async def hang_up(_serializer):
        hang_ups.append(True)

    monkeypatch.setattr(bot, "register_tools", lambda _llm, _tools, given: controls.update(call=given))
    monkeypatch.setattr(twilio, "redirect_call", redirect)
    monkeypatch.setattr(TwilioFrameSerializer, "_hang_up_call", hang_up)
    return (*_scripted_call(monkeypatch, limits, then=transfer, twilio_account_sid="AC1", twilio_auth_token="tok"),
            hang_ups)


def test_a_limit_reached_during_a_transfer_does_not_hang_up_on_the_caller(monkeypatch):
    redirected = []

    async def redirect(*_args):
        redirected.append(True)

    client, store, params, said, hang_ups = _transfer_on_start(monkeypatch, (0.5, 0), redirect)
    store.update("assistants", store.list("assistants")[0]["id"], {"endCallMessage": "Bye."})
    with client.websocket_connect("/ws/twilio") as ws:
        _handshake(ws, params)
        for _ in range(100):  # the transfer waits until the line is quiet, past the limit
            if redirected:
                break
            time.sleep(0.1)
        # Twilio closes the media stream as it moves the call.
    call = _wait_until_ended(store, params["callId"])
    assert redirected and call["endedReason"] == "assistant-forwarded-call"
    assert "Bye." not in said and not hang_ups


def test_a_caller_speaking_does_not_cancel_a_transfer(monkeypatch):
    redirected = []

    async def redirect(*_args):
        redirected.append(True)

    client, store, params, _said, _hang_ups = _transfer_on_start(monkeypatch, (0, 0), redirect, interrupted=True)
    with client.websocket_connect("/ws/twilio") as ws:
        _handshake(ws, params)
        for _ in range(50):
            if redirected:
                break
            time.sleep(0.1)
    call = _wait_until_ended(store, params["callId"])
    assert redirected and call["endedReason"] == "assistant-forwarded-call"


def test_a_transfer_whose_stream_never_closes_still_ends(monkeypatch):
    from openring import bot

    async def redirect(*_args):
        pass

    monkeypatch.setattr(bot, "TRANSFER_SECONDS", 1.5)
    monkeypatch.setattr(bot, "END_GRACE_SECONDS", 0.5)
    client, store, params, _said, hang_ups = _transfer_on_start(monkeypatch, (0.5, 0), redirect)
    with client.websocket_connect("/ws/twilio") as ws:
        _handshake(ws, params)
        call = _wait_until_ended(store, params["callId"], seconds=10)
    assert call["endedReason"] == "assistant-forwarded-call" and not hang_ups  # the caller stays with the new line


def test_the_limits_apply_again_when_a_transfer_fails(monkeypatch):
    from openring import twilio

    async def redirect(*_args):
        raise twilio.TwilioError("no such number")

    client, store, params, _said, hang_ups = _transfer_on_start(monkeypatch, (0.5, 0), redirect)
    with client.websocket_connect("/ws/twilio") as ws:
        _handshake(ws, params)
        call = _wait_until_ended(store, params["callId"])
    assert call["endedReason"] == "exceeded-max-duration" and hang_ups


def test_a_quiet_caller_hears_the_idle_message_then_the_call_ends(monkeypatch):
    """Vapi's messagePlan: after the assistant speaks, a quiet caller hears an idle message, up to the limit;
    the end-call message comes last."""
    from openring import providers

    monkeypatch.setattr(providers, "IDLE_LIMITS", (("idleTimeoutSeconds", 0.1, 60),
                                                   ("idleMessageMaxSpokenCount", 1, 10)))
    client, store, params, said = _scripted_call(monkeypatch, (600, 2.0))
    assistant = store.list("assistants")[0]
    store.update("assistants", assistant["id"], {
        "endCallMessage": "Goodbye.",
        "messagePlan": {"idleMessages": ["Still there?"], "idleTimeoutSeconds": 0.2, "idleMessageMaxSpokenCount": 2}})
    with client.websocket_connect("/ws/twilio") as ws:
        _handshake(ws, params)
        call = _wait_until_ended(store, params["callId"])
    assert call["endedReason"] == "silence-timed-out"
    assert said == [ASSISTANT["firstMessage"], "Still there?", "Still there?", "Goodbye."]


def test_a_voicemail_gets_the_message_and_the_call_hangs_up(monkeypatch):
    frames = []
    client, store, params, said = _scripted_call(monkeypatch, (600, 30), frames=frames)
    assistant = store.list("assistants")[0]
    store.update("assistants", assistant["id"], {"voicemailMessage": "Hi {{customer.name}}, please call us back."})
    store.update("calls", params["callId"], {"answeredBy": "machine_end_beep", "customer": {"name": "Maria"}})
    with client.websocket_connect("/ws/twilio") as ws:
        _handshake(ws, params)
        call = _wait_until_ended(store, params["callId"])
    assert call["endedReason"] == "voicemail"
    assert said == ["Hi Maria, please call us back."]  # not the greeting
    assert frames[0].interruptible is False  # the beep or noise on the line must not cut it off
    assert [m["message"] for m in call["messages"]] == said
