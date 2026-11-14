"""After a call, what the assistant's model makes of it: Vapi's `analysisPlan`.

`summaryPlan` writes a short summary, `structuredDataPlan` pulls out data in the shape of a JSON schema, and
`successEvaluationPlan` judges, on a rubric, whether the call did what the system prompt set out to do. Each sends the
transcript to the assistant's own model once, all three at the same time, each within its `timeoutSeconds`. What comes
back is the call's `analysis` (`summary`, `structuredData`, `successEvaluation`, as on Vapi), in the call record and the
end-of-call report; what it cost is in `costBreakdown.analysisCostBreakdown`.

Vapi analyzes every call by default, with a model of its own. Here the analysis spends your model's tokens, so it runs
only for an assistant that has an `analysisPlan`. Inside one, Vapi's defaults hold: summary and success evaluation on,
structured data on when it has a schema, nothing for calls under `minMessagesThreshold` messages (2). A part that fails
or runs out of time is left out; the report still goes.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any

import httpx
from loguru import logger

from . import netguard

TIMEOUT_SECONDS = 5.0  # Vapi's default for each part
MAX_TRANSCRIPT = 100_000  # characters of transcript sent: the end of a longer call

RUBRICS = {
    "NumericScale": "A score from 1 to 10.",
    "DescriptiveScale": "One of: Excellent, Good, Fair, Poor.",
    "Checklist": "A checklist of the call's objectives, each marked done or not done.",
    "Matrix": "A grid of the call's objectives against levels of performance.",
    "PercentageScale": "A percentage from 0% to 100%.",
    "LikertScale": "One of: Strongly Agree, Agree, Neutral, Disagree, Strongly Disagree (that the call succeeded).",
    "AutomaticRubric": "The call broken down into the criteria that matter for it, each with its own score.",
    "PassFail": "true if the call succeeded, false if it did not.",
}

SUMMARY = [
    {"role": "system", "content": "You take notes on phone calls. Summarize the call below in two or three sentences: "
                                  "who called, what they wanted and how it ended. Write only the summary."},
    {"role": "user", "content": "Transcript:\n\n{{transcript}}\n\nWhy the call ended: {{endedReason}}"},
]
STRUCTURED = [
    {"role": "system", "content": "You extract data from phone calls. Read the call below and answer with one JSON "
                                  "object that follows this JSON Schema, and nothing else:\n\n{{schema}}\n\n"
                                  "Leave out what the call doesn't say."},
    {"role": "user", "content": "Transcript:\n\n{{transcript}}\n\nWhy the call ended: {{endedReason}}"},
]
SUCCESS = [
    {"role": "system", "content": "You evaluate phone calls made by an AI assistant. From its instructions, work out "
                                  "what the call was meant to achieve, and judge whether it did. Answer only with the "
                                  "result, in this form:\n\n{{rubric}}"},
    {"role": "user", "content": "The assistant's instructions:\n\n{{systemPrompt}}\n\nTranscript:\n\n{{transcript}}"},
]


class AnalysisError(ValueError):
    """An analysisPlan Vapi wouldn't take."""


def plan_of(assistant: dict[str, Any]) -> dict[str, Any] | None:
    """The parts to run, with their messages and timeouts, or None. Raises AnalysisError for a plan Vapi refuses."""
    found = assistant.get("analysisPlan")
    if found is None:
        return None
    if not isinstance(found, dict):
        raise AnalysisError("analysisPlan must be an object")

    def part(name: str, on_by_default: bool) -> dict[str, Any] | None:
        settings = found.get(name) or {}
        if not isinstance(settings, dict):
            raise AnalysisError(f"analysisPlan.{name} must be an object")
        if not settings.get("enabled", on_by_default):
            return None
        timeout = settings.get("timeoutSeconds", TIMEOUT_SECONDS)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 60:
            raise AnalysisError(f"analysisPlan.{name}.timeoutSeconds goes from 1 to 60")
        messages = settings.get("messages")
        if messages is not None and not (isinstance(messages, list) and all(
                isinstance(m, dict) and isinstance(m.get("content"), str) for m in messages)):
            raise AnalysisError(f"analysisPlan.{name}.messages is a list of {{role, content}}")
        return {"messages": messages, "timeout": float(timeout), **settings}

    plan: dict[str, Any] = {}
    if summary := part("summaryPlan", True):
        plan["summary"] = summary
    structured = part("structuredDataPlan", False)
    if structured:
        if not isinstance(structured.get("schema"), dict):
            raise AnalysisError("analysisPlan.structuredDataPlan needs a JSON schema in schema")
        plan["structuredData"] = structured
    if success := part("successEvaluationPlan", True):
        rubric = success.get("rubric", "PassFail")
        if rubric not in RUBRICS:
            raise AnalysisError(f"analysisPlan.successEvaluationPlan.rubric is one of {', '.join(RUBRICS)}")
        plan["successEvaluation"] = {**success, "rubric": rubric}
    threshold = found.get("minMessagesThreshold", 2)
    if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 0:
        raise AnalysisError("analysisPlan.minMessagesThreshold is a whole number")
    return {"parts": plan, "minMessages": threshold} if plan else None


def transcript_text(messages: list[dict[str, Any]]) -> str:
    """The call as Vapi writes `artifact.transcript`: "AI: ..." and "User: ..." lines."""
    lines = [f"{'User' if m.get('role') == 'user' else 'AI'}: {m['message']}" for m in messages
             if m.get("role") in ("user", "assistant", "bot") and isinstance(m.get("message"), str) and m["message"]]
    return "\n".join(lines)[-MAX_TRANSCRIPT:]


