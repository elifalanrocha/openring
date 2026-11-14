"""Calls over WhatsApp through Wavoip (beta, unofficial): a WhatsApp number answers and places calls with an assistant.

Wavoip (wavoip.com) links a WhatsApp account as a "device" and carries its calls. This speaks the protocol of Wavoip's
own MIT-licensed browser client (github.com/wavoip/wavoip-api), with no dependency:

- A device's calls travel over Socket.IO (v4, WebSocket only) at `devices.wavoip.com`, path `/<token>/websocket`.
  The server says `device:init`, `device:open`, `device:connecting`, ..., `call:offer` (someone is calling),
  `call:answered` (someone picked up our call), `call:ended`, `call:rejected`, `call:unanswered`, `call:failed`. We say
  `call.accept`, `call.reject`, `call.start`, `call.cancel`, `call.end`; each is acknowledged with
  `{"type": "success"|"error", "result": ...}`.
- A call's audio travels over a plain WebSocket, `wss://<host>:<port>?token=<token>`, whose address comes in the offer
  or the answer (`{"type": "relay", "host", "port"}`): 16-bit PCM at 16 kHz, 20 ms a message, both ways, silence
  included (a side that stops sending is taken for a dropped call). A 4-byte message is a ping, answered `pong`.

Unofficial: the device is a linked WhatsApp account, not Meta's Calling API, and WhatsApp may restrict or ban a number
used this way. A device of Wavoip's official kind offers WebRTC instead of a relay; those calls are left ringing.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from loguru import logger

from . import netguard

DEVICES_URL = "https://devices.wavoip.com"
SAMPLE_RATE = 16000
FRAME_BYTES = 640  # 20 ms of 16-bit audio at 16 kHz
FRAME_SECONDS = 0.02
ACK_SECONDS = 10.0
PING_BYTES = 4
FINAL_CLOSES = (1000, 1008)  # the relay ended the call, or refused the token: not a drop to come back from
MEDIA_RETRY_SECONDS = 30.0
MAX_BUFFERED_SECONDS = 30.0  # of the assistant's speech waiting to be sent
TOKEN = re.compile(r"[^\s/?#]{4,200}")
# What a device's status means for someone reading the dashboard.
STATUS = {
    "open": "ready",
    "connecting": "waiting for a WhatsApp account to be linked (scan the QR code in Wavoip's panel)",
    "close": "no WhatsApp account linked",
    "restarting": "restarting",
    "hibernating": "asleep (waking it up)",
    "BUILDING": "starting",
    "WAITING_PAYMENT": "Wavoip is waiting for a payment",
    "EXTERNAL_INTEGRATION_ERROR": "Wavoip lost its link to WhatsApp; restart the device in Wavoip's panel",
    "error": "in error",
}


class WavoipError(Exception):
    """Wavoip refused, or couldn't be reached. The text is for the person running the server."""


def is_token(value: Any) -> bool:
    return isinstance(value, str) and TOKEN.fullmatch(value) is not None


def without_token(doc: dict[str, Any]) -> dict[str, Any]:
    """A phone number as the API shows it: the device's token never leaves."""
    if "wavoipToken" not in doc:
        return doc
    return {**{k: v for k, v in doc.items() if k != "wavoipToken"}, "wavoipTokenSet": bool(doc.get("wavoipToken"))}


def _digits(number: Any) -> str:
    return re.sub(r"\D", "", str(number or ""))


def _why(reply: Any) -> str | None:
    """None for an acknowledgement that says success, else what went wrong."""
    if isinstance(reply, dict) and reply.get("type") == "success":
        return None
    if isinstance(reply, dict) and reply.get("result"):
        return str(reply["result"])[:200]
    return "Wavoip gave no answer" if reply is None else "Wavoip answered something unexpected"


