"""Calls over WhatsApp through WaCalls (beta, unofficial, self-hosted): a WhatsApp number answers and places calls.

WaCalls (github.com/JotaDev66/WaCalls, MIT, Go) links a WhatsApp account as a "session" on a server of your own and
carries its calls. Nothing goes through a third party. OpenRing plays the part of WaCalls's own browser client, whose
protocol is:

- `GET /api/events?clientId=...` is a Server-Sent Events stream of JSON events, each tagged with its `sessionId`:
  `session-list`, `auth-state` (is the account linked), `incoming` (someone is calling), `call-status` (`ringing`,
  `connected`) and `call-ended` (`reason`: user_ended, declined, timeout, busy, cancelled, failed, do_not_disturb).
- `POST /api/sessions/{sid}/calls {"phone"}` rings someone, `POST .../calls/{id}/accept` and `.../reject` answer an
  incoming call, `DELETE .../calls/{id}` hangs up. A client (`X-Client-Id`) can be on one call at a time, so every call
  uses a client id of its own.
- A call's audio is a WebRTC data channel named `pcm`: this side makes the offer (`POST .../calls/{id}/webrtc
  {"sdp_offer"}` answers `{"sdp_answer"}`), and then 16-bit little-endian PCM, mono, 16 kHz, goes both ways in messages
  of any size. No ICE servers: WaCalls is on this computer or on a network it can reach.

WaCalls has no authentication of its own: keep it on 127.0.0.1 or a trusted network, and give OpenRing its address
(`WACALLS_URL`). Unofficial, like every client of a linked WhatsApp account: WhatsApp may restrict or ban a number used
this way.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

import httpx
from loguru import logger

from . import netguard
from .wavoip import FRAME_BYTES, FRAME_SECONDS, MAX_BUFFERED_SECONDS, SAMPLE_RATE

CHANNEL = "pcm"
SESSION_ID = re.compile(r"[A-Za-z0-9._-]{1,100}")
EVENTS_CLIENT = "openring-events"
OPEN_SECONDS = 15.0
REQUEST_SECONDS = 15.0
# How a call that ended before anyone talked is recorded, by WaCalls's reason.
UNREACHED = {"declined": "customer-busy", "busy": "customer-busy", "do_not_disturb": "customer-busy",
             "failed": "wacalls-failed-to-connect-call"}


class WacallsError(Exception):
    """WaCalls refused, or couldn't be reached. The text is for the person running the server."""


def is_session(value: Any) -> bool:
    return isinstance(value, str) and SESSION_ID.fullmatch(value) is not None


def caller_of(peer: Any) -> str:
    """The phone number in a WhatsApp address (`5511999990001:12@s.whatsapp.net`); "" for an anonymous one (`@lid`)."""
    user, _, server = str(peer or "").partition("@")
    if "lid" in server.split("."):
        return ""
    return re.sub(r"\D", "", re.split(r"[:.]", user)[0])


def _why(response: httpx.Response) -> str:
    try:
        said = response.json().get("error")
    except (ValueError, AttributeError):
        said = None
    return str(said)[:200] if said else f"WaCalls answered HTTP {response.status_code}"


