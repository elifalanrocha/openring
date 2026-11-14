"""Twilio helpers: request signature validation, TwiML for media streams, placing calls."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit
from xml.sax.saxutils import escape, quoteattr

from . import netguard

# What Twilio's answering machine detection says (`AnsweredBy`) when no person picked up.
MACHINE = ("machine_start", "machine_end_beep", "machine_end_silence", "machine_end_other", "fax")


class TwilioError(Exception):
    """Twilio refused a request; the message is Twilio's own explanation."""


def machine_detection(assistant: dict[str, Any]) -> dict[str, str]:
    """Vapi's `voicemailDetection` as Twilio's answering machine detection (Calls API parameters). On with a
    `voicemailDetection` object or a `voicemailMessage`, off with "off". Twilio waits for the beep before it asks
    what to play, so a message lands in the recording; a person gets the greeting a few seconds later."""
    detection = assistant.get("voicemailDetection")
    plan = detection if isinstance(detection, dict) else {}
    asked = isinstance(detection, dict) or assistant.get("voicemailMessage")
    if detection == "off" or plan.get("enabled") is False or not asked:
        return {}
    wait = next((plan[k] for k in ("beepMaxAwaitSeconds", "machineDetectionTimeout") if plan.get(k) is not None), 30)
    return {"MachineDetection": "DetectMessageEnd", "MachineDetectionTimeout": str(min(max(round(wait), 3), 59))}


async def create_call(
    account_sid: str, auth_token: str, *, to: str, from_: str, url: str, status_callback: str,
    machine_detection: dict[str, str] | None = None,
) -> str:
    """Start an outbound call. Twilio fetches `url` for instructions once it is answered. Returns the CallSid."""
    async with netguard.http_client(timeout=15) as client:
        r = await client.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls.json",
            auth=(account_sid, auth_token),
            # Twilio requests both URLs with POST by default; trial accounts refuse the Method parameters.
            data={"To": to, "From": from_, "Url": url, "StatusCallback": status_callback, **(machine_detection or {})},
        )
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code >= 400:
        raise TwilioError(f"{body.get('message') or r.text} (Twilio error {body.get('code', r.status_code)})")
    return body["sid"]


async def redirect_call(account_sid: str, auth_token: str, call_sid: str, twiml: str) -> None:
    """Give a live call new instructions; its media stream ends and Twilio follows `twiml`."""
    async with netguard.http_client(timeout=15) as client:
        r = await client.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls/{call_sid}.json",
            auth=(account_sid, auth_token),
            data={"Twiml": twiml},
        )
    if r.status_code >= 400:
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        raise TwilioError(f"{body.get('message') or r.text} (Twilio error {body.get('code', r.status_code)})")


def compute_signature(auth_token: str, url: str, params: dict[str, str]) -> str:
    """Twilio's X-Twilio-Signature: base64(HMAC-SHA1(token, url + sorted key/value pairs))."""
    data = url + "".join(k + params[k] for k in sorted(params))
    digest = hmac.new(auth_token.encode(), data.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def is_valid_signature(auth_token: str, url: str, params: dict[str, str], signature: str) -> bool:
    # Twilio signs some requests with the default port in the URL (":443") and some without, so check
    # both forms, as Twilio's own SDKs do.
    parts = urlsplit(url)
    host = parts.netloc.rsplit(":", 1)[0] if parts.port else parts.netloc
    port = parts.port or (443 if parts.scheme == "https" else 80)
    variants = {urlunsplit(parts._replace(netloc=host)), urlunsplit(parts._replace(netloc=f"{host}:{port}"))}
    return any(hmac.compare_digest(compute_signature(auth_token, u, params), signature or "") for u in variants)


def stream_twiml(ws_url: str, parameters: dict[str, str]) -> str:
    params = "".join(
        f"<Parameter name={quoteattr(k)} value={quoteattr(v)} />" for k, v in parameters.items()
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response><Connect><Stream url={quoteattr(ws_url)}>{params}</Stream></Connect></Response>"
    )


def dial_twiml(number: str) -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?><Response><Dial>{escape(number)}</Dial></Response>'


def is_recording(message: str) -> bool:
    """Whether a `voicemailMessage` is a recording to play, not a sentence that happens to start with a link."""
    return bool(re.fullmatch(r"https?://\S+", message) or re.match(r"https?://\S+\.(wav|mp3)\b", message, re.I))


def play_twiml(url: str) -> str:
    # A {{variable}} in the URL may hold spaces and other characters a URL can't carry.
    url = quote(url, safe="/:?&=#%+,;@!$'()*[]~")
    return f'<?xml version="1.0" encoding="UTF-8"?><Response><Play>{escape(url)}</Play><Hangup/></Response>'


def confirm_twiml(play_url: str, action_url: str, digit: str, prompt: str) -> str:
    """Play a recording and ask for a key, twice; the key goes to `action_url`. Silence ends the call."""
    play = f"<Play>{escape(play_url)}</Play><Say>{escape(prompt)}</Say>"
    gather = (f'<Gather numDigits="1" timeout="6" action={quoteattr(action_url)} method="POST" '
              f'finishOnKey="">{play}</Gather>')
    return f'<?xml version="1.0" encoding="UTF-8"?><Response>{gather}{gather}<Hangup/></Response>'


def hangup_twiml() -> str:
    return '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'


def say_twiml(text: str) -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?><Response><Say>{escape(text)}</Say><Hangup/></Response>'
