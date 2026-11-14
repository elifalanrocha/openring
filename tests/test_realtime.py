"""Speech-to-speech models (OpenAI Realtime): the model hears and speaks, with no transcriber or voice."""

import json
import time

import pytest

from openring.providers import ProviderError, build_services, realtime_of, resolve_plan

REALTIME = {"transcriber": {"provider": "deepgram", "language": "en"},
            "model": {"provider": "openai", "model": "gpt-realtime",
                      "messages": [{"role": "system", "content": "You are a receptionist."}]},
            "voice": {"provider": "openai", "voiceId": "cedar"}}


def test_which_models_are_speech_to_speech():
    assert realtime_of("openai", "gpt-realtime") == "openai"
    assert realtime_of("openai", "gpt-realtime-2025-08-28") == "openai"
    assert realtime_of("openai", "gpt-4o-mini-realtime-preview") == "openai"
    assert realtime_of("openai", "gpt-4.1-mini") is None and realtime_of("groq", "gpt-realtime") is None


def test_only_the_models_key_is_needed(monkeypatch):
    for k in ("DEEPGRAM_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    plan = resolve_plan(REALTIME)  # no transcriber or voice key, and that's fine
    assert (plan.realtime, plan.realtime_voice) == ("openai", "cedar") and not plan.warnings
    monkeypatch.delenv("OPENAI_API_KEY")
    with pytest.raises(ProviderError):
        resolve_plan(REALTIME)


def test_a_voice_from_another_provider_falls_back_to_the_models_own():
    plan = resolve_plan({**REALTIME, "voice": {"provider": "11labs", "voiceId": "x"}, "endCallMessage": "Bye",
                         "model": {**REALTIME["model"], "fallbackModels": ["gpt-4.1-mini"]}}, check_keys=False)
    assert plan.realtime_voice == "marin"
    text = " ".join(plan.warnings)
    assert "marin" in text and "fallbacks" in text and "end-call" in text
    assert plan.fallbacks == {}


def test_the_service_is_openai_realtime_with_the_voice(monkeypatch):
    pytest.importorskip("pipecat.services.openai.realtime.llm")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    stt, llm, tts = build_services(resolve_plan({**REALTIME, "voice": {**REALTIME["voice"], "speed": 1.1}}))
    assert stt is None and tts is None
    assert type(llm).__name__ == "OpenAIRealtimeLLMService"
    session = llm._settings.session_properties
    assert session.audio.output.voice == "cedar" and session.audio.output.speed == 1.1
    assert llm._settings.model == "gpt-realtime"


def test_a_call_runs_with_no_transcriber_or_voice(monkeypatch, quiet_providers):
    from openring import bot

    from test_api import AUTH
    from test_call_flow import _answered_call, _handshake, _pipeline_started, _wait_until_ended

    built = []

    def realtime_stack(plan):
        built.append(plan)
        return None, quiet_providers("model"), None

    monkeypatch.setattr(bot, "build_services", realtime_stack)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    sent = _pipeline_started(monkeypatch)
    client, store, params = _answered_call(monkeypatch)
    assistant = store.list("assistants")[0]
    assert client.patch(f"/assistant/{assistant['id']}", headers=AUTH, json={
        "model": REALTIME["model"], "voice": REALTIME["voice"]}).status_code == 200
    with client.websocket_connect("/ws/twilio") as ws:
        _handshake(ws, params)
        for _ in range(150):
            if sent:
                break
            time.sleep(0.1)
        ws.send_text(json.dumps({"event": "stop", "streamSid": "MZ1"}))
    call = _wait_until_ended(store, params["callId"])
    assert built and built[0].realtime == "openai"
    assert call["status"] == "ended" and not call["endedReason"].startswith("pipeline-error"), call["endedReason"]
