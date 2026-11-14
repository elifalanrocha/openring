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
    assert len(s["perDay"]) == 7 and s["perDay"][-1] == {"date": "2026-10-30", "calls": 2, "minutes": 1.5, "failed": 0}
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
