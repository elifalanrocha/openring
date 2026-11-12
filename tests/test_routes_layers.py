"""Routes of transcribers and voices: the same idea as model routes, in order, resting after a failure."""

import re

import pytest

from openring import routes
from openring.providers import build_services, resolve_plan

from test_api import ASSISTANT, AUTH, make_client

LISTENING = {"name": "Listen", "kind": "transcriber", "cooldownMinutes": 5, "models": [
    {"provider": "deepgram", "model": "nova-3"}, {"provider": "assembly-ai"}, {"provider": "soniox"}]}
SPEAKING = {"name": "Speak", "kind": "voice", "cooldownMinutes": 5, "models": [
    {"provider": "11labs", "voiceId": "v-eleven"}, {"provider": "cartesia", "voiceId": "v-cartesia"}]}


@pytest.fixture(autouse=True)
def nothing_resting(monkeypatch):
    monkeypatch.setattr(routes, "_resting", {})
    monkeypatch.setattr(routes, "_speed", {})
    for k in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "ASSEMBLYAI_API_KEY", "SONIOX_API_KEY", "ELEVENLABS_API_KEY",
              "CARTESIA_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.setenv(k, "fake")


def lookup(*docs):
    return lambda route_id: next(({**d, "id": route_id} for d in docs if d["name"] == route_id), None)


def on_routes(**layers):
    return {**ASSISTANT, **{layer: {**ASSISTANT[layer], "routeId": name} for layer, name in layers.items()}}


def test_a_transcriber_route_gives_the_first_and_keeps_the_rest_as_fallbacks():
    assistant = on_routes(transcriber="Listen")
    plan = resolve_plan(routes.expand(assistant, lookup(LISTENING)))
    assert (plan.stt_provider, plan.stt_model, plan.stt_language) == ("deepgram", "nova-3", "pt-BR")
    assert [(p.stt_provider, p.stt_language) for p in plan.fallbacks["transcriber"]] == [
        ("assembly-ai", "pt-BR"), ("soniox", "pt-BR")]  # the language the assistant asked for follows each one
    assert plan.stt_route["cooldownMinutes"] == 5 and plan.stt_route["kind"] == "transcriber"
    assert plan.route is None and plan.tts_route is None


def test_a_voice_route_gives_the_first_voice_and_keeps_the_rest_as_fallbacks():
    plan = resolve_plan(routes.expand(on_routes(voice="Speak"), lookup(SPEAKING)))
    assert (plan.tts_provider, plan.tts_voice) == ("elevenlabs", "v-eleven")
    assert [(p.tts_provider, p.tts_voice) for p in plan.fallbacks["voice"]] == [("cartesia", "v-cartesia")]
    assert plan.tts_route["kind"] == "voice"


def test_all_three_routes_work_at_once_and_a_route_of_another_kind_is_ignored():
    models = {"name": "Brains", "models": [{"provider": "groq", "model": "openai/gpt-oss-20b"},
                                           {"provider": "openai", "model": "gpt-4.1-mini"}]}
    both = {**on_routes(transcriber="Listen", voice="Speak"), "model": {**ASSISTANT["model"], "routeId": "Brains"}}
    plan = resolve_plan(routes.expand(both, lookup(LISTENING, SPEAKING, models)))
    assert (plan.stt_provider, plan.llm_provider, plan.tts_provider) == ("deepgram", "groq", "elevenlabs")
    assert plan.fallbacks["transcriber"] and plan.fallbacks["model"] and plan.fallbacks["voice"]
    wrong = {**ASSISTANT, "voice": {**ASSISTANT["voice"], "routeId": "Listen"},
             "model": {**ASSISTANT["model"], "routeId": "Speak"}}
    kept = resolve_plan(routes.expand(wrong, lookup(LISTENING, SPEAKING)))
    assert (kept.tts_provider, kept.tts_voice, kept.llm_provider) == ("elevenlabs", "voice123", "groq")
    assert not kept.fallbacks.get("voice") and not kept.fallbacks.get("model")


