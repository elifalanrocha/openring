"""Callers that are AI agents acting for a person: noticed from what they say, marked on the call, answered briefly."""

import pytest

from openring.bot import AI_CALLER_HINT, ai_caller
from openring.providers import resolve_plan

from test_api import AUTH


@pytest.mark.parametrize("said", [
    "Hi, I am an AI assistant calling on behalf of Maria to book a cleaning.",
    "Hello, this is Google's automated system calling for a customer.",
    "This is an automated call to check your opening hours.",
    "Hi, I'm a virtual agent. Do you have a table for four tonight?",
    "Olá, sou um assistente virtual e ligo em nome de um cliente.",
    "Hola, soy un asistente virtual, llamo en nombre de un cliente.",
])
def test_an_agent_is_noticed_in_english_portuguese_and_spanish(said):
    assert ai_caller(said)


@pytest.mark.parametrize("said", [
    "I need a table for two tonight.", "Sou um cliente antigo e quero remarcar.", "I'm calling about my order.",
    "My assistant will call you back.", "Is this the virtual tour company?", "",
])
def test_people_are_not_taken_for_agents(said):
    assert ai_caller(said) is None


def test_the_hint_is_on_unless_the_assistant_turns_it_off():
    base = {"transcriber": {"provider": "deepgram", "language": "en"},
            "model": {"provider": "groq", "model": "openai/gpt-oss-20b"}, "voice": {"provider": "kokoro"}}
    assert resolve_plan(base, check_keys=False).ai_caller_hint is True
    assert resolve_plan({**base, "aiCallerPlan": {"enabled": False}}, check_keys=False).ai_caller_hint is False
    assert "never" in AI_CALLER_HINT.lower() and "payment" in AI_CALLER_HINT


def test_a_call_from_an_agent_is_marked(monkeypatch, quiet_providers):
    from pipecat.frames.frames import TranscriptionFrame

    from test_call_flow import _handshake, _scripted_call, _wait_until_ended

    heard = [TranscriptionFrame("Hi, I'm an AI assistant calling on behalf of Ana.", "caller", "2026-11-15T10:00:00Z")]
    client, store, params, _said = _scripted_call(monkeypatch, (1.5, 0), on_start=heard)
    with client.websocket_connect("/ws/twilio") as ws:
        _handshake(ws, params)
        call = _wait_until_ended(store, params["callId"])
    assert call["callerIsAI"] is True and "AI assistant" in call["callerIsAISaid"]
    assert not call["endedReason"].startswith("pipeline-error"), call["endedReason"]
    assert client.get(f"/call/{params['callId']}", headers=AUTH).json()["callerIsAI"] is True
