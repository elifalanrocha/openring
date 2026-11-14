"""The assistant a new server starts with, so the first thing to do is talk to it.

It uses what costs nothing and needs no account: Whisper to listen and Kokoro to speak, both on this computer, when
they are installed. The model is Ollama's when Ollama is running with a model pulled; otherwise Groq's, which needs a
(free) key, and the dashboard's first step says so. It is made once: delete it and it stays deleted.
"""

from __future__ import annotations

from typing import Any

from .store import Store

MADE = "starter_assistant"  # in the settings table, once this server has been given its starter
NAME = "My first assistant"
GREETING = "Hi! I'm your first OpenRing assistant. What would you like to talk about?"
INSTRUCTIONS = (
    "You are a friendly voice assistant on a phone call. Keep every answer to one or two short sentences, in plain "
    "spoken language, with no lists or symbols. If you don't know something, say so."
)


def assistant(ollama_models: list[str], whisper: bool, kokoro: bool) -> dict[str, Any]:
    """The starter's JSON, in Vapi's shape, for what this computer has."""
    model = ({"provider": "ollama", "model": ollama_models[0]} if ollama_models
             else {"provider": "groq", "model": "openai/gpt-oss-20b"})
    return {
        "name": NAME,
        "firstMessage": GREETING,
        "transcriber": ({"provider": "whisper", "model": "base", "language": "en"} if whisper
                        else {"provider": "deepgram", "model": "nova-3", "language": "en"}),
        "model": {**model, "messages": [{"role": "system", "content": INSTRUCTIONS}]},
        "voice": ({"provider": "kokoro", "voiceId": "af_heart"} if kokoro
                  else {"provider": "deepgram", "voiceId": "thalia"}),
    }


def make(store: Store, ollama_models: list[str], whisper: bool, kokoro: bool) -> dict[str, Any] | None:
    """Give a server with no assistant its starter, once. Returns it, or None when there is nothing to do."""
    if store.get_setting(MADE) or store.list("assistants", limit=1):
        return None
    store.set_setting(MADE, "1")
    return store.create("assistants", assistant(ollama_models, whisper, kokoro))
