"""Outbound calls (POST /call). Twilio's REST API is replaced by a stub that records each request."""

import xml.etree.ElementTree as ET

import pytest

from openring import twilio
from openring.twilio import TwilioError, compute_signature

from test_api import ASSISTANT, AUTH, make_client


@pytest.fixture
def placed(monkeypatch):
    calls = []

    async def create_call(account_sid, auth_token, *, to, from_, url, status_callback, machine_detection=None):
        calls.append({"to": to, "from": from_, "url": url, "status_callback": status_callback,
                      **({"machine_detection": machine_detection} if machine_detection else {})})
        return "CA-out-1"

    monkeypatch.setattr(twilio, "create_call", create_call)
    for k in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.setenv(k, "fake")
    return calls


def _setup(**overrides):
    client = make_client(twilio_account_sid="AC1", **overrides)
    a = client.post("/assistant", json=ASSISTANT, headers=AUTH).json()
    n = client.post("/phone-number", json={"number": "+1 (555) 123-4567", "assistantId": a["id"]}, headers=AUTH).json()
    return client, a, n


def _call(client, n, number="+55 11 91234-5678"):
    return client.post("/call", json={"phoneNumberId": n["id"], "customer": {"number": number}}, headers=AUTH)


def _signed(client, path, form):
    sig = compute_signature("tok", "https://ring.example.com" + path, form)
    return client.post(path, data=form, headers={"X-Twilio-Signature": sig})


def test_outbound_call_is_placed_and_answered_by_its_assistant(placed):
    client, a, n = _setup()
    r = _call(client, n)
    assert r.status_code == 201, r.text
    call = r.json()
    assert (call["type"], call["status"], call["phoneCallProviderId"]) == ("outboundPhoneCall", "queued", "CA-out-1")
    assert placed == [{
        "to": "+5511912345678",
        "from": "+15551234567",
        "url": f"https://ring.example.com/twilio/voice?callId={call['id']}",
        "status_callback": f"https://ring.example.com/twilio/status?callId={call['id']}",
    }]
    # Answered: Twilio fetches the call's URL (signed with its query string) and gets the media stream.
    path = f"/twilio/voice?callId={call['id']}"
    twiml = _signed(client, path, {"CallSid": "CA-out-1", "To": "+5511912345678"}).text
    stream = ET.fromstring(twiml).find("./Connect/Stream")
    assert {p.get("name"): p.get("value") for p in stream.findall("Parameter")} == {
        "assistantId": a["id"], "callId": call["id"],
    }
    # The same URL for some other Twilio call does not connect.
    assert "<Stream" not in _signed(client, path, {"CallSid": "CA-someone-else"}).text


def test_outbound_call_needs_a_number_to_call_twilio_and_public_url(placed):
    client, _, n = _setup()
    assert client.post("/call", json={"phoneNumberId": n["id"]}, headers=AUTH).status_code == 400
    assert client.post("/call", json={"phoneNumberId": "nope"}, headers=AUTH).status_code == 404
    for overrides, missing in (({"twilio_auth_token": ""}, "TWILIO_AUTH_TOKEN"), ({"public_url": ""}, "PUBLIC_URL")):
        client, _, n = _setup(**overrides)
        r = _call(client, n)
        assert r.status_code == 400 and missing in r.json()["detail"]
    assert placed == []


def test_refused_outbound_call_is_recorded(monkeypatch, placed):
    async def refuse(*_args, **_kwargs):
        raise TwilioError("Trial accounts may only make calls to verified numbers. (Twilio error 21219)")

    monkeypatch.setattr(twilio, "create_call", refuse)
    client, _, n = _setup()
    r = _call(client, n)
    assert r.status_code == 400 and "verified numbers" in r.json()["detail"]
    call = client.get("/call", headers=AUTH).json()[0]
    assert (call["status"], call["endedReason"]) == ("ended", "twilio-failed-to-connect-call")


