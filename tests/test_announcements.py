"""Recorded messages and on-call escalations: calls that play a recording instead of holding a conversation."""

import io
import struct
import time
import xml.etree.ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from openring import announcements, twilio
from openring.api import create_app
from openring.config import Settings
from openring.store import Store
from openring.twilio import compute_signature

from test_api import AUTH

PUBLIC = "https://ring.example.com"


def wav(seconds=2.0, rate=8000):
    """A silent 16-bit mono WAV of `seconds`."""
    data = bytes(int(seconds * rate) * 2)
    fmt = struct.pack("<HHIIHH", 1, 1, rate, rate * 2, 2, 16)
    return (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE" + b"fmt " + struct.pack("<I", 16) + fmt
            + b"data" + struct.pack("<I", len(data)) + data)


@pytest.fixture
def server(monkeypatch, tmp_path):
    """An OpenRing with a Twilio number, a stubbed Twilio REST API and recordings in a temporary folder."""
    monkeypatch.setenv("OPENRING_ANNOUNCEMENTS_DIR", str(tmp_path))
    placed = []

    async def create_call(account_sid, auth_token, *, to, from_, url, status_callback, machine_detection=None):
        placed.append(to)
        return f"CA-{len(placed)}"

    monkeypatch.setattr(twilio, "create_call", create_call)
    settings = Settings(api_key="test-key", public_url=PUBLIC, db_path=":memory:", twilio_account_sid="AC1",
                        twilio_auth_token="tok", validate_twilio_signature=True)
    store = Store(":memory:")
    with TestClient(create_app(settings, store)) as client:
        client.app.state.escalations.poll = 0.05
        audio = client.post("/announcement", headers=AUTH, files={"file": ("alert.wav", wav(), "audio/wav")}).json()
        pager = {"name": "Pager", "announcement": {"audioId": audio["id"]}}
        a = client.post("/assistant", headers=AUTH, json=pager).json()
        n = client.post("/phone-number", headers=AUTH, json={"number": "+15551234567", "assistantId": a["id"]}).json()
        yield client, store, placed, audio, a, n


def signed(client, path, form):
    return client.post(path, data=form, headers={"X-Twilio-Signature": compute_signature("tok", PUBLIC + path, form)})


def wait(condition, seconds=8):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        if condition():
            return True
        time.sleep(0.05)
    return bool(condition())


def test_a_recording_is_judged_by_its_bytes_and_size(server, monkeypatch):
    client, store, *_ = server
    audio = client.get("/announcement", headers=AUTH).json()[0]
    assert (audio["format"], audio["durationSeconds"], audio["name"]) == ("wav", 2.0, "alert.wav")
    mp3 = client.post("/announcement", headers=AUTH, files={"file": ("x.mp3", b"ID3" + bytes(64), "audio/mpeg")})
    assert mp3.status_code == 201 and mp3.json()["format"] == "mp3"
    for name, data in (("fake.wav", b"<html>not audio</html>"), ("empty.wav", b""), ("x.mp3", b"MZ\x90\x00")):
        refused = client.post("/announcement", headers=AUTH, files={"file": (name, data, "audio/wav")})
        assert refused.status_code == 400, name
    monkeypatch.setattr(announcements, "MAX_BYTES", 100)
    big = client.post("/announcement", headers=AUTH, files={"file": ("big.wav", wav(), "audio/wav")})
    assert big.status_code == 400
    assert client.post("/announcement", files={"file": ("x.wav", wav(), "audio/wav")}).status_code == 401
    monkeypatch.setattr(announcements, "MAX_BYTES", 10_000_000)
    monkeypatch.setattr(announcements, "MAX_SECONDS", 1)
    long = client.post("/announcement", headers=AUTH, files={"file": ("long.wav", wav(seconds=2), "audio/wav")})
    assert long.status_code == 400 and "minutes" in long.text


def test_an_assistant_names_a_recording_that_exists_and_one_in_use_is_kept(server):
    client, store, _placed, audio, a, _n = server
    bad = client.post("/assistant", headers=AUTH, json={"name": "x", "announcement": {"audioId": "nope"}})
    assert bad.status_code == 400 and "announcement.audioId" in bad.text
    assert client.delete(f"/announcement/{audio['id']}", headers=AUTH).status_code == 409
    other = client.post("/announcement", headers=AUTH, files={"file": ("b.wav", wav(), "audio/wav")}).json()
    assert client.delete(f"/announcement/{other['id']}", headers=AUTH).status_code == 200
    assert client.get(f"/announcement/{other['id']}", headers=AUTH).status_code == 404


def test_a_recorded_message_plays_through_a_one_hour_link_and_costs_only_the_line(server):
    client, store, placed, _audio, _a, n = server
    call = client.post("/call", headers=AUTH, json={"phoneNumberId": n["id"], "customer": {"number": "+15557654321"}})
    assert call.status_code == 201, call.text
    call = call.json()
    twiml = signed(client, f"/twilio/voice?callId={call['id']}", {"CallSid": "CA-1"}).text
    url = ET.fromstring(twiml).find("./Play").text
    assert url.startswith(PUBLIC + "/announcement/play/") and ET.fromstring(twiml).find("./Hangup") is not None
    assert "/ws/twilio" not in twiml  # no stream, no transcriber, model or voice
    played = client.get(url.removeprefix(PUBLIC))
    assert played.status_code == 200 and played.content[:4] == b"RIFF" and played.headers["cache-control"] == "no-store"
    assert client.get("/announcement/play/not-a-token").status_code == 404
    assert store.get("calls", call["id"])["status"] == "in-progress"

    signed(client, f"/twilio/status?callId={call['id']}", {"CallSid": "CA-1", "CallStatus": "completed",
                                                            "CallDuration": "37"})
    ended = store.get("calls", call["id"])
    assert (ended["status"], ended["durationSeconds"], ended["mode"]) == ("ended", 37.0, "announcement")
    assert ended["costBreakdown"]["stt"] == ended["costBreakdown"]["llm"] == ended["costBreakdown"]["tts"] == 0
    assert ended["costBreakdown"]["line"] > 0 and ended["cost"] == ended["costBreakdown"]["line"]


