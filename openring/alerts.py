"""Alerts (OpenRing's own): tell your server when a call fails, so nobody has to watch the dashboard.

One setting, `alertWebhook`: a URL. When a call ends for a reason that means OpenRing or a provider failed (a rejected
key, no credit, a line that wouldn't connect), a small message of OpenRing's own goes to that URL:

    {"type": "call-failed", "call": {"id", "assistantId", "endedReason", "endedAt", "durationSeconds"}, "at": "..."}

A watcher looks at ended calls every `INTERVAL` seconds, so a restart never repeats an alert (it remembers the last
call it told about) and the call path is never touched. The address is from outside, so it goes through `netguard`;
nothing is retried; the caller's number, the transcript and the keys are not in the message.
"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from . import netguard
from .stats import _failed
from .store import Store

KEY = "alert_webhook"
SEEN_KEY = "alert_seen_at"
INTERVAL = 30.0
TIMEOUT = 10.0
MAX_PER_ROUND = 20  # a burst of failures is told as the first few: the dashboard has the rest


class AlertError(ValueError):
    """The alert settings are wrong."""


def webhook(store: Store) -> str | None:
    return store.get_setting(KEY) or None


async def check_url(url: Any) -> str | None:
    """The address, checked; None clears the alert."""
    if url in (None, ""):
        return None
    text = str(url).strip()
    if not text.startswith(("https://", "http://")) or len(text) > 500:
        raise AlertError("alertWebhook is an address starting with https:// (or http://)")
    try:
        await netguard.check_url_async(text)
    except netguard.UnsafeURL as exc:
        raise AlertError("that address is refused: it points to a cloud metadata or link-local address") from exc
    return text


def message(call: dict[str, Any]) -> dict[str, Any]:
    shown = ("id", "assistantId", "type", "endedReason", "endedAt", "durationSeconds")
    return {"type": "call-failed", "at": call.get("endedAt"), "call": {k: call.get(k) for k in shown}}


def new_failures(store: Store) -> list[dict[str, Any]]:
    """Failed calls that ended since the last alert, oldest first."""
    seen = store.get_setting(SEEN_KEY) or ""
    found = [c for c in store.list("calls", limit=2000)
             if c.get("status") == "ended" and _failed(c) and str(c.get("endedAt") or "") > seen]
    return sorted(found, key=lambda c: str(c.get("endedAt")))


async def tell(store: Store, client: Any = None) -> int:
    """Send the alerts that are due; returns how many went."""
    url = webhook(store)
    due = new_failures(store)
    if not due:
        return 0
    if not url:  # nobody to tell: remember, so turning it on later doesn't flood
        store.set_setting(SEEN_KEY, str(due[-1].get("endedAt")))
        return 0
    sent = 0
    owned = client is None
    client = client or netguard.client()
    try:
        for call in due[:MAX_PER_ROUND]:
            try:
                await netguard.check_url_async(url)
                reply = await client.post(url, json=message(call), timeout=TIMEOUT)
                logger.info("alert for call {}: {}", call["id"], reply.status_code)
                sent += 1
            except Exception as exc:  # the address down must not stop the watcher
                logger.warning("alert for call {} not delivered ({})", call["id"], exc.__class__.__name__)
        store.set_setting(SEEN_KEY, str(due[-1].get("endedAt")))
    finally:
        if owned:
            await client.aclose()
    return sent


async def watch(store: Store) -> None:
    """Runs for the life of the server."""
    while True:
        try:
            await tell(store)
        except Exception:
            logger.exception("the alert watcher failed")
        await asyncio.sleep(INTERVAL)


async def test(store: Store, client: Any = None) -> bool:
    """Send a harmless test message to the saved address; False when none is saved."""
    from datetime import datetime, timezone

    url = webhook(store)
    if not url:
        return False
    owned = client is None
    client = client or netguard.client()
    try:
        await netguard.check_url_async(url)
        body = {"type": "test", "at": datetime.now(timezone.utc).isoformat()}
        reply = await client.post(url, json=body, timeout=TIMEOUT)
    except Exception as exc:
        raise AlertError(f"the address did not take the test ({exc.__class__.__name__})") from exc
    finally:
        if owned:
            await client.aclose()
    if reply.status_code >= 400:
        raise AlertError(f"the address answered {reply.status_code}")
    return True
