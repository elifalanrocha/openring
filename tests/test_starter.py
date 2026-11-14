"""A new server starts with an assistant to talk to, made once from what costs nothing."""

from fastapi.testclient import TestClient

from openring import starter
from openring.api import create_app
from openring.config import Settings
from openring.providers import resolve_plan
from openring.store import Store

from test_api import AUTH


def test_the_starter_uses_what_this_computer_has():
    local = starter.assistant(["llama3.2", "qwen3"], whisper=True, kokoro=True)
    assert local["transcriber"]["provider"] == "whisper" and local["voice"]["provider"] == "kokoro"
    assert local["model"]["provider"] == "ollama" and local["model"]["model"] == "llama3.2"

    bare = starter.assistant([], whisper=False, kokoro=False)
    assert (bare["transcriber"]["provider"], bare["model"]["provider"], bare["voice"]["provider"]) == (
        "deepgram", "groq", "deepgram")
    for doc in (local, bare):  # each is an assistant OpenRing can plan a call for
        assert resolve_plan(doc, check_keys=False).first_message == starter.GREETING
        assert doc["model"]["messages"][0]["role"] == "system"


def test_it_is_made_once_and_never_over_someones_own_assistants():
    store = Store(":memory:")
    made = starter.make(store, [], True, True)
    assert made["name"] == starter.NAME and len(store.list("assistants")) == 1
    assert starter.make(store, [], True, True) is None
    store.delete("assistants", made["id"])
    assert starter.make(store, [], True, True) is None and store.list("assistants") == []  # deleted stays deleted

    theirs = Store(":memory:")
    theirs.create("assistants", {"name": "Reception"})
    assert starter.make(theirs, [], True, True) is None
    assert [a["name"] for a in theirs.list("assistants")] == ["Reception"]


def test_a_new_server_has_its_first_assistant_when_it_starts():
    store = Store(":memory:")
    settings = Settings(api_key="test-key", public_url="", db_path=":memory:")
    with TestClient(create_app(settings, store)) as client:
        found = client.get("/assistant", headers=AUTH).json()
        assert [a["name"] for a in found] == [starter.NAME]
        client.delete(f"/assistant/{found[0]['id']}", headers=AUTH)
    with TestClient(create_app(settings, store)) as client:  # the next start leaves the choice alone
        assert client.get("/assistant", headers=AUTH).json() == []