class SocketIO:
    """The little of Socket.IO a device needs: events both ways, with acknowledgements, coming back after a drop."""

    def __init__(self, url: str, path: str, auth: dict[str, Any],
                 on_event: Callable[[str, list[Any], Callable[..., Awaitable[None]] | None], Awaitable[None]],
                 on_state: Callable[[bool, str | None], None]):
        parts = urlsplit(url)
        scheme = "wss" if parts.scheme == "https" else "ws"
        self.address = f"{scheme}://{parts.netloc}{path}/?EIO=4&transport=websocket"
        self.auth, self.on_event, self.on_state = auth, on_event, on_state
        self._ws: Any = None
        self._acks: dict[int, asyncio.Future] = {}
        self._next_ack = 0
        self._closed = False
        self._tasks: set[asyncio.Task] = set()

    async def run(self) -> None:
        """Stay connected until `close`, waiting longer after each failure (up to a minute)."""
        wait = 1.0
        while not self._closed:
            try:
                await self._session()
                wait = 1.0
                why = "the connection dropped"
            except asyncio.CancelledError:
                raise
            except WavoipError as exc:
                why = str(exc)
            except Exception as exc:  # a refused connection, a bad certificate, a broken message
                why = exc.__class__.__name__
            self._drop_acks()
            if self._closed:
                break
            self.on_state(False, why)
            await asyncio.sleep(wait)
            wait = min(wait * 2, 60.0)

    async def _session(self) -> None:
        from websockets.asyncio.client import connect

        async with connect(self.address, open_timeout=15, max_size=2**20) as ws:
            opened = await asyncio.wait_for(ws.recv(), 15)
            if not isinstance(opened, str) or not opened.startswith("0"):
                raise WavoipError("this is not a Socket.IO server")
            hello = json.loads(opened[1:] or "{}")
            patience = (float(hello.get("pingInterval", 25000)) + float(hello.get("pingTimeout", 20000))) / 1000 + 5
            await ws.send("40" + json.dumps(self.auth))
            while True:
                message = await asyncio.wait_for(ws.recv(), patience)
                if not isinstance(message, str):
                    continue
                if message == "2":  # the server's ping
                    await ws.send("3")
                elif message.startswith("40"):
                    self._ws = ws
                    self.on_state(True, None)
                elif message.startswith("44"):
                    refused = json.loads(message[2:] or "{}")
                    raise WavoipError(f"Wavoip refused the device: {str(refused.get('message') or refused)[:200]}")
                elif message.startswith("42"):
                    self._event(message[2:])
                elif message.startswith("43"):
                    self._acknowledged(message[2:])
                elif message.startswith(("41", "1")):  # the server hung up on us
                    return

    def _event(self, text: str) -> None:
        number, body = re.fullmatch(r"(\d*)(.*)", text, re.S).groups()
        try:
            name, *args = json.loads(body)
        except (ValueError, TypeError):
            return
        ack = None
        if number:
            async def ack(*answer: Any) -> None:
                await self._send("43" + number + json.dumps(list(answer)))
        task = asyncio.ensure_future(self._handle(str(name), args, ack))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _handle(self, name: str, args: list[Any], ack: Any) -> None:
        try:
            await self.on_event(name, args, ack)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Wavoip event {} failed", name)

    def _acknowledged(self, text: str) -> None:
        number, body = re.fullmatch(r"(\d*)(.*)", text, re.S).groups()
        waiting = self._acks.pop(int(number), None) if number else None
        if waiting is not None and not waiting.done():
            try:
                answer = json.loads(body or "[]")
            except ValueError:
                answer = []
            waiting.set_result(answer[0] if answer else None)

    def _drop_acks(self) -> None:
        self._ws = None
        for waiting in self._acks.values():
            if not waiting.done():
                waiting.set_exception(WavoipError("the connection to Wavoip dropped"))
        self._acks.clear()

    async def _send(self, text: str) -> None:
        if self._ws is None:
            raise WavoipError("not connected to Wavoip")
        try:
            await self._ws.send(text)
        except Exception as exc:
            raise WavoipError("the connection to Wavoip dropped") from exc

    async def emit(self, event: str, *args: Any, timeout: float = ACK_SECONDS) -> Any:
        """Send an event and wait for its acknowledgement: what the server answered."""
        self._next_ack += 1
        number = self._next_ack
        waiting: asyncio.Future = asyncio.get_running_loop().create_future()
        self._acks[number] = waiting
        try:
            await self._send(f"42{number}" + json.dumps([event, *args]))
            return await asyncio.wait_for(waiting, timeout)
        except TimeoutError as exc:
            raise WavoipError(f"Wavoip didn't answer {event} in {timeout:g} s") from exc
        finally:
            self._acks.pop(number, None)

    async def close(self) -> None:
        self._closed = True
        for task in list(self._tasks):
            task.cancel()
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass


class Session:
    """One call's audio: what the caller says comes out of `received`, what the assistant says goes in `queue`."""

    def __init__(self, device: Device, call_id: str, relay: dict[str, Any]):
        self.device, self.id = device, call_id
        self.host, self.port = str(relay.get("host") or ""), str(relay.get("port") or "")
        self.received: asyncio.Queue[bytes] = asyncio.Queue(maxsize=500)
        self.over = False
        self.on_peer_left: Callable[[], Awaitable[None]] | None = None
        self._ws: Any = None
        self._out = bytearray()
        self._tasks: list[asyncio.Task] = []
        self._hung_up = False

    @property
    def address(self) -> str:
        scheme = "wss" if self.device.hub.url.startswith("https") else "ws"
        return f"{scheme}://{self.host}:{self.port}?token={quote(self.device.token, safe='')}"

    async def connect(self) -> None:
        """Open the audio connection and start sending: silence, until the assistant has something to say."""
        if not re.fullmatch(r"[A-Za-z0-9.\-]{1,253}", self.host) or not self.port.isdigit():
            raise WavoipError("Wavoip gave no address for the call's audio")
        await self.device.hub.check_relay(self.host, int(self.port))
        self._ws = await self._open()
        self._tasks = [asyncio.ensure_future(self._receive()), asyncio.ensure_future(self._send())]

    async def _open(self) -> Any:
        from websockets.asyncio.client import connect

        try:
            return await connect(self.address, open_timeout=10, max_size=2**20)
        except Exception as exc:  # the address carries the token: the error must not
            raise WavoipError(f"the call's audio connection failed ({exc.__class__.__name__})") from None

    async def _receive(self) -> None:
        from websockets.exceptions import ConnectionClosed

        while not self.over:
            try:
                ws = self._ws
                message = await ws.recv()
            except ConnectionClosed as closed:
                code = closed.rcvd.code if closed.rcvd else 1006
                if self.over or code in FINAL_CLOSES or not await self._reopen():
                    break
                continue
            except Exception:
                break
            if isinstance(message, bytes | bytearray):
                if len(message) == PING_BYTES:
                    await self._quietly(ws.send("pong"))
                elif not self.received.full():  # a pipeline that stopped reading loses audio, not memory
                    self.received.put_nowait(bytes(message))
        await self.left()

    async def _reopen(self) -> bool:
        """The audio connection dropped in the middle: try again for half a minute, as Wavoip's client does."""
        until = time.monotonic() + MEDIA_RETRY_SECONDS
        while not self.over and time.monotonic() < until:
            await asyncio.sleep(1)
            try:
                self._ws = await self._open()
                return True
            except WavoipError:
                continue
        return False

    async def _send(self) -> None:
        """A 20 ms message every 20 ms, of speech or of silence."""
        silence, due = bytes(FRAME_BYTES), time.monotonic()
        while not self.over:
            frame = bytes(self._out[:FRAME_BYTES]) if self._out else silence
            del self._out[:FRAME_BYTES]
            if (ws := self._ws) is not None:  # None while a dropped connection is reopened
                await self._quietly(ws.send(frame.ljust(FRAME_BYTES, b"\0")))
            due += FRAME_SECONDS
            late = time.monotonic() - due
            if late > 0.2:  # the computer stalled: carry on from now, without a burst
                due = time.monotonic()
            elif late < 0:
                await asyncio.sleep(-late)
            else:
                await asyncio.sleep(0)

    @staticmethod
    async def _quietly(sending: Awaitable[Any]) -> None:
        try:
            await sending
        except Exception:  # a connection dropping: the receiver notices and reopens it
            pass

    def queue(self, pcm: bytes) -> None:
        if len(self._out) < MAX_BUFFERED_SECONDS * SAMPLE_RATE * 2:
            self._out += pcm

    def clear(self) -> None:
        self._out.clear()

    @property
    def buffered(self) -> int:
        return len(self._out)

    async def left(self) -> None:
        """The other side is gone (it hung up, the call failed, the audio dropped for good)."""
        if self.over:
            return
        await self._close()
        if self.on_peer_left is not None:
            await self.on_peer_left()

    async def hang_up(self) -> None:
        """End the call from our side. Safe to call twice, and after the other side left."""
        await self._close()
        if not self._hung_up:
            self._hung_up = True
            self.device.sessions.pop(self.id, None)
            try:
                await self.device.io.emit("call.end", self.id, timeout=5)
            except WavoipError:
                pass

    async def _close(self) -> None:
        self.over = True
        current = asyncio.current_task()
        for task in self._tasks:
            if task is not current:
                task.cancel()
        ws, self._ws = self._ws, None
        if ws is not None:
            await self._quietly(ws.close())