def test_unanswered_outbound_call_is_closed_by_the_status_callback(placed):
    client, _, n = _setup()
    call = _call(client, n).json()
    path = f"/twilio/status?callId={call['id']}"
    assert client.post(path, data={"CallStatus": "no-answer"}).status_code == 403  # unsigned
    assert _signed(client, path, {"CallSid": "CA-out-1", "CallStatus": "no-answer"}).status_code == 204
    ended = client.get(f"/call/{call['id']}", headers=AUTH).json()
    assert (ended["status"], ended["endedReason"]) == ("ended", "customer-did-not-answer")


@pytest.mark.parametrize("fields, expected", [
    ({}, None),
    ({"voicemailDetection": "off", "voicemailMessage": "Call us back."}, None),
    ({"voicemailDetection": {"provider": "twilio"}}, "30"),
    ({"voicemailMessage": "Call us back."}, "30"),  # a message to leave turns it on, as on Vapi
    ({"voicemailDetection": {"provider": "vapi", "beepMaxAwaitSeconds": 20}}, "20"),
    ({"voicemailDetection": {"provider": "twilio", "beepMaxAwaitSeconds": 0}}, "3"),  # Twilio's lowest
    ({"voicemailDetection": {"provider": "twilio", "beepMaxAwaitSeconds": 60}}, "59"),  # and highest
    ({"voicemailDetection": {"provider": "twilio", "machineDetectionTimeout": 12}}, "12"),
    ({"voicemailDetection": {"provider": "twilio", "beepMaxAwaitSeconds": None}}, "30"),  # null is "not set"
    ({"voicemailDetection": {"provider": "twilio", "machineDetectionTimeout": None}}, "30"),
    ({"voicemailDetection": {"provider": "twilio", "enabled": False}}, None),  # Vapi's own way to turn it off
    ({"voicemailDetection": {"provider": "twilio", "enabled": False}, "voicemailMessage": "Call us back."}, None),
    ({"voicemailDetection": {"provider": "twilio", "enabled": True}}, "30"),
])
def test_voicemail_detection_goes_to_twilio_when_the_assistant_asks(placed, fields, expected):
    client, a, n = _setup()
    assert client.patch(f"/assistant/{a['id']}", json=fields, headers=AUTH).status_code == 200
    assert _call(client, n).status_code == 201
    detection = placed[0].get("machine_detection")
    if expected is None:
        assert detection is None  # it costs money and delays the greeting: never by default
    else:
        assert detection == {"MachineDetection": "DetectMessageEnd", "MachineDetectionTimeout": expected}


@pytest.mark.parametrize("fields, words", [
    ({"voicemailDetection": "on"}, "voicemailDetection"),
    ({"voicemailDetection": {"beepMaxAwaitSeconds": 90}}, "beepMaxAwaitSeconds"),
    ({"voicemailDetection": {"machineDetectionTimeout": "soon"}}, "machineDetectionTimeout"),
    ({"voicemailMessage": 5}, "voicemailMessage"),
])
def test_voicemail_settings_are_checked(placed, fields, words):
    client, a, _ = _setup()
    r = client.patch(f"/assistant/{a['id']}", json=fields, headers=AUTH)
    assert r.status_code == 400 and words in r.json()["detail"]


def _answered(client, a, n, answered_by, **fields):
    """An outbound call whose callee was answered by `answered_by`: what Twilio is told, and the call afterwards."""
    client.patch(f"/assistant/{a['id']}", json=fields, headers=AUTH)
    call = _call(client, n).json()
    form = {"CallSid": "CA-out-1", "AnsweredBy": answered_by}
    twiml = _signed(client, f"/twilio/voice?callId={call['id']}", form).text
    return twiml, client.get(f"/call/{call['id']}", headers=AUTH).json()


def test_a_person_who_answers_reaches_the_assistant(placed):
    client, a, n = _setup()
    for answered_by in ("human", "unknown"):
        twiml, call = _answered(client, a, n, answered_by, voicemailMessage="Call us back.")
        assert "<Stream" in twiml and call["status"] == "queued" and "answeredBy" not in call


