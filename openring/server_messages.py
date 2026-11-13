"""Vapi's server messages: what OpenRing tells your server about a call, and asks it.

They go to the assistant's `server.url` (or the phone number's, for `assistant-request`) in Vapi's
shape, `{"message": {"type": ..., "call": {...}, ...}}`, so a server written for Vapi works unchanged.
An assistant picks which ones it gets with `serverMessages`, as on Vapi.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
from loguru import logger

from . import netguard
from .tools import without_secrets

# What Vapi sends when the assistant doesn't list `serverMessages`. OpenRing sends status-update,
# conversation-update, speech-update, user-interrupted, hang and end-of-call-report of them; tool-calls go to each
# tool's server (see tools.py). Transcripts go only to an assistant that lists them (`transcript_kind`).
DEFAULT_MESSAGES = (
    "conversation-update", "end-of-call-report", "function-call", "hang", "speech-update", "status-update",
    "tool-calls", "transfer-destination-request", "user-interrupted",
)
ASSISTANT_REQUEST_TIMEOUT = 7.5  # seconds; Vapi's limit for answering assistant-request
FINAL_TRANSCRIPTS = 'transcript[transcriptType="final"]'  # Vapi's name for finished sentences only
HANG_SECONDS = 5.0  # a caller who has waited this long for an answer gets a `hang` message sent about it
BACKLOG = 20  # a partial transcript is dropped when this many messages still wait for the server


def server_of(doc: dict[str, Any] | None) -> dict[str, Any] | None:
    """The `server` an assistant or phone number points to (Vapi's older `serverUrl` too), or None."""
    doc = doc or {}
    server = dict(doc.get("server") or {})
    server["url"] = server.get("url") or doc.get("serverUrl")
    if not server["url"]:
        return None
    if not server.get("secret") and doc.get("serverUrlSecret"):
        server["secret"] = doc["serverUrlSecret"]
    return server


def wants(assistant: dict[str, Any], kind: str) -> bool:
    chosen = assistant.get("serverMessages")
    return kind in (DEFAULT_MESSAGES if chosen is None else chosen)


def _headers(server: dict[str, Any]) -> dict[str, str]:
    headers = {str(k): str(v) for k, v in (server.get("headers") or {}).items()}
    if server.get("secret"):
        headers.setdefault("X-Vapi-Secret", str(server["secret"]))  # the header Vapi servers check
    return headers


def message(kind: str, call: dict[str, Any] | None, **fields: Any) -> dict[str, Any]:
    return {"message": {"type": kind, "timestamp": int(time.time() * 1000), "call": without_secrets(call), **fields}}


async def post(server: dict[str, Any], payload: dict[str, Any], *, timeout: float | None = None,
               client: httpx.AsyncClient | None = None) -> httpx.Response:
    try:
        await netguard.check_url_async(server["url"])
    except netguard.UnsafeURL as exc:
        raise httpx.ConnectError(str(exc)) from exc
    owned = client is None
    client = client or netguard.client()
    try:
        return await client.post(server["url"], json=payload, headers=_headers(server),
                                 timeout=timeout or float(server.get("timeoutSeconds") or 20))
    finally:
        if owned:
            await client.aclose()


async def send(assistant: dict[str, Any], kind: str, call: dict[str, Any] | None, **fields: Any) -> None:
    """Tell the assistant's server about the call, if it wants this kind of message. Never raises."""
    server = server_of(assistant)
    if server is None or not wants(assistant, kind):
        return
    try:
        r = await post(server, message(kind, call, **fields))
        if r.status_code >= 400:
            logger.warning("{} to {} answered HTTP {}", kind, netguard.safe_url_for_logs(server["url"]), r.status_code)
    except httpx.HTTPError as exc:
        logger.warning("{} to {} failed: {}", kind, netguard.safe_url_for_logs(server["url"]), _why(exc))


def _why(exc: Exception) -> str:
    """A refused address says why; other failures name their kind (their text may repeat the URL)."""
    return str(exc) if "OpenRing never calls" in str(exc) else exc.__class__.__name__


class Outbox:
    """Messages sent during a call without holding it up, delivered in order before the final report."""

    def __init__(self, assistant: dict[str, Any], current_call):
        self.assistant = assistant
        self.current_call = current_call
        self._chain: asyncio.Future | None = None
        self._waiting = 0

    def send(self, kind: str, *, droppable: bool = False, **fields: Any) -> None:
        """Queue a message. A `droppable` one is left out when the server is far behind: it is out of date by then."""
        if server_of(self.assistant) is None or not wants(self.assistant, kind):
            return
        if droppable and self._waiting >= BACKLOG:
            return
        previous, call = self._chain, self.current_call()
        self._waiting += 1

        async def deliver():
            try:
                if previous is not None:
                    await asyncio.shield(previous)
                await send(self.assistant, kind, call, **fields)
            finally:
                self._waiting -= 1

        self._chain = asyncio.ensure_future(deliver())

    async def flush(self, timeout: float = 10) -> None:
        if self._chain is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._chain), timeout)
            except TimeoutError:
                logger.warning("server messages still pending after {} s; sending the report anyway", timeout)


