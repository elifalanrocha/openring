from pipecat.observers.user_bot_latency_observer import (
    LatencyBreakdown,
    LatencyContribution,
    MeasuredFrom,
)

from openring.bot import Usage, performance_metrics, turn_latency


def _breakdown(total, **parts):
    return LatencyBreakdown(
        measured_from=MeasuredFrom.USER_SILENCE,
        total_secs=total,
        contributions=[
            LatencyContribution(
                key=key, label=key, owner="test", owner_kind="service", start_time=0, duration_secs=secs
            )
            for key, secs in parts.items()
        ],
    )


def test_turn_latency_uses_vapi_fields_in_ms():
    turn = turn_latency(_breakdown(
        1.05, endpointing_wait=0.2, turn_detection=0.05, transcription=0.12,
        llm_inference=0.34, speech_synthesis=0.3, output_transport=0.04,
    ))
    assert turn == {
        "endpointingLatency": 250, "transcriberLatency": 120, "modelLatency": 340,
        "voiceLatency": 300, "turnLatency": 1050,
    }


def test_performance_metrics_averages_each_field():
    turns = [turn_latency(_breakdown(0.9, llm_inference=0.3)), turn_latency(_breakdown(1.1, llm_inference=0.5))]
    metrics = performance_metrics(turns)
    assert metrics["turnLatencies"] == turns
    assert metrics["turnLatencyAverage"] == 1000 and metrics["modelLatencyAverage"] == 400
    assert performance_metrics([]) == {"turnLatencies": []}


def test_performance_metrics_keep_the_wait_for_the_first_word():
    assert performance_metrics([], first_word_ms=850) == {"turnLatencies": [], "firstWordLatency": 850}


def test_usage_adds_up_what_providers_bill_by():
    from pipecat.metrics.metrics import (
        LLMTokenUsage,
        LLMUsageMetricsData,
        STTUsage,
        STTUsageMetricsData,
        TTFBMetricsData,
        TTSUsageMetricsData,
    )

    usage = Usage()
    tokens = LLMTokenUsage(prompt_tokens=120, completion_tokens=30, total_tokens=150)
    usage.add([LLMUsageMetricsData(processor="llm", value=tokens), TTSUsageMetricsData(processor="tts", value=42)])
    usage.add([LLMUsageMetricsData(processor="llm", value=tokens), TTFBMetricsData(processor="llm", value=0.2),
               STTUsageMetricsData(processor="stt", value=STTUsage(audio_seconds=12.34))])
    assert usage.cost_breakdown() == {"llmPromptTokens": 240, "llmCompletionTokens": 60, "ttsCharacters": 42,
                                      "transcriberAudioSeconds": 12.3}


def test_call_limits_end_a_long_call_and_a_silent_caller():
    from openring.bot import CallLimits

    limits = CallLimits(max_duration=600, silence_timeout=30, now=0)
    assert limits.due(29) is None
    assert limits.due(30) == "silence-timed-out"  # nobody said a word since the call started
    limits.user(True, 31)
    assert limits.due(100) is None  # the caller is talking: never cut them off for silence
    limits.user(False, 100)
    limits.bot(True, 101)
    assert limits.due(140) is None  # nor while the assistant talks
    limits.bot(False, 140)
    assert limits.due(169) is None  # however long it talked, the caller gets the whole limit to answer
    assert limits.due(170) == "silence-timed-out"
    limits.model(True, 170)
    assert limits.due(300) is None  # nor while the model works on an answer
    limits.model(False, 300)
    limits.tool("t1", True, 300)
    limits.tool("t2", True, 300)
    limits.tool("t1", False, 400)
    assert limits.due(500) is None  # nor while a tool still runs
    limits.tool("t2", False, 500)
    limits.tool("t2", False, 500)  # a tool's end is seen once per direction
    assert limits.due(529) is None and limits.due(530) == "silence-timed-out"
    limits.model(True, 530)
    limits.user(True, 531)  # a caller who starts talking interrupts the model
    limits.user(False, 540)
    assert limits.due(570) == "silence-timed-out"
    assert limits.due(600) == "exceeded-max-duration"  # the length is a ceiling, whoever is talking
    assert CallLimits(max_duration=0, silence_timeout=0, now=0).due(10**6) is None  # 0 turns a limit off


def test_a_turn_with_only_its_whole_time_counts_only_in_that_average():
    """Pipecat can't always split a turn into stages; the turn's own time still counts, the stages' averages don't
    drop toward zero."""
    full = turn_latency(_breakdown(1.0, llm_inference=0.4))
    metrics = performance_metrics([full, {"turnLatency": 3000}])
    assert metrics["turnLatencyAverage"] == 2000
    assert metrics["modelLatencyAverage"] == full["modelLatency"]
    assert performance_metrics([{"turnLatency": 800}]) == {"turnLatencies": [{"turnLatency": 800}],
                                                           "turnLatencyAverage": 800}


def test_a_voice_with_no_audio_says_the_likely_cause():
    from openring.bot import short_error

    error = "3 consecutive TTS contexts completed with no audio (tts_service.py:2313)"
    assert "premade voice" in short_error(error, "elevenlabs")
    assert short_error(error, "cartesia") == "3 consecutive TTS contexts completed with no audio"
    assert short_error("invalid api key", "elevenlabs") == "invalid api key"
