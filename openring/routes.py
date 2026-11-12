"""Model routes, like OmniRoute's combos: a saved list of models, in order, that assistants share.

An assistant with `model.routeId` gets the route's first model, with the others as its `fallbackModels`:
the next one takes over when a model fails (a rejected key, no credit left, a rate limit, an outage) or,
with `maxLatencyMs`, when it takes longer than that to start answering. A model that failed or was slow
rests for `cooldownMinutes`, across calls: the next calls start on the model after it, until it is back.

A route's `preset` sets how it behaves, like OmniRoute's mode packs, cut down to what a phone call needs:
answer fast, keep going, cost little. "fastest" puts the model with the quickest measured answers first,
"cheapest" the one with the lowest price, "reliable" keeps your order and rests a failing model longer.
"custom" leaves `order`, `maxLatencyMs` and `cooldownMinutes` to you.

A route can hold transcribers or voices instead (`kind`: "transcriber" or "voice"; `models` then lists them as
`{"provider", "model", "language"}` or `{"provider", "voiceId", "model"}`). An assistant uses it with
`transcriber.routeId` or `voice.routeId`. They are ordered as listed or by price and rest after a failure like
models do; there is no speed order or latency limit for them, since nothing measures how fast they answer.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Callable
from typing import Any

from loguru import logger

from . import prices

DEFAULT_COOLDOWN = 5  # minutes
MAX_MODELS = 10
KINDS = ("model", "transcriber", "voice")
ITEM_KEYS = {"model": ("provider", "model", "url", "apiKey"), "transcriber": ("provider", "model", "language"),
             "voice": ("provider", "voiceId", "model")}
# Where an assistant's transcriber or voice keeps the route's other members, and what the route chooses for it.
LAYER_FALLBACKS = {"transcriber": "transcribers", "voice": "voices"}
LAYER_CHOSEN = ("provider", "model", "voiceId", "fallbackPlan", "route")
ORDERS = ("listed", "fastest", "cheapest")
PRESETS: dict[str, dict[str, Any]] = {
    "fastest": {"order": "fastest", "maxLatencyMs": 1500, "cooldownMinutes": 5},
    "cheapest": {"order": "cheapest", "maxLatencyMs": 3000, "cooldownMinutes": 5},
    "reliable": {"order": "listed", "maxLatencyMs": 2000, "cooldownMinutes": 15},
}
SETTINGS = ("order", "maxLatencyMs", "cooldownMinutes")  # what a preset decides; changing one means "custom"
SAMPLES = 20  # answer times kept per model, the most recent ones
SPEED_KEY = "model_speed"  # where they are saved (the store's settings table)
PROBE_TIMEOUT = 15  # seconds

# Resting models, (provider, model) -> when they may lead a route again (time.monotonic()). A transcriber or a
# voice is keyed (kind, provider, model or voiceId).
_resting: dict[tuple[str, str], float] = {}
# Recent time to the first word of an answer, ms, per (provider, model): from calls and from `measure`.
_speed: dict[tuple[str, str], list[int]] = {}


def _key(provider: str, model: str, kind: str) -> tuple[str, ...]:
    return (provider, model) if kind == "model" else (kind, provider, model)


def rest(provider: str, model: str, minutes: float, kind: str = "model") -> None:
    if minutes > 0:
        _resting[_key(provider, model, kind)] = time.monotonic() + minutes * 60


def resting(provider: str, model: str, kind: str = "model") -> int:
    """Seconds left before this model (or transcriber, or voice) may lead a route again; 0 when it can."""
    left = _resting.get(_key(provider, model, kind), 0) - time.monotonic()
    return max(round(left), 0)


def let_down(route: dict[str, Any] | None, service: Any) -> None:
    """A call's route moved off this service: rest what it ran for the route's cooldown."""
    kind = (route or {}).get("kind") or "model"
    # (provider, model) or (provider, voiceId), set by build_services
    target = getattr(service, "openring_model" if kind == "model" else "openring_member", None)
    if route and target:
        rest(*target, route.get("cooldownMinutes", DEFAULT_COOLDOWN), kind)


def rest_after_switch(plan: Any, layer: str, service: Any) -> None:
    """A call's `layer` ("model", "transcriber" or "voice") moved off `service`: rest it on that layer's route."""
    route = {"model": plan.route, "transcriber": plan.stt_route, "voice": plan.tts_route}[layer]
    let_down(route, service)


def ident(kind: str, item: dict[str, Any]) -> tuple[str, str]:
    """How a route's member is told apart from the others: its provider, and its model (or its voice)."""
    return str(item.get("provider")), str(item.get("voiceId" if kind == "voice" else "model") or "")


def price_of(kind: str, item: dict[str, Any]) -> float | None:
    """What a member costs, to compare by: a model's blended price, a transcriber's per minute, a voice's per 1M
    characters. None when unknown."""
    provider, name = str(item.get("provider")), str(item.get("model") or "")
    if kind == "transcriber":
        return prices.stt(provider, name or None, item.get("language"))
    if kind == "voice":
        return prices.tts(provider, name or None)
    return prices.blended(provider, name)


