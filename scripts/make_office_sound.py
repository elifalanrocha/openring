"""Make openring/sounds/office.ogg, the "office" background sound (`backgroundSound: "office"`).

    python scripts/make_office_sound.py

No recording goes into it, so it carries nobody's rights: distant chatter is Kokoro (the local voice OpenRing ships,
Apache-2.0) reading made-up office lines in six languages at once, dulled and put in a room; the typing and the room's
hum are synthesized. The result is 30 seconds that loop without a seam, 16 kHz mono, quiet enough to sit under a voice.
The same seed makes the same file. Needs the kokoro extra (pip install -e ".[kokoro]").
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "openring" / "sounds" / "office.ogg"
RATE = 16000
SECONDS = 30
SEED = 2026_11_14

# Who talks across the office: Kokoro voice, its language, what they say (written for this file).
TALKERS = [
    ("am_adam", "en-us", ["Did you get a chance to look at the report?", "The meeting moved to three o'clock.",
                          "I think the client called back this morning.", "Let's go over the numbers after lunch."]),
    ("af_sarah", "en-us", ["Can you send me the latest version?", "Sure, I can take that one.",
                           "Has anyone seen the blue folder?", "I'll put it on the shared drive."]),
    ("bm_george", "en-gb", ["We need to finish this by Friday.", "The printer upstairs is out of paper again.",
                            "Right, I'll ring them after the call."]),
    ("ef_dora", "es", ["¿Ya revisaste el informe?", "La reunión es a las tres.", "Te mando el archivo ahora."]),
    ("pf_dora", "pt-br", ["Você viu o relatório?", "A reunião ficou para as três.", "O cliente ligou de novo."]),
    ("ff_siwis", "fr-fr", ["Tu as vu le rapport?", "On en parle après le déjeuner.", "Je t'envoie le fichier."]),
    ("em_alex", "es", ["El cliente llamó otra vez.", "Hablamos después del almuerzo.", "¿Quién tiene la llave?"]),
    ("im_nicola", "it", ["Hai visto il rapporto?", "La riunione è alle tre.", "Ti mando il file adesso."]),
    ("bf_emma", "en-gb", ["Could you pop that in an email?", "I'm just finishing the slides.", "Tea, anyone?"]),
    ("pm_alex", "pt-br", ["Te mando o arquivo agora.", "A gente vê isso depois do almoço.",
                          "Quem pegou o carregador?"]),
]


def db(level: float) -> float:
    return 10 ** (level / 20)


def rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(x)))) or 1e-9


def shaped(x: np.ndarray, response) -> np.ndarray:
    """`x` filtered by `response(frequencies)`, in one FFT (the whole signal is short)."""
    spectrum = np.fft.rfft(x)
    return np.fft.irfft(spectrum * response(np.fft.rfftfreq(len(x), 1 / RATE)), len(x))


def lowpass(x: np.ndarray, corner: float) -> np.ndarray:
    return shaped(x, lambda f: 1 / np.sqrt(1 + (f / corner) ** 4))


def band(x: np.ndarray, low: float, high: float) -> np.ndarray:
    return shaped(x, lambda f: 1 / np.sqrt(1 + (low / np.maximum(f, 1)) ** 4) / np.sqrt(1 + (f / high) ** 4))


def room(x: np.ndarray, rng: np.random.Generator, seconds: float = 0.6, wet: float = 0.65) -> np.ndarray:
    """A small office's reverberation: noise decaying 60 dB over `seconds`, convolved in the frequency domain."""
    n = int(seconds * RATE)
    impulse = rng.standard_normal(n) * np.exp(-6.9 * np.arange(n) / n)
    impulse = lowpass(impulse, 4000)
    impulse /= np.sqrt(np.sum(impulse ** 2))
    size = len(x) + n
    reverb = np.fft.irfft(np.fft.rfft(x, size) * np.fft.rfft(impulse, size), size)[:len(x)]
    return (1 - wet) * x + wet * reverb


