"""Drafting an assistant from a description (`POST /assistant/draft`), against a model server on this computer."""

import json

import pytest

from openring import drafts

from test_api import AUTH, make_client

DESCRIPTION = "A receptionist for a dental clinic that books and moves appointments, never gives prices."
GOOD = {"name": "Clinic Reception", "firstMessage": "Hi, Sorriso Clinic, how can I help?",
        "systemPrompt": "You are the receptionist of a dental clinic. Keep answers short. Never give prices."}


def chunks(text, size=17):
    return [{"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "m",
             "choices": [{"index": 0, "delta": {"content": text[i:i + size]}, "finish_reason": None}]}
            for i in range(0, len(text), size)]


def ask(client, url, **body):
    return client.post("/assistant/draft", headers=AUTH,
                       json={"description": DESCRIPTION, "model": {"provider": "custom-llm", "url": url}, **body})


def test_a_draft_is_written_by_the_model_and_nothing_is_saved(model_server):
    url, scripts, asked = model_server
    scripts["v1"] = [chunks("Sure! Here it is:\n" + json.dumps(GOOD))]
    client = make_client()
    r = ask(client, url + "/v1")
    assert r.status_code == 200, r.text
    d = r.json()
    assert (d["name"], d["firstMessage"]) == (GOOD["name"], GOOD["firstMessage"])
    assert d["model"]["messages"] == [{"role": "system", "content": GOOD["systemPrompt"]}]
    assert d["draftedWith"].startswith("custom-llm")
    assert client.get("/assistant", headers=AUTH).json() == []  # a draft, not an assistant
    sent = asked[0][1]
    assert sent["messages"][0]["role"] == "system" and "JSON" in sent["messages"][0]["content"]
    assert sent["messages"][1]["content"] == DESCRIPTION
    assert client.post("/assistant/draft", json={"description": DESCRIPTION}).status_code == 401


def test_an_answer_that_is_not_a_draft_is_refused(model_server):
    url, scripts, _asked = model_server
    client = make_client()
    for answer in ("I can't help with that.", json.dumps({**GOOD, "systemPrompt": ""}), "{broken"):
        scripts["v1"] = [chunks(answer)]
        r = ask(client, url + "/v1")
        assert r.status_code == 400 and "usable draft" in r.text, answer
    scripts["refuse"] = lambda body: True
    r = ask(client, url + "/v1")
    assert r.status_code == 400 and "HTTP 400" in r.text


@pytest.mark.parametrize("description", ["", "short", "x" * 2001])
def test_the_description_says_enough_and_not_too_much(description):
    r = make_client().post("/assistant/draft", headers=AUTH, json={"description": description,
                                                                   "model": {"provider": "custom-llm", "url": "x"}})
    assert r.status_code == 400


def test_without_a_connected_model_it_says_what_to_do(monkeypatch):
    for provider, (_base, env) in drafts.LLM_PROVIDERS.items():
        if env:
            monkeypatch.delenv(env, raising=False)
    r = make_client().post("/assistant/draft", headers=AUTH, json={"description": DESCRIPTION})
    assert r.status_code == 400 and "connect a model provider" in r.text


def test_it_picks_the_first_connected_drafter(monkeypatch):
    for _provider, (_base, env) in drafts.LLM_PROVIDERS.items():
        if env:
            monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    provider, model, base, key = drafts.choose(None)
    assert (provider, model, key) == ("openai", "gpt-4.1-mini", "sk-test") and base.startswith("https://")
    with pytest.raises(drafts.DraftError):
        drafts.choose({"provider": "made-up"})


def test_a_recording_assistant_costs_only_the_line(monkeypatch, tmp_path):
    from test_announcements import wav

    monkeypatch.setenv("OPENRING_ANNOUNCEMENTS_DIR", str(tmp_path))
    client = make_client()
    audio = client.post("/announcement", headers=AUTH, files={"file": ("a.wav", wav(), "audio/wav")}).json()
    a = client.post("/assistant", headers=AUTH, json={"name": "P", "announcement": {"audioId": audio["id"]}}).json()
    assert client.get(f"/assistant/{a['id']}/profile", headers=AUTH).json() == {"announcement": True}