def record_speed(provider: str, model: str, ms: float) -> None:
    samples = _speed.setdefault((provider, model), [])
    samples.append(round(ms))
    del samples[:-SAMPLES]


def speed(provider: str, model: str) -> int | None:
    """Median time to the first word of an answer, ms, over the recent ones; None before any."""
    samples = sorted(_speed.get((provider, model)) or [])
    if not samples:
        return None
    mid = len(samples) // 2
    return samples[mid] if len(samples) % 2 else round((samples[mid - 1] + samples[mid]) / 2)


def load_speed(store: Any) -> None:
    try:
        saved = json.loads(store.get_setting(SPEED_KEY) or "[]")
        for provider, model, samples in saved:
            _speed[(str(provider), str(model))] = [int(s) for s in samples][-SAMPLES:]
    except (ValueError, TypeError) as exc:
        logger.warning("saved model speeds ignored: {}", exc)


def save_speed(store: Any) -> None:
    store.set_setting(SPEED_KEY, json.dumps([[p, m, s] for (p, m), s in _speed.items()]))


def settings(route: dict[str, Any]) -> dict[str, Any]:
    """The route's order, latency limit and cooldown: its preset's, or its own when custom."""
    preset = PRESETS.get(str(route.get("preset")))
    out = dict(preset) if preset else {
        "order": route.get("order") or "listed", "maxLatencyMs": route.get("maxLatencyMs"),
        "cooldownMinutes": route.get("cooldownMinutes", DEFAULT_COOLDOWN)}
    if (route.get("kind") or "model") != "model":  # nothing measures a transcriber or a voice
        out["maxLatencyMs"] = None
        if out["order"] == "fastest":
            out["order"] = "listed"
    return out


def ordered(route: dict[str, Any]) -> list[dict[str, Any]]:
    """The route's models in the order calls try them. Resting ones go last (still there in case every other
    one fails); "fastest" and "cheapest" put models not measured or priced yet after the others, as listed."""
    models = [m for m in route.get("models") or [] if isinstance(m, dict)]
    order = settings(route)["order"]
    kind = route.get("kind") or "model"

    def key(item: tuple[int, dict[str, Any]]) -> tuple[bool, bool, float, int]:
        i, m = item
        provider, name = ident(kind, m)
        figure = speed(provider, name) if order == "fastest" else (price_of(kind, m) if order == "cheapest" else 0)
        return (resting(provider, name, kind) > 0, figure is None, figure or 0, i)

    return [m for _, m in sorted(enumerate(models), key=key)]


def expand(assistant: dict[str, Any], lookup: Callable[[str], dict[str, Any] | None]) -> dict[str, Any]:
    """The assistant with its route's models filled in. Without a route (or when it was deleted) the assistant
    keeps its own provider and model; the dashboard saves the route's first one there."""
    for layer in LAYER_FALLBACKS:
        assistant = _expand_layer(assistant, layer, lookup)
    model = assistant.get("model") or {}
    route = lookup(str(model["routeId"])) if model.get("routeId") else None
    if not route or not route.get("models") or (route.get("kind") or "model") != "model":
        return assistant
    first, *others = ordered(route)
    shared = {k: v for k, v in model.items() if k not in ("provider", "model", "url", "apiKey", "fallbackModels")}
    plan = {"id": route["id"], "name": route.get("name"), "preset": route.get("preset") or "custom",
            **settings(route)}
    return {**assistant, "model": {**shared, **first, "fallbackModels": others, "route": plan}}


def _expand_layer(assistant: dict[str, Any], layer: str, lookup: Callable[[str], dict[str, Any] | None]
                  ) -> dict[str, Any]:
    """The assistant's transcriber or voice with its route's first member in place, the others as its
    `fallbackPlan`. What the layer says besides the provider (language, keyterms, speed...) is kept for all."""
    config = assistant.get(layer) or {}
    route = lookup(str(config["routeId"])) if config.get("routeId") else None
    if not route or not route.get("models") or route.get("kind") != layer:
        return assistant
    first, *others = ordered(route)
    shared = {k: v for k, v in config.items() if k not in LAYER_CHOSEN}
    plan = {"id": route["id"], "name": route.get("name"), "kind": layer,
            "preset": route.get("preset") or "custom", **settings(route)}
    fallback = {**(config.get("fallbackPlan") or {}), LAYER_FALLBACKS[layer]: [{**shared, **o} for o in others]}
    return {**assistant, layer: {**shared, **first, "fallbackPlan": fallback, "route": plan}}


