from datetime import datetime, timezone

from openring.stats import summarize

NOW = datetime(2026, 10, 30, 15, 0, tzinfo=timezone.utc)


def call(created, **fields):
    return {"createdAt": created, "status": "ended", "endedReason": "customer-ended-call", **fields}


def turns(*latencies):
    return {"performanceMetrics": {"turnLatencies": [
        {"turnLatency": t, "endpointingLatency": 200, "transcriberLatency": 100, "modelLatency": 300,
         "voiceLatency": t - 600} for t in latencies]}}


def test_summarize_counts_window_and_latency():
    calls = [
        call("2026-10-30T10:00:00Z", type="webCall", assistantId="a", durationSeconds=90, artifact=turns(1000, 1400)),
        call("2026-10-29T10:00:00Z", type="inboundPhoneCall", assistantId="a", durationSeconds=30,
             artifact=turns(1200)),
        call("2026-10-29T11:00:00Z", type="inboundPhoneCall", assistantId="b", endedReason="pipeline-error: boom"),
        {"createdAt": "2026-10-30T14:59:00Z", "status": "in-progress", "type": "webCall", "assistantId": "b"},
        call("2026-09-01T10:00:00Z", type="webCall", durationSeconds=600),  # outside 7 days
    ]
    s = summarize(calls, days=7, now=NOW)
    assert s["calls"] == 4 and s["inProgress"] == 1 and s["failed"] == 1
    assert s["successRate"] == 67  # 2 of 3 ended calls finished normally
    assert s["minutes"] == 2.0 and s["platformFeeSaved"] == 0.1
    assert s["latencyMedianMs"] == 1200 and s["latencyAverageMs"] == 1200 and s["turns"] == 3
    assert s["latencyParts"]["modelLatency"] == 300
    assert s["byType"] == {"webCall": 2, "inboundPhoneCall": 2}
    assert s["byAssistant"][0] == {"assistantId": "a", "calls": 2, "minutes": 2.0}
    assert len(s["perDay"]) == 7
    assert s["perDay"][-1] == {"date": "2026-10-30", "calls": 2, "minutes": 1.5, "failed": 0, "cost": 0.0}
    assert s["perDay"][-2]["failed"] == 1


def test_summarize_all_time_and_empty():
    assert summarize([], days=None, now=NOW)["perDay"][-1]["date"] == "2026-10-30"
    every = summarize([call("2026-09-01T10:00:00Z", durationSeconds=60)], days=None, now=NOW)
    assert every["calls"] == 1 and every["perDay"][0]["date"] == "2026-09-01"
    empty = summarize([], days=30, now=NOW)
    assert empty["successRate"] is None and empty["latencyMedianMs"] is None and len(empty["perDay"]) == 30


def test_summarize_usage_first_word_and_transfers():
    usage = {"llmPromptTokens": 900, "llmCompletionTokens": 100, "ttsCharacters": 300, "transcriberAudioSeconds": 60}
    calls = [
        call("2026-10-30T10:00:00Z", costBreakdown=usage,
             artifact={"performanceMetrics": {"turnLatencies": [], "firstWordLatency": 800}}),
        call("2026-10-30T11:00:00Z", costBreakdown=usage, endedReason="assistant-forwarded-call",
             artifact={"performanceMetrics": {"turnLatencies": [], "firstWordLatency": 1200}}),
        call("2026-10-30T12:00:00Z", artifact=turns(*range(1000, 2000, 50))),  # an older call: no usage recorded
    ]
    s = summarize(calls, days=7, now=NOW)
    assert s["usage"] == {"calls": 2, "llmPromptTokens": 1800, "llmCompletionTokens": 200, "ttsCharacters": 600,
                          "transcriberAudioSeconds": 120.0, "tokensPerCall": 1000}
    assert s["firstWordMedianMs"] == 1000 and s["transferRate"] == 33
    assert s["latencyP95Ms"] == 1900  # 20 turns from 1000 to 1950 ms: the 19th
    empty = summarize([], days=7, now=NOW)
    assert empty["usage"]["tokensPerCall"] is None and empty["latencyP95Ms"] is None


def test_stage_averages_leave_out_turns_without_stages():
    from openring import stats

    turns = [{"turnLatency": 1000, "modelLatency": 400}, {"turnLatency": 2000}]
    assert stats._part_average(turns, "modelLatency") == 400
    assert stats._part_average([{"turnLatency": 900}], "modelLatency") is None


def test_presets_come_with_their_cost_and_only_measured_speeds():
    from test_api import AUTH, make_client

    client = make_client()
    got = client.get("/assistant/presets", headers=AUTH)
    assert got.status_code == 200  # not taken for an assistant called "presets"
    presets = {p["id"]: p for p in got.json()}
    assert list(presets) == ["balanced", "high-intelligence", "ultra-fast", "cost-saver"]
    fast, saver = presets["ultra-fast"], presets["cost-saver"]
    assert fast["latency"]["turn"] == 210 + 160 + 750 + 280  # measured on OpenRing's own test calls
    assert presets["balanced"]["latency"]["model"] is None or presets["balanced"]["latency"]["turn"]  # unknown stays
    assert saver["totalPerMinute"] < fast["totalPerMinute"]  # free transcriber and voice
    assert all(p["missingKeys"] == sorted(p["missingKeys"]) for p in presets.values())


def test_calls_can_be_listed_for_one_assistant():
    from fastapi.testclient import TestClient

    from openring.api import create_app
    from openring.config import Settings
    from openring.store import Store

    from test_api import AUTH

    store = Store(":memory:")
    client = TestClient(create_app(Settings(api_key="test-key", public_url="", db_path=":memory:"), store))
    store.create("calls", {"assistantId": "a1", "status": "ended"})
    store.create("calls", {"assistantId": "a2", "status": "ended"})
    mine = client.get("/call?assistantId=a1", headers=AUTH).json()
    assert [c["assistantId"] for c in mine] == ["a1"] and len(client.get("/call", headers=AUTH).json()) == 2



def test_each_day_says_what_its_calls_cost_and_the_overview_can_be_one_assistants():
    from test_api import AUTH, make_client

    s = summarize([call(NOW.isoformat(), cost=0.25), call(NOW.isoformat(), cost=0.5)], 7, NOW)
    assert s["perDay"][-1]["cost"] == 0.75
    assert make_client().get("/stats?assistantId=nobody", headers=AUTH).json()["calls"] == 0
