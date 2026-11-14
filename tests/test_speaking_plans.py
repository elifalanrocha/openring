"""Vapi's startSpeakingPlan, stopSpeakingPlan and keypadInputPlan: when the assistant answers, what interrupts it, and
keys the caller presses."""

import asyncio

import pytest

from openring.providers import ProviderError, resolve_plan

BASE = {"transcriber": {"provider": "deepgram", "language": "en"},
        "model": {"provider": "groq", "model": "openai/gpt-oss-20b"}, "voice": {"provider": "kokoro"}}


def plan(**fields):
    return resolve_plan({**BASE, **fields}, check_keys=False)


def test_nothing_set_leaves_pipecats_defaults():
    p = plan()
    assert (p.vad_start_secs, p.vad_stop_secs, p.interrupt_words, p.keypad) == (None, None, 0, False)
    assert not p.warnings


def test_the_plans_reach_the_call_and_what_is_not_used_yet_is_named():
    p = plan(startSpeakingPlan={"waitSeconds": 0.8, "smartEndpointingPlan": {"provider": "livekit"}},
             stopSpeakingPlan={"numWords": 2, "voiceSeconds": 0.3, "backoffSeconds": 3},
             keypadInputPlan={"enabled": True, "timeoutSeconds": 3, "delimiters": ["#", "*"]})
    assert (p.vad_stop_secs, p.vad_start_secs, p.interrupt_words) == (0.8, 0.3, 2)
    assert (p.keypad, p.keypad_timeout, p.keypad_delimiters) == (True, 3.0, ("#", "*"))
    assert "stopSpeakingPlan.backoffSeconds" in p.warnings[-1] and "smartEndpointingPlan" in p.warnings[-1]
    assert not plan(stopSpeakingPlan={"backoffSeconds": 1}).warnings  # Vapi's default needs nothing
    assert plan(keypadInputPlan={"enabled": False}).keypad is False


@pytest.mark.parametrize("fields", [
    {"startSpeakingPlan": {"waitSeconds": 9}}, {"stopSpeakingPlan": {"numWords": 11}},
    {"stopSpeakingPlan": {"voiceSeconds": 1}}, {"stopSpeakingPlan": {"numWords": True}},
    {"keypadInputPlan": {"enabled": True, "delimiters": ["0"]}}, {"keypadInputPlan": {"enabled": True,
                                                                                     "timeoutSeconds": 99}},
    {"startSpeakingPlan": "fast"},
])
def test_values_out_of_vapis_ranges_are_refused(fields):
    with pytest.raises(ProviderError):
        plan(**fields)


def keys_sent(fields, presses, wait=0.0):
    """Run the keypad step on `presses` (with `wait` seconds after) and return what it told the model."""
    from pipecat.audio.dtmf.types import KeypadEntry
    from pipecat.frames.frames import InputDTMFFrame, LLMMessagesAppendFrame
    from pipecat.tests.utils import SleepFrame, run_test

    from openring.bot import keypad_input

    frames = [InputDTMFFrame(KeypadEntry(k)) for k in presses] + ([SleepFrame(sleep=wait)] if wait else [])
    down, _up = asyncio.run(run_test(keypad_input(plan(keypadInputPlan=fields)), frames_to_send=frames))
    return [(f.messages, f.run_llm) for f in down if isinstance(f, LLMMessagesAppendFrame)]


def test_keys_go_to_the_model_when_a_delimiter_is_pressed():
    assert keys_sent({"enabled": True, "timeoutSeconds": 5}, "1234#") == [
        ([{"role": "user", "content": "1234"}], True)]


def test_keys_go_to_the_model_after_a_pause():
    assert keys_sent({"enabled": True, "timeoutSeconds": 0.2, "delimiters": []}, "42", wait=0.5) == [
        ([{"role": "user", "content": "42"}], True)]


def test_a_delimiter_alone_sends_nothing():
    assert keys_sent({"enabled": True}, "#") == []


def test_a_call_runs_with_every_plan_set(monkeypatch, quiet_providers):
    """The VAD timings, the words to interrupt and the keypad step all go into a real pipeline, and the call ends."""
    import json
    import time

    from test_api import AUTH
    from test_call_flow import _answered_call, _handshake, _pipeline_started, _wait_until_ended

    sent = _pipeline_started(monkeypatch)
    client, store, params = _answered_call(monkeypatch)
    assistant = store.list("assistants")[0]
    r = client.patch(f"/assistant/{assistant['id']}", headers=AUTH, json={
        "startSpeakingPlan": {"waitSeconds": 0.6}, "stopSpeakingPlan": {"numWords": 2, "voiceSeconds": 0.3},
        "keypadInputPlan": {"enabled": True, "timeoutSeconds": 1}})
    assert r.status_code == 200, r.text
    with client.websocket_connect("/ws/twilio") as ws:
        _handshake(ws, params)
        for _ in range(150):
            if sent:
                break
            time.sleep(0.1)
        ws.send_text(json.dumps({"event": "dtmf", "streamSid": "MZ1", "dtmf": {"digit": "5"}}))
        ws.send_text(json.dumps({"event": "stop", "streamSid": "MZ1"}))
    call = _wait_until_ended(store, params["callId"])
    assert call["status"] == "ended" and not call["endedReason"].startswith("pipeline-error"), call["endedReason"]