def test_an_escalation_calls_down_the_list_until_someone_confirms(server):
    client, store, placed, _audio, a, n = server
    r = client.post("/escalation", headers=AUTH, json={
        "name": "Payments down", "phoneNumberId": n["id"], "assistantId": a["id"], "rounds": 2,
        "customers": [{"number": "+15550000001", "name": "Ana"}, {"number": "+15550000002", "name": "Bruno"}]})
    assert r.status_code == 201, r.text
    esc = r.json()
    assert wait(lambda: placed == ["+15550000001"])
    first = store.get("escalations", esc["id"])["attempts"][0]["callId"]
    signed(client, f"/twilio/status?callId={first}", {"CallSid": "CA-1", "CallStatus": "no-answer"})  # Ana sleeps

    assert wait(lambda: placed == ["+15550000001", "+15550000002"])
    second = store.get("escalations", esc["id"])["attempts"][1]["callId"]
    twiml = signed(client, f"/twilio/voice?callId={second}", {"CallSid": "CA-2"}).text
    gather = ET.fromstring(twiml).find("./Gather")
    assert gather.get("action") == f"{PUBLIC}/twilio/ack?callId={second}" and gather.get("numDigits") == "1"
    assert gather.find("Say").text == "Press 1 to confirm."

    wrong = signed(client, f"/twilio/ack?callId={second}", {"CallSid": "CA-2", "Digits": "7"}).text
    assert ET.fromstring(wrong).find("./Gather") is not None  # asked again
    assert store.get("escalations", esc["id"])["status"] == "in-progress"
    ok = signed(client, f"/twilio/ack?callId={second}", {"CallSid": "CA-2", "Digits": "1"}).text
    assert "Confirmed" in ok
    done = store.get("escalations", esc["id"])
    assert (done["status"], done["endedReason"], done["acknowledgedBy"]) == ("ended", "acknowledged", "+15550000002")
    signed(client, f"/twilio/status?callId={second}",
           {"CallSid": "CA-2", "CallStatus": "completed", "CallDuration": "9"})
    time.sleep(0.4)
    assert placed == ["+15550000001", "+15550000002"]  # nobody else is called


def test_an_escalation_nobody_confirms_goes_round_the_list_then_says_so(server):
    client, store, placed, _audio, a, n = server
    esc = client.post("/escalation", headers=AUTH, json={"phoneNumberId": n["id"], "assistantId": a["id"],
                                                         "rounds": 2, "customers": [{"number": "+15550000001"}]}).json()
    for attempt in range(2):
        assert wait(lambda attempt=attempt: len(store.get("escalations", esc["id"])["attempts"]) == attempt + 1)
        call_id = store.get("escalations", esc["id"])["attempts"][attempt]["callId"]
        signed(client, f"/twilio/status?callId={call_id}", {"CallSid": f"CA-{attempt + 1}", "CallStatus": "busy"})
    assert wait(lambda: store.get("escalations", esc["id"])["status"] == "ended")
    done = store.get("escalations", esc["id"])
    assert done["endedReason"] == "nobody-confirmed" and [x["endedReason"] for x in done["attempts"]] == [
        "customer-busy", "customer-busy"]


def test_an_escalation_is_checked_before_anyone_is_called_and_can_be_stopped(server):
    client, store, placed, _audio, a, n = server
    talker = client.post("/assistant", headers=AUTH, json={"name": "Talker"}).json()
    base = {"phoneNumberId": n["id"], "assistantId": a["id"], "customers": [{"number": "+15550000001"}]}
    for bad in ({"assistantId": talker["id"]}, {"customers": []}, {"customers": [{"number": "x"}]}, {"rounds": 9},
                {"confirmDigit": "12"}, {"rounds": True}):
        assert client.post("/escalation", headers=AUTH, json={**base, **bad}).status_code == 400, bad
    assert placed == []

    esc = client.post("/escalation", headers=AUTH, json=base).json()
    assert client.patch(f"/escalation/{esc['id']}", headers=AUTH, json={"name": "x"}).status_code == 400
    stopped = client.patch(f"/escalation/{esc['id']}", headers=AUTH, json={"status": "cancelled"}).json()
    assert (stopped["status"], stopped["endedReason"]) == ("ended", "cancelled")
    assert client.get("/escalation", headers=AUTH).json()[0]["id"] == esc["id"]


def test_an_escalation_cut_off_by_a_restart_is_not_called_again(monkeypatch):
    store = Store(":memory:")
    store.create("escalations", {"status": "in-progress", "customers": [], "rounds": 1})
    announcements.Escalations(store, None).resume()
    assert store.list("escalations")[0]["endedReason"] == "worker-shutdown"


def test_wav_length_comes_from_the_header():
    assert announcements.wav_seconds(wav(3.5)) == 3.5
    assert announcements.wav_seconds(b"RIFF\x00\x00\x00\x00WAVE") is None
    with pytest.raises(announcements.AnnouncementError):
        announcements.kind_of(io.BytesIO(b"GIF89a").read())