def transcript_kind(assistant: dict[str, Any], final: bool) -> str | None:
    """The message type a transcript goes out as, or None when the assistant didn't ask for it.

    Transcripts are not among Vapi's defaults: `transcript` asks for partial and final ones, and
    `transcript[transcriptType="final"]` for the final ones alone, under that same name.
    """
    chosen = assistant.get("serverMessages") or ()
    if "transcript" in chosen:
        return "transcript"
    return FINAL_TRANSCRIPTS if final and FINAL_TRANSCRIPTS in chosen else None


class Live:
    """What happens on a call, told as it happens: Vapi's speech-update, user-interrupted, hang and transcript.

    The call tells it who started or stopped speaking and what was heard and said; it keeps the turn count and
    notices a caller left waiting. `turn` counts the exchanges so far: 0 until the caller first answers the assistant.
    """

    def __init__(self, outbox: Outbox, hang_seconds: float | None = None):
        self.outbox = outbox
        self.hang_seconds = HANG_SECONDS if hang_seconds is None else hang_seconds
        self.turn = 0
        self._assistant_speaking = False
        self._user_speaking = False
        self._assistant_spoke = False  # since the caller last started a turn
        self._owed = False  # the caller said something nobody has answered yet
        self._hang: asyncio.Task | None = None

    def assistant_speaking(self, started: bool) -> None:
        if started == self._assistant_speaking:  # Pipecat says it twice, up and down the pipeline
            return
        self._assistant_speaking = started
        if started:
            self._assistant_spoke, self._owed = True, False
            self._stop_waiting()
        self.outbox.send("speech-update", status="started" if started else "stopped", role="assistant", turn=self.turn)

    def user_speaking(self, started: bool) -> None:
        if started == self._user_speaking:
            return
        self._user_speaking = started
        if started:
            self._stop_waiting()
            if self._assistant_speaking:
                self.outbox.send("user-interrupted")
            if self._assistant_spoke:
                self.turn += 1
                self._assistant_spoke = False
        elif self._owed:
            self._wait()
        self.outbox.send("speech-update", status="started" if started else "stopped", role="user", turn=self.turn)

    def heard(self, text: str, final: bool) -> None:
        """What the transcriber made of the caller's speech."""
        if not (text or "").strip():
            return
        if final:
            self._owed = True
            if not self._user_speaking and not self._assistant_speaking:
                self._wait()
        self._transcript("user", text, final)

    def said(self, text: str) -> None:
        """What the assistant said, once it's said."""
        if (text or "").strip():
            self._transcript("assistant", text, True)

    def close(self) -> None:
        self._stop_waiting()

    def _transcript(self, role: str, text: str, final: bool) -> None:
        kind = transcript_kind(self.outbox.assistant, final)
        if kind:
            self.outbox.send(kind, droppable=not final, role=role,
                             transcriptType="final" if final else "partial", transcript=text)

    def _wait(self) -> None:
        if self._hang is None and self.hang_seconds > 0 and wants(self.outbox.assistant, "hang"):
            self._hang = asyncio.ensure_future(self._late())

    def _stop_waiting(self) -> None:
        if self._hang is not None:
            self._hang.cancel()
            self._hang = None

    async def _late(self) -> None:
        await asyncio.sleep(self.hang_seconds)
        self._hang = None
        self._owed = False  # once per wait
        self.outbox.send("hang")


class AssistantRequestError(Exception):
    """The server did not name an assistant; the message is what to tell the caller's call record."""

    def __init__(self, ended_reason: str, detail: str):
        super().__init__(detail)
        self.ended_reason = ended_reason


async def request_assistant(
    server: dict[str, Any], call: dict[str, Any], phone_number: dict[str, Any], *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Ask the phone number's server which assistant answers this call.

    Returns {"assistantId": ...} or {"assistant": {...}} (a transient assistant), or the same for a squad, as Vapi's
    servers answer.
    """
    payload = message("assistant-request", call, phoneNumber=phone_number, customer=call.get("customer"))
    where = netguard.safe_url_for_logs(server["url"])  # the detail is logged; the full URL may hold a secret
    try:
        r = await post(server, payload, timeout=ASSISTANT_REQUEST_TIMEOUT, client=client)
    except httpx.HTTPError as exc:
        raise AssistantRequestError("assistant-request-failed", f"{where}: {_why(exc)}") from exc
    if r.status_code >= 400:
        raise AssistantRequestError("assistant-request-failed", f"{where} answered HTTP {r.status_code}")
    try:
        body = r.json()
    except ValueError as exc:
        raise AssistantRequestError("assistant-request-returned-invalid-json", "the answer is not JSON") from exc
    if not isinstance(body, dict):
        raise AssistantRequestError("assistant-request-returned-invalid-json", "the answer is not a JSON object")
    if body.get("error"):
        raise AssistantRequestError("assistant-request-returned-error", str(body["error"]))
    if body.get("assistantId"):
        return {"assistantId": str(body["assistantId"])}
    if isinstance(body.get("assistant"), dict):
        return {"assistant": body["assistant"]}
    if body.get("squadId"):
        return {"squadId": str(body["squadId"])}
    if isinstance(body.get("squad"), dict):
        return {"squad": body["squad"]}
    raise AssistantRequestError("assistant-request-returned-no-assistant", "the answer has no assistantId or assistant")