class Session:
    """One call's audio: what the caller says comes out of `received`, what the assistant says goes in `queue`."""

    def __init__(self, hub: Hub, session_id: str, call_id: str, owner: str | None = None):
        self.hub, self.session_id, self.id = hub, session_id, call_id
        self.owner = owner or f"openring-{uuid.uuid4().hex[:12]}"  # WaCalls lets a client be on one call at a time
        self.received: asyncio.Queue[bytes] = asyncio.Queue(maxsize=500)
        self.over = False
        self.on_peer_left: Callable[[], Awaitable[None]] | None = None
        self._pc: Any = None
        self._channel: Any = None
        self._out = bytearray()
        self._sending: asyncio.Task | None = None
        self._hung_up = False

    @property
    def path(self) -> str:
        return f"/api/sessions/{quote(self.session_id, safe='')}/calls/{quote(self.id, safe='')}"

    async def connect(self) -> None:
        """Open the audio: the offer, WaCalls's answer, the data channel. Then send: silence, until there is speech."""
        from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription

        opened = asyncio.Event()
        self._pc = pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))  # none: aiortc's default would ask Google
        self._channel = channel = pc.createDataChannel(CHANNEL, ordered=True)

        @channel.on("open")
        def _open() -> None:
            opened.set()

        @channel.on("message")
        def _message(data: Any) -> None:
            if isinstance(data, bytes | bytearray) and len(data) > 1 and not self.received.full():
                # a full queue loses audio, not memory
                self.received.put_nowait(bytes(data[: len(data) - len(data) % 2]))

        @pc.on("connectionstatechange")
        async def _state() -> None:
            if pc.connectionState in ("failed", "closed") and not self.over:
                await self.left()

        try:
            await pc.setLocalDescription(await pc.createOffer())
            reply = await self.hub.request("POST", f"{self.path}/webrtc", self.owner,
                                           {"sdp_offer": pc.localDescription.sdp})
            await pc.setRemoteDescription(RTCSessionDescription(sdp=str(reply.get("sdp_answer") or ""), type="answer"))
            await asyncio.wait_for(opened.wait(), OPEN_SECONDS)
        except WacallsError:
            raise
        except Exception as exc:
            raise WacallsError(f"the call's audio connection failed ({exc.__class__.__name__})") from None
        self._sending = asyncio.ensure_future(self._send())

    async def _send(self) -> None:
        """A 20 ms message every 20 ms, of speech or of silence."""
        silence, due = bytes(FRAME_BYTES), time.monotonic()
        while not self.over:
            frame = bytes(self._out[:FRAME_BYTES]) if self._out else silence
            del self._out[:FRAME_BYTES]
            try:
                if self._channel.readyState == "open":
                    self._channel.send(frame.ljust(FRAME_BYTES, b"\0"))
            except Exception:  # a channel closing: the connection state says so
                pass
            due += FRAME_SECONDS
            late = time.monotonic() - due
            if late > 0.2:  # the computer stalled: carry on from now, without a burst
                due = time.monotonic()
            elif late < 0:
                await asyncio.sleep(-late)
            else:
                await asyncio.sleep(0)

    def queue(self, pcm: bytes) -> None:
        if len(self._out) < MAX_BUFFERED_SECONDS * SAMPLE_RATE * 2:
            self._out += pcm

    def clear(self) -> None:
        self._out.clear()

    @property
    def buffered(self) -> int:
        return len(self._out)

    async def left(self) -> None:
        """The other side is gone (it hung up, the call failed, the audio dropped)."""
        if self.over:
            return
        await self._close()
        self.hub.live.pop(self.id, None)
        if self.on_peer_left is not None:
            await self.on_peer_left()

    async def hang_up(self) -> None:
        """End the call from our side. Safe to call twice, and after the other side left."""
        await self._close()
        if not self._hung_up:
            self._hung_up = True
            self.hub.live.pop(self.id, None)
            try:
                await self.hub.request("DELETE", self.path, self.owner)
            except WacallsError:
                pass

    async def _close(self) -> None:
        self.over = True
        if self._sending is not None and self._sending is not asyncio.current_task():
            self._sending.cancel()
        pc, self._pc = self._pc, None
        if pc is not None:
            try:
                await asyncio.wait_for(pc.close(), 5)
            except Exception:
                pass


