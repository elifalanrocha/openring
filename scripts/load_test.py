"""How many calls at once a computer takes: OpenRing's own share of a call, measured.

    python scripts/load_test.py                 # 1, 5, 10, 20, 40 and 80 calls, 20 s each
    python scripts/load_test.py --levels 10 30 --seconds 30

It starts an OpenRing in a subprocess whose transcriber, model and voice are stand-ins that answer at once: what
remains is OpenRing's own work on a call (the Twilio media stream, audio conversion, the Silero voice activity
detector, the turn detector, the pipeline). It then opens that many Twilio media streams at once, each sending 20 ms
of audio every 20 ms, and reports the server's CPU, memory and how late it answers /health.

Real providers add network waits, not CPU here (a local voice such as Kokoro adds a lot of CPU: count it apart).
Nothing leaves this computer. The numbers belong to the computer they ran on: say which when you quote them.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KEY = "load-test-key"
FRAME = base64.b64encode(b"\xff" * 160).decode()  # 20 ms of silence, 8 kHz mu-law


def memory_mb() -> float:
    """This process's resident memory, without psutil."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                                                     "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                                                     "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]
        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        current = ctypes.windll.kernel32.GetCurrentProcess
        current.restype = wintypes.HANDLE
        info = ctypes.windll.psapi.GetProcessMemoryInfo
        info.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        info(current(), ctypes.byref(counters), counters.cb)
        return counters.WorkingSetSize / 1e6
    with open("/proc/self/statm") as f:
        return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1e6


def serve(port: int) -> None:
    """The server side: OpenRing with stand-in providers, printing its CPU and memory once a second."""
    sys.path.insert(0, str(ROOT))
    import uvicorn
    from pipecat.processors.frame_processor import FrameProcessor

    from openring import bot
    from openring.api import create_app
    from openring.config import Settings
    from openring.store import Store

    class Standin(FrameProcessor):
        def __init__(self, layer: str):
            super().__init__()
            self.openring_label, self.openring_price = layer, (layer, "none", None, None)

        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            await self.push_frame(frame, direction)

    bot.build_services = lambda plan: (Standin("transcriber"), Standin("model"), Standin("voice"))
    for k in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "ELEVENLABS_API_KEY"):
        os.environ[k] = "stand-in"
    db = Path(tempfile.mkdtemp()) / "load.db"
    settings = Settings(api_key=KEY, public_url=f"http://127.0.0.1:{port}", db_path=str(db),
                        validate_twilio_signature=False)

    def sample() -> None:
        last_cpu, last_wall = time.process_time(), time.monotonic()
        while True:
            time.sleep(1)
            cpu, wall = time.process_time(), time.monotonic()
            print(json.dumps({"cpu": (cpu - last_cpu) / (wall - last_wall), "mb": memory_mb()}), flush=True)
            last_cpu, last_wall = cpu, wall

    threading.Thread(target=sample, daemon=True).start()
    uvicorn.run(create_app(settings, Store(str(db))), host="127.0.0.1", port=port, log_level="error")


def request(port: int, path: str, data: dict | None = None, form: dict | None = None) -> bytes:
    body = json.dumps(data).encode() if data is not None else urllib.parse.urlencode(form).encode() if form else None
    headers = {"Authorization": f"Bearer {KEY}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", body, headers)
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read()


def ring(port: int, n: int) -> dict[str, str]:
    """An incoming call, as Twilio announces it: the parameters its media stream must send."""
    twiml = request(port, "/twilio/voice", form={"To": "+15551234567", "From": f"+1555000{n:04d}", "CallSid": f"CA{n}"})
    stream = ET.fromstring(twiml).find("./Connect/Stream")
    return {p.get("name"): p.get("value") for p in stream.findall("Parameter")}