class Device:
    """One Wavoip device: the WhatsApp number behind a phone number saved with `provider: "wavoip"`."""

    def __init__(self, hub: Hub, number_id: str, token: str):
        self.hub, self.number_id, self.token = hub, number_id, token
        self.connected = False
        self.problem: str | None = "connecting to Wavoip"
        self.status: str | None = None
        self.kind: str | None = None  # OFFICIAL or UNOFFICIAL
        self.linked: str | None = None  # the WhatsApp number linked to the device
        self.restricted = False
        self.sessions: dict[str, Session] = {}
        self.dialed: dict[str, str] = {}  # Wavoip's id of a call we placed, still ringing -> our call
        self.io = SocketIO(hub.url, f"/{quote(token, safe='')}/websocket", {"version": "official"},
                           self._on_event, self._on_state)
        self._task: asyncio.Task | None = None
        self._warned = False

    def start(self) -> None:
        self._task = asyncio.ensure_future(self.io.run())

    async def stop(self) -> None:
        for session in list(self.sessions.values()):
            await session.hang_up()
        await self.io.close()
        if self._task is not None:
            self._task.cancel()

    def _on_state(self, connected: bool, why: str | None) -> None:
        if connected != self.connected or why != self.problem:
            logger.info("Wavoip device of number {}: {}", self.number_id, "connected" if connected else why)
        self.connected, self.problem = connected, why
        if not connected:
            for session in list(self.sessions.values()):  # nobody is left to say these calls ended
                asyncio.ensure_future(session.left())
            for wavoip_id, call_id in list(self.dialed.items()):
                self.dialed.pop(wavoip_id, None)
                self.hub.failed(call_id, "wavoip-failed-to-connect-call")

    def view(self) -> dict[str, Any]:
        """What the dashboard shows about the device."""
        if not self.connected:
            state = f"not connected to Wavoip: {self.problem}"
        elif self.kind == "OFFICIAL":
            state = "a device of Wavoip's official kind: OpenRing takes calls from unofficial devices only, for now"
        else:
            state = STATUS.get(self.status or "", self.status or "connected, waiting for the device's status")
        return {"wavoipConnected": self.connected, "wavoipStatus": state, "wavoipDeviceStatus": self.status,
                "wavoipLinkedNumber": f"+{self.linked}" if self.linked else None, "wavoipRestricted": self.restricted}

    async def _on_event(self, name: str, args: list[Any], ack: Any) -> None:
        first = args[0] if args else None
        if name == "device:init":
            status, kind, contact, _qr, restricted, *_ = [*args, None, None, None, None, None]
            self.status, self.kind, self.restricted = status, kind, bool(restricted)
            self.linked = _digits((contact or {}).get("phone")) or None if isinstance(contact, dict) else None
        elif name == "device:open":
            self.status = "open"
            self.linked = _digits(first.get("phone")) or None if isinstance(first, dict) else self.linked
        elif name in ("device:connecting", "device:close"):
            self.status, self.linked = name.split(":")[1], None
        elif name in ("device:restarting", "device:hibernating", "device:building"):
            self.status = "BUILDING" if name.endswith("building") else name.split(":")[1]
            if name.endswith("hibernating"):
                await self.hub.wake(self.token)
        elif name == "device:restriction:changed":
            self.restricted = bool(first)
        elif name == "call:offer":
            await self._offer(first if isinstance(first, dict) else {}, ack)
        elif name == "call:answered":
            await self._answered(str(first), args[1] if len(args) > 1 and isinstance(args[1], dict) else {})
        elif name in ("call:ended", "call:failed"):
            session = self.sessions.pop(str(first), None)
            if session is not None:
                if name == "call:failed":
                    logger.warning("WhatsApp call {} failed: {}", first, str(args[1] if len(args) > 1 else "")[:80])
                await session.left()
            elif call_id := self.dialed.pop(str(first), None):
                self.hub.failed(call_id, "wavoip-failed-to-connect-call" if name == "call:failed"
                                else "customer-did-not-answer")
        elif name in ("call:rejected", "call:unanswered"):
            if call_id := self.dialed.pop(str(first), None):
                self.hub.failed(call_id, "customer-busy" if name == "call:rejected" else "customer-did-not-answer")

    async def _offer(self, offer: dict[str, Any], ack: Any) -> None:
        """Someone is calling the WhatsApp number."""
        if ack is not None:
            await ack()
        wavoip_id, relay = str(offer.get("id") or ""), offer.get("offer")
        caller = _digits((offer.get("peer") or {}).get("phone")) if isinstance(offer.get("peer"), dict) else ""
        if not wavoip_id or not isinstance(relay, dict) or relay.get("type") != "relay":
            if not self._warned:
                self._warned = True
                logger.warning("number {}: a WhatsApp call came without a relay for its audio (a device of Wavoip's "
                               "official kind?); OpenRing leaves those ringing", self.number_id)
            return
        answer = await self.hub.answer(self.number_id, f"+{caller}" if caller else "", wavoip_id)
        if isinstance(answer, str):  # nobody to answer: say so the way a phone does
            logger.warning("WhatsApp call to number {} refused: {}", self.number_id, answer)
            await self._refuse(wavoip_id)
            return
        session = Session(self, wavoip_id, relay)
        try:
            if why := _why(await self.io.emit("call.accept", wavoip_id, {"type": "none"})):
                raise WavoipError(f"Wavoip didn't let the call be answered: {why}")
            self.sessions[wavoip_id] = session
            await session.connect()
        except (WavoipError, OSError) as exc:
            logger.warning("WhatsApp call {} to number {} failed to start: {}", wavoip_id, self.number_id, exc)
            self.hub.failed(answer["id"], "wavoip-failed-to-connect-call")
            await session.hang_up()
            return
        await self.hub.run(answer["id"], session)

    async def _refuse(self, wavoip_id: str) -> None:
        try:
            await self.io.emit("call.reject", wavoip_id, timeout=5)
        except WavoipError:
            pass

    async def _answered(self, wavoip_id: str, relay: dict[str, Any]) -> None:
        """Someone picked up a call we placed."""
        call_id = self.dialed.pop(wavoip_id, None)
        if call_id is None:
            return
        session = Session(self, wavoip_id, relay)
        try:
            if relay.get("type") != "relay":
                raise WavoipError("the call has no relay for its audio (a device of Wavoip's official kind?)")
            self.sessions[wavoip_id] = session
            await session.connect()
        except (WavoipError, OSError) as exc:
            logger.warning("WhatsApp call {} from number {} failed to start: {}", wavoip_id, self.number_id, exc)
            self.hub.failed(call_id, "wavoip-failed-to-connect-call")
            await session.hang_up()
            return
        await self.hub.run(call_id, session)

    async def place_call(self, call_id: str, to: str) -> str:
        """Ring `to` on WhatsApp. Returns Wavoip's id of the call; the hub hears how it goes."""
        if not self.connected:
            raise WavoipError(f"not connected to Wavoip: {self.problem}")
        if self.kind == "OFFICIAL":
            raise WavoipError("this is a device of Wavoip's official kind; OpenRing calls through unofficial ones only")
        if self.status != "open":
            raise WavoipError(f"the WhatsApp device is {STATUS.get(self.status or '', self.status or 'not ready')}")
        reply = await self.io.emit("call.start", f"+{_digits(to)}", {"type": "none"})
        if why := _why(reply):
            raise WavoipError(why)
        result = reply.get("result") if isinstance(reply.get("result"), dict) else {}
        wavoip_id = str(result.get("id") or "")
        if not wavoip_id:
            raise WavoipError("Wavoip started the call without saying which one it is")
        self.dialed[wavoip_id] = call_id
        return wavoip_id


