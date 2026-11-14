"""Browser test calls: a real WebRTC connection on this computer, opened the way the dashboard opens it.

The providers are stubs that stay off the network, so the assistant never hears or answers anything; the
test checks the connection, the call record and that hanging up ends the call.
"""

import asyncio
import time

import httpx
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import AudioStreamTrack

from openring.api import create_app
from openring.config import Settings
from openring.store import Store

from test_api import ASSISTANT, AUTH, make_client


async def _until(check, timeout=10):
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.1)


def test_web_call_needs_auth_assistant_offer_and_keys(monkeypatch):
    client = make_client()
    assert client.post("/call/web", json={}).status_code == 401
    assert client.post("/call/web", json={"assistantId": "nope", "sdp": "x"}, headers=AUTH).status_code == 404
    a = client.post("/assistant", json=ASSISTANT, headers=AUTH).json()
    assert client.post("/call/web", json={"assistantId": a["id"]}, headers=AUTH).status_code == 400
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    r = client.post("/call/web", json={"assistantId": a["id"], "sdp": "x"}, headers=AUTH)
    assert r.status_code == 400 and "DEEPGRAM_API_KEY" in r.json()["detail"]


def test_web_call_connects_and_ends_when_the_page_hangs_up(monkeypatch, quiet_providers):
    for k in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.setenv(k, "fake")
    store = Store(":memory:")
    app = create_app(Settings(api_key="test-key", public_url="", db_path=":memory:"), store)

    async def scenario():
        page = RTCPeerConnection()
        page.addTrack(AudioStreamTrack())  # silence, like a muted microphone
        await page.setLocalDescription(await page.createOffer())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
            a = (await http.post("/assistant", json=ASSISTANT, headers=AUTH)).json()
            offer = {"assistantId": a["id"], "sdp": page.localDescription.sdp, "type": page.localDescription.type}
            r = await http.post("/call/web", json=offer, headers=AUTH)
            assert r.status_code == 201, r.text
            call_id = r.json()["id"]
            await page.setRemoteDescription(RTCSessionDescription(**r.json()["answer"]))
            await _until(lambda: page.connectionState == "connected")
            assert store.get("calls", call_id)["status"] == "in-progress"
            await asyncio.sleep(1)  # let the greeting start
            await page.close()
            await _until(lambda: store.get("calls", call_id)["status"] == "ended", timeout=15)
        return store.get("calls", call_id)

    call = asyncio.run(asyncio.wait_for(scenario(), 60))
    assert call["type"] == "webCall" and call["endedReason"] == "customer-ended-call"
    assert call["messages"][0] == {"role": "assistant", "message": ASSISTANT["firstMessage"]}


def test_a_browser_call_hears_the_background_sound_in_real_time(monkeypatch, quiet_providers):
    """WebRTC queues whatever audio comes: the office sound must arrive at the pace it plays, not flood the track."""
    import numpy as np

    for k in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.setenv(k, "fake")
    store = Store(":memory:")
    app = create_app(Settings(api_key="test-key", public_url="", db_path=":memory:"), store)

    async def scenario():
        page = RTCPeerConnection()
        page.addTrack(AudioStreamTrack())
        tracks = []
        page.on("track", tracks.append)
        await page.setLocalDescription(await page.createOffer())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
            a = (await http.post("/assistant", json={**ASSISTANT, "backgroundSound": "office"}, headers=AUTH)).json()
            offer = {"assistantId": a["id"], "sdp": page.localDescription.sdp, "type": page.localDescription.type}
            r = await http.post("/call/web", json=offer, headers=AUTH)
            call_id = r.json()["id"]
            await page.setRemoteDescription(RTCSessionDescription(**r.json()["answer"]))
            await _until(lambda: page.connectionState == "connected" and tracks)
            loudest, started, heard = 0.0, None, 0.0
            while heard < 3.0:
                frame = await asyncio.wait_for(tracks[0].recv(), 5)
                samples = frame.to_ndarray().astype(np.float64)
                loudest = max(loudest, float(np.sqrt(np.mean(samples ** 2))))
                heard += frame.samples / frame.sample_rate
                started = started or time.monotonic()
            took = time.monotonic() - started
            await page.close()
            await _until(lambda: store.get("calls", call_id)["status"] == "ended", timeout=15)
        return loudest, heard, took, store.get("calls", call_id)

    loudest, heard, took, call = asyncio.run(asyncio.wait_for(scenario(), 90))
    assert loudest > 50  # the providers said nothing; the office did
    assert took > heard * 0.8  # played as it plays, not a burst
    assert call["endedReason"] == "customer-ended-call"
