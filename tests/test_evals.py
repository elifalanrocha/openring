"""Evals (evals.py, /eval, /eval/run): a scripted chat with an assistant and checks on its answers, as on Vapi."""

import asyncio
import json

import httpx
import pytest

from openring import evals
from openring.api import create_app
from openring.config import Settings
from openring.providers import resolve_plan
from openring.store import Store

from test_api import ASSISTANT, AUTH

BOOKING_TOOL = {"type": "function", "function": {"name": "book", "description": "Book a table", "parameters": {
    "type": "object", "properties": {"people": {"type": "integer"}, "time": {"type": "string"}},
    "required": ["people"]}}}


BOOK_TWO = [{"name": "book", "arguments": {"people": 2}}]


def assistant(**extra):
    return {**ASSISTANT, "model": {**ASSISTANT["model"], "model": "openai/gpt-oss-20b",
                                   "tools": [BOOKING_TOOL]}, **extra}


def scripted_model(monkeypatch, answers):
    """The assistant's (and the judge's) model: each request gets the next answer; records what each was sent."""
    sent, queue = [], list(answers)

    async def chat(base, model, headers, messages, tools=None):
        sent.append({"model": model, "messages": json.loads(json.dumps(messages)), "tools": tools})
        answer = queue.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(evals, "chat", chat)
    return sent


def run(eval_body, monkeypatch, answers, **assistant_extra):
    monkeypatch.setenv("GROQ_API_KEY", "k")
    sent = scripted_model(monkeypatch, answers)
    plan = resolve_plan(assistant(**assistant_extra), check_keys=False)
    outcome = asyncio.run(evals.run(evals.check_eval(eval_body), plan, Store(":memory:")))
    return outcome, sent


@pytest.mark.parametrize(("body", "why"), [
    ({"messages": []}, "messages"),
    ({"type": "chat.other", "messages": [{"role": "user", "content": "hi"}]}, "type"),
    ({"messages": [{"role": "robot", "content": "hi"}]}, "role"),
    ({"messages": [{"role": "user"}]}, "needs content"),
    ({"messages": [{"role": "assistant", "judgePlan": {"type": "vibes"}}]}, "judgePlan.type"),
    ({"messages": [{"role": "assistant", "judgePlan": {"type": "regex", "content": "("}}]}, "not a pattern"),
    ({"messages": [{"role": "assistant", "judgePlan": {"type": "exact"}}]}, "content or toolCalls"),
    ({"messages": [{"role": "assistant", "judgePlan": {"type": "ai", "model": {"provider": "groq"}}}]}, "model"),
    ({"messages": [{"role": "assistant"}]}, "judgePlan, or content"),
])
def test_an_eval_vapi_refuses_is_refused(body, why):
    with pytest.raises(evals.EvalError, match=why):
        evals.check_eval(body)


def test_the_assistant_answers_with_its_prompt_greeting_and_tools_and_is_checked(monkeypatch):
    body = {"messages": [
        {"role": "user", "content": "A table for two at eight, please."},
        {"role": "assistant", "judgePlan": {"type": "exact", "toolCalls": BOOK_TWO}},
        {"role": "tool", "content": '{"booked": true}'},
        {"role": "assistant", "judgePlan": {"type": "regex", "content": "(?i)booked|confirmed"}},
    ]}
    outcome, sent = run(body, monkeypatch, [
        ("", [{"id": "call_1", "name": "book", "arguments": {"people": 2, "time": "20:00"}}]),
        ("Your table is booked for eight.", []),
    ])
    assert outcome["endedReason"] == "mockConversation.done" and outcome["results"][0]["status"] == "pass"
    first = sent[0]
    assert first["model"] == "openai/gpt-oss-20b" and first["tools"][0]["function"]["name"] == "book"
    roles = [m["role"] for m in first["messages"]]
    assert roles == ["system", "assistant", "user"]  # the system prompt, the greeting, then the caller
    assert first["messages"][1]["content"] == ASSISTANT["firstMessage"]
    tool_answer = sent[1]["messages"][-1]
    assert tool_answer == {"role": "tool", "tool_call_id": "call_1", "content": '{"booked": true}'}
    shown = outcome["results"][0]["messages"]
    assert [m["role"] for m in shown] == ["user", "assistant", "tool", "assistant"]
    assert shown[1]["toolCalls"] == [{"name": "book", "arguments": {"people": 2, "time": "20:00"}}]
    assert all(m["judge"] == {"status": "pass"} for m in shown if m["role"] == "assistant")