def test_what_the_assistant_already_lists_as_fallback_is_kept_besides_the_route_members():
    assistant = on_routes(voice="Speak")
    assistant["voice"]["fallbackPlan"] = {"voices": [{"provider": "openai", "voiceId": "alloy"}], "other": 1}
    expanded = routes.expand(assistant, lookup(SPEAKING))
    assert expanded["voice"]["fallbackPlan"]["other"] == 1
    assert [v["provider"] for v in expanded["voice"]["fallbackPlan"]["voices"]] == ["cartesia"]  # the route decides


def test_a_member_that_let_the_route_down_rests_and_the_next_call_starts_on_the_next():
    plan = resolve_plan(routes.expand(on_routes(transcriber="Listen", voice="Speak"), lookup(LISTENING, SPEAKING)))
    stt, _, tts = build_services(plan)
    first_stt, first_tts = stt.services[0], tts.services[0]
    assert first_stt.openring_member == ("deepgram", "nova-3") and first_tts.openring_member == ("11labs", "v-eleven")
    routes.let_down(None, first_stt)  # not on a route: nothing rests
    assert routes.resting("deepgram", "nova-3", "transcriber") == 0
    routes.let_down(plan.stt_route, first_stt)
    routes.let_down(plan.tts_route, first_tts)
    assert routes.resting("deepgram", "nova-3", "transcriber") > 0 and routes.resting("11labs", "v-eleven", "voice") > 0
    again = resolve_plan(routes.expand(on_routes(transcriber="Listen", voice="Speak"), lookup(LISTENING, SPEAKING)))
    assert (again.stt_provider, again.tts_provider) == ("assembly-ai", "cartesia")
    assert again.fallbacks["transcriber"][-1].stt_provider == "deepgram"  # resting ones go last, still there


def test_resting_a_transcriber_never_rests_a_model_with_the_same_name():
    routes.rest("openai", "gpt-4.1-mini", 5, "transcriber")
    routes.rest("openai", "alloy", 5, "voice")
    assert routes.resting("openai", "gpt-4.1-mini") == 0  # the model key is not the transcriber's
    assert routes.resting("openai", "gpt-4.1-mini", "voice") == 0 and routes.resting("openai", "alloy", "voice") > 0


def test_cheapest_orders_by_price_and_leaves_the_unpriced_last():
    route = {"name": "Cheap", "kind": "transcriber", "order": "cheapest", "models": [
        {"provider": "deepgram", "model": "nova-3"}, {"provider": "made-up"}, {"provider": "assembly-ai"}]}
    # Per minute of audio, from prices.py: Soniox 0.002, AssemblyAI 0.0025, Deepgram 0.0077; no price for the rest.
    route["models"].append({"provider": "soniox"})
    assert [m["provider"] for m in routes.ordered(route)] == ["soniox", "assembly-ai", "deepgram", "made-up"]
    voices = {"name": "Cheap", "kind": "voice", "order": "cheapest", "models": [
        {"provider": "11labs", "voiceId": "a"}, {"provider": "kokoro", "voiceId": "af_heart"}]}
    assert routes.ordered(voices)[0]["provider"] == "kokoro"  # free, runs on this server


def test_route_bodies_of_each_kind_are_checked():
    for change, words in (({"kind": "robot"}, "kind"), ({"models": [{"provider": "11labs"}], "kind": "voice"},
                                                        "voiceId"),
                          ({"preset": "fastest"}, "fastest"), ({"order": "fastest"}, "order")):
        with pytest.raises(ValueError, match=re.escape(words)):
            routes.clean({**LISTENING, **change} if change.get("kind") != "voice" and "voiceId" not in words
                         else {**SPEAKING, **change})
    cleaned = routes.clean({**LISTENING, "maxLatencyMs": 900, "models": [
        {"provider": "deepgram", "model": "nova-3", "language": "en", "url": "https://elsewhere.example",
         "apiKey": "secret"}]})
    assert cleaned["maxLatencyMs"] is None  # nothing measures how fast a transcriber answers
    assert cleaned["models"] == [{"provider": "deepgram", "model": "nova-3", "language": "en"}]  # no url or key
    preset = routes.clean({**LISTENING, "preset": "reliable"})
    assert preset["maxLatencyMs"] is None and preset["cooldownMinutes"] == 15