def clean(body: dict[str, Any]) -> dict[str, Any]:
    """A route as stored. Raises ValueError for anything a call would trip on."""
    name = str(body.get("name") or "").strip()
    if not name:
        raise ValueError("name is required")
    kind = body.get("kind") or "model"
    if kind not in KINDS:
        raise ValueError(f"kind is one of {', '.join(KINDS)}")
    models = body.get("models")
    if not isinstance(models, list) or not models:
        example = {"model": '[{"provider": "groq", "model": "openai/gpt-oss-20b"}]',
                   "transcriber": '[{"provider": "deepgram", "model": "nova-3"}]',
                   "voice": '[{"provider": "11labs", "voiceId": "..."}]'}[kind]
        raise ValueError(f"models is required: in order, e.g. {example}")
    if len(models) > MAX_MODELS:
        raise ValueError(f"a route holds up to {MAX_MODELS} {'models' if kind == 'model' else kind + 's'}")
    needs = {"model": ("provider", "model"), "transcriber": ("provider",), "voice": ("provider", "voiceId")}[kind]
    cleaned = []
    for i, m in enumerate(models):
        if not isinstance(m, dict) or not all(m.get(k) for k in needs):
            raise ValueError(f"models[{i}] needs a {' and a '.join(needs)}")
        cleaned.append({k: m[k] for k in ITEM_KEYS[kind] if m.get(k)})
    kept = {**body, "kind": kind} if kind != "model" else {k: v for k, v in body.items() if k != "kind"}
    preset = body.get("preset") or "custom"
    if preset != "custom" and preset not in PRESETS:
        raise ValueError(f"preset is one of {', '.join([*PRESETS, 'custom'])}")
    if kind != "model" and preset == "fastest":
        raise ValueError("a route of transcribers or voices has no fastest preset: nothing measures their speed")
    if preset in PRESETS:  # the preset decides; what was sent for these is ignored
        return {**kept, "name": name, "models": cleaned, "preset": preset, **PRESETS[preset],
                **({"maxLatencyMs": None} if kind != "model" else {})}
    order = body.get("order") or "listed"
    if order not in ORDERS or (kind != "model" and order == "fastest"):
        raise ValueError(f"order is one of {', '.join(o for o in ORDERS if kind == 'model' or o != 'fastest')}")
    latency = body.get("maxLatencyMs")
    if kind != "model":
        latency = None  # nothing measures how fast they answer
    if latency is not None:
        if not isinstance(latency, (int, float)) or not 300 <= latency <= 30_000:
            raise ValueError("maxLatencyMs goes from 300 to 30000, or null to switch on errors only")
        latency = int(latency)
    cooldown = body.get("cooldownMinutes", DEFAULT_COOLDOWN)
    if not isinstance(cooldown, (int, float)) or not 0 <= cooldown <= 1440:
        raise ValueError("cooldownMinutes goes from 0 (never rest a model) to 1440")
    return {**kept, "name": name, "models": cleaned, "preset": "custom", "order": order,
            "maxLatencyMs": latency, "cooldownMinutes": cooldown}


def with_status(route: dict[str, Any]) -> dict[str, Any]:
    """The route for the dashboard: per model, how long it still rests, its measured speed and its price,
    plus `next`, the order the next call tries them in."""
    kind = route.get("kind") or "model"

    def status(m: dict[str, Any]) -> dict[str, Any]:
        provider, name = ident(kind, m)
        if kind != "model":
            unit = price_of(kind, m)
            key = "perMinute" if kind == "transcriber" else "perMillionCharacters"
            return {**m, "restingSeconds": resting(provider, name, kind),
                    "price": {key: unit} if unit is not None else None}
        price = prices.llm(provider, name)
        return {**m, "restingSeconds": resting(provider, name), "firstTokenMs": speed(provider, name),
                "samples": len(_speed.get((provider, name)) or []),
                "price": {"input": price[0], "output": price[1]} if price else None}

    return {**route, "preset": route.get("preset") or "custom", **settings(route),
            "models": [status(m) for m in route.get("models") or []],
            "next": [i for m in ordered(route) for i, x in enumerate(route.get("models") or []) if x is m]}


async def probe(model: dict[str, Any], plan: Any) -> dict[str, Any]:
    """Time one short answer from a model: how long until its first word, as on a call. `plan` is the model's
    resolved stack (base URL and key)."""
    from openai import AsyncOpenAI

    from .providers import LOCAL_LLMS, _llm_headers

    key = plan.llm_api_key or os.environ.get(plan.llm_api_key_env or "", "")
    if not key and plan.llm_provider not in LOCAL_LLMS and plan.llm_api_key_env:
        return {"error": f"no key: add {plan.llm_api_key_env}"}
    client = AsyncOpenAI(api_key=key or "not-needed", base_url=plan.llm_base_url,
                         default_headers=_llm_headers(plan.llm_provider), timeout=PROBE_TIMEOUT, max_retries=0)
    started = time.monotonic()
    try:
        async with asyncio.timeout(PROBE_TIMEOUT):
            stream = await client.chat.completions.create(
                model=plan.llm_model, stream=True,
                messages=[{"role": "system", "content": "You are on a phone call. Answer in a few words."},
                          {"role": "user", "content": "Hi, can you hear me?"}],
            )
            first = None
            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    first = time.monotonic()
                    break
            await stream.close()
    except TimeoutError:
        return {"error": f"no answer after {PROBE_TIMEOUT} s"}
    except Exception as exc:  # a rejected key, an unknown model, a server that isn't running
        return {"error": str(getattr(exc, "message", None) or exc)[:200]}
    finally:
        await client.close()
    ms = round(((first or time.monotonic()) - started) * 1000)
    record_speed(str(model.get("provider")), str(model.get("model")), ms)
    return {"firstTokenMs": ms}
