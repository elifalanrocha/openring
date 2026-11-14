"""Evals, as on Vapi: a scripted chat with an assistant, and checks on what it answers (`/eval`, `/eval/run`).

An eval (`type: "chat.mockConversation"`) is a list of messages. `user`, `system` and `tool` messages are said to the
assistant as written (a `tool` message answers the assistant's last tool call). An `assistant` message with a
`judgePlan` is where the assistant answers, with its own model, system prompt and tools, and the answer is checked:

- `exact`: the answer is `content`, or its tool calls are `toolCalls` (names, and arguments when given), in order;
- `regex`: `content` is a pattern found in the answer; a tool call's name and each given argument match theirs;
- `ai`: a judge model (`model: {provider, model, messages}`, `{{messages}}` and `{{messages[-1]}}` filled in) says
  pass or fail.

An `assistant` message without a judgePlan is a scripted answer, put in the history as is. After a check fails,
`continuePlan.exitOnFailureEnabled` stops the run, and `contentOverride` / `toolCallsOverride` replace the answer in
the history. The chat starts as a call does: system prompt, then the assistant's greeting.

A run (`POST /eval/run`, target an assistant or a squad's first member) is queued, runs in the background and ends
with `results[0].status` pass when every check passed, as Vapi's do. Tools are never run: the eval says what they
return. Each answer and judgment spends the target's or the judge's model tokens.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from . import netguard

MAX_MESSAGES = 100
MODEL_SECONDS = 30.0
JUDGES = ("exact", "regex", "ai")
NO_TOOL_RESULT = "The eval gave no result for this tool call."


class EvalError(ValueError):
    """An eval Vapi wouldn't take."""


def _tool_calls(value: Any, where: str) -> list[dict[str, Any]] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(t, dict) and isinstance(t.get("name"), str) for t in value):
        raise EvalError(f"{where}.toolCalls is a list of {{name, arguments}}")
    for t in value:
        if "arguments" in t and not isinstance(t["arguments"], dict):
            raise EvalError(f"{where}.toolCalls[].arguments is an object")
    return value


def check_eval(body: dict[str, Any]) -> dict[str, Any]:
    """The eval's own fields, checked. Raises EvalError."""
    if body.get("type", "chat.mockConversation") != "chat.mockConversation":
        raise EvalError('type is "chat.mockConversation"')
    name, description = body.get("name"), body.get("description")
    if name is not None and (not isinstance(name, str) or len(name) > 80):
        raise EvalError("name is up to 80 characters")
    if description is not None and (not isinstance(description, str) or len(description) > 500):
        raise EvalError("description is up to 500 characters")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise EvalError("messages is a list of the conversation's messages")
    if len(messages) > MAX_MESSAGES:
        raise EvalError(f"an eval has at most {MAX_MESSAGES} messages")
    for i, m in enumerate(messages):
        where = f"messages[{i}]"
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant", "system", "tool"):
            raise EvalError(f"{where}.role is user, assistant, system or tool")
        if m.get("content") is not None and not isinstance(m["content"], str):
            raise EvalError(f"{where}.content is text")
        if m["role"] != "assistant":
            if not isinstance(m.get("content"), str):
                raise EvalError(f"{where}: a {m['role']} message needs content")
            continue
        judge = m.get("judgePlan")
        if judge is not None:
            if not isinstance(judge, dict) or judge.get("type") not in JUDGES:
                raise EvalError(f"{where}.judgePlan.type is exact, regex or ai")
            _tool_calls(judge.get("toolCalls"), f"{where}.judgePlan")
            if judge["type"] == "regex":
                for pattern in [judge.get("content"), *(t["name"] for t in judge.get("toolCalls") or [])]:
                    try:
                        re.compile(pattern or "")
                    except re.error as exc:
                        raise EvalError(f"{where}.judgePlan: {pattern!r} is not a pattern ({exc})") from exc
            if judge["type"] in ("exact", "regex") and judge.get("content") is None and not judge.get("toolCalls"):
                raise EvalError(f"{where}.judgePlan needs content or toolCalls to check")
            if judge["type"] == "ai":
                model = judge.get("model")
                if not isinstance(model, dict) or not model.get("provider") or not isinstance(
                        model.get("messages"), list) or not model["messages"]:
                    raise EvalError(f"{where}.judgePlan.model needs provider, model and messages")
        elif m.get("content") is None and not m.get("toolCalls"):
            raise EvalError(f"{where}: an assistant message needs a judgePlan, or content to say")
        cont = m.get("continuePlan")
        if cont is not None:
            if not isinstance(cont, dict):
                raise EvalError(f"{where}.continuePlan is an object")
            _tool_calls(cont.get("toolCallsOverride"), f"{where}.continuePlan")
    return {"type": "chat.mockConversation", "messages": messages,
            **({"name": name} if name else {}), **({"description": description} if description else {})}


