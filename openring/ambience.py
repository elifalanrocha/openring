"""Background sound under the assistant's voice, Vapi's `backgroundSound`: "off", "office", or an audio file's URL.

"office" is OpenRing's own: distant chatter, keyboards and a room's hum, made without any recording by
`scripts/make_office_sound.py` (a transcriber makes no words of it). A URL is fetched once per server run through
netguard (never a cloud metadata address), up to `MAX_BYTES`, decoded by soundfile (WAV, FLAC, OGG, MP3), brought to
the office sound's loudness so it stays in the background, and looped.

Pipecat's output transport asks a mixer for audio even while the assistant is silent, as fast as the transport takes
it. Phone lines over a WebSocket take it in real time; WebRTC, SIP and WhatsApp queue whatever comes. So the mixer keeps
time itself and never runs more than `LEAD_SECONDS` ahead of the clock. Until the sound is ready, and if it can't be
had, the call goes on without it; it fades in over `FADE_SECONDS`.
"""

from __future__ import annotations

import asyncio
import io
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from loguru import logger

from . import netguard

OFFICE = Path(__file__).parent / "sounds" / "office.ogg"
MAX_BYTES = 10 * 1024 * 1024
MAX_SECONDS = 300  # a longer file is cut there: five minutes loop well enough
FETCH_SECONDS = 15
LEAD_SECONDS = 0.2
FADE_SECONDS = 1.0
LEVEL_DBFS = -41.0  # a sound from a URL is brought to the office sound's loudness
CACHED = 8  # decoded sounds kept, by (source, sample rate)

_sounds: OrderedDict[tuple[str, int], Any] = OrderedDict()


class SoundError(Exception):
    """A background sound that can't be had: the call goes on without it."""


def source_of(value: Any) -> tuple[str | None, str | None]:
    """(source, warning) for an assistant's `backgroundSound`: source is None (off), "office" or a URL."""
    if value in (None, "", "off"):
        return None, None
    if value == "office":
        return "office", None
    if isinstance(value, str) and value.lower().startswith(("https://", "http://")):
        return value, None
    return None, "backgroundSound is off, office or the URL of an audio file; no background sound"


async def fetch(url: str) -> bytes:
    """The file at `url`, at most MAX_BYTES."""
    chunks, size = [], 0
    try:
        async with netguard.client(timeout=FETCH_SECONDS, follow_redirects=True) as client:
            async with client.stream("GET", url) as response:
                if response.status_code >= 400:
                    raise SoundError(f"answered HTTP {response.status_code}")
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise SoundError(f"is over {MAX_BYTES // 1024 // 1024} MB")
                    chunks.append(chunk)
    except SoundError:
        raise
    except Exception as exc:  # the URL itself may hold a secret: say what failed, not where
        why = str(exc) if "OpenRing never calls" in str(exc) else exc.__class__.__name__
        raise SoundError(f"couldn't be fetched ({why})") from exc
    return b"".join(chunks)


def decode(data: bytes, sample_rate: int, *, level: float | None = LEVEL_DBFS, seamless: bool = False) -> Any:
    """16-bit mono samples at `sample_rate`, at `level` dBFS RMS (None: as recorded), looping without a click (a
    `seamless` sound already does)."""
    import numpy as np
    import soundfile as sf
    import soxr

    try:
        with sf.SoundFile(io.BytesIO(data)) as file:
            rate = file.samplerate
            audio = file.read(frames=MAX_SECONDS * rate, dtype="float32", always_2d=True)
    except Exception as exc:
        raise SoundError("isn't an audio file soundfile reads (WAV, FLAC, OGG or MP3)") from exc
    sound = audio.mean(axis=1)
    if len(sound) < rate // 10:
        raise SoundError("is too short to loop")
    if rate != sample_rate:
        sound = soxr.resample(sound, rate, sample_rate)
    if not seamless:
        fade = min(len(sound) // 4, sample_rate // 10)  # the end runs on into the start, over 0.1 s
        ramp = np.linspace(0, 1, fade, dtype=np.float32)
        sound[:fade] = sound[:fade] * ramp + sound[-fade:] * (1 - ramp)
        sound = sound[:-fade]
    if level is not None:
        rms = float(np.sqrt(np.mean(np.square(sound))))
        if rms > 1e-6:
            sound = sound * (10 ** (level / 20) / rms)
    return (np.clip(sound, -1, 1) * 32767).astype(np.int16)


async def load(source: str, sample_rate: int) -> Any:
    key = (source, sample_rate)
    if key in _sounds:
        _sounds.move_to_end(key)
        return _sounds[key]
    if source == "office":  # made at its level, and to loop
        sound = await asyncio.to_thread(decode, OFFICE.read_bytes(), sample_rate, level=None, seamless=True)
    else:
        sound = await asyncio.to_thread(decode, await fetch(source), sample_rate)
    _sounds[key] = sound
    while len(_sounds) > CACHED:
        _sounds.popitem(last=False)
    return sound


def attach(transport: Any, source: str | None) -> None:
    """Play `source` (None: nothing) under a call's audio: set as its output's mixer before the pipeline starts.
    Every transport OpenRing builds is one of Pipecat's, whose output reads the mixer from its params on start."""
    if source:
        transport.output()._params.audio_out_mixer = mixer(source)


def mixer(source: str) -> Any:
    """A Pipecat audio mixer playing `source` under the call's audio (see the module)."""
    import numpy as np
    from pipecat.audio.mixers.base_audio_mixer import BaseAudioMixer

    class Ambience(BaseAudioMixer):
        def __init__(self) -> None:
            self.sound: Any = None
            self._rate = 0
            self._position = 0
            self._played = 0  # samples mixed since the sound was ready, for the fade-in
            self._due: float | None = None  # when the audio mixed so far has played (time.monotonic())
            self._loading: asyncio.Task | None = None

        async def start(self, sample_rate: int) -> None:
            self._rate = sample_rate
            self._loading = asyncio.create_task(self._load())

        async def _load(self) -> None:
            try:
                self.sound = await load(source, self._rate)
            except SoundError as exc:
                where = "office" if source == "office" else netguard.safe_url_for_logs(source)
                logger.warning("background sound {} {}: the call goes on without it", where, exc)

        async def stop(self) -> None:
            if self._loading is not None and not self._loading.done():
                self._loading.cancel()

        async def process_frame(self, frame: Any) -> None:
            pass

        async def mix(self, audio: bytes) -> bytes:
            count = len(audio) // 2
            if not self._rate:
                return audio
            await self._keep_time(count)
            if self.sound is None or not count:
                return audio
            sound = self.sound
            at = (self._position + np.arange(count)) % len(sound)
            self._position = (self._position + count) % len(sound)
            gain = np.minimum(1.0, (self._played + np.arange(count)) / (FADE_SECONDS * self._rate))
            self._played += count
            voice = np.frombuffer(audio[:count * 2], dtype=np.int16).astype(np.float32)
            mixed = voice + sound[at].astype(np.float32) * gain
            return np.clip(mixed, -32768, 32767).astype(np.int16).tobytes()

        async def _keep_time(self, count: int) -> None:
            now = time.monotonic()
            if self._due is None or now - self._due > 0.5:  # the first audio, or the line fell behind: from now
                self._due = now
            self._due += count / self._rate
            ahead = self._due - now - LEAD_SECONDS
            if ahead > 0:
                await asyncio.sleep(ahead)

    return Ambience()
