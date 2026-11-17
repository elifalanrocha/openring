"""After the call (analysis.py): Vapi's analysisPlan, summary, structured data and success evaluation."""

import asyncio
import json
import time

import httpx
import pytest

from openring import analysis, netguard
from openring.providers import resolve_plan

from test_api import ASSISTANT, AUTH, make_client

SCHEMA = {"type": "object", "properties": {"name": {"type": "string"}, "partySize": {"type": "integer"}}}
CALL = {"id": "c1", "endedReason": "customer-ended-call", "messages": [
    {"role": "assistant", "message": "Good evening, Luna."},
    {"role": "user", "message": "A table for two at eight, name Maria."},
    {"role": "assistant", "message": "Booked for two at eight."},
]}


def plan_with(analysis_plan, **assistant):
    return resolve_plan({**ASSISTANT, **assistant, "analysisPlan": analysis_plan}, check_keys=False)


def test_no_plan_no_analysis_and_vapis_defaults_inside_one():
    assert analysis.plan_of(ASSISTANT) is None  # it spends your tokens: only when asked
    parts = analysis.plan_of({"analysisPlan": {}})["parts"]
    assert set(parts) == {"summary", "successEvaluation"} and parts["successEvaluation"]["rubric"] == "PassFail"
    parts = analysis.plan_of({"analysisPlan": {"summaryPlan": {"enabled": False},
                                               "successEvaluationPlan": {"enabled": False},
                                               "structuredDataPlan": {"enabled": True, "schema": SCHEMA}}})["parts"]
    assert set(parts) == {"structuredData"}
    assert analysis.plan_of({"analysisPlan": {"summaryPlan": {"enabled": False},
                                              "successEvaluationPlan": {"enabled": False}}}) is None


@pytest.mark.parametrize("bad", [
    "yes", {"summaryPlan": {"timeoutSeconds": 0}}, {"structuredDataPlan": {"enabled": True}},
    {"successEvaluationPlan": {"rubric": "Vibes"}}, {"summaryPlan": {"messages": "summarize"}},
    {"minMessagesThreshold": -1},
])
def test_a_plan_vapi_refuses_is_refused_on_save(bad):
    client = make_client()
    r = client.post("/assistant", json={**ASSISTANT, "analysisPlan": bad}, headers=AUTH)
    assert r.status_code == 400, r.text


def test_vapis_template_variables_are_filled():
    filled = analysis.fill([{"role": "system", "content": "{{rubric}} / {{ schema }}"},
                            {"role": "user", "content": "{{transcript}} ({{endedReason}}) {{systemPrompt}} {{other}}"}],
                           {"rubric": "R", "schema": "S", "transcript": "T", "endedReason": "E", "systemPrompt": "P"})
    assert [m["content"] for m in filled] == ["R / S", "T (E) P {{other}}"]
    assert analysis.transcript_text(CALL["messages"]).splitlines()[:2] == [
        "AI: Good evening, Luna.", "User: A table for two at eight, name Maria."]


def model_server(monkeypatch, answer):
    """A pretend chat completions API: `answer(system prompt)` is what the model says; records what it was sent."""
    sent = []

    def handle(request):
        body = json.loads(request.content)
        sent.append((str(request.url), body))
        said = answer(body["messages"][0]["content"])
        if isinstance(said, Exception):
            raise said
        return httpx.Response(200, json={"choices": [{"message": {"content": said}}],
                                         "usage": {"prompt_tokens": 1000, "completion_tokens": 100}})

    monkeypatch.setattr(netguard, "client", lambda **o: httpx.AsyncClient(
        transport=netguard.GuardedTransport(httpx.MockTransport(handle)), **{k: v for k, v in o.items()}))
    return sent


def answers(system):
    if "take notes" in system:
        return "Maria booked a table for two at eight."
    if "extract data" in system:
        return 'Here it is:\n```json\n{"name": "Maria", "partySize": 2}\n```'
    return "True: the table was booked."


def test_the_call_is_summarized_its_data_pulled_out_and_judged(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "k")
    sent = model_server(monkeypatch, answers)
    plan = plan_with({"structuredDataPlan": {"enabled": True, "schema": SCHEMA}})
    found = asyncio.run(analysis.analyze(plan, CALL))
    assert found["analysis"] == {"summary": "Maria booked a table for two at eight.",
                                 "structuredData": {"name": "Maria", "partySize": 2}, "successEvaluation": "true"}
    assert len(sent) == 3 and all(url.endswith("/chat/completions") for url, _ in sent)
    assert {body["model"] for _, body in sent} == {ASSISTANT["model"]["model"]}  # the assistant's own model
    extract = next(body for _, body in sent if "extract data" in body["messages"][0]["content"])
    assert '"partySize"' in extract["messages"][0]["content"] and "Maria" in extract["messages"][1]["content"]
    judge = next(body for _, body in sent if "evaluate" in body["messages"][0]["content"])
    assert "Você é a recepcionista." in judge["messages"][1]["content"]  # the system prompt, for the objectives
    assert found["costs"]["summaryPromptTokens"] == 1000 and found["costs"]["summaryCompletionTokens"] == 100


