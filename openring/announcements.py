"""Recorded messages and on-call escalations: calls that play a recording instead of holding a conversation.

An assistant with `announcement: {"audioId": ...}` (OpenRing's own field) plays an uploaded recording when the call is
answered and hangs up: no transcriber, model or voice, so the call costs only the line. A campaign with such an
assistant plays the same message to everyone on its list.

An escalation (`/escalation`) calls an on-call list in order with such an assistant until someone presses the
confirmation key, going round the list again up to `rounds` times, like an incident pager.

Twilio only for now: it fetches the recording from `GET /announcement/play/{token}`, a one-hour link made for one call.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import struct
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from loguru import logger

from .store import Store, now_iso

MAX_BYTES = 10_000_000
MAX_SECONDS = 600
LINK_SECONDS = 3600  # a call's link to its recording works this long
MAX_PEOPLE, MAX_ROUNDS = 20, 5
CALL_WAIT_SECONDS = 15 * 60  # an escalation gives up on a call that never ends after this long
CONFIRM_PROMPT = "Press {digit} to confirm."


class AnnouncementError(ValueError):
    """What's wrong with an upload or a request; the text is for the person sending it."""


def directory() -> Path:
    """OPENRING_ANNOUNCEMENTS_DIR, or an `announcements` folder next to the database."""
    chosen = os.environ.get("OPENRING_ANNOUNCEMENTS_DIR", "").strip()
    if chosen:
        return Path(chosen)
    return Path(os.environ.get("OPENRING_DB_PATH", "data/openring.db")).parent / "announcements"


def kind_of(data: bytes) -> str:
    """'wav' or 'mp3', judged by the bytes themselves, never by the file's name."""
    if len(data) > 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if data[:3] == b"ID3" or (len(data) > 2 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0):
        return "mp3"
    raise AnnouncementError("a recording is a WAV or MP3 file")


def wav_seconds(data: bytes) -> float | None:
    """How long a WAV file plays, from its header; None when the header can't be read."""
    pos, rate = 12, None
    while pos + 8 <= len(data):
        chunk, size = data[pos:pos + 4], struct.unpack("<I", data[pos + 4:pos + 8])[0]
        if chunk == b"fmt " and size >= 16:
            rate = struct.unpack("<I", data[pos + 16:pos + 20])[0]  # bytes per second
        elif chunk == b"data" and rate:
            return round(min(size, len(data) - pos - 8) / rate, 1)
        pos += 8 + size + (size & 1)
    return None


def save(store: Store, name: str, data: bytes) -> dict[str, Any]:
    if not data:
        raise AnnouncementError("the file is empty")
    if len(data) > MAX_BYTES:
        raise AnnouncementError(f"a recording has at most {MAX_BYTES // 1_000_000} MB")
    kind = kind_of(data)
    seconds = wav_seconds(data) if kind == "wav" else None
    if seconds is not None and seconds > MAX_SECONDS:
        raise AnnouncementError(f"a recording lasts at most {MAX_SECONDS // 60} minutes")
    doc = store.create("announcements", {"name": name[:200] or "recording", "format": kind, "bytes": len(data),
                                         **({"durationSeconds": seconds} if seconds is not None else {})})
    path = path_for(doc)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return doc


def path_for(doc: dict[str, Any]) -> Path:
    return directory() / f"{doc['id']}.{doc['format']}"


def remove(store: Store, doc: dict[str, Any]) -> None:
    path_for(doc).unlink(missing_ok=True)
    store.delete("announcements", doc["id"])


def plan_of(assistant: dict[str, Any] | None) -> dict[str, Any] | None:
    """The assistant's `announcement`, when it plays a recording."""
    found = (assistant or {}).get("announcement")
    return found if isinstance(found, dict) and found.get("audioId") else None


class Links:
    """One-hour links that let the carrier fetch a call's recording without the API key."""

    def __init__(self) -> None:
        self._links: dict[str, tuple[str, float]] = {}

    def make(self, audio_id: str) -> str:
        now = time.monotonic()
        self._links = {k: v for k, v in self._links.items() if v[1] > now}
        token = secrets.token_urlsafe(32)
        self._links[token] = (audio_id, now + LINK_SECONDS)
        return token

    def audio(self, token: str) -> str | None:
        found = self._links.get(token)
        return found[0] if found and found[1] > time.monotonic() else None