def _arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        found = json.loads(raw or "{}")
    except ValueError:
        return {}
    return found if isinstance(found, dict) else {}


async def chat(base: str, model: str, headers: dict[str, str], messages: list[dict[str, Any]],
               tools: list[dict[str, Any]] | None = None) -> tuple[str, list[dict[str, Any]]]:
    """(text, tool calls as {id, name, arguments}) from an OpenAI-compatible chat completions API."""
    body: dict[str, Any] = {"model": model, "messages": messages, "temperature": 0}
    if tools:
        body["tools"] = tools
    async with netguard.client(timeout=MODEL_SECONDS) as client:
        res = await client.post(base.rstrip("/") + "/chat/completions", headers=headers, json=body)
    if res.status_code >= 400:
        raise httpx.HTTPStatusError(f"HTTP {res.status_code}", request=res.request, response=res)
    message = ((res.json().get("choices") or [{}])[0].get("message")) or {}
    calls = [{"id": c.get("id") or f"call_{i}", "name": (c.get("function") or {}).get("name") or "",
              "arguments": _arguments((c.get("function") or {}).get("arguments"))}
             for i, c in enumerate(message.get("tool_calls") or [])]
    return (message.get("content") or "").strip(), calls


def _calls_match(got: list[dict[str, Any]], expected: list[dict[str, Any]], regex: bool) -> str | None:
    """Why the tool calls don't match, or None."""
    if len(got) < len(expected):
        return f"expected {len(expected)} tool call(s), got {len(got)}"
    for want, have in zip(expected, got):
        name_ok = re.fullmatch(want["name"], have["name"]) if regex else want["name"] == have["name"]
        if not name_ok:
            return f"expected a call to {want['name']}, got {have['name'] or 'none'}"
        for key, value in (want.get("arguments") or {}).items():
            if key not in have["arguments"]:
                return f"{have['name']}: argument {key} missing"
            actual = have["arguments"][key]
            same = (re.fullmatch(str(value), str(actual)) is not None) if regex else actual == value
            if not same:
                return f"{have['name']}: {key} was {json.dumps(actual, ensure_ascii=False)}, " \
                       f"expected {json.dumps(value, ensure_ascii=False)}"
    return None


async def judge(plan: dict[str, Any], text: str, calls: list[dict[str, Any]],
                conversation: list[dict[str, Any]]) -> dict[str, Any]:
    """{"status": "pass"|"fail", "failureReason"?} for one answer."""
    kind = plan["type"]
    reason = None
    if kind in ("exact", "regex"):
        expected = plan.get("content")
        if expected is not None:
            if kind == "exact" and text.strip() != expected.strip():
                reason = f"expected {expected!r}, got {text!r}"
            elif kind == "regex" and not re.search(expected, text, re.S):
                reason = f"{text!r} doesn't match {expected!r}"
        if reason is None and plan.get("toolCalls"):
            reason = _calls_match(calls, plan["toolCalls"], kind == "regex")
    else:
        reason = await _ai_judgment(plan["model"], text, calls, conversation)
    return {"status": "fail", "failureReason": reason} if reason else {"status": "pass"}


async def _ai_judgment(model: dict[str, Any], text: str, calls: list[dict[str, Any]],
                       conversation: list[dict[str, Any]]) -> str | None:
    from . import drafts
    from .providers import _llm_headers

    last = {"role": "assistant", "content": text, **({"toolCalls": calls} if calls else {})}
    whole = json.dumps([*conversation, last], ensure_ascii=False)

    def filled(content: str) -> str:
        return content.replace("{{messages[-1]}}", json.dumps(last, ensure_ascii=False)).replace(
            "{{messages}}", whole)

    try:
        provider, name, base, key = drafts.choose(model)
        headers = {**({"Authorization": f"Bearer {key}"} if key else {}), **(_llm_headers(provider) or {})}
        said, _ = await chat(base, name, headers, [{"role": m.get("role") or "system",
                                                    "content": filled(str(m.get("content") or ""))}
                                                   for m in model["messages"]])
    except Exception as exc:
        return f"the judge model couldn't answer ({exc.__class__.__name__})"
    verdict = re.findall(r"\b(pass|fail)\b", said.lower())
    if not verdict:
        return f"the judge answered neither pass nor fail: {said[:200]!r}"
    return None if verdict[0] == "pass" else f"the judge said: {said[:300]}"