def test_voicemail_without_a_message_hangs_up(placed):
    client, a, n = _setup()
    twiml, call = _answered(client, a, n, "machine_end_beep", voicemailDetection={"provider": "twilio"})
    assert ET.fromstring(twiml).find("./Hangup") is not None and "<Stream" not in twiml
    assert (call["status"], call["endedReason"], call["answeredBy"]) == ("ended", "voicemail", "machine_end_beep")


def test_voicemail_with_a_recording_plays_it(placed):
    client, a, n = _setup()
    url = "https://example.com/message.mp3?a=1&b=2"
    twiml, call = _answered(client, a, n, "machine_end_silence", voicemailMessage=url)
    assert ET.fromstring(twiml).find("./Play").text == url
    assert ET.fromstring(twiml).find("./Hangup") is not None
    assert (call["status"], call["endedReason"]) == ("ended", "voicemail")


def test_voicemail_with_a_sentence_goes_to_the_assistant_to_say_it(placed):
    client, a, n = _setup()
    twiml, call = _answered(client, a, n, "machine_end_beep", voicemailMessage="Please call us back.")
    assert "<Stream" in twiml
    assert (call["status"], call["answeredBy"]) == ("queued", "machine_end_beep")  # the stream ends the call


@pytest.mark.parametrize("message, played", [
    ("https://example.com/message", "https://example.com/message"),  # a link and nothing else: a recording
    (" https://example.com/message.mp3 ", "https://example.com/message.mp3"),  # never read aloud for a stray space
    ("https://example.com/a.wav?who=Ana Maria", "https://example.com/a.wav?who=Ana%20Maria"),  # a variable may do this
    ("https://example.com is our site, call us.", None),  # a sentence that starts with a link is said
])
def test_voicemail_tells_a_recording_from_a_sentence(placed, message, played):
    client, a, n = _setup()
    twiml, call = _answered(client, a, n, "machine_end_beep", voicemailMessage=message)
    if played:
        assert ET.fromstring(twiml).find("./Play").text == played and call["endedReason"] == "voicemail"
    else:
        assert "<Stream" in twiml and call["status"] == "queued"


def test_voicemail_with_only_blanks_in_the_message_hangs_up(placed):
    client, a, n = _setup()
    twiml, call = _answered(client, a, n, "machine_end_beep", voicemailMessage="   ")
    assert ET.fromstring(twiml).find("./Hangup") is not None and call["endedReason"] == "voicemail"


def test_a_replayed_machine_webhook_leaves_a_finished_call_alone(placed):
    client, a, n = _setup()
    client.patch(f"/assistant/{a['id']}", json={"voicemailMessage": "Call us back."}, headers=AUTH)
    call = _call(client, n).json()
    client.app.state.store.update("calls", call["id"], {"status": "ended", "endedReason": "customer-ended-call"})
    _signed(client, f"/twilio/voice?callId={call['id']}", {"CallSid": "CA-out-1", "AnsweredBy": "machine_end_beep"})
    after = client.get(f"/call/{call['id']}", headers=AUTH).json()
    assert after["endedReason"] == "customer-ended-call" and "answeredBy" not in after


def test_a_fax_gets_no_message(placed):
    client, a, n = _setup()
    twiml, call = _answered(client, a, n, "fax", voicemailMessage="Please call us back.")
    assert ET.fromstring(twiml).find("./Hangup") is not None and "<Stream" not in twiml
    assert call["endedReason"] == "voicemail"


def test_twilio_is_asked_to_detect_machines_only_when_given_the_parameters(monkeypatch):
    import asyncio

    import httpx

    sent = []

    def handle(request):
        sent.append(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(201, json={"sid": "CA1"})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handle), **kw))
    base = {"to": "+15550000001", "from_": "+15550000002", "url": "https://x.test/v", "status_callback": "https://x.test/s"}
    asyncio.run(twilio.create_call("AC1", "tok", **base))
    detection = twilio.machine_detection({"voicemailDetection": {"beepMaxAwaitSeconds": 25}})
    asyncio.run(twilio.create_call("AC1", "tok", **base, machine_detection=detection))
    assert "MachineDetection" not in sent[0]
    assert (sent[1]["MachineDetection"], sent[1]["MachineDetectionTimeout"]) == ("DetectMessageEnd", "25")
