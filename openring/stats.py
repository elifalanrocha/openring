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
        per_day[d] = {"date": d.isoformat(), "calls": 0, "minutes": 0.0, "failed": 0}
        d += timedelta(days=1)
    for c, t in picked:
        if t.date() in per_day:
            day = per_day[t.date()]
            day["calls"] += 1
            day["minutes"] += float(c.get("durationSeconds") or 0) / 60
            day["failed"] += int(_failed(c))
    for day in per_day.values():
        day["minutes"] = round(day["minutes"], 1)

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
        "latencyParts": {p: round(sum(t.get(p, 0) for t in turns) / len(turns)) if turns else None
                         for p in LATENCY_PARTS},
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


def profile(calls: list[dict[str, Any]], plan: Any) -> dict[str, Any]:
    """What an assistant costs per minute and how long it takes to answer, layer by layer.

    Measured on its own last calls when it has any; otherwise the cost is an estimate from list prices (`ESTIMATE`) and
    the latency is unknown: OpenRing doesn't guess how fast a provider is.
    """
    from . import prices

    done = [c for c in calls if c.get("status") == "ended" and float(c.get("durationSeconds") or 0) > 0
            and isinstance(c.get("costBreakdown"), dict)][:MEASURED_CALLS]
    minutes = sum(float(c["durationSeconds"]) for c in done) / 60
    turns = [t for c in done for t in ((c.get("artifact") or {}).get("performanceMetrics") or {}).get("turns") or []
             if isinstance(t, dict)]
    latency = {p: round(sum(t.get(p, 0) for t in turns) / len(turns)) if turns else None for p in LATENCY_PARTS}
    latency["turnLatency"] = round(sum(t.get("turnLatency", 0) for t in turns) / len(turns)) if turns else None
    if done and minutes > 0:
        cost = {k: round(sum(float(c["costBreakdown"].get(k) or 0) for c in done) / minutes, 5)
                for k in ("stt", "llm", "tts")}
        unpriced = sorted({str(u) for c in done for u in c["costBreakdown"].get("unpriced") or []})
        return {"measured": True, "calls": len(done), "minutes": round(minutes, 1), "costPerMinute": cost,
                "totalPerMinute": round(sum(cost.values()), 5), "unpriced": unpriced, "latency": latency}

    heard = prices.stt(plan.stt_provider, plan.stt_model, plan.stt_language)
    model = prices.llm(plan.llm_provider, plan.llm_model)
    voice = prices.tts(plan.tts_provider, plan.tts_model)
    requests = ESTIMATE["modelRequests"]
    cost = {
        "stt": heard,
        "llm": None if model is None else round(
            requests * (ESTIMATE["promptTokens"] * model[0] + ESTIMATE["answerTokens"] * model[1]) / 1e6, 5),
        "tts": None if voice is None else round(ESTIMATE["spokenCharacters"] * voice / 1e6, 5),
    }
    known = [v for v in cost.values() if v is not None]
    names = {"stt": "transcriber", "llm": "model", "tts": "voice"}
    return {"measured": False, "calls": 0, "minutes": 0, "costPerMinute": cost,
            "totalPerMinute": round(sum(known), 5) if known else None,
            "unpriced": [names[k] for k, v in cost.items() if v is None], "latency": latency, "assumes": ESTIMATE}