class Hub:
    """Every WhatsApp number of this server that goes through WaCalls, over one event stream.

    `numbers()` lists the saved phone numbers; `answer(number_id, caller, wacalls_id)` records an incoming call and
    returns it, or says why nobody answers; `run(call_id, session)` runs a connected call; `failed(call_id, reason)`
    records a call that never connected. `url` is the address of the WaCalls server ("" when there is none).
    """

    def __init__(self, numbers: Callable[[], list[dict[str, Any]]], *, answer: Callable[..., Awaitable[Any]],
                 run: Callable[[str, Session], Awaitable[None]], failed: Callable[[str, str], None], url: str = ""):
        self.numbers, self.answer, self.run, self.failed = numbers, answer, run, failed
        self.url = url.rstrip("/")
        self.numbers_of: dict[str, str] = {}  # WaCalls session id -> phone number id
        self.info: dict[str, dict[str, Any]] = {}  # what WaCalls said about each session
        self.live: dict[str, Session] = {}  # WaCalls call id -> its audio
        self.dialed: dict[str, str] = {}  # WaCalls call id of a call we placed, still ringing -> our call
        self.connected = False
        self.problem: str | None = "connecting to WaCalls"
        self._task: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()
        self._seen: list[str] = []

    # ---- numbers and their state -------------------------------------------------------------------------
    async def refresh(self) -> None:
        """Follow the numbers saved now: connect to WaCalls when there is one, stop when there is none."""
        self.numbers_of = {n["wacallsSession"]: n["id"] for n in self.numbers()
                           if n.get("provider") == "wacalls" and is_session(n.get("wacallsSession"))}
        if self.numbers_of and self.url and self._task is None:
            self._task = asyncio.ensure_future(self._stream())
        elif not self.numbers_of and self._task is not None:
            await self._stop_stream()

    async def stop(self) -> None:
        self.numbers_of = {}
        await self._stop_stream()

    async def _stop_stream(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
        self.connected, self.problem = False, "connecting to WaCalls"
        await asyncio.gather(*(self._end(s) for s in list(self.live.values())))
        self.dialed.clear()

    @staticmethod
    async def _end(session: Session) -> None:
        """Hang a call up because the server is stopping; its pipeline hears that the caller is gone."""
        say = session.on_peer_left
        await session.hang_up()
        if say is not None:
            await say()

    def view(self, number: dict[str, Any]) -> dict[str, Any]:
        """What the dashboard shows about the WhatsApp account behind a number."""
        mine = self.info.get(str(number.get("wacallsSession")))
        if not self.url:
            state = "WACALLS_URL is not set: OpenRing doesn't know where WaCalls is"
        elif not self.connected:
            state = f"not connected to WaCalls: {self.problem}"
        elif mine is None:
            state = "WaCalls has no session with this id"
        elif not mine.get("paired"):
            state = "waiting for a WhatsApp account to be linked (scan the QR code in WaCalls)"
        else:
            state = "ready"
        return {"wacallsConnected": self.connected, "wacallsStatus": state,
                "wacallsLinkedNumber": f"+{caller_of(mine.get('jid'))}" if mine and mine.get("paired") and
                caller_of(mine.get("jid")) else None}

    # ---- talking to WaCalls ------------------------------------------------------------------------------
    async def request(self, method: str, path: str, owner: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            async with netguard.http_client(timeout=REQUEST_SECONDS) as client:
                response = await client.request(method, self.url + path, headers={"X-Client-Id": owner},
                                                **({"json": body} if body is not None else {}))
        except httpx.HTTPError as exc:
            raise WacallsError(f"WaCalls isn't reachable ({exc.__class__.__name__})") from None
        if response.status_code >= 300:
            raise WacallsError(_why(response))
        try:
            data = response.json() if response.content else {}
        except ValueError:
            raise WacallsError("WaCalls answered something unexpected") from None
        return data if isinstance(data, dict) else {}

    async def _stream(self) -> None:
        """The event stream, kept open; when it breaks, calls that were on it are over and it is opened again."""
        delay = 1.0
        while True:
            try:
                async with netguard.http_client(timeout=httpx.Timeout(10, read=90)) as client:
                    async with client.stream("GET", f"{self.url}/api/events",
                                             params={"clientId": EVENTS_CLIENT}) as response:
                        if response.status_code != 200:
                            raise WacallsError(f"WaCalls answered HTTP {response.status_code}")
                        self._state(True, None)
                        delay = 1.0
                        async for line in response.aiter_lines():
                            if line.startswith("data:"):
                                self._event(line[5:].strip())
                why = "WaCalls closed the event stream"
            except asyncio.CancelledError:
                raise
            except (httpx.HTTPError, WacallsError, OSError) as exc:
                why = f"WaCalls isn't reachable ({exc.__class__.__name__})" if not isinstance(exc, WacallsError) \
                    else str(exc)
            self._state(False, why)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 10.0)

    def _state(self, connected: bool, why: str | None) -> None:
        if connected != self.connected or why != self.problem:
            logger.info("WaCalls at {}: {}", self.url, "connected" if connected else why)
        self.connected, self.problem = connected, why
        if not connected:
            for session in list(self.live.values()):  # nobody is left to say these calls ended
                self._spawn(session.left())
            for wacalls_id, call_id in list(self.dialed.items()):
                self.dialed.pop(wacalls_id, None)
                self.failed(call_id, "wacalls-failed-to-connect-call")

    def _spawn(self, coroutine: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ---- events ------------------------------------------------------------------------------------------
    def _event(self, text: str) -> None:
        try:
            event = json.loads(text)
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        kind, session_id, call_id = event.get("type"), event.get("sessionId"), str(event.get("id") or "")
        if kind == "session-list":
            self.info = {str(s["id"]): s for s in event.get("sessions") or [] if isinstance(s, dict) and "id" in s}
        elif kind == "auth-state" and isinstance(session_id, str):
            self.info.setdefault(session_id, {"id": session_id}).update(paired=bool(event.get("paired")),
                                                                         state=event.get("state"))
        elif kind == "incoming" and session_id in self.numbers_of and call_id:
            if call_id not in self._seen:
                self._seen = [*self._seen[-99:], call_id]
                self._spawn(self._incoming(self.numbers_of[session_id], str(session_id), call_id,
                                           caller_of(event.get("peer"))))
        elif kind == "call-status" and event.get("status") == "connected" and call_id in self.dialed:
            call = self.dialed.pop(call_id)
            if session := self.live.get(call_id):
                self._spawn(self.run(call, session))  # someone picked up
        elif kind == "call-ended" and call_id:
            session, call = self.live.pop(call_id, None), self.dialed.pop(call_id, None)
            if call is not None:
                reason = str(event.get("reason") or "")
                self.failed(call, UNREACHED.get(reason, "customer-did-not-answer"))
            if session is not None:
                self._spawn(session.left())

    async def _incoming(self, number_id: str, session_id: str, call_id: str, caller: str) -> None:
        """Someone is calling the WhatsApp number."""
        answer = await self.answer(number_id, f"+{caller}" if caller else "", call_id)
        session = Session(self, session_id, call_id)
        if isinstance(answer, str):  # nobody to answer: say so the way a phone does
            logger.warning("WhatsApp call to number {} refused: {}", number_id, answer)
            try:
                await self.request("POST", f"{session.path}/reject", session.owner, {})
            except WacallsError:
                pass
            return
        try:
            await self.request("POST", f"{session.path}/accept", session.owner, {})
            self.live[call_id] = session
            await session.connect()
        except (WacallsError, OSError) as exc:
            logger.warning("WhatsApp call {} to number {} failed to start: {}", call_id, number_id, exc)
            self.failed(answer["id"], "wacalls-failed-to-connect-call")
            await session.hang_up()
            return
        await self.run(answer["id"], session)

    async def place_call(self, call_id: str, to: str, number: dict[str, Any]) -> str:
        """Ring `to` on WhatsApp. Returns WaCalls's id of the call; the hub hears how it goes."""
        session_id = str(number.get("wacallsSession") or "")
        if not self.url:
            raise WacallsError("WACALLS_URL is not set")
        if not is_session(session_id):
            raise WacallsError("the number has no WaCalls session")
        await self.refresh()
        for _ in range(100):  # a number added a moment ago is still connecting
            if self.connected and session_id in self.info:
                break
            await asyncio.sleep(0.05)
        if not self.connected:
            raise WacallsError(f"not connected to WaCalls: {self.problem}")
        if not (self.info.get(session_id) or {}).get("paired"):
            raise WacallsError("the WhatsApp account isn't linked (scan the QR code in WaCalls)")
        owner = f"openring-{uuid.uuid4().hex[:12]}"
        reply = await self.request("POST", f"/api/sessions/{quote(session_id, safe='')}/calls", owner,
                                   {"phone": re.sub(r"\D", "", to)})
        started = reply.get("call") if isinstance(reply.get("call"), dict) else {}
        wacalls_id = str(started.get("callId") or "")
        if not wacalls_id:
            raise WacallsError("WaCalls started the call without saying which one it is")
        session = Session(self, session_id, wacalls_id, owner)
        self.live[wacalls_id], self.dialed[wacalls_id] = session, call_id
        try:
            await session.connect()  # the audio is ready by the time someone picks up
        except WacallsError:
            self.dialed.pop(wacalls_id, None)
            await session.hang_up()
            raise
        return wacalls_id

