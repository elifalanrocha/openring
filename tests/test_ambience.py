"""Background sound under the assistant's voice (ambience.py): Vapi's `backgroundSound`, "off", "office" or a URL."""

import asyncio
import base64
import io
import json
import time

import httpx
import numpy as np
import pytest

from openring import ambience, netguard
from openring.providers import resolve_plan

from test_api import ASSISTANT, AUTH
from test_call_flow import _handshake, _stream_params, _wait_until_ended


def wav(seconds=1.0, rate=48000, channels=2, level=0.5, tone=440.0):
    import soundfile as sf

    t = np.arange(int(seconds * rate)) / rate
    audio = np.stack([np.sin(2 * np.pi * tone * t) * level] * channels, axis=1)
    out = io.BytesIO()
    sf.write(out, audio, rate, format="WAV")
    return out.getvalue()


def dbfs(samples):
    return 20 * np.log10(np.sqrt(np.mean(np.square(samples.astype(np.float64) / 32768))))


def test_background_sound_is_off_office_or_a_url():
    assert ambience.source_of(None) == (None, None)
    assert ambience.source_of("off") == (None, None)
    assert ambience.source_of("office") == ("office", None)
    assert ambience.source_of("https://x.test/rain.mp3") == ("https://x.test/rain.mp3", None)
    for bad in ("cafe", 3, {"url": "https://x.test/a.wav"}, "ftp://x.test/a.wav"):
        source, warning = ambience.source_of(bad)
        assert source is None and "off, office or the URL" in warning


def test_the_plan_carries_the_sound_and_says_when_it_was_not_understood():
    assert resolve_plan({**ASSISTANT, "backgroundSound": "office"}, check_keys=False).background_sound == "office"
    plan = resolve_plan({**ASSISTANT, "backgroundSound": "jungle"}, check_keys=False)
    assert plan.background_sound is None and any("backgroundSound" in w for w in plan.warnings)
    assert resolve_plan(ASSISTANT, check_keys=False).background_sound is None  # off unless asked (Vapi: office)


def test_a_file_becomes_quiet_mono_audio_at_the_lines_rate_that_loops_without_a_click():
    sound = ambience.decode(wav(seconds=1.0, rate=48000, channels=2, level=0.9), 8000)
    assert sound.dtype == np.int16 and 7000 < len(sound) < 8000  # 1 s at 8 kHz, less the 0.1 s blended into the start
    assert abs(dbfs(sound) - ambience.LEVEL_DBFS) < 0.5  # as loud as the office sound, however loud the file
    jump = abs(int(sound[-1]) - int(sound[0]))
    steps = np.abs(np.diff(sound.astype(np.int32)))
    assert jump <= steps.max() * 1.5  # from the end back to the start is a step like any other


def test_a_long_file_is_cut_and_a_bad_one_refused(monkeypatch):
    monkeypatch.setattr(ambience, "MAX_SECONDS", 2)
    assert len(ambience.decode(wav(seconds=5, rate=8000, channels=1), 8000)) <= 2 * 8000
    with pytest.raises(ambience.SoundError, match="isn't an audio file"):
        ambience.decode(b"<html>not a sound</html>", 8000)
    with pytest.raises(ambience.SoundError, match="too short"):
        ambience.decode(wav(seconds=0.01, rate=8000, channels=1), 8000)


def test_the_office_sound_ships_and_is_quiet():
    sound = asyncio.run(ambience.load("office", 16000))
    assert len(sound) == 30 * 16000
    assert -44 < dbfs(sound) < -38  # under a voice, not over it


def guarded(handler):
    return lambda **options: httpx.AsyncClient(transport=netguard.GuardedTransport(httpx.MockTransport(handler)),
                                               **{k: v for k, v in options.items() if k != "transport"})


def test_a_sound_from_a_url_is_fetched_within_limits(monkeypatch):
    data = wav(seconds=0.5, rate=8000, channels=1)
    monkeypatch.setattr(netguard, "client", guarded(lambda r: httpx.Response(200, content=data)))
    assert asyncio.run(ambience.fetch("https://sounds.test/rain.wav")) == data
    monkeypatch.setattr(ambience, "MAX_BYTES", 1000)
    with pytest.raises(ambience.SoundError, match="over"):
        asyncio.run(ambience.fetch("https://sounds.test/rain.wav"))
    monkeypatch.setattr(netguard, "client", guarded(lambda r: httpx.Response(404)))
    with pytest.raises(ambience.SoundError, match="HTTP 404"):
        asyncio.run(ambience.fetch("https://sounds.test/rain.wav"))


