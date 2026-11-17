"""Numbers for the dashboard's overview, computed from the call records."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Any

# What hosted voice platforms charge per minute just for orchestration, on top of the providers.
PLATFORM_FEE_PER_MINUTE = 0.05
# endedReason prefixes that mean the call broke rather than finished.
FAILED_REASONS = ("pipeline-error", "provider-error", "assistant-not-found", "twilio-failed")
LATENCY_PARTS = ("endpointingLatency", "transcriberLatency", "modelLatency", "voiceLatency")


def _created(call: dict[str, Any]) -> datetime | None:
    try:
        return datetime.fromisoformat(str(call.get("createdAt", "")).replace("Z", "+00:00"))
    except ValueError:
        return None


def _failed(call: dict[str, Any]) -> bool:
    return str(call.get("endedReason") or "").startswith(FAILED_REASONS)


def _median(values: list[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else round((ordered[mid - 1] + ordered[mid]) / 2)


def _percentile(values: list[int], pct: int) -> int | None:
    """The value below which `pct` percent of the values fall (nearest rank)."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, -(-len(ordered) * pct // 100) - 1)]


def _usage(window: list[dict[str, Any]]) -> dict[str, Any]:
    """Tokens, voice characters and transcribed audio: what providers bill by, summed over the calls."""
    fields = ("llmPromptTokens", "llmCompletionTokens", "ttsCharacters", "transcriberAudioSeconds")
    rows = [c["costBreakdown"] for c in window if isinstance(c.get("costBreakdown"), dict)]
    total = {f: sum(float(r.get(f) or 0) for r in rows) for f in fields}
    tokens = total["llmPromptTokens"] + total["llmCompletionTokens"]
    return {
        "calls": len(rows),
        **{f: round(v, 1) if f == "transcriberAudioSeconds" else int(v) for f, v in total.items()},
        "tokensPerCall": round(tokens / len(rows)) if rows else None,
    }


def _part_average(turns: list[dict[str, Any]], part: str) -> int | None:
    """The average of one stage over the turns that measured it (a turn can have only its whole time)."""
    measured = [t[part] for t in turns if isinstance(t.get(part), (int, float))]
    return round(sum(measured) / len(measured)) if measured else None


def _cost(window: list[dict[str, Any]]) -> dict[str, Any]:
    """What the providers charged, at list prices, over the calls priced so far (calls record it since 0.1.3)."""
    priced = [c for c in window if isinstance(c.get("cost"), (int, float))]
    total = sum(float(c["cost"]) for c in priced)
    minutes = sum(float(c.get("durationSeconds") or 0) for c in priced) / 60
    return {
        "calls": len(priced),
        "total": round(total, 4),
        "perMinute": round(total / minutes, 4) if minutes else None,
        "perCall": round(total / len(priced), 4) if priced else None,
        # Calls where a provider has no known price: their total leaves it out.
        "incomplete": sum(1 for c in priced if (c.get("costBreakdown") or {}).get("unpriced")),
    }


