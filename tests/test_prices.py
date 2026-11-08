"""List prices: a dollar figure for every call, and the order of "cheapest" model routes."""

import json

import pytest

from openring import prices
from openring.bot import Usage


@pytest.fixture(autouse=True)
def shipped_prices_only(monkeypatch, tmp_path):
    monkeypatch.setattr(prices, "USER_FILE", tmp_path / "prices.json")
    prices.reload()
    yield
    prices.reload()


def test_model_prices_come_from_the_shipped_list():
    assert prices.llm("groq", "openai/gpt-oss-20b") == (0.075, 0.3)  # per 1M tokens: input, output
    assert prices.llm("openrouter", "openai/gpt-4.1-mini") is not None
    assert prices.llm("ollama", "anything") == (0.0, 0.0)  # runs on this computer
    assert prices.llm("groq", "not-a-model") is None  # unknown is unknown, never a guess
    assert prices.blended("groq", "openai/gpt-oss-20b") == pytest.approx(0.075 * 0.75 + 0.3 * 0.25)


def test_speech_and_voice_prices():
    assert prices.stt("deepgram", "nova-3") == 0.0077  # per minute
    assert prices.stt("deepgram", "nova-3", "multi") == 0.0092  # multilingual costs more
    assert prices.stt("whisper", "base") == 0.0
    assert prices.tts("elevenlabs", "eleven_flash_v2_5") == 40.0  # per 1M characters, either spelling
    assert prices.tts("11labs", "eleven_multilingual_v2") == 80.0
    assert prices.tts("kokoro", None) == 0.0
    assert prices.tts("inworld", "inworld-tts-2") == 25.0
    assert prices.tts("rime-ai", "coda") == 50.0


def test_your_own_prices_override_the_list(tmp_path):
    (tmp_path / "prices.json").write_text(json.dumps({
        "llm": {"custom-llm": {"my-model": [0.2, 0.6]}, "groq": {"openai/gpt-oss-20b": [1, 2]}},
        "tts": {"rime-ai": {"*": 30}},
    }), encoding="utf-8")
    prices.reload()
    assert prices.llm("custom-llm", "my-model") == (0.2, 0.6)
    assert prices.llm("groq", "openai/gpt-oss-20b") == (1.0, 2.0)
    assert prices.tts("rime-ai", "coda") == 30.0
    assert "my-model" in prices.table()["llm"]["custom-llm"]


def test_a_broken_price_file_is_ignored(tmp_path):
    (tmp_path / "prices.json").write_text("{not json", encoding="utf-8")
    prices.reload()
    assert prices.llm("groq", "openai/gpt-oss-20b") == (0.075, 0.3)


class Service:
    def __init__(self, name, layer, provider, model, language=None):
        self.name, self.openring_label = name, f"{provider} {model}"
        self.openring_price = (layer, provider, model, language)


def test_a_call_is_priced_per_provider_it_used():
    from pipecat.metrics.metrics import (
        LLMTokenUsage,
        LLMUsageMetricsData,
        STTUsage,
        STTUsageMetricsData,
        TTSUsageMetricsData,
    )

    services = [Service("stt#1", "transcriber", "deepgram", None, "multi"),
                Service("llm#1", "model", "groq", "openai/gpt-oss-20b"),
                Service("llm#2", "model", "openai", "gpt-4.1-mini"),  # took over halfway
                Service("tts#1", "voice", "rime-ai", None)]  # no known price
    usage = Usage()
    usage.add([
        STTUsageMetricsData(processor="stt#1", model="nova-3", value=STTUsage(audio_seconds=120)),
        LLMUsageMetricsData(processor="llm#1", value=LLMTokenUsage(prompt_tokens=10_000, completion_tokens=500,
                                                                   total_tokens=10_500)),
        LLMUsageMetricsData(processor="llm#2", value=LLMTokenUsage(prompt_tokens=4_000, completion_tokens=200,
                                                                   total_tokens=4_200)),
        TTSUsageMetricsData(processor="tts#1", value=900),
    ])
    cost = usage.cost_breakdown(services)
    llm = (10_000 * 0.075 + 500 * 0.3) / 1e6 + (4_000 * 0.4 + 200 * 1.6) / 1e6
    assert cost["llm"] == pytest.approx(llm, abs=1e-6)
    assert cost["stt"] == pytest.approx(2 * 0.0092)  # two minutes, multilingual
    assert cost["tts"] == 0 and cost["unpriced"] == ["rime-ai None"]
    assert cost["total"] == pytest.approx(cost["llm"] + cost["stt"], abs=1e-6)
    assert cost["llmPromptTokens"] == 14_000  # the usage fields stay as they were


