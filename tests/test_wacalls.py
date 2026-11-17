"""WhatsApp calls through WaCalls: OpenRing against a pretend WaCalls on this computer.

The pretend speaks WaCalls's real protocol (Server-Sent Events, the REST calls, and a WebRTC data channel named `pcm`
answered by a real aiortc peer), as read from WaCalls's own source. Nothing here reaches WhatsApp.
"""

import asyncio
import json
import threading
import time

import pytest
from aiohttp import web
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from fastapi.testclient import TestClient

from openring import wacalls
from openring.api import create_app
from openring.config import Settings
from openring.store import Store

from test_api import ASSISTANT, AUTH

CALLER = "5511999990001"
SESSION = "sess-1"


class PretendWaCalls:
    """A WaCalls server with one linked session."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.requests: list[tuple[str, str, str, dict]] = []  # method, path, X-Client-Id, body
        self.frames: list[bytes] = []  # audio OpenRing sent over the data channel
        self.streams: list[asyncio.Queue] = []
        self.event_clients: list[str] = []
        self.channel = None
        self.pcs: list[RTCPeerConnection] = []
        self.start_status, self.start_error = 200, None
        self.paired = True
        self._ready = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()
        assert self._ready.wait(20)

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._serve())
        self.loop.run_forever()

    async def _serve(self):
        app = web.Application()
        app.router.add_get("/api/events", self._events)
        app.router.add_post("/api/sessions/{sid}/calls", self._start)
        app.router.add_post("/api/sessions/{sid}/calls/{id}/webrtc", self._webrtc)
        app.router.add_post("/api/sessions/{sid}/calls/{id}/accept", self._accept)
        app.router.add_post("/api/sessions/{sid}/calls/{id}/reject", self._reject)
        app.router.add_delete("/api/sessions/{sid}/calls/{id}", self._end)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        self._ready.set()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    async def _note(self, request):
        body = await request.json() if request.can_read_body else {}
        self.requests.append((request.method, request.path, request.headers.get("X-Client-Id", ""), body))
        return body

    def _session(self):
        return {"id": SESSION, "name": "Line", "jid": "5511888880000:3@s.whatsapp.net", "state": "connected",
                "paired": self.paired}

    async def _events(self, request):
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
        await response.prepare(request)
        queue: asyncio.Queue = asyncio.Queue()
        self.streams.append(queue)
        self.event_clients.append(request.query.get("clientId", ""))
        await self._write(response, {"type": "session-list", "sessions": [self._session()]})
        try:
            while (event := await queue.get()) is not None:
                await self._write(response, event)
        finally:
            self.streams.remove(queue)
        return response

    @staticmethod
    async def _write(response, event):
        await response.write(b"data: " + json.dumps(event).encode() + b"\n\n")

    async def _start(self, request):
        body = await self._note(request)
        if self.start_status != 200:
            return web.json_response({"error": self.start_error}, status=self.start_status)
        assert body.get("phone")
        return web.json_response({"call": {"callId": "out-1"}})

    async def _webrtc(self, request):
        body = await self._note(request)
        pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self.pcs.append(pc)

        @pc.on("datachannel")
        def _channel(channel):
            if channel.label == wacalls.CHANNEL:
                self.channel = channel
                channel.on("message", self.frames.append)

        await pc.setRemoteDescription(RTCSessionDescription(sdp=body["sdp_offer"], type="offer"))
        await pc.setLocalDescription(await pc.createAnswer())
        return web.json_response({"sdp_answer": pc.localDescription.sdp})

    async def _accept(self, request):
        await self._note(request)
        return web.json_response({"call": {"callId": request.match_info["id"]}})

    async def _reject(self, request):
        await self._note(request)
        return web.json_response({"status": "ok"})

    async def _end(self, request):
        await self._note(request)
        return web.Response(status=204)

    # ---- from the test's thread ----
    def push(self, event):
        def put():
            for queue in self.streams:
                queue.put_nowait(event)

        self.loop.call_soon_threadsafe(put)

    def drop_streams(self):
        self.push(None)

    def incoming(self, call_id="in-1", peer=f"{CALLER}:12@s.whatsapp.net"):
        self.push({"type": "incoming", "sessionId": SESSION, "id": call_id, "peer": peer, "offeredAt": 1})

    def status(self, call_id, status):
        self.push({"type": "call-status", "sessionId": SESSION, "id": call_id, "status": status, "peer": "x"})

    def ended(self, call_id, reason="user_ended"):
        self.push({"type": "call-ended", "sessionId": SESSION, "id": call_id, "reason": reason, "endedAt": 1})

    def to_openring(self, data):
        self.loop.call_soon_threadsafe(self.channel.send, data)

    def made(self, method, suffix):
        return [r for r in self.requests if r[0] == method and r[1].endswith(suffix)]

    def close(self):
        async def stop():
            for queue in self.streams:  # open event streams would hold the server up
                queue.put_nowait(None)
            await asyncio.sleep(0.1)
            for pc in self.pcs:
                await pc.close()
            await self.runner.cleanup()

        asyncio.run_coroutine_threadsafe(stop(), self.loop).result(10)
        self.loop.call_soon_threadsafe(self.loop.stop)


@pytest.fixture
def pretend():
    server = PretendWaCalls()
    yield server
    server.close()


@pytest.fixture
def line(pretend, monkeypatch, quiet_providers):
    """An OpenRing server with an assistant on a WhatsApp number of the pretend WaCalls: (client, store, number)."""
    for k in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.setenv(k, "fake")
    store = Store(":memory:")
    settings = Settings(api_key="test-key", public_url="", db_path=":memory:", wacalls_url=pretend.url)
    with TestClient(create_app(settings, store)) as client:
        a = client.post("/assistant", json=ASSISTANT, headers=AUTH).json()
        number = client.post("/phone-number", headers=AUTH, json={
            "provider": "wacalls", "number": "+5511888880000", "wacallsSession": SESSION,
            "assistantId": a["id"]}).json()
        _wait(lambda: _shown(client, number).get("wacallsConnected"))
        yield client, store, number


def _shown(client, number):
    return client.get(f"/phone-number/{number['id']}", headers=AUTH).json()


def _wait(condition, seconds=10):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        if condition():
            return True
        time.sleep(0.05)
    return bool(condition())


def _calls(store):
    return store.list("calls")


def test_who_is_calling_comes_from_the_whatsapp_address():
    assert wacalls.caller_of("5511999990001@s.whatsapp.net") == CALLER
    assert wacalls.caller_of("5511999990001:12@s.whatsapp.net") == CALLER  # with the device that rings
    assert wacalls.caller_of("5511999990001.0:12@s.whatsapp.net") == CALLER
    assert wacalls.caller_of("12345678901234@lid") == ""  # an anonymous address is no phone number
    assert wacalls.caller_of(None) == "" and wacalls.caller_of("") == ""
    assert wacalls.is_session("b3f9-01.x_Z") and not wacalls.is_session("a/b") and not wacalls.is_session("")


def test_a_whatsapp_number_needs_a_session_and_the_address_of_wacalls(line, pretend):
    client, _store, number = line
    shown = _shown(client, number)
    assert shown["wacallsStatus"] == "ready" and shown["wacallsLinkedNumber"] == "+5511888880000"
    assert pretend.event_clients == [wacalls.EVENTS_CLIENT]

    for session in (None, "", "a b", "with/slash", "x" * 300, 5):
        refused = client.post("/phone-number", headers=AUTH, json={
            "provider": "wacalls", "number": "+5511777770000", "wacallsSession": session})
        assert refused.status_code == 400 and "wacallsSession" in refused.json()["detail"]

    pretend.paired = False
    pretend.push({"type": "auth-state", "sessionId": SESSION, "paired": False, "state": "qr"})
    assert _wait(lambda: "scan the QR" in _shown(client, number)["wacallsStatus"])

    other = client.post("/phone-number", headers=AUTH, json={
        "provider": "wacalls", "number": "+5511777770000", "wacallsSession": "nobody"}).json()
    assert other["wacallsStatus"] == "WaCalls has no session with this id" or _wait(
        lambda: _shown(client, other)["wacallsStatus"] == "WaCalls has no session with this id")


def test_without_the_address_of_wacalls_a_number_is_refused(monkeypatch):
    monkeypatch.delenv("WACALLS_URL", raising=False)
    client = TestClient(create_app(Settings(api_key="test-key", db_path=":memory:"), Store(":memory:")))
    refused = client.post("/phone-number", headers=AUTH, json={
        "provider": "wacalls", "number": "+5511777770000", "wacallsSession": SESSION})
    assert refused.status_code == 400 and "WACALLS_URL" in refused.json()["detail"]


def test_a_whatsapp_call_is_answered_by_the_assistant(line, pretend):
    client, store, number = line
    pretend.incoming("in-1")
    assert _wait(lambda: pretend.made("POST", "/in-1/accept") and pretend.made("POST", "/in-1/webrtc"))
    accept = pretend.made("POST", "/in-1/accept")[0]
    assert accept[1] == f"/api/sessions/{SESSION}/calls/in-1/accept" and accept[2].startswith("openring-")
    assert pretend.made("POST", "/in-1/webrtc")[0][2] == accept[2]  # one client for the call, as WaCalls wants
    assert _wait(lambda: len(pretend.frames) > 10)  # audio flows at once: silence, until there is something to say
    assert all(len(frame) == 640 for frame in pretend.frames)

    call = _calls(store)[0]
    assert call["type"] == "inboundPhoneCall" and call["customer"] == {"number": f"+{CALLER}"}
    assert call["phoneCallProvider"] == "wacalls" and call["phoneCallProviderId"] == "in-1"
    assert call["phoneNumberId"] == number["id"]
    assert _wait(lambda: _calls(store)[0]["status"] == "in-progress")

    pretend.to_openring(bytes(640))  # the caller's audio reaches the call
    started, sent = time.monotonic(), len(pretend.frames)
    time.sleep(1.0)
    rate = (len(pretend.frames) - sent) / (time.monotonic() - started)
    assert 35 < rate < 65  # a 20 ms message every 20 ms, neither a trickle nor a flood

    pretend.ended("in-1")
    assert _wait(lambda: _calls(store)[0]["status"] == "ended")
    assert _calls(store)[0]["endedReason"] == "customer-ended-call"
    assert not client.app.state.wacalls.live


def test_two_calls_at_once_each_get_their_own_client(line, pretend):
    _client, store, _number = line
    pretend.incoming("in-1")
    pretend.incoming("in-2", peer="5511999990002@s.whatsapp.net")
    assert _wait(lambda: len(pretend.made("POST", "/accept")) == 2)
    assert len({r[2] for r in pretend.made("POST", "/accept")}) == 2  # WaCalls allows one call per client
    assert _wait(lambda: len(_calls(store)) == 2 and all(c["status"] == "in-progress" for c in _calls(store)))


def test_a_whatsapp_call_nobody_can_answer_is_rejected(line, pretend):
    client, store, number = line
    client.patch(f"/phone-number/{number['id']}", json={"assistantId": None}, headers=AUTH)
    pretend.incoming("in-2")
    assert _wait(lambda: pretend.made("POST", "/in-2/reject"))
    assert not pretend.made("POST", "/in-2/accept") and not pretend.made("POST", "/in-2/webrtc")


def test_stopping_the_server_hangs_up_the_calls_it_has(pretend, monkeypatch, quiet_providers):
    for k in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.setenv(k, "fake")
    store = Store(":memory:")
    settings = Settings(api_key="test-key", public_url="", db_path=":memory:", wacalls_url=pretend.url)
    with TestClient(create_app(settings, store)) as client:
        a = client.post("/assistant", json=ASSISTANT, headers=AUTH).json()
        client.post("/phone-number", headers=AUTH, json={
            "provider": "wacalls", "number": "+5511888880000", "wacallsSession": SESSION, "assistantId": a["id"]})
        assert _wait(lambda: pretend.event_clients)
        pretend.incoming("in-1")
        assert _wait(lambda: _calls(store) and _calls(store)[0]["status"] == "in-progress")
    assert pretend.made("DELETE", "/in-1")  # WaCalls is told, so the caller isn't left on a silent line
    assert _calls(store)[0]["status"] == "ended"


def test_the_assistant_calls_someone_on_whatsapp(line, pretend):
    client, store, number = line
    placed = client.post("/call", headers=AUTH, json={
        "phoneNumberId": number["id"], "customer": {"number": "+55 11 99999-0001"}})
    assert placed.status_code == 201, placed.text
    call = placed.json()
    assert call["phoneCallProvider"] == "wacalls" and call["phoneCallProviderId"] == "out-1"
    start = pretend.made("POST", f"/api/sessions/{SESSION}/calls")[0]
    assert start[3] == {"phone": CALLER}
    assert pretend.made("POST", "/out-1/webrtc")[0][2] == start[2]
    assert call["status"] == "queued"  # ringing: the assistant says nothing yet
    time.sleep(0.5)
    assert store.get("calls", call["id"])["status"] == "queued"

    pretend.status("out-1", "connected")  # someone picked up
    assert _wait(lambda: store.get("calls", call["id"])["status"] == "in-progress")
    assert _wait(lambda: len(pretend.frames) > 5)

    pretend.ended("out-1")
    assert _wait(lambda: store.get("calls", call["id"])["status"] == "ended")
    assert store.get("calls", call["id"])["type"] == "outboundPhoneCall"


@pytest.mark.parametrize("reason, said", [
    ("declined", "customer-busy"), ("busy", "customer-busy"), ("do_not_disturb", "customer-busy"),
    ("timeout", "customer-did-not-answer"), ("cancelled", "customer-did-not-answer"),
    ("failed", "wacalls-failed-to-connect-call")])
def test_a_whatsapp_call_that_is_not_picked_up_says_why(line, pretend, reason, said):
    client, store, number = line
    call = client.post("/call", headers=AUTH, json={
        "phoneNumberId": number["id"], "customer": {"number": f"+{CALLER}"}}).json()
    pretend.ended("out-1", reason)
    assert _wait(lambda: store.get("calls", call["id"])["status"] == "ended")
    assert store.get("calls", call["id"])["endedReason"] == said
    assert not client.app.state.wacalls.live


def test_a_call_wacalls_refuses_to_start_is_refused_with_its_reason(line, pretend):
    client, store, number = line
    pretend.start_status, pretend.start_error = 429, "max concurrent calls"
    refused = client.post("/call", headers=AUTH, json={
        "phoneNumberId": number["id"], "customer": {"number": f"+{CALLER}"}})
    assert refused.status_code == 400 and "max concurrent calls" in refused.text
    assert "WaCalls did not place the call" in refused.text
    assert _calls(store)[0]["endedReason"] == "wacalls-failed-to-connect-call"


def test_an_account_that_is_not_linked_places_no_call(line, pretend):
    client, store, number = line
    pretend.push({"type": "auth-state", "sessionId": SESSION, "paired": False, "state": "qr"})
    assert _wait(lambda: not client.app.state.wacalls.info[SESSION]["paired"])
    refused = client.post("/call", headers=AUTH, json={
        "phoneNumberId": number["id"], "customer": {"number": f"+{CALLER}"}})
    assert refused.status_code == 400 and "isn't linked" in refused.text
    assert not pretend.made("POST", f"/api/sessions/{SESSION}/calls")


def test_when_wacalls_goes_away_the_call_ends_and_it_comes_back(line, pretend):
    client, store, number = line
    pretend.incoming("in-4")
    assert _wait(lambda: _calls(store) and _calls(store)[0]["status"] == "in-progress")
    pretend.drop_streams()
    assert _wait(lambda: _calls(store)[0]["status"] == "ended")
    assert _wait(lambda: _shown(client, number)["wacallsConnected"], 15)  # the stream is opened again
    assert len(pretend.event_clients) == 2


def test_a_server_with_no_whatsapp_number_never_connects(pretend, quiet_providers):
    settings = Settings(api_key="test-key", public_url="", db_path=":memory:", wacalls_url=pretend.url)
    with TestClient(create_app(settings, Store(":memory:"))):
        time.sleep(0.3)
        assert not pretend.event_clients
