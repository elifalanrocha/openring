"""Drafting an assistant from a description, with a model the server already has: name, greeting and system prompt.

Nothing is saved: the draft fills the editor, and the person changes and saves it. It uses the first model provider
with a key (or a local one that answers), in DRAFTERS' order, unless the request names one.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx

from . import netguard
from .providers import LLM_HEADERS, LLM_PROVIDERS

# A fast, cheap model for each provider that can draft; the person's own choice of model for calls is untouched.
DRAFTERS = {
    "groq": "openai/gpt-oss-20b", "openai": "gpt-4.1-mini", "openrouter": "openai/gpt-4.1-mini",
    "anthropic": "claude-haiku-5-5", "google": "gemini-2.5-flash", "deepseek": "deepseek-chat",
    "mistral": "mistral-small-latest", "cerebras": "gpt-oss-120b", "together-ai": "openai/gpt-oss-20b",
    "xai": "grok-3-mini", "ollama": "llama3.2",
}
MAX_DESCRIPTION = 2000
INSTRUCTIONS = """You write voice AI phone assistants. From the description, answer with JSON only, no other text:
{"name": "...", "firstMessage": "...", "systemPrompt": "..."}
- name: 2 to 4 words.
- firstMessage: what the assistant says when the call starts, one or two short spoken sentences.
- systemPrompt: who the assistant is, what it does and does not do, how it speaks on a phone call (short sentences,
  one question at a time, no lists or symbols, says when it doesn't know), and how the call ends. Under 1500
  characters. Never invent prices, hours, addresses or policies the description doesn't give: tell the assistant to
  say it will check, or to offer a transfer.
Write in the language of the description."""


class DraftError(Exception):
    """Why no draft came back; the text is for the person asking."""


def choose(asked: dict[str, Any] | None) -> tuple[str, str, str, str | None]:
    """(provider, model, base URL, key) to draft with."""
    if asked and asked.get("provider"):
        provider = str(asked["provider"])
        if provider == "custom-llm":
            url = str(asked.get("url") or "")
            if not url:
                raise DraftError("model.url is required for custom-llm")
            return provider, str(asked.get("model") or "model"), url, None
        if provider not in LLM_PROVIDERS:
            raise DraftError(f"unknown model provider {provider}")
        base, env = LLM_PROVIDERS[provider]
        return provider, str(asked.get("model") or DRAFTERS.get(provider, "")), base or "https://api.openai.com/v1", \
            os.environ.get(env or "", "").strip() or None
    for provider, model in DRAFTERS.items():
        base, env = LLM_PROVIDERS[provider]
        key = os.environ.get(env or "", "").strip()
        if key:
            return provider, model, base or "https://api.openai.com/v1", key
    raise DraftError("connect a model provider first (Providers page): drafting uses it to write the assistant")


async def draft(description: str, model: dict[str, Any] | None = None) -> dict[str, Any]:
    """An assistant's name, firstMessage and system prompt, in Vapi's shape, from a description."""
    description = " ".join(str(description or "").split())
    if len(description) < 10:
        raise DraftError("describe the assistant in a sentence or two: who calls, what it does")
    if len(description) > MAX_DESCRIPTION:
        raise DraftError(f"keep the description under {MAX_DESCRIPTION} characters")
    provider, model_name, base, key = choose(model)
    body = {"model": model_name, "stream": True, "temperature": 0.4,
            "messages": [{"role": "system", "content": INSTRUCTIONS}, {"role": "user", "content": description}]}
    headers = {**({"Authorization": f"Bearer {key}"} if key else {}), **(LLM_HEADERS.get(provider) or {})}
    text = []
    try:
        await netguard.check_url_async(base)
        async with netguard.client(timeout=60) as client, \
                client.stream("POST", base.rstrip("/") + "/chat/completions", json=body, headers=headers) as res:
            if res.status_code >= 400:
                raise DraftError(f"{provider} answered HTTP {res.status_code}")
            async for line in res.aiter_lines():
                if not line.startswith("data:") or line.strip() == "data: [DONE]":
                    continue
                try:
                    chunk = json.loads(line[5:])
                except ValueError:
                    continue
                for choice in chunk.get("choices") or []:
                    text.append(((choice.get("delta") or choice.get("message") or {}).get("content")) or "")
    except (httpx.HTTPError, netguard.UnsafeURL) as exc:
        raise DraftError(f"couldn't reach {provider}: {exc.__class__.__name__}") from exc
    return shape("".join(text), provider, model_name)


def shape(text: str, provider: str, model_name: str) -> dict[str, Any]:
    """The model's answer as an assistant draft. Raises DraftError when it isn't the JSON asked for."""
    found = re.search(r"\{.*\}", text, re.S)
    try:
        data = json.loads(found.group(0)) if found else None
    except ValueError:
        data = None
    if not isinstance(data, dict) or not all(isinstance(data.get(k), str) and data[k].strip()
                                             for k in ("name", "firstMessage", "systemPrompt")):
        raise DraftError("the model's answer wasn't a usable draft; try again or describe it differently")
    return {"name": data["name"].strip()[:80], "firstMessage": data["firstMessage"].strip()[:500],
            "model": {"messages": [{"role": "system", "content": data["systemPrompt"].strip()[:4000]}]},
            "draftedWith": f"{provider} {model_name}"}