def test_a_sound_url_never_reaches_cloud_metadata(monkeypatch):
    asked = []
    monkeypatch.setattr(netguard, "client", guarded(lambda r: asked.append(r) or httpx.Response(200)))
    with pytest.raises(ambience.SoundError, match="OpenRing never calls"):
        asyncio.run(ambience.fetch("http://169.254.169.254/latest/meta-data/"))
    assert asked == []


def run_mixer(source, frames, *, sample_rate=8000, frame_samples=160, voice=0):
    async def scenario():
        mixer = ambience.mixer(source)
        await mixer.start(sample_rate)
        await mixer._loading
        audio = (np.full(frame_samples, voice, dtype=np.int16)).tobytes()
        out = [np.frombuffer(await mixer.mix(audio), dtype=np.int16) for _ in range(frames)]
        await mixer.stop()
        return mixer, out
    return asyncio.run(scenario())


def test_the_mixer_fades_the_sound_in_under_the_voice_and_loops_it(monkeypatch):
    sound = (np.arange(400) % 50 * 100 + 1000).astype(np.int16)  # a short sound, to loop several times

    async def load(source, rate):
        return sound

    monkeypatch.setattr(ambience, "load", load)
    monkeypatch.setattr(ambience, "LEAD_SECONDS", 60)  # no waiting for the clock in this test
    mixer, out = run_mixer("https://sounds.test/a.wav", 60, voice=100)
    first, later = out[0], out[-1]
    assert abs(int(first[0]) - 100) < 5  # the sound starts silent and fades in
    assert np.all(later >= 100 + 1000 - 1)  # a second in: voice plus the whole sound
    played = np.concatenate(out) - 100
    assert np.array_equal(played[-400:], np.roll(sound, -(len(played) % 400))[-400:])  # round and round


def test_the_mixer_keeps_real_time_so_queueing_transports_never_fill_up(monkeypatch):
    async def load(source, rate):
        return np.zeros(8000, dtype=np.int16)

    monkeypatch.setattr(ambience, "load", load)
    started = time.monotonic()
    _, out = run_mixer("office", 50)  # 50 frames of 20 ms: one second of audio
    took = time.monotonic() - started
    assert len(out) == 50 and took >= 1.0 - ambience.LEAD_SECONDS - 0.1  # not faster than it plays


def test_a_sound_that_cant_be_had_leaves_the_call_as_it_was(monkeypatch):
    async def load(source, rate):
        raise ambience.SoundError("answered HTTP 404")

    monkeypatch.setattr(ambience, "load", load)
    monkeypatch.setattr(ambience, "LEAD_SECONDS", 60)
    mixer, out = run_mixer("https://sounds.test/gone.wav", 3, voice=7)
    assert mixer.sound is None and all(np.all(frame == 7) for frame in out)


def test_attach_sets_the_outputs_mixer_only_when_there_is_a_sound():
    class Params:
        audio_out_mixer = None

    class Output:
        _params = Params()

    class Transport:
        out = Output()

        def output(self):
            return self.out

    transport = Transport()
    ambience.attach(transport, None)
    assert transport.out._params.audio_out_mixer is None
    ambience.attach(transport, "office")
    assert type(transport.out._params.audio_out_mixer).__name__ == "Ambience"


def test_a_phone_call_with_office_sound_carries_it_while_the_assistant_is_silent(monkeypatch, quiet_providers):
    from fastapi.testclient import TestClient

    from openring.api import create_app
    from openring.config import Settings
    from openring.store import Store

    from test_call_flow import _pipeline_started

    _pipeline_started(monkeypatch)
    for key in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.setenv(key, "fake")
    store = Store(":memory:")
    client = TestClient(create_app(Settings(api_key="test-key", public_url="https://x.test", db_path=":memory:",
                                            validate_twilio_signature=False), store))
    assistant = client.post("/assistant", json={**ASSISTANT, "backgroundSound": "office"}, headers=AUTH).json()
    client.post("/phone-number", json={"number": "+15551234567", "assistantId": assistant["id"]}, headers=AUTH)
    params = _stream_params(client.post("/twilio/voice", data={"To": "+15551234567", "From": "+5511",
                                                                "CallSid": "CA1"}).text)
    heard = b""
    with client.websocket_connect("/ws/twilio") as ws:
        _handshake(ws, params)
        until = time.monotonic() + 20  # the first Pipecat import in a process takes a few seconds
        while time.monotonic() < until and len(heard) < 8000 * 2:  # two seconds of the line
            message = json.loads(ws.receive_text())
            if message.get("event") == "media":
                heard += base64.b64decode(message["media"]["payload"])
        ws.send_text(json.dumps({"event": "stop", "streamSid": "MZ1"}))
    _wait_until_ended(store, params["callId"])
    assert len(heard) >= 8000 * 2  # the providers said nothing, yet the line carried sound
    assert len(set(heard[8000:])) > 20  # mu-law silence is one byte value; the office is many