def test_a_part_that_fails_is_left_out_and_short_calls_are_not_analyzed(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "k")
    model_server(monkeypatch, lambda s: httpx.ConnectError("down") if "take notes" in s else "no json here")
    found = asyncio.run(analysis.analyze(plan_with({"structuredDataPlan": {"enabled": True, "schema": SCHEMA}}), CALL))
    assert found["analysis"] == {}  # summary failed, no JSON came, and "no json here" says neither true nor false
    plan = plan_with({"minMessagesThreshold": 5})
    assert asyncio.run(analysis.analyze(plan, CALL)) is None


def test_a_slow_model_doesnt_hold_the_report_past_its_timeout(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "k")

    async def slow(*_args, **_kw):
        await asyncio.sleep(5)

    monkeypatch.setattr(analysis, "complete", slow)
    started = time.monotonic()
    only_summary = {"summaryPlan": {"timeoutSeconds": 1}, "successEvaluationPlan": {"enabled": False}}
    found = asyncio.run(analysis.analyze(plan_with(only_summary), CALL))
    assert time.monotonic() - started < 3 and "summary" not in found["analysis"]


@pytest.mark.parametrize(("rubric", "said", "expected"), [
    ("PassFail", "false. The caller hung up.", "false"), ("PassFail", "PASS", "true"),
    ("NumericScale", "8", "8"), ("DescriptiveScale", "Good", "Good"),
])
def test_success_is_given_as_vapi_gives_it(rubric, said, expected):
    assert analysis.verdict(said, rubric) == expected


def test_the_report_carries_the_analysis_and_the_call_its_cost(monkeypatch, quiet_providers):
    from fastapi.testclient import TestClient

    from openring import bot, server_messages
    from openring.api import create_app
    from openring.config import Settings
    from openring.store import Store

    from test_call_flow import _handshake, _stream_params, _wait_until_ended

    for key in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.setenv(key, "fake")
    reports = []

    async def record(server, payload, **_kw):
        reports.append(payload["message"])
        return httpx.Response(200)

    monkeypatch.setattr(server_messages, "post", record)

    async def complete(base, model, headers, messages, timeout):
        return "A short call.", {"prompt": 2_000_000, "completion": 0}

    monkeypatch.setattr(analysis, "complete", complete)
    monkeypatch.setattr(analysis, "transcript_text", lambda m: "AI: hi")
    monkeypatch.setattr(bot, "transcript_messages", lambda conversation: CALL["messages"])
    store = Store(":memory:")
    client = TestClient(create_app(Settings(api_key="test-key", public_url="https://x.test", db_path=":memory:",
                                            validate_twilio_signature=False), store))
    model = {**ASSISTANT["model"], "model": "openai/gpt-oss-20b"}  # priced, unlike Llama 3.3 (gone from Groq)
    assistant = client.post("/assistant", json={**ASSISTANT, "model": model, "server": {"url": "https://hooks.test/x"},
                                                "analysisPlan": {"successEvaluationPlan": {"enabled": False}}},
                            headers=AUTH).json()
    client.post("/phone-number", json={"number": "+15551234567", "assistantId": assistant["id"]}, headers=AUTH)
    params = _stream_params(client.post("/twilio/voice", data={"To": "+15551234567", "From": "+5511",
                                                                "CallSid": "CA1"}).text)
    with client.websocket_connect("/ws/twilio") as ws:
        _handshake(ws, params)
        time.sleep(2)
        ws.send_text(json.dumps({"event": "stop", "streamSid": "MZ1"}))
    call = _wait_until_ended(store, params["callId"])
    for _ in range(300):  # up to 30 s: the analysis runs after the call ends, and CI's runners are slow
        if any(r["type"] == "end-of-call-report" for r in reports):
            break
        time.sleep(0.1)
    call = store.get("calls", params["callId"])
    report = next(r for r in reports if r["type"] == "end-of-call-report")
    assert report["analysis"] == {"summary": "A short call."} == call["analysis"]
    spent = call["costBreakdown"]["analysis"]  # 2M prompt tokens of Groq's gpt-oss-20b at its list price
    assert spent > 0 and call["costBreakdown"]["analysisCostBreakdown"]["summaryPromptTokens"] == 2_000_000
    assert call["cost"] == call["costBreakdown"]["total"]


# ---- Structured outputs (/structured-output) ------------------------------------------------------------------------

