"""WhatsApp calls through Wavoip: OpenRing against a pretend Wavoip on this computer.

The device server is the reference Socket.IO server (python-socketio), so OpenRing's own little Socket.IO client is
checked against the real protocol; the audio relay is a plain WebSocket server. Nothing here reaches Wavoip.
"""

import asyncio
import threading
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from openring import wavoip
from openring.api import create_app
from openring.config import Settings
from openring.store import Store

from test_api import ASSISTANT, AUTH

socketio = pytest.importorskip("socketio")

TOKEN = "dev-token-0123456789"
CALLER = "5511999990001"
RELAY_PING = b"\x00\x01\x02\x03"


class PretendWavoip:
    """Wavoip's two servers: a device's Socket.IO, and the relay with a call's audio."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.said: list[tuple[str, tuple]] = []  # what OpenRing told the device server
        self.auth = None
        self.frames: list[bytes] = []  # audio OpenRing sent to the relay
        self.texts: list[str] = []
        self.relay_tokens: list[str] = []
        self.relay_clients: list = []
        self.start_reply = {"type": "success", "result": {"id": "out-1", "peer": {"phone": CALLER}}}
        self.offer_acked = threading.Event()
        self._ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        assert self._ready.wait(20)

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._serve())
        self.loop.run_forever()

    async def _serve(self):
        import uvicorn
        from websockets.asyncio.server import serve

        sio = self.sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins="*")

        @sio.event
        async def connect(sid, _environ, auth):
            self.sid, self.auth = sid, auth
            await sio.emit("device:init", ("open", "UNOFFICIAL", {"phone": "5511888880000"}, None, False), to=sid)

        @sio.on("*")
        async def anything(event, _sid, *args):
            self.said.append((event, args))
            if event == "call.start":
                return self.start_reply
            return {"type": "success"}

        app = socketio.ASGIApp(sio, socketio_path=f"/{TOKEN}/websocket")
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error", ws="websockets-sansio")
        self.server = uvicorn.Server(config)
        self.loop.create_task(self.server.serve())
        while not self.server.started:
            await asyncio.sleep(0.02)
        self.port = self.server.servers[0].sockets[0].getsockname()[1]

        async def relay(ws):
            self.relay_tokens += parse_qs(urlsplit(ws.request.path).query).get("token", [])
            self.relay_clients.append(ws)
            try:
                async for message in ws:
                    (self.texts if isinstance(message, str) else self.frames).append(message)
            except Exception:
                pass

        self.relay = await serve(relay, "127.0.0.1", 0)
        self.relay_port = self.relay.sockets[0].getsockname()[1]
        self._ready.set()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    @property
    def plan(self):
        return {"type": "relay", "host": "127.0.0.1", "port": str(self.relay_port)}

    def do(self, coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(10)

    def emit(self, event, *args):
        self.do(self.sio.emit(event, args, to=self.sid))

    def offer(self, call_id="in-1", plan=None):
        self.offer_acked.clear()
        offer = {"id": call_id, "peer": {"phone": CALLER, "displayName": "Ana", "profilePicture": None},
                 "offer": plan or self.plan}
        self.do(self.sio.emit("call:offer", offer, to=self.sid, callback=self.offer_acked.set))

    def to_relay(self, data):
        self.do(self.relay_clients[-1].send(data))

    def told(self, event):
        return [args for name, args in self.said if name == event]

    def close(self):
        async def stop():
            self.relay.close()
            self.server.should_exit = True
            await asyncio.sleep(0.2)

        self.do(stop())
        self.loop.call_soon_threadsafe(self.loop.stop)


@pytest.fixture
def pretend():
    server = PretendWavoip()
    yield server
    server.close()


@pytest.fixture
def line(pretend, monkeypatch, quiet_providers):
    """An OpenRing server with an assistant on a WhatsApp number of the pretend Wavoip: (client, store, number)."""
    for k in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.setenv(k, "fake")
    store = Store(":memory:")
    settings = Settings(api_key="test-key", public_url="", db_path=":memory:", wavoip_url=pretend.url)
    with TestClient(create_app(settings, store)) as client:
        a = client.post("/assistant", json=ASSISTANT, headers=AUTH).json()
        number = client.post("/phone-number", headers=AUTH, json={
            "provider": "wavoip", "number": "+5511888880000", "wavoipToken": TOKEN, "assistantId": a["id"]}).json()
        _wait(lambda: client.get(f"/phone-number/{number['id']}", headers=AUTH).json().get("wavoipConnected"))
        yield client, store, number


def _wait(condition, seconds=10):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        if condition():
            return True
        time.sleep(0.05)
    return bool(condition())


def _calls(store):
    return store.list("calls")


def test_a_whatsapp_number_needs_its_token_and_never_shows_it(line, pretend):
    client, store, number = line
    assert "wavoipToken" not in number and number["wavoipTokenSet"] is True
    shown = client.get(f"/phone-number/{number['id']}", headers=AUTH).json()
    listed = client.get("/phone-number", headers=AUTH).json()
    patched = client.patch(f"/phone-number/{number['id']}", json={"name": "WhatsApp"}, headers=AUTH).json()
    for view in (shown, listed[0], patched):
        assert TOKEN not in str(view) and view["wavoipTokenSet"] is True
    assert store.get("phone_numbers", number["id"])["wavoipToken"] == TOKEN  # kept through a change of name
    assert shown["wavoipStatus"] == "ready" and shown["wavoipLinkedNumber"] == "+5511888880000"
    assert pretend.auth == {"version": "official"}

    for token in (None, "", "a b", "with/slash", "x" * 300, 5):
        refused = client.post("/phone-number", headers=AUTH, json={
            "provider": "wavoip", "number": "+5511777770000", "wavoipToken": token})
        assert refused.status_code == 400 and "wavoipToken" in refused.json()["detail"]

    gone = client.delete(f"/phone-number/{number['id']}", headers=AUTH)
    assert gone.status_code == 200 and TOKEN not in gone.text
    assert _wait(lambda: not client.app.state.whatsapp.devices)


def test_a_whatsapp_call_is_answered_by_the_assistant(line, pretend):
    client, store, number = line
    pretend.offer("in-1")
    assert pretend.offer_acked.wait(5)  # Wavoip waits for the offer to be acknowledged
    assert _wait(lambda: pretend.told("call.accept"))
    assert pretend.told("call.accept")[0] == ("in-1", {"type": "none"})
    assert _wait(lambda: len(pretend.frames) > 10)  # audio flows at once: silence, until there is something to say
    assert pretend.relay_tokens == [TOKEN]
    assert all(len(frame) == wavoip.FRAME_BYTES for frame in pretend.frames)

    call = _calls(store)[0]
    assert call["type"] == "inboundPhoneCall" and call["customer"] == {"number": f"+{CALLER}"}
    assert call["phoneCallProvider"] == "wavoip" and call["phoneCallProviderId"] == "in-1"
    assert call["phoneNumberId"] == number["id"]
    assert _wait(lambda: _calls(store)[0]["status"] == "in-progress")

    pretend.to_relay(RELAY_PING)  # the relay's keepalive
    assert _wait(lambda: pretend.texts == ["pong"])
    pretend.to_relay(bytes(wavoip.FRAME_BYTES))  # the caller's audio reaches the call

    started, sent = time.monotonic(), len(pretend.frames)
    time.sleep(1.0)
    rate = (len(pretend.frames) - sent) / (time.monotonic() - started)
    assert 35 < rate < 65  # a 20 ms message every 20 ms, neither a trickle nor a flood

    pretend.emit("call:ended", "in-1", {"status": "ENDED"})
    assert _wait(lambda: _calls(store)[0]["status"] == "ended")
    assert _calls(store)[0]["endedReason"] == "customer-ended-call"
    assert not client.app.state.whatsapp.devices[number["id"]].sessions


def test_a_whatsapp_call_nobody_can_answer_is_rejected(line, pretend):
    client, store, number = line
    client.patch(f"/phone-number/{number['id']}", json={"assistantId": None}, headers=AUTH)
    pretend.offer("in-2")
    assert _wait(lambda: pretend.told("call.reject"))
    assert pretend.told("call.reject")[0] == ("in-2",) and not pretend.told("call.accept")
    assert not pretend.relay_clients


def test_a_call_from_a_device_of_the_official_kind_is_left_ringing(line, pretend):
    client, store, _number = line
    pretend.offer("in-3", plan={"type": "webRTC", "sdp": "v=0"})
    assert pretend.offer_acked.wait(5)
    time.sleep(0.5)
    assert not pretend.told("call.accept") and not pretend.told("call.reject") and not _calls(store)


def test_the_assistant_calls_someone_on_whatsapp(line, pretend):
    client, store, number = line
    placed = client.post("/call", headers=AUTH, json={
        "phoneNumberId": number["id"], "customer": {"number": "+55 11 99999-0001"}})
    assert placed.status_code == 201, placed.text
    call = placed.json()
    assert call["phoneCallProvider"] == "wavoip" and call["phoneCallProviderId"] == "out-1"
    assert pretend.told("call.start") == [(f"+{CALLER}", {"type": "none"})]
    assert call["status"] == "queued" and not pretend.relay_clients  # ringing: no audio yet

    pretend.emit("call:answered", "out-1", pretend.plan)
    assert _wait(lambda: store.get("calls", call["id"])["status"] == "in-progress")
    assert _wait(lambda: len(pretend.frames) > 5)

    pretend.emit("call:ended", "out-1")
    assert _wait(lambda: store.get("calls", call["id"])["status"] == "ended")
    assert store.get("calls", call["id"])["type"] == "outboundPhoneCall"


@pytest.mark.parametrize("event, reason", [
    ("call:rejected", "customer-busy"), ("call:unanswered", "customer-did-not-answer"),
    ("call:ended", "customer-did-not-answer"), ("call:failed", "wavoip-failed-to-connect-call")])
def test_a_whatsapp_call_that_is_not_picked_up_says_why(line, pretend, event, reason):
    client, store, number = line
    call = client.post("/call", headers=AUTH, json={
        "phoneNumberId": number["id"], "customer": {"number": f"+{CALLER}"}}).json()
    pretend.emit(event, "out-1", *(["NO_CALL_PERMISSION"] if event == "call:failed" else []))
    assert _wait(lambda: store.get("calls", call["id"])["status"] == "ended")
    assert store.get("calls", call["id"])["endedReason"] == reason
    assert not pretend.relay_clients


def test_a_call_wavoip_refuses_to_start_is_refused_with_its_reason(line, pretend):
    client, store, number = line
    pretend.start_reply = {"type": "error", "result": "The account is restricted"}
    refused = client.post("/call", headers=AUTH, json={
        "phoneNumberId": number["id"], "customer": {"number": f"+{CALLER}"}})
    assert refused.status_code >= 400 and "The account is restricted" in refused.text
    assert TOKEN not in refused.text
    assert _calls(store)[0]["endedReason"] == "wavoip-failed-to-connect-call"


def test_when_wavoip_goes_away_the_call_ends_and_the_number_says_so(line, pretend):
    client, store, number = line
    pretend.offer("in-4")
    assert _wait(lambda: _calls(store) and _calls(store)[0]["status"] == "in-progress")
    pretend.do(pretend.sio.disconnect(pretend.sid))
    assert _wait(lambda: _calls(store)[0]["status"] == "ended")
    # It comes back by itself.
    assert _wait(lambda: client.get(f"/phone-number/{number['id']}", headers=AUTH).json()["wavoipConnected"], 15)


def test_the_audio_goes_only_where_it_may():
    async def scenario():
        hub = wavoip.Hub(lambda: [], answer=None, run=None, failed=None)  # the real Wavoip: an https address
        for host in ("127.0.0.1", "10.0.0.5", "169.254.169.254", "localhost"):
            with pytest.raises(wavoip.WavoipError):
                await hub.check_relay(host, 443)
        device = wavoip.Device(hub, "n1", TOKEN)
        for relay in ({"host": "evil.example/path", "port": "443"}, {"host": "a.example", "port": "44x"}, {}):
            with pytest.raises(wavoip.WavoipError):
                await wavoip.Session(device, "c1", relay).connect()

    asyncio.run(scenario())