def _tools_for(assistant: dict[str, Any], store: Any) -> list[dict[str, Any]]:
    from .tools import assistant_tools, function_schema

    found = []
    for tool in assistant_tools(assistant, store):
        try:
            schema = function_schema(tool)
        except Exception:  # a kind the model can't be shown here; the call would skip it too
            continue
        found.append({"type": "function", "function": {
            "name": schema["name"], "description": schema.get("description") or "",
            "parameters": {"type": "object", "properties": schema.get("properties") or {},
                           "required": schema.get("required") or []}}})
    return found


async def run(eval_doc: dict[str, Any], plan: Any, store: Any) -> dict[str, Any]:
    """The run's outcome: {"status": "ended", "endedReason", "results": [{"status", "messages"}]}."""
    from .analysis import model_for

    try:
        _, model, base, headers = model_for(plan)
    except Exception as exc:
        return {"status": "ended", "endedReason": "assistant-error", "endedMessage": str(exc)[:300], "results": []}
    tools = _tools_for(plan.assistant, store)
    history: list[dict[str, Any]] = [dict(m) for m in plan.messages]
    greeting = plan.assistant.get("firstMessage")
    mode = plan.assistant.get("firstMessageMode") or "assistant-speaks-first"
    if greeting and mode == "assistant-speaks-first":
        history.append({"role": "assistant", "content": greeting})
    shown: list[dict[str, Any]] = []  # what the run reports, as Vapi's results[].messages
    pending: list[str] = []  # tool calls the assistant made that no tool message has answered yet
    passed, ended = True, "mockConversation.done"

    def answer_pending() -> None:
        while pending:
            history.append({"role": "tool", "tool_call_id": pending.pop(0), "content": NO_TOOL_RESULT})

    def said(text: str, calls: list[dict[str, Any]]) -> None:
        message: dict[str, Any] = {"role": "assistant", "content": text or None}
        if calls:
            message["tool_calls"] = [{"id": c["id"], "type": "function", "function": {
                "name": c["name"], "arguments": json.dumps(c.get("arguments") or {})}} for c in calls]
            pending.extend(c["id"] for c in calls)
        history.append(message)

    for i, m in enumerate(eval_doc["messages"]):
        role = m["role"]
        if role == "tool":
            if pending:
                history.append({"role": "tool", "tool_call_id": pending.pop(0), "content": m["content"]})
            else:
                answer_pending()
                history.append({"role": "user", "content": m["content"]})
            shown.append({"role": "tool", "content": m["content"]})
            continue
        if role in ("user", "system"):
            answer_pending()
            history.append({"role": role, "content": m["content"]})
            shown.append({"role": role, "content": m["content"]})
            continue
        answer_pending()
        judge_plan = m.get("judgePlan")
        if judge_plan is None:  # a scripted answer
            calls = [{"id": f"scripted_{i}_{n}", **c} for n, c in enumerate(m.get("toolCalls") or [])]
            said(m.get("content") or "", calls)
            shown.append({"role": "assistant", "content": m.get("content") or "",
                          **({"toolCalls": m["toolCalls"]} if m.get("toolCalls") else {})})
            continue
        try:
            text, calls = await chat(base, model, headers, history, tools)
        except Exception as exc:
            why = f"the assistant's model couldn't answer ({exc.__class__.__name__})"
            shown.append({"role": "assistant", "content": "", "judge": {"status": "fail", "failureReason": why}})
            passed, ended = False, "assistant-error"
            break
        verdict = await judge(judge_plan, text, calls, [x for x in shown if x["role"] != "system"])
        shown.append({"role": "assistant", "content": text,
                      **({"toolCalls": [{"name": c["name"], "arguments": c["arguments"]} for c in calls]}
                         if calls else {}), "judge": verdict})
        cont = m.get("continuePlan") or {}
        if verdict["status"] == "fail":
            passed = False
            if cont.get("exitOnFailureEnabled"):
                break
            if cont.get("contentOverride") is not None or cont.get("toolCallsOverride"):
                text = cont.get("contentOverride") or ""
                calls = [{"id": f"override_{i}_{n}", **c} for n, c in enumerate(cont.get("toolCallsOverride") or [])]
        said(text, calls)
    return {"status": "ended", "endedReason": ended,
            "results": [{"status": "pass" if passed else "fail", "messages": shown}]}
