"""Renaming and deleting a call, as Vapi's `PATCH /call/{id}` and `DELETE /call/{id}` do."""

import pytest
from fastapi.testclient import TestClient

from openring import memory, recordings
from openring.api import create_app
from openring.config import Settings
from openring.store import Store

from test_api import AUTH


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENRING_RECORDINGS_DIR", str(tmp_path))
    store = Store(":memory:")
    settings = Settings(api_key="test-key", public_url="https://x.test", db_path=":memory:")
    return TestClient(create_app(settings, store)), store, tmp_path


def _call(store, **fields):
    return store.create("calls", {"status": "ended", "customer": {"number": "+5511999990001"}, "assistantId": "a1",
                                  "messages": [{"role": "user", "message": "My card ends in 4242"}], **fields})


def test_a_call_can_be_named_and_nothing_else_about_it_changes(server):
    client, store, _ = server
    call = _call(store, endedReason="customer-ended-call")

    renamed = client.patch(f"/call/{call['id']}", json={"name": "Ana, booking"}, headers=AUTH)
    assert renamed.status_code == 200 and renamed.json()["name"] == "Ana, booking"
    assert renamed.json()["messages"] == call["messages"] and renamed.json()["endedReason"] == "customer-ended-call"

    for body in ({"status": "queued"}, {"name": "x", "endedReason": "made-up"}, {"name": 5}, {"name": "x" * 41}, {},
                 {"messages": []}, {"cost": 0}):
        assert client.patch(f"/call/{call['id']}", json=body, headers=AUTH).status_code == 400
    assert store.get("calls", call["id"])["status"] == "ended"
    assert client.patch("/call/nobody", json={"name": "x"}, headers=AUTH).status_code == 404
    assert client.patch(f"/call/{call['id']}", json={"name": "x"}).status_code == 401  # no key


def test_deleting_a_call_takes_its_record_transcript_and_recording(server):
    client, store, folder = server
    call, other = _call(store), _call(store)
    for kept in (call, other):
        (folder / f"{kept['id']}.wav").write_bytes(b"RIFF....WAVE")

    gone = client.delete(f"/call/{call['id']}", headers=AUTH)
    assert gone.status_code == 200 and gone.json()["id"] == call["id"]  # Vapi answers with the call it deleted
    assert store.get("calls", call["id"]) is None and not (folder / f"{call['id']}.wav").exists()
    assert client.get(f"/call/{call['id']}", headers=AUTH).status_code == 404
    assert client.delete(f"/call/{call['id']}", headers=AUTH).status_code == 404
    assert store.get("calls", other["id"]) and (folder / f"{other['id']}.wav").exists()  # nobody else's
    assert client.delete(f"/call/{other['id']}").status_code == 401
    assert store.get("calls", other["id"])


def test_a_call_still_going_on_is_not_deleted(server, monkeypatch):
    client, store, folder = server
    live = _call(store, status="in-progress")
    assert client.delete(f"/call/{live['id']}", headers=AUTH).status_code == 409
    ended = _call(store)
    monkeypatch.setattr(recordings, "_writing", {ended["id"]})  # its recording is still being written
    assert client.delete(f"/call/{ended['id']}", headers=AUTH).status_code == 409
    assert store.get("calls", live["id"]) and store.get("calls", ended["id"])


def test_a_recording_is_found_by_the_calls_own_id_never_by_the_address(server):
    client, store, folder = server
    outside = folder.parent / "keep.wav"
    outside.write_bytes(b"not a call")
    for path in ("..%2Fkeep", "../keep", "..%5Ckeep", "%2e%2e%2fkeep"):
        assert client.delete(f"/call/{path}", headers=AUTH).status_code in (404, 405)
    assert outside.exists()


def test_a_deleted_call_is_out_of_the_callers_memory(server):
    client, store, _ = server
    past = _call(store)
    now = {"id": "now", "assistantId": "a1", "customer": {"number": "+5511999990001"}}
    remembers = {"id": "a1", "callerMemory": True}
    assert "4242" in memory.recall(store, now, remembers)[0]
    assert client.delete(f"/call/{past['id']}", headers=AUTH).status_code == 200
    assert memory.recall(store, now, remembers) is None
