"""When the caller's turn ends (turns.py): a caller who pauses and goes on must not lose what they said after the pause.

The frames below replay real calls (Deepgram on a phone line): one sentence, a short pause, a second sentence, and
transcripts arriving late: the second sentence's 0.45 s after the caller stopped, later than the STT's p99 latency
allows for; the first sentence's last words just after the second one started; a final transcript right behind the
interim that starts a turn. Pipecat's own strategy and queues lost the second sentence each time.
"""

import asyncio
import time
from dataclasses import dataclass, field

import pytest

pytest.importorskip("pipecat.turns.user_stop.turn_analyzer_user_turn_stop_strategy")

from pipecat.audio.turn.base_turn_analyzer import BaseTurnAnalyzer, BaseTurnParams, EndOfTurnState  # noqa: E402
from pipecat.frames.frames import (  # noqa: E402
    DataFrame,
    InterimTranscriptionFrame,
    LLMContextFrame,
    STTMetadataFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext  # noqa: E402
from pipecat.processors.aggregators.llm_response_universal import (  # noqa: E402
    LLMUserAggregator,
    LLMUserAggregatorParams,
)
from pipecat.tests.utils import SleepFrame, run_test  # noqa: E402
from pipecat.turns.user_stop.turn_analyzer_user_turn_stop_strategy import (  # noqa: E402
    TurnAnalyzerUserTurnStopStrategy,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies  # noqa: E402

from openring import turns  # noqa: E402


class Done(BaseTurnAnalyzer):
    """A turn detector that answers each stop in turn: `verdicts` ("incomplete" after a sentence the caller goes
    on from, as on the real call), then complete."""

    def __init__(self, *verdicts):
        super().__init__()
        self.verdicts = list(verdicts)

    @property
    def speech_triggered(self) -> bool:
        return False

    @property
    def params(self) -> BaseTurnParams:
        return BaseTurnParams()

    def append_audio(self, buffer, is_speech):
        return EndOfTurnState.INCOMPLETE

    async def analyze_end_of_turn(self):
        return (self.verdicts.pop(0) if self.verdicts else EndOfTurnState.COMPLETE), None

    def clear(self):
        pass


def said(text):
    return TranscriptionFrame(text, "caller", "2026-11-15T10:00:00Z", finalized=True)


class Started(VADUserStartedSpeakingFrame):
    """A VAD start stamped when it is sent, not when the list of frames was built."""

    def __init__(self):
        super().__init__(start_secs=0.2)

    @property
    def timestamp(self):
        return time.time()

    @timestamp.setter
    def timestamp(self, _value):
        pass


def started():
    return Started()


@dataclass
class Burst(DataFrame):
    """Frames the transcriber pushes back to back, as Deepgram sends an interim and its final together."""

    frames: list = field(default_factory=list)


class Stopped(VADUserStoppedSpeakingFrame):
    """A VAD stop stamped when it is sent, not when the list of frames was built."""

    def __init__(self):
        super().__init__(stop_secs=0.2)

    @property
    def timestamp(self):
        return time.time()

    @timestamp.setter
    def timestamp(self, _value):
        pass


def what_the_model_got(strategy, frames, *, keep=True, stop_timeout=5.0):
    """The user messages of the context the aggregator hands the model, as they were at each turn's end. The frames
    come from a stand-in for the transcriber, through `turns.keep_words` as on a call (unless `keep` is False)."""
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.processors.frame_processor import FrameProcessor

    turns_ended = []

    class Transcriber(FrameProcessor):
        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            for each in (frame.frames if isinstance(frame, Burst) else [frame]):
                await self.push_frame(each, direction)

    transcriber = turns.keep_words(Transcriber()) if keep else Transcriber()

    class Snapshot(FrameProcessor):
        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            if isinstance(frame, LLMContextFrame):
                turns_ended.append([m["content"] for m in frame.context.get_messages() if m.get("role") == "user"])
            await self.push_frame(frame, direction)

    context = LLMContext()
    aggregator = LLMUserAggregator(context, params=LLMUserAggregatorParams(
        user_turn_strategies=UserTurnStrategies(stop=[strategy]), user_turn_stop_timeout=stop_timeout))
    asyncio.run(run_test(Pipeline([transcriber, aggregator, Snapshot()]), frames_to_send=frames))
    return turns_ended


def a_pause_and_a_late_transcript():
    """First sentence, a pause, the second sentence; the second transcript arrives late (STT p99 is 0.35 s)."""
    return [
        STTMetadataFrame(service_name="stt", ttfs_p99_latency=0.35),
        started(), SleepFrame(sleep=0.4), said("Hi, I'm calling for Maria."), Stopped(), SleepFrame(sleep=0.05),
        started(), SleepFrame(sleep=0.3), Stopped(),
        SleepFrame(sleep=0.45),  # the safety net (0.35 - 0.2 s) runs out here
        said("Do you have a table for two tonight?"),
        SleepFrame(sleep=0.5),
    ]


def test_pipecats_own_strategy_ends_the_turn_without_the_second_sentence():
    """The failure this module exists for: if Pipecat fixes it, this test says so and turns.py can go."""
    stock = TurnAnalyzerUserTurnStopStrategy(turn_analyzer=Done(EndOfTurnState.INCOMPLETE))
    got = what_the_model_got(stock, a_pause_and_a_late_transcript())
    assert got and not any("table for two" in " ".join(messages) for messages in got[:1])


def test_the_turn_waits_for_the_transcript_of_the_last_words():
    strategy = turns.fresh_transcript_strategy()(turn_analyzer=Done(EndOfTurnState.INCOMPLETE))
    got = what_the_model_got(strategy, a_pause_and_a_late_transcript())
    assert len(got) == 1, got  # one turn, ended once
    assert "Maria" in " ".join(got[0]) and "table for two" in " ".join(got[0])


def test_speech_that_brings_no_words_ends_the_turn_with_what_was_said_before(monkeypatch):
    monkeypatch.setattr(turns, "GRACE_SECONDS", 0.3)
    strategy = turns.fresh_transcript_strategy()(turn_analyzer=Done(EndOfTurnState.INCOMPLETE))
    frames = [STTMetadataFrame(service_name="stt", ttfs_p99_latency=0.35),
              started(), SleepFrame(sleep=0.4), said("Book me for Friday."), Stopped(), SleepFrame(sleep=0.05),
              started(), SleepFrame(sleep=0.1), Stopped(),  # a cough: no transcript ever comes
              SleepFrame(sleep=0.8)]
    got = what_the_model_got(strategy, frames)
    assert got and "Book me for Friday." in " ".join(got[-1])


def the_end_of_a_sentence_after_the_next_one_started():
    """A real call (Deepgram, 8 kHz): the first sentence's last words came 0.06 s after the second sentence started."""
    return [
        STTMetadataFrame(service_name="stt", ttfs_p99_latency=0.35),
        started(), SleepFrame(sleep=0.5), Stopped(),  # "Hi. I'm an AI assistant calling on behalf of Maria."
        SleepFrame(sleep=0.28), said("Hi. I'm an AI assistant."), said("Calling on behalf"),
        SleepFrame(sleep=0.25), started(),  # "Do you have a table for two tonight at eight?"
        SleepFrame(sleep=0.06), said("of Maria."),  # the end of the first sentence, late
        SleepFrame(sleep=0.6), Stopped(),
        SleepFrame(sleep=0.38), said("Do you have a table for two tonight at eight?"),
        SleepFrame(sleep=0.5),
    ]


def test_pipecats_own_strategy_takes_the_late_end_of_a_sentence_for_the_next_one():
    stock = TurnAnalyzerUserTurnStopStrategy(turn_analyzer=Done(EndOfTurnState.INCOMPLETE))
    got = what_the_model_got(stock, the_end_of_a_sentence_after_the_next_one_started())
    assert got and "table for two" not in " ".join(got[0])


def test_the_late_end_of_a_sentence_counts_with_that_sentence():
    strategy = turns.fresh_transcript_strategy()(turn_analyzer=Done(EndOfTurnState.INCOMPLETE))
    got = what_the_model_got(strategy, the_end_of_a_sentence_after_the_next_one_started())
    assert len(got) == 1, got
    assert "Maria" in " ".join(got[0]) and "table for two" in " ".join(got[0])


def a_final_right_behind_the_interim_that_starts_a_turn():
    """After the turn ended, the caller's next words: their interim and final come together, and the interim starts the
    new turn (a transcript starts a turn), which interrupts."""
    return [
        STTMetadataFrame(service_name="stt", ttfs_p99_latency=0.35),
        started(), SleepFrame(sleep=0.4), said("Hi, I'm calling for Maria."), Stopped(),
        SleepFrame(sleep=0.6),
        Burst(frames=[InterimTranscriptionFrame("Do you have", "caller", "2026-11-15T10:00:01Z"),
                      said("Do you have a table for two?")]),
        SleepFrame(sleep=1.5),
    ]


def test_pipecat_drops_a_final_queued_behind_the_interim_that_starts_a_turn():
    """The failure `keep_words` exists for: if Pipecat stops dropping it, this test says so."""
    strategy = turns.fresh_transcript_strategy()(turn_analyzer=Done())
    got = what_the_model_got(strategy, a_final_right_behind_the_interim_that_starts_a_turn(), keep=False,
                             stop_timeout=0.5)
    assert got and not any("table for two" in " ".join(messages) for messages in got)


def test_no_interruption_drops_what_the_caller_said():
    strategy = turns.fresh_transcript_strategy()(turn_analyzer=Done())
    got = what_the_model_got(strategy, a_final_right_behind_the_interim_that_starts_a_turn(), stop_timeout=0.5)
    assert any("table for two" in " ".join(messages) for messages in got), got


def test_a_call_uses_the_fixed_strategy():
    assert isinstance(turns.stop_strategies()[0], turns.fresh_transcript_strategy())
