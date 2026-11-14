"""A real call, end to end, on this computer: OpenRing with your provider keys, and a caller with a synthesized voice.

    python scripts/smoke_call.py

It starts an OpenRing of its own (port 8791, a throwaway database) with the provider keys in ~/.openring/.env, and calls
it over a simulated Twilio line: the caller's voice is Kokoro reading sentences, sent as 8 kHz mu-law every 20 ms, the
way Twilio carries a phone call. Everything else is real: the assistant's transcriber (Deepgram), model (Groq) and
voice (Kokoro, or --voice), the pipeline and the messages to the assistant's server. It checks that:

- the caller's words reach the transcript, the model answers, and the answer comes back as audio;
- a caller that says it is an AI agent is marked;
- keys pressed on the keypad reach the model;
- the assistant's server gets speech-update, transcript and end-of-call-report, and the call's webhook log lists them;
- a second call from the same number is given the first one (memory per caller);
- the latency of each turn and the cost are recorded.

It needs DEEPGRAM_API_KEY and GROQ_API_KEY, costs a few cents of Deepgram credit and calls nobody.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import warnings
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
KEY = "smoke-test-key"
CALLER = "+15550000042"
SILENCE = b"\xff" * 160  # 20 ms of silence, 8 kHz mu-law
NEEDED = ("DEEPGRAM_API_KEY", "GROQ_API_KEY")
SAID = "Hi, I'm an AI assistant calling on behalf of Maria. Do you have a table for two tonight at eight?"
PROMPT = ("You are the host of Luna, a small restaurant. Answer in one short sentence. "
          "Tonight there is a table for two at eight. "
          "When the caller types digits on the keypad, repeat the digits back one by one.")


def load_keys() -> None:
    """The provider keys of ~/.openring/.env, for a run of this script only; never printed."""
    path = Path.home() / ".openring" / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        name, _, value = line.partition("=")
        name, value = name.strip(), value.strip().strip('"').strip("'")
        if name.endswith("_API_KEY") and name != "OPENRING_API_KEY" and value and not os.environ.get(name):
            os.environ[name] = value


def serve(port: int) -> None:
    """The server side: a real OpenRing with a throwaway database; prints READY once the Kokoro voice is loaded."""
    load_keys()
    import uvicorn

    from openring import providers
    from openring.api import create_app
    from openring.config import Settings
    from openring.store import Store

    providers.warm_up()
    while not providers.kokoro_ready():
        time.sleep(0.5)
    db = Path(tempfile.mkdtemp()) / "smoke.db"
    settings = Settings(api_key=KEY, public_url=f"http://127.0.0.1:{port}", db_path=str(db),
                        validate_twilio_signature=False)
    app = create_app(settings, Store(str(db)))
    print("READY", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


# ---- the caller's side ------------------------------------------------------------------------------------------

HOOKS: list[dict] = []  # what the assistant's server received


class Hooks(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 (http.server's name)
        HOOKS.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))).get("message") or {})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *_):
        pass


def hooks_of(call_id: str, kind: str | None = None, role: str | None = None) -> list[dict]:
    return [m for m in HOOKS if (m.get("call") or {}).get("id") == call_id and (kind is None or m.get("type") == kind)
            and (role is None or m.get("role") == role)]


def request(port: int, path: str, data: dict | None = None, form: dict | None = None, method: str | None = None):
    body = json.dumps(data).encode() if data is not None else urllib.parse.urlencode(form).encode() if form else None
    headers = {"Authorization": f"Bearer {KEY}", **({"Content-Type": "application/json"} if data is not None else {})}
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", body, headers, method=method)
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read()
    return json.loads(raw) if raw[:1] in (b"{", b"[") else raw


def voice(text: str) -> bytes:
    """`text` read by Kokoro, as a phone line carries it: 8 kHz mu-law."""
    import numpy as np
    import soxr
    from kokoro_onnx import Kokoro

    from openring import providers

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        try:
            import audioop
        except ImportError:  # Python 3.13: audioop-lts
            import audioop  # type: ignore[no-redef]
    model, voices = (str(providers.KOKORO_DIR / name) for name in providers.KOKORO_FILES)
    samples, rate = Kokoro(model, voices).create(text, voice="am_adam", speed=1.0, lang="en-us")
    pcm = (np.clip(soxr.resample(samples, rate, 8000), -1, 1) * 32767).astype("<i2").tobytes()
    return audioop.lin2ulaw(pcm, 2)


class Line:
    """One simulated Twilio call: a frame every 20 ms, the caller's words or silence, and the assistant's audio back."""

    def __init__(self, ws, sid: str):
        self.ws, self.sid = ws, sid
        self.heard_bytes, self.last_heard = 0, 0.0

    async def listen(self) -> None:
        async for raw in self.ws:
            message = json.loads(raw)
            if message.get("event") == "media":
                self.heard_bytes += len(base64.b64decode(message["media"]["payload"]))
                self.last_heard = time.monotonic()

    async def _frame(self, chunk: bytes, due: float) -> float:
        payload = base64.b64encode(chunk.ljust(160, b"\xff")).decode()
        await self.ws.send(json.dumps({"event": "media", "streamSid": self.sid, "media": {"payload": payload}}))
        due += 0.02
        await asyncio.sleep(max(0.0, due - time.monotonic()))
        return due

    async def say(self, audio: bytes) -> None:
        due = time.monotonic()
        for i in range(0, len(audio), 160):
            due = await self._frame(audio[i:i + 160], due)

    async def quiet_until(self, done, limit: float) -> bool:
        """Silence until `done()`, the assistant's answer has played and it has been quiet for a second, or `limit`
        seconds. A transcript arrives before its audio, so the audio decides when the caller may speak again."""
        due, until, before = time.monotonic(), time.monotonic() + limit, self.heard_bytes
        while time.monotonic() < until:
            if done() and self.heard_bytes > before and time.monotonic() - self.last_heard > 1.0:
                return True
            due = await self._frame(SILENCE, due)
        return done()

    async def press(self, keys: str) -> None:
        due = time.monotonic()
        for key in keys:
            await self.ws.send(json.dumps({"event": "dtmf", "streamSid": self.sid,
                                           "dtmf": {"track": "inbound_track", "digit": key}}))
            for _ in range(15):  # 0.3 s between keys, the line still carrying silence
                due = await self._frame(SILENCE, due)


async def call(port: int, script) -> str:
    """Place one call through the simulated line, run `script(line, call_id)`, hang up. Returns the call id."""
    from websockets.asyncio.client import connect
    from websockets.exceptions import ConnectionClosed

    twiml = request(port, "/twilio/voice", form={"To": "+15551234567", "From": CALLER, "CallSid": f"CA{time.time()}"})
    stream = ET.fromstring(twiml).find("./Connect/Stream")
    params = {p.get("name"): p.get("value") for p in stream.findall("Parameter")}
    sid = f"MZ{int(time.time() * 1000)}"
    async with connect(f"ws://127.0.0.1:{port}/ws/twilio", max_size=2**22) as ws:
        await ws.send(json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"}))
        await ws.send(json.dumps({"event": "start", "streamSid": sid, "start": {
            "streamSid": sid, "callSid": params["callId"], "accountSid": "AC1", "customParameters": params,
            "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1}}}))
        line = Line(ws, sid)
        listening = asyncio.create_task(line.listen())
        try:
            await script(line, params["callId"])
            await ws.send(json.dumps({"event": "stop", "streamSid": sid}))
        except ConnectionClosed:  # the server hung up: the call's record says why
            ended = await asyncio.to_thread(request, port, f"/call/{params['callId']}")
            print(f"  The server ended the call early: {ended.get('endedReason')}", flush=True)
        finally:
            await asyncio.sleep(0.5)
            listening.cancel()
    return params["callId"]


async def until_ended(port: int, call_id: str, seconds: float = 30) -> dict:
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        found = await asyncio.to_thread(request, port, f"/call/{call_id}")
        if found.get("status") == "ended":
            return found
        await asyncio.sleep(0.5)
    return found


async def run(port: int, hook_url: str, voice_of_assistant: dict) -> list[tuple[bool, str]]:
    results: list[tuple[bool, str]] = []

    def check(ok: bool, what: str) -> None:
        results.append((bool(ok), what))
        print(("PASS  " if ok else "FAIL  ") + what, flush=True)

    assistant = await asyncio.to_thread(request, port, "/assistant", {
        "name": "Smoke test", "firstMessage": "Good evening, Luna restaurant.",
        "transcriber": {"provider": "deepgram", "model": "nova-3", "language": "en"},
        "model": {"provider": "groq", "model": "openai/gpt-oss-20b",
                  "messages": [{"role": "system", "content": PROMPT}]},
        "voice": voice_of_assistant,
        "server": {"url": hook_url}, "callerMemory": {"enabled": True},
        "keypadInputPlan": {"enabled": True, "timeoutSeconds": 2, "delimiters": ["#"]},
        "serverMessages": ["status-update", "speech-update", "transcript", "end-of-call-report"]})
    await asyncio.to_thread(request, port, "/phone-number", {"number": "+15551234567", "assistantId": assistant["id"]})
    print("Synthesizing the caller's voice with Kokoro...", flush=True)
    sentence = await asyncio.to_thread(voice, SAID)
    print(f"Calling: the caller says {len(sentence) / 8000:.1f} s of speech, then presses 4 2 #.", flush=True)

    async def first(line: Line, call_id: str) -> None:
        answers = lambda: len(hooks_of(call_id, "transcript", "assistant"))  # noqa: E731
        await line.quiet_until(lambda: answers() >= 1, 20)  # the greeting
        before = answers()
        await line.say(sentence)
        await line.quiet_until(lambda: answers() > before, 25)  # the answer
        before = answers()
        await line.press("42#")
        await line.quiet_until(lambda: answers() > before, 25)

    started = time.monotonic()
    first_id = await call(port, first)
    one = await until_ended(port, first_id)
    said = [m["message"] for m in one.get("messages") or [] if m["role"] == "user"]
    answered = [m["message"] for m in one.get("messages") or [] if m["role"] == "assistant"]
    print(f"\n  Caller (as transcribed): {said}\n  Assistant: {answered}\n", flush=True)
    check(any("table for two" in s.lower() for s in said) and any("maria" in s.lower() for s in said),
          "both of the caller's sentences reached the transcript")
    check(len(answered) >= 3, f"the assistant greeted and answered twice ({len(answered)} answers)")
    check(one.get("callerIsAI") is True, f"the AI caller was noticed: {one.get('callerIsAISaid')!r}")
    check(any(s.strip() == "42" for s in said), "the keys 4 2 # reached the model as '42'")
    # The model was asked to read the digits back; whether it does is the model's business, that it answers is ours.
    order = [(m["role"], m["message"].strip()) for m in one.get("messages") or [] if m["role"] in ("user", "assistant")]
    keys_at = next((i for i, m in enumerate(order) if m == ("user", "42")), None)
    check(keys_at is not None and any(role == "assistant" for role, _ in order[keys_at + 1:]),
          "the model answered the keys")
    turns = ((one.get("artifact") or {}).get("performanceMetrics") or {}).get("turnLatencies") or []
    check(bool(turns), f"turn latency recorded: {turns}")
    cost = one.get("costBreakdown") or {}
    check((cost.get("stt") or 0) > 0 and (cost.get("llm") or 0) > 0, f"cost recorded: US$ {one.get('cost')}")
    kinds = {m.get("type") for m in hooks_of(first_id)}
    check({"speech-update", "transcript", "status-update"} <= kinds, f"live server messages arrived: {sorted(kinds)}")
    await asyncio.sleep(2)
    check(bool(hooks_of(first_id, "end-of-call-report")), "the end-of-call report arrived")
    log = await asyncio.to_thread(request, port, f"/call/{first_id}/webhooks")
    check(log and all(d.get("status") == 200 for d in log), f"webhook log: {len(log)} deliveries, all HTTP 200")

    async def second(line: Line, call_id: str) -> None:
        await line.quiet_until(lambda: len(hooks_of(call_id, "transcript", "assistant")) >= 1, 15)

    second_id = await call(port, second)
    two = await until_ended(port, second_id)
    check(two.get("rememberedCallIds") == [first_id], "the second call from the number was given the first one")
    print(f"\n  {time.monotonic() - started:.0f} s for both calls.", flush=True)
    return results


async def main_async(port: int, voice_of_assistant: dict) -> int:
    hooks = ThreadingHTTPServer(("127.0.0.1", 0), Hooks)
    threading.Thread(target=hooks.serve_forever, daemon=True).start()
    server = subprocess.Popen([sys.executable, __file__, "--serve", str(port)], stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, text=True)
    try:
        ready = await asyncio.to_thread(server.stdout.readline)
        if "READY" not in ready:
            print("The OpenRing for the test didn't start.")
            return 1
        for _ in range(60):
            try:
                await asyncio.to_thread(request, port, "/health")
                break
            except OSError:
                await asyncio.sleep(0.5)
        results = await run(port, f"http://127.0.0.1:{hooks.server_port}/hook", voice_of_assistant)
        failed = [what for ok, what in results if not ok]
        print(f"\n{len(results) - len(failed)} of {len(results)} checks passed.")
        return 1 if failed else 0
    finally:
        server.terminate()
        hooks.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8791)
    parser.add_argument("--voice", default="kokoro:af_heart",
                        help="the assistant's voice as provider:voiceId[:model], e.g. "
                             "11labs:hpp4J3VqNfWAUOO0d1Us:eleven_flash_v2_5 (default kokoro:af_heart)")
    parser.add_argument("--serve", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.serve:
        serve(args.serve)
        return
    load_keys()
    missing = [k for k in NEEDED if not os.environ.get(k)]
    if missing:
        print(f"Set {' and '.join(missing)} (in ~/.openring/.env or the environment) to run a real call.")
        raise SystemExit(2)
    provider, _, rest = args.voice.partition(":")
    voice_id, _, model = rest.partition(":")
    assistant_voice = {"provider": provider, "voiceId": voice_id, **({"model": model} if model else {})}
    print(f"The assistant speaks with {args.voice}.", flush=True)
    raise SystemExit(asyncio.run(main_async(args.port, assistant_voice)))


if __name__ == "__main__":
    main()
