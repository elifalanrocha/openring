"""QA attacks on routes of transcribers and voices: odd bodies, changing a route under its assistants."""

import pytest
from test_routes_layers import LISTENING, SPEAKING, nothing_resting, on_routes  # noqa: F401  (fixture)

from test_api import AUTH, make_client


@pytest.mark.parametrize("kind, base", [("transcriber", LISTENING), ("voice", SPEAKING), ("model", None)])
def test_odd_members_get_a_400_never_a_500(kind, base):
    client = make_client()
    odd = [5, True, ["groq"], "groq", {"provider": 5, "model": "x", "voiceId": "v"},
           {"provider": ["a"], "voiceId": "v", "model": "m"},
           {"provider": {"x": 1}, "model": {"y": 2}, "voiceId": {"z": 3}}, {"provider": "groq", "model": 7}]
    for member in odd:
        body = {"name": "odd", "kind": kind, "models": [member]} if kind != "model" else {
            "name": "odd", "models": [member]}
        r = client.post("/model-route", json=body, headers=AUTH)
        assert r.status_code in (201, 400), (kind, member, r.status_code, r.text[:200])
    for bad_kind in (5, ["voice"], {"a": 1}, "VOICE", " voice"):
        r = client.post("/model-route", json={"name": "x", "kind": bad_kind, "models": [{"provider": "groq"}]},
                        headers=AUTH)
        assert r.status_code == 400, (bad_kind, r.status_code)


def test_a_route_cannot_change_kind_under_the_assistants_that_use_it():
    client = make_client()
    listen = client.post("/model-route", json=LISTENING, headers=AUTH).json()
    made = client.post("/assistant", json=on_routes(transcriber=listen["id"]), headers=AUTH)
    assert made.status_code == 201
    r = client.patch(f"/model-route/{listen['id']}", json={"kind": "voice", "models": SPEAKING["models"]},
                     headers=AUTH)
    assert r.status_code == 400 and "kind" in r.json()["detail"]
    r = client.patch(f"/model-route/{listen['id']}", json={"kind": "transcriber", "name": "Same kind"}, headers=AUTH)
    assert r.status_code == 200  # saying the same kind again is fine
    assert client.get(f"/model-route/{listen['id']}", headers=AUTH).json()["kind"] == "transcriber"


def test_a_members_list_can_be_replaced_but_only_with_members_of_its_kind():
    client = make_client()
    speak = client.post("/model-route", json=SPEAKING, headers=AUTH).json()
    r = client.patch(f"/model-route/{speak['id']}", json={"models": [{"provider": "deepgram", "model": "nova-3"}]},
                     headers=AUTH)
    assert r.status_code == 400 and "voiceId" in r.json()["detail"]  # a transcriber is no voice
    r = client.patch(f"/model-route/{speak['id']}", json={"models": [{"provider": "cartesia", "voiceId": "only"}]},
                     headers=AUTH)
    assert r.status_code == 200 and [m["voiceId"] for m in r.json()["models"]] == ["only"]


def test_a_voice_route_cannot_smuggle_a_url_or_key_into_a_member():
    client = make_client()
    body = {**SPEAKING, "models": [{"provider": "11labs", "voiceId": "v", "url": "http://169.254.169.254/",
                                    "apiKey": "stolen", "server": {"url": "http://x"}}]}
    saved = client.post("/model-route", json=body, headers=AUTH).json()
    assert saved["models"][0] == {"provider": "11labs", "voiceId": "v", "restingSeconds": 0, "price": {
        "perMillionCharacters": 40.0}}