async def call(port: int, n: int, seconds: float, late: list[float]) -> bool:
    """One Twilio media stream: a frame every 20 ms for `seconds`. True when the server kept up."""
    from websockets.asyncio.client import connect

    params = await asyncio.to_thread(ring, port, n)
    sid = f"MZ{n}"
    async with connect(f"ws://127.0.0.1:{port}/ws/twilio", max_size=2**22) as ws:
        await ws.send(json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"}))
        await ws.send(json.dumps({"event": "start", "streamSid": sid, "start": {
            "streamSid": sid, "callSid": f"CA{n}", "accountSid": "AC1", "customParameters": params,
            "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1}}}))
        due = time.monotonic()
        for _ in range(int(seconds / 0.02)):
            await ws.send(json.dumps({"event": "media", "streamSid": sid, "media": {"payload": FRAME}}))
            due += 0.02
            wait = due - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            else:
                late.append(-wait)
        await ws.send(json.dumps({"event": "stop", "streamSid": sid}))
    return True


async def level(port: int, calls: int, seconds: float, samples: list[dict]) -> dict:
    late: list[float] = []
    health: list[float] = []
    stop = asyncio.Event()

    async def watch() -> None:
        while not stop.is_set():
            started = time.monotonic()
            await asyncio.to_thread(request, port, "/health")
            health.append((time.monotonic() - started) * 1000)
            await asyncio.sleep(0.2)

    first = len(samples)
    watcher = asyncio.create_task(watch())
    results = await asyncio.gather(*(call(port, i, seconds, late) for i in range(calls)), return_exceptions=True)
    stop.set()
    await watcher
    window = samples[first + 2:] or samples[first:]  # leave out the first seconds: calls are still starting
    for _ in range(40):  # the server ends each call once its stream stops
        recent = json.loads(await asyncio.to_thread(request, port, f"/call?limit={calls}"))
        if all(c.get("status") == "ended" for c in recent):
            break
        await asyncio.sleep(0.25)
    clean = sum(c.get("status") == "ended" and c.get("endedReason") == "customer-ended-call" for c in recent)
    reasons = {}
    for c in recent:
        reasons[c.get("endedReason") or c.get("status")] = reasons.get(c.get("endedReason") or c.get("status"), 0) + 1
    errors = [repr(r)[:120] for r in results if r is not True][:3]
    return {"reasons": reasons, "errors": errors, "calls": calls, "ok": min(clean, sum(r is True for r in results)),
            "cpu_cores": round(statistics.mean(s["cpu"] for s in window), 2) if window else None,
            "memory_mb": round(max(s["mb"] for s in window)) if window else None,
            "health_p50_ms": round(statistics.median(health)), "health_p95_ms": round(
                statistics.quantiles(health, n=20)[-1]) if len(health) > 2 else None,
            "client_late_frames": len(late)}


async def main_async(args: argparse.Namespace) -> None:
    port = args.port
    server = subprocess.Popen([sys.executable, __file__, "--serve", str(port)], stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, text=True)
    samples: list[dict] = []
    threading.Thread(target=lambda: [samples.append(json.loads(line)) for line in server.stdout], daemon=True).start()
    try:
        for _ in range(120):
            try:
                await asyncio.to_thread(request, port, "/health")
                break
            except OSError:
                await asyncio.sleep(0.5)
        a = json.loads(request(port, "/assistant", {"name": "Load", "firstMessage": "Hello",
                                                    "transcriber": {"provider": "deepgram", "language": "en"},
                                                    "model": {"provider": "groq", "model": "openai/gpt-oss-20b"},
                                                    "voice": {"provider": "11labs", "voiceId": "x"}}))
        request(port, "/phone-number", {"number": "+15551234567", "assistantId": a["id"]})
        await level(port, 1, 6, samples)  # warm up: the first call imports Pipecat and loads the models
        print(f"OpenRing load test · {platform.processor() or platform.machine()} · {os.cpu_count()} CPUs · "
              f"Python {platform.python_version()} · {args.seconds:g} s per level")
        print(f"{'calls':>6} {'ok':>4} {'CPU cores':>10} {'memory MB':>10} {'/health p50':>12} {'p95 ms':>7}")
        for calls in args.levels:
            r = await level(port, calls, args.seconds, samples)
            print(f"{r['calls']:>6} {r['ok']:>4} {r['cpu_cores']:>10} {r['memory_mb']:>10} "
                  f"{r['health_p50_ms']:>12} {r['health_p95_ms']:>7}", flush=True)
            if r["ok"] < calls or (r["health_p95_ms"] or 0) > 1000:
                print(f"  stopped: the server is no longer keeping up. Endings: {r['reasons']}")
                for error in r["errors"]:
                    print(f"  client: {error}")
                break
    finally:
        server.terminate()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--levels", type=int, nargs="+", default=[1, 5, 10, 20, 40, 80])
    parser.add_argument("--seconds", type=float, default=20)
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--serve", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.serve:
        serve(args.serve)
        return
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