class Hub:
    """Every Wavoip device of this server: one per phone number saved with `provider: "wavoip"`.

    `numbers()` lists those phone numbers; `answer(number_id, caller, wavoip_id)` records an incoming call and returns
    it, or says why nobody answers; `run(call_id, session)` runs a connected call; `failed(call_id, reason)` records
    a call that never connected.
    """

    def __init__(self, numbers: Callable[[], list[dict[str, Any]]], *, answer: Callable[..., Awaitable[Any]],
                 run: Callable[[str, Session], Awaitable[None]], failed: Callable[[str, str], None],
                 url: str = DEVICES_URL):
        self.numbers, self.answer, self.run, self.failed = numbers, answer, run, failed
        self.url = url.rstrip("/")
        self.devices: dict[str, Device] = {}

    async def refresh(self) -> None:
        """Connect the devices of the numbers saved now, and drop those of numbers deleted or given another token."""
        wanted = {n["id"]: n["wavoipToken"] for n in self.numbers()
                  if n.get("provider") == "wavoip" and is_token(n.get("wavoipToken"))}
        for number_id, device in list(self.devices.items()):
            if wanted.get(number_id) != device.token:
                del self.devices[number_id]
                await device.stop()
        for number_id, token in wanted.items():
            if number_id not in self.devices:
                self.devices[number_id] = device = Device(self, number_id, token)
                device.start()

    async def stop(self) -> None:
        devices, self.devices = list(self.devices.values()), {}
        for device in devices:
            await device.stop()

    def view(self, number_id: str) -> dict[str, Any]:
        device = self.devices.get(number_id)
        return device.view() if device else {}

    async def place_call(self, call_id: str, to: str, number: dict[str, Any]) -> str:
        device = self.devices.get(number["id"])
        if device is None:
            await self.refresh()
            device = self.devices.get(number["id"])
        if device is None:
            raise WavoipError("the number has no Wavoip token")
        for _ in range(50):  # a device added a moment ago is still connecting
            if device.connected and device.status:
                break
            await asyncio.sleep(0.1)
        return await device.place_call(call_id, to)

    async def check_relay(self, host: str, port: int) -> None:
        """A call's audio goes where Wavoip says: never to this computer or its network, unless Wavoip itself is here
        (a test)."""
        if not self.url.startswith("https"):
            return
        import ipaddress
        import socket

        try:
            found = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise WavoipError("the address Wavoip gave for the call's audio doesn't exist") from exc
        for *_, address in found:
            if not ipaddress.ip_address(address[0].split("%", 1)[0]).is_global:
                raise WavoipError("Wavoip pointed the call's audio at a private address, which OpenRing won't use")

    async def wake(self, token: str) -> None:
        """A device left alone goes to sleep; asking about it wakes it up."""
        try:
            async with netguard.http_client(timeout=10) as client:
                await client.get(f"{self.url}/{quote(token, safe='')}/whatsapp/all_info")
        except httpx.HTTPError:
            pass