def test_a_failed_check_says_why_and_the_plan_decides_what_happens_next(monkeypatch):
    body = {"messages": [
        {"role": "user", "content": "Table for two."},
        {"role": "assistant", "judgePlan": {"type": "exact", "toolCalls": BOOK_TWO},
         "continuePlan": {"toolCallsOverride": [{"name": "book", "arguments": {"people": 2}}]}},
        {"role": "tool", "content": "booked"},
        {"role": "assistant", "judgePlan": {"type": "exact", "content": "Done."},
         "continuePlan": {"exitOnFailureEnabled": True}},
        {"role": "user", "content": "never said"},
        {"role": "assistant", "judgePlan": {"type": "regex", "content": "."}},
    ]}
    outcome, sent = run(body, monkeypatch, [
        ("", [{"id": "c1", "name": "book", "arguments": {"people": 3}}]),
        ("All done!", []),
    ])
    result = outcome["results"][0]
    assert result["status"] == "fail" and len(sent) == 2  # stopped at the second failure
    judged = [m["judge"] for m in result["messages"] if "judge" in m]
    assert "people was 3, expected 2" in judged[0]["failureReason"]
    assert "expected 'Done.'" in judged[1]["failureReason"]
    override = sent[1]["messages"][-2]  # the override replaced the wrong call in the history
    assert json.loads(override["tool_calls"][0]["function"]["arguments"]) == {"people": 2}


def test_an_ai_judge_reads_the_conversation_and_says_pass_or_fail(monkeypatch):
    judge = {"type": "ai", "model": {"provider": "groq", "model": "judge-model", "messages": [
        {"role": "system", "content": "Pass if the last answer is polite. Last: {{messages[-1]}} All: {{messages}}"}]}}
    body = {"messages": [{"role": "user", "content": "Hello"}, {"role": "assistant", "judgePlan": judge},
                         {"role": "user", "content": "Bye"}, {"role": "assistant", "judgePlan": judge}]}
    outcome, sent = run(body, monkeypatch, [("Good evening!", []), ("PASS", []), ("Whatever.", []),
                                            ("fail: rude", [])])
    asked = sent[1]
    assert asked["model"] == "judge-model" and '"Good evening!"' in asked["messages"][0]["content"]
    assert '"Hello"' in asked["messages"][0]["content"]
    judged = [m["judge"] for m in outcome["results"][0]["messages"] if "judge" in m]
    assert judged[0] == {"status": "pass"} and "rude" in judged[1]["failureReason"]


def test_a_model_that_fails_ends_the_run_with_the_reason(monkeypatch):
    body = {"messages": [{"role": "user", "content": "Hi"}, {"role": "assistant", "judgePlan": {
        "type": "regex", "content": "."}}]}
    outcome, _ = run(body, monkeypatch, [httpx.ConnectError("down")])
    assert outcome["endedReason"] == "assistant-error" and outcome["results"][0]["status"] == "fail"


def test_an_eval_is_saved_run_against_an_assistant_and_read_back(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "k")
    scripted_model(monkeypatch, [("Good evening, how many people?", [])])
    store = Store(":memory:")
    app = create_app(Settings(api_key="test-key", public_url="", db_path=":memory:"), store)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
            a = (await http.post("/assistant", json=assistant(), headers=AUTH)).json()
            e = (await http.post("/eval", headers=AUTH, json={"name": "asks how many", "messages": [
                {"role": "user", "content": "I'd like a table"},
                {"role": "assistant", "judgePlan": {"type": "regex", "content": "(?i)how many"}}]})).json()
            assert (await http.post("/eval", json={"messages": []}, headers=AUTH)).status_code == 400
            listed = (await http.get("/eval", headers=AUTH)).json()
            run = (await http.post("/eval/run", headers=AUTH, json={
                "type": "eval", "evalId": e["id"], "target": {"type": "assistant", "assistantId": a["id"]}})).json()
            assert run.get("status") == "queued", run
            for _ in range(50):
                done = (await http.get(f"/eval/run/{run['id']}", headers=AUTH)).json()
                if done["status"] == "ended":
                    break
                await asyncio.sleep(0.05)
            runs = (await http.get(f"/eval/run?evalId={e['id']}", headers=AUTH)).json()
            assert (await http.get("/eval", headers={})).status_code == 401
            return listed, done, runs

    listed, done, runs = asyncio.run(scenario())
    assert listed["results"][0]["name"] == "asks how many"
    assert done["endedReason"] == "mockConversation.done" and done["results"][0]["status"] == "pass"
    assert done["target"] == {"type": "assistant", "assistantId": done["target"]["assistantId"]}
    assert runs["metadata"]["totalItems"] == 1