def test_the_route_api_serves_all_kinds_and_checks_each_assistants_route():
    client = make_client()
    listen = client.post("/model-route", json=LISTENING, headers=AUTH)
    assert listen.status_code == 201, listen.text
    assert listen.json()["kind"] == "transcriber" and "firstTokenMs" not in listen.json()["models"][0]
    speak = client.post("/model-route", json=SPEAKING, headers=AUTH).json()
    brains = client.post("/model-route", json={"name": "Brains", "models": [
        {"provider": "groq", "model": "openai/gpt-oss-20b"}]}, headers=AUTH).json()
    assert "kind" not in brains or brains["kind"] == "model"
    assert [r["name"] for r in client.get("/model-route?kind=voice", headers=AUTH).json()] == ["Speak"]
    assert len(client.get("/model-route", headers=AUTH).json()) == 3
    bad = client.post("/model-route", json={**SPEAKING, "models": [{"provider": "nope", "voiceId": "x"}]},
                      headers=AUTH)
    assert bad.status_code == 400 and "models[0]" in bad.json()["detail"]
    assert client.post(f"/model-route/{speak['id']}/measure", headers=AUTH).status_code == 400

    ok = on_routes(transcriber=listen.json()["id"], voice=speak["id"])
    assert client.post("/assistant", json=ok, headers=AUTH).status_code == 201
    crossed = on_routes(voice=listen.json()["id"])  # a transcriber route on the voice
    r = client.post("/assistant", json=crossed, headers=AUTH)
    assert r.status_code == 400 and "voice.routeId" in r.json()["detail"] and "transcribers" in r.json()["detail"]
    r = client.post("/assistant", json={**ASSISTANT, "model": {**ASSISTANT["model"], "routeId": speak["id"]}},
                    headers=AUTH)
    assert r.status_code == 400 and "model.routeId" in r.json()["detail"]
    r = client.post("/assistant", json=on_routes(transcriber="made-up"), headers=AUTH)
    assert r.status_code == 400 and "transcriber.routeId" in r.json()["detail"]


def test_assistants_on_a_deleted_transcriber_or_voice_route_keep_their_own_and_stay_editable():
    client = make_client()
    listen = client.post("/model-route", json=LISTENING, headers=AUTH).json()
    speak = client.post("/model-route", json=SPEAKING, headers=AUTH).json()
    made = client.post("/assistant", json=on_routes(transcriber=listen["id"], voice=speak["id"]), headers=AUTH).json()
    for route in (listen, speak):
        assert client.delete(f"/model-route/{route['id']}", headers=AUTH).status_code == 200
    kept = client.get(f"/assistant/{made['id']}", headers=AUTH).json()
    assert "routeId" not in kept["transcriber"] and "routeId" not in kept["voice"]
    assert kept["voice"]["voiceId"] == "voice123" and kept["transcriber"]["provider"] == "deepgram"
    assert client.patch(f"/assistant/{made['id']}", json={"name": "Again"}, headers=AUTH).status_code == 200


def test_a_switch_on_any_layer_rests_that_layers_member_on_that_layers_route():
    plan = resolve_plan(routes.expand(on_routes(transcriber="Listen", voice="Speak"), lookup(LISTENING, SPEAKING)))
    stt, llm, tts = build_services(plan)
    routes.rest_after_switch(plan, "transcriber", stt.services[0])
    assert routes.resting("deepgram", "nova-3", "transcriber") > 0
    assert routes.resting("11labs", "v-eleven", "voice") == 0  # only the layer that switched
    routes.rest_after_switch(plan, "voice", tts.services[0])
    assert routes.resting("11labs", "v-eleven", "voice") > 0
    routes.rest_after_switch(plan, "model", llm)  # no model route on this assistant: nothing happens, no error
    assert not [k for k in routes._resting if len(k) == 2]