def summarize(calls: list[dict[str, Any]], days: int | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Totals for the last `days` days (every call when None), plus one entry per day for the chart."""
    now = now or datetime.now(timezone.utc)
    start = (now - timedelta(days=days - 1)).date() if days else None
    picked = [(c, _created(c)) for c in calls]
    picked = [(c, t) for c, t in picked if t and (start is None or t.date() >= start)]
    window = [c for c, _ in picked]

    ended = [c for c in window if c.get("status") == "ended"]
    failed = [c for c in ended if _failed(c)]
    seconds = sum(float(c.get("durationSeconds") or 0) for c in window)
    turns = [
        t
        for c in window
        for t in ((c.get("artifact") or {}).get("performanceMetrics") or {}).get("turnLatencies") or []
        if isinstance(t, dict) and t.get("turnLatency")
    ]
    minutes = seconds / 60
    first_words = [
        int(fw)
        for c in window
        if isinstance(fw := ((c.get("artifact") or {}).get("performanceMetrics") or {}).get("firstWordLatency"), int)
    ]
    forwarded = sum(1 for c in ended if c.get("endedReason") == "assistant-forwarded-call")

    first = start or min((t.date() for _, t in picked), default=now.date())
    first = max(first, now.date() - timedelta(days=89))  # the chart shows at most 90 days
    per_day: dict[date, dict[str, Any]] = {}
    d = first
    while d <= now.date():
        per_day[d] = {"date": d.isoformat(), "calls": 0, "minutes": 0.0, "failed": 0, "cost": 0.0}
        d += timedelta(days=1)
    for c, t in picked:
        if t.date() in per_day:
            day = per_day[t.date()]
            day["calls"] += 1
            day["minutes"] += float(c.get("durationSeconds") or 0) / 60
            day["failed"] += int(_failed(c))
            day["cost"] += float(c.get("cost") or 0)
    for day in per_day.values():
        day["minutes"], day["cost"] = round(day["minutes"], 1), round(day["cost"], 4)

    by_assistant: dict[str, dict[str, Any]] = {}
    for c in window:
        entry = by_assistant.setdefault(str(c.get("assistantId") or ""), {"calls": 0, "minutes": 0.0})
        entry["calls"] += 1
        entry["minutes"] += float(c.get("durationSeconds") or 0) / 60

    return {
        "days": days,
        "calls": len(window),
        "inProgress": sum(1 for c in window if c.get("status") in ("queued", "in-progress")),
        "failed": len(failed),
        "successRate": round(100 * (len(ended) - len(failed)) / len(ended)) if ended else None,
        "minutes": round(minutes, 1),
        "averageDurationSeconds": round(seconds / len(window)) if window else None,
        "turns": len(turns),
        "latencyMedianMs": _median([t["turnLatency"] for t in turns]),
        "latencyP95Ms": _percentile([t["turnLatency"] for t in turns], 95),
        "firstWordMedianMs": _median(first_words),
        "transferRate": round(100 * forwarded / len(ended)) if ended else None,
        "usage": _usage(window),
        "cost": _cost(window),
        "latencyAverageMs": round(sum(t["turnLatency"] for t in turns) / len(turns)) if turns else None,
        "latencyParts": {p: _part_average(turns, p) for p in LATENCY_PARTS},
        "platformFeeSaved": round(minutes * PLATFORM_FEE_PER_MINUTE, 2),
        "byType": dict(Counter(str(c.get("type") or "unknown") for c in window)),
        "endedReasons": dict(Counter(str(c.get("endedReason")) for c in ended).most_common(8)),
        "byAssistant": sorted(
            (
                {"assistantId": k, "calls": v["calls"], "minutes": round(v["minutes"], 1)}
                for k, v in by_assistant.items()
            ),
            key=lambda x: -x["calls"],
        )[:5],
        "perDay": list(per_day.values()),
    }


# What a minute of conversation is taken to be when an assistant has no calls to measure: the assistant speaks about
# half the time, and the model is asked five times with the conversation so far.
ESTIMATE = {"spokenCharacters": 450, "modelRequests": 5, "promptTokens": 1200, "answerTokens": 60}
MEASURED_CALLS = 50


def llm_per_minute(price: tuple[float, float] | None) -> float | None:
    """US$ a minute of call costs in model tokens, from its list price (input, output per 1M tokens) and `ESTIMATE`."""
    if price is None:
        return None
    return round(ESTIMATE["modelRequests"] * (ESTIMATE["promptTokens"] * price[0]
                                              + ESTIMATE["answerTokens"] * price[1]) / 1e6, 5)


def model_options(provider: str, extra: list[str] | None = None) -> dict[str, Any]:
    """The models to choose from for a provider, each with what it costs a minute (list prices) and how fast it
    answered (the time to its first word: this server's own calls and measurements first, else OpenRing's test calls;
    unknown stays unknown). `extra` are models the provider offers that have no list price (an Ollama's own)."""
    from . import prices, routes

    options = []
    for name in sorted({*prices.models(provider), *(extra or [])}):
        price = prices.llm(provider, name)
        here = routes.speed(provider, name)
        reference = MEASURED_MS.get(("model", provider, name))
        source = "this server" if here is not None else "OpenRing's tests" if reference else None
        options.append({"provider": provider, "id": name, "inputPerM": None if price is None else price[0],
                        "outputPerM": None if price is None else price[1], "costPerMinute": llm_per_minute(price),
                        "latencyMs": here if here is not None else reference, "latencySource": source})
    return {"provider": provider, "models": options, "assumes": ESTIMATE}


def profile(calls: list[dict[str, Any]], plan: Any) -> dict[str, Any]:
    """What an assistant costs per minute and how long it takes to answer, layer by layer.

    Measured on its own last calls when it has any; otherwise the cost is an estimate from list prices (`ESTIMATE`) and
    the latency is unknown: OpenRing doesn't guess how fast a provider is.
    """
    from . import prices

    done = [c for c in calls if c.get("status") == "ended" and float(c.get("durationSeconds") or 0) > 0
            and isinstance(c.get("costBreakdown"), dict)][:MEASURED_CALLS]
    minutes = sum(float(c["durationSeconds"]) for c in done) / 60
    turns = [t for c in done
             for t in ((c.get("artifact") or {}).get("performanceMetrics") or {}).get("turnLatencies") or []
             if isinstance(t, dict)]
    latency = {p: _part_average(turns, p) for p in LATENCY_PARTS}
    latency["turnLatency"] = round(sum(t.get("turnLatency", 0) for t in turns) / len(turns)) if turns else None
    if done and minutes > 0:
        cost = {k: round(sum(float(c["costBreakdown"].get(k) or 0) for c in done) / minutes, 5)
                for k in ("stt", "llm", "tts")}
        unpriced = sorted({str(u) for c in done for u in c["costBreakdown"].get("unpriced") or []})
        return {"measured": True, "calls": len(done), "minutes": round(minutes, 1), "costPerMinute": cost,
                "totalPerMinute": round(sum(cost.values()), 5), "unpriced": unpriced, "latency": latency}

    heard = 0.0 if plan.realtime else prices.stt(plan.stt_provider, plan.stt_model, plan.stt_language)
    model = prices.llm(plan.llm_provider, plan.llm_model)  # a speech-to-speech model's audio isn't priced yet
    voice = 0.0 if plan.realtime else prices.tts(plan.tts_provider, plan.tts_model)
    cost = {
        "stt": heard,
        "llm": llm_per_minute(model),
        "tts": None if voice is None else round(ESTIMATE["spokenCharacters"] * voice / 1e6, 5),
    }
    known = [v for v in cost.values() if v is not None]
    names = {"stt": "transcriber", "llm": "model", "tts": "voice"}
    return {"measured": False, "calls": 0, "minutes": 0, "costPerMinute": cost,
            "totalPerMinute": round(sum(known), 5) if known else None,
            "unpriced": [names[k] for k, v in cost.items() if v is None], "latency": latency, "assumes": ESTIMATE}


# ---- Presets: a whole stack in one click, as Vapi's editor offers ----------------------------------------------------

# A premade ElevenLabs voice (Bella): free accounts can use it through the API, library voices they can't.
PRESET_VOICE = {"provider": "11labs", "voiceId": "hpp4J3VqNfWAUOO0d1Us", "model": "eleven_flash_v2_5"}
PRESETS = [
    {"id": "balanced", "name": "Balanced", "about": "Accurate transcriber, a capable small model, a natural voice",
     "transcriber": {"provider": "deepgram", "model": "nova-3"},
     "model": {"provider": "openai", "model": "gpt-4.1-mini"}, "voice": PRESET_VOICE},
    {"id": "high-intelligence", "name": "High intelligence", "about": "A frontier model for calls that need reasoning",
     "transcriber": {"provider": "deepgram", "model": "nova-3"},
     "model": {"provider": "anthropic", "model": "claude-sonnet-5-5"}, "voice": PRESET_VOICE},
    {"id": "ultra-fast", "name": "Ultra fast", "about": "The fastest answers OpenRing measured",
     "transcriber": {"provider": "deepgram", "model": "nova-3"},
     "model": {"provider": "groq", "model": "openai/gpt-oss-20b"}, "voice": PRESET_VOICE},
    {"id": "cost-saver", "name": "Cost saver", "about": "Transcriber and voice free on this computer; slower on a CPU",
     "transcriber": {"provider": "whisper", "model": "base"},
     "model": {"provider": "groq", "model": "openai/gpt-oss-20b"},
     "voice": {"provider": "kokoro", "voiceId": "af_heart"}},
]
# Milliseconds per stage, measured on OpenRing's own real test calls (scripts/smoke_call.py, 2026-11-14, a 2018
# laptop, phone audio). Anything not measured stays unknown: OpenRing doesn't guess how fast a provider is.
MEASURED_MS = {
    ("transcriber", "deepgram"): 160,
    ("model", "groq", "openai/gpt-oss-20b"): 750,
    ("voice", "11labs"): 280,
    ("voice", "elevenlabs"): 280,
    ("voice", "kokoro"): 2600,
}
ENDPOINTING_MS = 210  # silence until the turn ends, measured on the same calls


def presets() -> list[dict[str, Any]]:
    """Each preset with its estimated cost per minute (list prices, `ESTIMATE`), its measured latency where OpenRing
    has measured it, and the keys this server still lacks for it."""
    import os

    from . import routes
    from .providers import LLM_PROVIDERS, STT_PROVIDERS, TTS_PROVIDERS, resolve_plan

    out = []
    for preset in PRESETS:
        plan = resolve_plan({"transcriber": preset["transcriber"], "model": preset["model"],
                             "voice": preset["voice"]}, check_keys=False, with_fallbacks=False)
        estimate = profile([], plan)
        stages = {"transcriber": MEASURED_MS.get(("transcriber", plan.stt_provider)),
                  # this server's own calls first (routes keeps each model's time to its first word)
                  "model": routes.speed(plan.llm_provider, plan.llm_model)
                  or MEASURED_MS.get(("model", plan.llm_provider, plan.llm_model)),
                  "voice": MEASURED_MS.get(("voice", plan.tts_provider))}
        known = None if None in stages.values() else ENDPOINTING_MS + sum(stages.values())
        envs = [STT_PROVIDERS.get(plan.stt_provider), (LLM_PROVIDERS.get(plan.llm_provider) or (None, None))[1],
                TTS_PROVIDERS.get(plan.tts_provider)]
        missing = sorted({e for e in envs if e and not os.environ.get(e, "").strip()})
        out.append({**preset, "costPerMinute": estimate["costPerMinute"], "totalPerMinute": estimate["totalPerMinute"],
                    "unpriced": estimate["unpriced"], "latency": {**stages, "turn": known},
                    "missingKeys": missing})
    return out