def system_prompt(assistant: dict[str, Any]) -> str:
    return "\n\n".join(m.get("content") or "" for m in (assistant.get("model") or {}).get("messages") or []
                       if m.get("role") == "system")


def fill(messages: list[dict[str, Any]], values: dict[str, str]) -> list[dict[str, Any]]:
    """Vapi's template variables ({{transcript}}, {{systemPrompt}}, {{endedReason}}, {{schema}}, {{rubric}})."""
    def one(text: str) -> str:
        return re.sub(r"\{\{\s*(\w+)\s*\}\}", lambda m: values.get(m.group(1), m.group(0)), text)

    return [{"role": m.get("role") or "user", "content": one(m["content"])} for m in messages]


def model_for(plan: Any) -> tuple[str, str, str, dict[str, str]]:
    """(provider, model, base URL, headers) the analysis asks: the call's own model, or for a speech-to-speech one
    (it answers no text requests) the first connected provider's quick model."""
    from .providers import _llm_headers

    if plan.realtime:
        from . import drafts

        provider, model, base, key = drafts.choose(None)
    else:
        provider, model = plan.llm_provider, plan.llm_model
        base = plan.llm_base_url or "https://api.openai.com/v1"
        key = plan.llm_api_key if provider == "custom-llm" else os.environ.get(plan.llm_api_key_env or "", "")
        key = None if key in (None, "", "not-needed") else key
    headers = {**({"Authorization": f"Bearer {key}"} if key else {}), **(_llm_headers(provider) or {})}
    return provider, model, base, headers


async def complete(base: str, model: str, headers: dict[str, str], messages: list[dict[str, Any]],
                   timeout: float) -> tuple[str, dict[str, int]]:
    """The model's answer and its token usage, from an OpenAI-compatible chat completions API."""
    async with netguard.client(timeout=timeout) as client:
        res = await client.post(base.rstrip("/") + "/chat/completions", headers=headers,
                                json={"model": model, "messages": messages, "temperature": 0})
    if res.status_code >= 400:
        raise httpx.HTTPStatusError(f"HTTP {res.status_code}", request=res.request, response=res)
    body = res.json()
    text = ((body.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    usage = body.get("usage") or {}
    return text.strip(), {"prompt": int(usage.get("prompt_tokens") or 0),
                          "completion": int(usage.get("completion_tokens") or 0)}


def json_object(text: str) -> dict[str, Any] | None:
    """The JSON object in a model's answer, code fence or not; None when there is none."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        found = json.loads(text[start:end + 1])
    except ValueError:
        return None
    return found if isinstance(found, dict) else None


def verdict(text: str, rubric: str) -> str | None:
    """The success evaluation as Vapi gives it: a string, "true" or "false" for PassFail."""
    if rubric == "PassFail":
        said = re.findall(r"\b(true|false|pass|fail|passed|failed)\b", text.lower())
        return None if not said else "true" if said[0] in ("true", "pass", "passed") else "false"
    return text or None


async def analyze(plan: Any, call: dict[str, Any]) -> dict[str, Any] | None:
    """{"analysis": {...}, "costs": {...}} for a call that ended, or None when its assistant asks for none."""
    try:
        asked = plan_of(plan.assistant)
    except AnalysisError as exc:
        logger.warning("call {}: analysisPlan: {}", call.get("id"), exc)
        return None
    messages = [m for m in call.get("messages") or [] if m.get("role") in ("user", "assistant", "bot")]
    if asked is None or len(messages) < asked["minMessages"]:
        return None
    try:
        provider, model, base, headers = model_for(plan)
    except Exception as exc:  # no model to ask: no analysis, the report still goes
        logger.warning("call {}: no model for the analysis: {}", call.get("id"), exc)
        return None
    values = {"transcript": transcript_text(messages), "systemPrompt": system_prompt(plan.assistant),
              "endedReason": str(call.get("endedReason") or "")}

    async def run(name: str, settings: dict[str, Any]) -> tuple[str, Any, dict[str, int] | None]:
        default = {"summary": SUMMARY, "structuredData": STRUCTURED, "successEvaluation": SUCCESS}[name]
        filled = fill(settings.get("messages") or default, {
            **values, "schema": json.dumps(settings.get("schema") or {}, ensure_ascii=False),
            "rubric": RUBRICS.get(settings.get("rubric") or "", "")})
        try:
            text, usage = await asyncio.wait_for(complete(base, model, headers, filled, settings["timeout"]),
                                                 settings["timeout"])
        except (Exception, asyncio.TimeoutError) as exc:
            logger.warning("call {}: {} not analyzed: {}", call.get("id"), name, exc.__class__.__name__)
            return name, None, None
        found: Any = text or None
        if name == "structuredData":
            found = json_object(text)
        elif name == "successEvaluation":
            found = verdict(text, settings["rubric"])
        return name, found, usage

    results = await asyncio.gather(*(run(name, settings) for name, settings in asked["parts"].items()))
    from . import prices

    rates = prices.llm(provider, model)
    analysis: dict[str, Any] = {}
    costs: dict[str, Any] = {}
    for name, found, usage in results:
        if found is not None:
            analysis[name] = found
        if usage:
            costs[f"{name}PromptTokens"] = usage["prompt"]
            costs[f"{name}CompletionTokens"] = usage["completion"]
            if rates is not None:
                costs[name] = round((usage["prompt"] * rates[0] + usage["completion"] * rates[1]) / 1e6, 6)
    return {"analysis": analysis, "costs": costs, "priced": rates is not None, "model": f"{provider} {model}"}