def chatter(rng: np.random.Generator, length: int) -> np.ndarray:
    from kokoro_onnx import Kokoro

    sys.path.insert(0, str(ROOT))
    from openring import ambience, providers

    model, voices = (str(providers.KOKORO_DIR / name) for name in providers.KOKORO_FILES)
    kokoro = Kokoro(model, voices)
    mix = np.zeros(length)
    for voice, lang, lines in TALKERS:
        track = np.zeros(length)
        at = int(rng.uniform(0, 6) * RATE)
        while at < length:
            line = lines[rng.integers(len(lines))]
            samples, rate = kokoro.create(line, voice=voice, speed=float(rng.uniform(0.92, 1.08)), lang=lang)
            said = ambience.resample(samples, rate, RATE)
            end = min(length, at + len(said))
            track[at:end] += said[:end - at] * db(rng.uniform(-4, 0))
            at = end + int(rng.uniform(0.3, 1.8) * RATE)  # a pause, someone answering elsewhere
        mix += track / rms(track[track != 0]) * db(rng.uniform(-3, 0))  # desks about as far, none stands out
        print(f"  {voice}: {np.count_nonzero(track) / RATE:.1f} s of talk", flush=True)
    # Many voices at once, dulled and far: a murmur, not words (a transcriber should make nothing of it).
    return room(lowpass(mix, 1400), rng, seconds=0.9, wet=0.85)


def typing(rng: np.random.Generator, length: int) -> np.ndarray:
    """Keyboards in bursts: a click is a few milliseconds of bright noise with a dull knock under it."""
    out = np.zeros(length)
    at = int(rng.uniform(0.5, 3) * RATE)
    while at < length:
        stop = at + int(rng.uniform(1.5, 5) * RATE)
        level = db(rng.uniform(-6, 0))
        while at < min(stop, length):
            n = int(0.012 * RATE)
            t = np.arange(n) / RATE
            click = band(rng.standard_normal(n), 1800, 6000) * np.exp(-t / 0.0015)
            knock = band(rng.standard_normal(n), 300, 1200) * np.exp(-t / 0.004) * 0.6
            end = min(length, at + n)
            out[at:end] += ((click + knock) * level * db(rng.uniform(-5, 0)))[:end - at]
            at += int(rng.uniform(0.08, 0.24) * RATE)
        at = stop + int(rng.uniform(2, 9) * RATE)
    return out


def hum(rng: np.random.Generator, length: int) -> np.ndarray:
    """The room itself: air handling, pink and low."""
    noise = shaped(rng.standard_normal(length), lambda f: 1 / np.sqrt(np.maximum(f, 20)))
    return lowpass(noise, 500)


def main() -> None:
    rng = np.random.default_rng(SEED)
    fade = RATE  # one second, from the end back into the start
    length = SECONDS * RATE + fade
    print("Chatter (Kokoro)...", flush=True)
    voices = chatter(rng, length)
    keys = typing(rng, length)
    air = hum(rng, length)
    sound = (voices / rms(voices) * db(-41) + keys / np.max(np.abs(keys)) * db(-33) + air / rms(air) * db(-54))
    head, tail = sound[:fade], sound[SECONDS * RATE:]
    ramp = np.sin(np.linspace(0, np.pi / 2, fade)) ** 2
    loop = sound[:SECONDS * RATE].copy()
    loop[:fade] = head * ramp + tail * (1 - ramp)  # the end runs on into the start
    import soundfile as sf

    OUT.parent.mkdir(parents=True, exist_ok=True)
    sf.write(OUT, np.clip(loop, -1, 1).astype(np.float32), RATE, format="OGG", subtype="VORBIS")
    print(f"{OUT.relative_to(ROOT)}: {SECONDS} s, {OUT.stat().st_size // 1024} KB, "
          f"{20 * np.log10(rms(loop)):.1f} dBFS RMS, peak {20 * np.log10(np.max(np.abs(loop))):.1f} dBFS")


if __name__ == "__main__":
    main()