def check_escalation(body: dict[str, Any]) -> dict[str, Any]:
    """The parts of an escalation OpenRing checks before it calls anyone. Raises AnnouncementError."""
    people = body.get("customers")
    if not isinstance(people, list) or not 1 <= len(people) <= MAX_PEOPLE:
        raise AnnouncementError(f"customers is required: the on-call list, in order, 1 to {MAX_PEOPLE} people")
    rounds = body.get("rounds", 2)
    if isinstance(rounds, bool) or not isinstance(rounds, int) or not 1 <= rounds <= MAX_ROUNDS:
        raise AnnouncementError(f"rounds is how many times the list is called, 1 to {MAX_ROUNDS}")
    digit = str(body.get("confirmDigit", "1"))
    if digit not in "0123456789*#" or len(digit) != 1:
        raise AnnouncementError("confirmDigit is one key: 0 to 9, * or #")
    return {"rounds": rounds, "confirmDigit": digit}


class Escalations:
    """Runs each escalation: one call at a time down the list, until someone confirms or the rounds run out.

    `dial(escalation, customer)` places a call and returns its record (ended already if it couldn't be placed).
    """

    def __init__(self, store: Store, dial: Callable[[dict[str, Any], dict[str, Any]], Awaitable[dict[str, Any]]],
                 poll_seconds: float = 1.0):
        self.store, self.dial, self.poll = store, dial, poll_seconds
        self._tasks: dict[str, asyncio.Task] = {}

    def start(self, escalation_id: str) -> None:
        self._tasks[escalation_id] = asyncio.ensure_future(self._run(escalation_id))

    def resume(self) -> None:
        """After a restart, an escalation that was calling is not called again blindly: it says it stopped."""
        for doc in self.store.list("escalations", limit=10_000):
            if doc.get("status") == "in-progress":
                self.store.update("escalations", doc["id"], {"status": "ended", "endedReason": "worker-shutdown",
                                                             "endedAt": now_iso()})

    async def close(self) -> None:
        for task in self._tasks.values():
            task.cancel()

    def acknowledged(self, escalation_id: str, call: dict[str, Any]) -> None:
        """Someone pressed the key: the escalation stops calling."""
        self.store.update("escalations", escalation_id, {
            "status": "ended", "endedReason": "acknowledged", "endedAt": now_iso(),
            "acknowledgedBy": (call.get("customer") or {}).get("number"), "acknowledgedCallId": call["id"]})

    def _current(self, escalation_id: str) -> dict[str, Any] | None:
        doc = self.store.get("escalations", escalation_id)
        return doc if doc and doc.get("status") == "in-progress" else None

    async def _run(self, escalation_id: str) -> None:
        try:
            doc = self._current(escalation_id)
            if doc is None:
                return
            attempts: list[dict[str, Any]] = list(doc.get("attempts") or [])
            for round_number in range(1, doc["rounds"] + 1):
                for person in doc["customers"]:
                    if self._current(escalation_id) is None:  # confirmed or cancelled meanwhile
                        return
                    call = await self.dial(doc, person)
                    attempts.append({"round": round_number, "number": person.get("number"), "callId": call["id"]})
                    self.store.update("escalations", escalation_id, {"attempts": attempts})
                    call = await self._until_ended(call["id"])
                    attempts[-1]["endedReason"] = call.get("endedReason")
                    self.store.update("escalations", escalation_id, {"attempts": attempts})
            if self._current(escalation_id) is not None:
                self.store.update("escalations", escalation_id, {"status": "ended", "endedReason": "nobody-confirmed",
                                                                 "endedAt": now_iso()})
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("escalation {} failed", escalation_id)
            self.store.update("escalations", escalation_id, {"status": "ended", "endedReason": "escalation-error",
                                                             "endedAt": now_iso()})
        finally:
            self._tasks.pop(escalation_id, None)

    async def _until_ended(self, call_id: str) -> dict[str, Any]:
        until = time.monotonic() + CALL_WAIT_SECONDS
        while time.monotonic() < until:
            call = self.store.get("calls", call_id) or {"id": call_id, "status": "ended"}
            if call.get("status") == "ended" or call.get("acknowledged"):
                return call
            await asyncio.sleep(self.poll)
        logger.warning("escalation call {} never ended; moving on", call_id)
        return self.store.get("calls", call_id) or {"id": call_id}