def transport_for(session: Session) -> Any:
    """The Pipecat transport of a WhatsApp call: its audio, in and out, at 16 kHz."""
    from pipecat.frames.frames import ClientConnectedFrame, InputAudioRawFrame, InterruptionFrame
    from pipecat.transports.base_input import BaseInputTransport
    from pipecat.transports.base_output import BaseOutputTransport
    from pipecat.transports.base_transport import BaseTransport, TransportParams

    params = TransportParams(audio_in_enabled=True, audio_out_enabled=True, audio_out_10ms_chunks=2)

    class Input(BaseInputTransport):
        def __init__(self, transport: Any):
            super().__init__(params)
            self._transport = transport
            self._receiving: asyncio.Task | None = None

        async def start(self, frame: Any) -> None:
            await super().start(frame)
            await self._transport._call_event_handler("on_client_connected", session)
            await self.push_frame(ClientConnectedFrame())
            if self._receiving is None:
                self._receiving = self.create_task(self._receive())
            await self.set_transport_ready(frame)

        async def _receive(self) -> None:
            while True:
                pcm = await session.received.get()
                await self.push_audio_frame(InputAudioRawFrame(pcm, sample_rate=SAMPLE_RATE, num_channels=1))

        async def _stop_receiving(self) -> None:
            if self._receiving is not None:
                await self.cancel_task(self._receiving)
                self._receiving = None

        async def stop(self, frame: Any) -> None:
            await super().stop(frame)
            await self._stop_receiving()

        async def cancel(self, frame: Any) -> None:
            await super().cancel(frame)
            await self._stop_receiving()

    class Output(BaseOutputTransport):
        def __init__(self) -> None:
            super().__init__(params)

        async def start(self, frame: Any) -> None:
            await super().start(frame)
            await self.set_transport_ready(frame)

        async def process_frame(self, frame: Any, direction: Any) -> None:
            await super().process_frame(frame, direction)
            if isinstance(frame, InterruptionFrame):  # what wasn't said yet goes unsaid
                session.clear()

        async def write_audio_frame(self, frame: Any) -> bool:
            if session.over:
                return False
            session.queue(frame.audio)
            while session.buffered > FRAME_BYTES * 2 and not session.over:  # as fast as the caller hears it
                await asyncio.sleep(0.005)
            return True

    class WhatsAppTransport(BaseTransport):
        def __init__(self) -> None:
            super().__init__()
            self._input = Input(self)
            self._output = Output()
            self._register_event_handler("on_client_connected")
            self._register_event_handler("on_client_disconnected")

        def input(self) -> Any:
            return self._input

        def output(self) -> Any:
            return self._output

    transport = WhatsAppTransport()

    async def peer_left() -> None:
        await transport._call_event_handler("on_client_disconnected", session)

    session.on_peer_left = peer_left
    return transport