def test_the_overview_adds_up_what_calls_cost():
    from openring.stats import summarize

    calls = [
        {"createdAt": "2026-11-01T10:00:00Z", "status": "ended", "durationSeconds": 60, "cost": 0.02,
         "costBreakdown": {"total": 0.02}},
        {"createdAt": "2026-11-01T11:00:00Z", "status": "ended", "durationSeconds": 120, "cost": 0.04,
         "costBreakdown": {"total": 0.04, "unpriced": ["rime-ai coda"]}},
        {"createdAt": "2026-11-01T12:00:00Z", "status": "ended", "durationSeconds": 60},  # before calls were priced
    ]
    cost = summarize(calls)["cost"]
    assert cost == {"calls": 2, "total": 0.06, "perMinute": 0.02, "perCall": 0.03, "incomplete": 1}


def test_missing_provider_prices():
    assert prices.stt("gladia", None) is None  # not until its dollar price is on a page we can point to
    assert prices.tts("rime-ai", "coda") == 50.0
    assert prices.tts("rime-ai", "mistv2") is None
    assert prices.tts("smallest-ai", "lightning_v3.1_pro") == 19.5
    assert prices.tts("neuphonic", None) is None


def test_model_specific_price_beats_wildcard():
    assert prices.stt("deepgram", "nova-3", "multi") == 0.0092
    assert prices.tts("11labs", "eleven_multilingual_v2") == 80.0
    assert prices.tts("11labs", "eleven_v3") == 80.0


def test_the_phone_line_is_priced_for_twilio_in_the_us_and_canada():
    assert prices.line("twilio", "inbound", "+16205665371") == 0.0129  # 0.0085 + 0.0044 for Media Streams
    assert prices.line("twilio", "inbound", "+18005551234") == 0.0264  # toll-free
    assert prices.line("twilio", "outbound", "+16205665371", "+15551234567") == 0.0184  # the callee's number counts
    # Only US and Canada are priced: Brazil, other carriers and a call without a number stay unknown, never a guess.
    assert prices.line("twilio", "inbound", "+5511999999999") is None
    assert prices.line("twilio", "outbound", "+16205665371", "+5511999999999") is None
    assert prices.line("twilio", "inbound", None) is None
    assert prices.line("telnyx", "inbound", "+16205665371") is None
    assert prices.line_minutes("twilio", 61) == 2 and prices.line_minutes("twilio", 0) == 0  # a started minute counts
    assert prices.line_minutes("telnyx", 90) == 1.5


def test_your_own_line_rate_applies_to_any_carrier_and_prefix(tmp_path):
    (tmp_path / "prices.json").write_text(json.dumps({
        "line": {"telnyx": {"inbound": 0.01, "outbound:+55": 0.05}, "twilio": {"outbound": 0.02}},
    }), encoding="utf-8")
    prices.reload()
    assert prices.line("telnyx", "inbound", "+442012345678") == 0.01
    assert prices.line("telnyx", "outbound", "+16205665371", "+5511999999999") == 0.05
    assert prices.line("telnyx", "outbound", "+16205665371", "+15551234567") is None  # that rate is for +55 only
    assert prices.line("twilio", "outbound", "+16205665371", "+5511999999999") == 0.02
    assert prices.line("twilio", "outbound", "+16205665371", "+15551234567") == 0.0184  # the shipped one stays


def test_a_calls_line_cost_follows_its_number_and_direction():
    from openring.bot import line_cost
    from openring.store import Store

    store = Store(":memory:")
    ours = store.create("phone_numbers", {"provider": "twilio", "number": "+16205665371"})
    inbound = {"type": "inboundPhoneCall", "phoneNumberId": ours["id"], "phoneCallProvider": "twilio",
               "customer": {"number": "+5511999999999"}}
    cost, carrier = line_cost(inbound, 61, store)
    assert (cost, carrier) == (pytest.approx(2 * 0.0129), "twilio")  # two minutes started, whoever calls
    outbound = {**inbound, "type": "outboundPhoneCall"}
    assert line_cost(outbound, 61, store) == (None, "twilio")  # Brazilian callee: no price
    assert line_cost({"type": "webCall"}, 61, store) is None  # a browser call has no line
    trunk = store.create("phone_numbers", {"provider": "byo-phone-number", "number": "+16205665371"})
    assert line_cost({"type": "inboundPhoneCall", "phoneNumberId": trunk["id"]}, 30, store) == (None, "sip")