def test_a_structured_output_vapi_refuses_is_refused():
    for bad, why in (({"schema": SCHEMA}, "name"), ({"name": "x"}, "schema"), ({"name": "x", "schema": SCHEMA,
                     "type": "llm"}, "type"), ({"name": "x", "schema": SCHEMA, "type": "regex"}, "regex"),
                     ({"name": "x", "schema": SCHEMA, "type": "regex", "regex": "("}, "pattern"),
                     ({"name": "x", "schema": SCHEMA, "model": "gpt"}, "model")):
        with pytest.raises(analysis.AnalysisError, match=why):
            analysis.check_output(bad)


def test_outputs_come_from_the_assistants_list_and_from_those_that_name_it():
    from openring.store import Store

    store = Store(":memory:")
    a = store.create("assistants", {"name": "a"})
    listed = store.create("structured_outputs", {"name": "listed", "schema": SCHEMA})
    naming = store.create("structured_outputs", {"name": "naming", "schema": SCHEMA, "assistantIds": [a["id"]]})
    store.create("structured_outputs", {"name": "other", "schema": SCHEMA, "assistantIds": ["someone-else"]})
    found = analysis.outputs_for({**a, "artifactPlan": {"structuredOutputIds": [listed["id"], "gone"]}}, store)
    assert sorted(o["name"] for o in found) == ["listed", "naming"] and naming["id"] in {o["id"] for o in found}


def test_each_output_is_pulled_out_by_the_model_or_a_pattern(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "k")
    sent = model_server(monkeypatch, lambda system: '{"result": 2}' if "partySize" in system else '{"result": null}')
    outputs = [{"id": "o1", "name": "party", "schema": {"type": "integer", "description": "partySize"}},
               {"id": "o2", "name": "name", "type": "regex", "regex": r"name (\w+)", "schema": {"type": "string"}},
               {"id": "o3", "name": "said eight", "type": "regex", "regex": "eight", "schema": {"type": "boolean"}},
               {"id": "o4", "name": "allergy", "schema": {"type": "string"}}]
    found = asyncio.run(analysis.extract(outputs, plan_with({}), CALL))
    results = {i: o["result"] for i, o in found["structuredOutputs"].items()}
    assert results == {"o1": 2, "o2": "Maria", "o3": True, "o4": None}
    assert found["structuredOutputs"]["o1"]["name"] == "party"
    assert len(sent) == 2  # patterns need no model
    assert found["costs"]["structuredOutputsPromptTokens"] == 2000


def output_app(monkeypatch):
    from openring.api import create_app
    from openring.config import Settings
    from openring.store import Store

    store = Store(":memory:")
    monkeypatch.setenv("GROQ_API_KEY", "k")
    app = create_app(Settings(api_key="test-key", public_url="", db_path=":memory:"), store)
    return store, app


def test_outputs_are_saved_linked_and_run_on_past_calls(monkeypatch):
    model_server(monkeypatch, lambda system: '{"result": 2}')
    store, app = output_app(monkeypatch)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
            model = {**ASSISTANT["model"], "model": "openai/gpt-oss-20b"}
            out = (await http.post("/structured-output", headers=AUTH, json={
                "name": "partySize", "schema": {"type": "integer"}})).json()
            bad = await http.post("/assistant", headers=AUTH, json={**ASSISTANT, "artifactPlan": {
                "structuredOutputIds": ["nope"]}})
            a = (await http.post("/assistant", headers=AUTH, json={**ASSISTANT, "model": model, "artifactPlan": {
                "structuredOutputIds": [out["id"]]}})).json()
            call = store.create("calls", {**CALL, "status": "ended", "assistantId": a["id"],
                                          "artifact": {"messages": CALL["messages"]}})
            preview = (await http.post("/structured-output/run", headers=AUTH, json={
                "structuredOutputId": out["id"], "callIds": [call["id"]], "previewEnabled": True})).json()
            untouched = store.get("calls", call["id"])
            ran = await http.post("/structured-output/run", headers=AUTH, json={
                "structuredOutputId": out["id"], "callIds": [call["id"]]})
            for _ in range(200):
                if (store.get("calls", call["id"])["artifact"]).get("structuredOutputs"):
                    break
                await asyncio.sleep(0.05)
            listed = (await http.get("/structured-output", headers=AUTH)).json()
            return bad, preview, untouched, ran, store.get("calls", call["id"]), listed, out

    bad, preview, untouched, ran, call, listed, out = asyncio.run(scenario())
    assert bad.status_code == 400 and "not found" in bad.text
    assert preview["structuredOutputs"] == {out["id"]: {"name": "partySize", "result": 2}}
    assert "structuredOutputs" not in untouched["artifact"]  # a preview changes nothing
    assert ran.status_code == 200 and call["artifact"]["structuredOutputs"][out["id"]]["result"] == 2
    assert call["artifact"]["messages"] == CALL["messages"]  # the rest of the artifact stays
    assert call["costBreakdown"]["analysisCostBreakdown"]["structuredOutputsPromptTokens"] == 1000
    assert listed["results"][0]["name"] == "partySize"
