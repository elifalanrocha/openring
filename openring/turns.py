"""When the caller's turn ends: Pipecat's smart-turn strategy, waiting for the transcript of the caller's last words.

Pipecat 1.15's TurnAnalyzerUserTurnStopStrategy ends a turn once its turn detector says the caller is done and it has
"a transcript". The transcript it keeps is any from the turn: when the caller pauses between two sentences, says the
second, and stops, the first sentence's transcript is still in hand, the safety-net timer (the STT's p99 latency, about
0.15 s on a phone line) runs out, and the turn ends before the second sentence's transcript arrives. The model answers
the first sentence alone, the late transcript reopens the turn, and the second sentence is lost. Found on a real call
(Deepgram, 8 kHz): "Do you have a table for two tonight at eight?" never reached the model.

Here a caller who speaks again needs the transcript of that speech: it is cleared when speech starts. If the new speech
brings no words at all (a cough, a noise), `GRACE_SECONDS` after it ends the turn goes on with what was said before.

Three more ways the words went missing on real calls:

- The last words of a sentence can come from the transcriber just after the caller starts the next one ("Calling on
  behalf" ... speech starts ... "of Maria."), or wait in the aggregator's queue behind the audio, which Pipecat handles
  first. Either way they arrive after the new speech started, and they are not its transcript: a transcript the
  transcriber finished less than `LATE_SECONDS` after the caller was heard again counts with the words before.
- A turn that starts interrupts, and an interruption empties the queues of the processors it reaches. A final
  transcript waiting behind the interim that started the turn was dropped, and the turn ended empty five seconds later.
  `keep_words` makes every transcript uninterruptible: Pipecat's queues keep those.
- The transcriber can send part of a sentence as final while the caller is still saying it ("Hi. I'm an AI" ...
  "assistant calling on behalf of Maria."). When the caller stops, that part was in hand, and the turn ended on it;
  the rest came 0.19 s after the stop, past Pipecat's wait. A transcript the transcriber sent before the caller stopped
  can't hold their last words: the turn waits up to `NEWER_SECONDS` for one sent after (on calls it comes 0.1 to 0.2 s
  after the stop), and ends on what it has if none comes.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

GRACE_SECONDS = 1.5  # after speech that brought no transcript, end the turn with the earlier words
LATE_SECONDS = 0.3  # no transcriber writes down new speech this fast: what it finishes sooner is the speech before
NEWER_SECONDS = 0.5  # after the caller stops, how long a transcript from before they stopped waits for the rest
HEARD = "openring_heard"  # frame.metadata key: when the transcriber pushed the transcript (time.time())


def keep_words(stt: Any) -> Any:
    """Mark every transcript `stt` pushes uninterruptible, and when it was pushed. Returns `stt`."""
    from pipecat.frames.frames import TranscriptionFrame

    push = stt.push_frame

    async def push_frame(frame: Any, *args: Any, **kwargs: Any) -> None:
        if isinstance(frame, TranscriptionFrame):
            frame.interruptible = False  # an interruption stops the assistant, never what the caller said
            frame.metadata.setdefault(HEARD, time.time())
        await push(frame, *args, **kwargs)

    stt.push_frame = push_frame
    return stt


def stop_strategies() -> list[Any]:
    """The stop strategies a call uses: Pipecat's default, with the fixes above."""
    from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3

    return [fresh_transcript_strategy()(turn_analyzer=LocalSmartTurnAnalyzerV3())]


_STRATEGY: type | None = None


def fresh_transcript_strategy() -> type:
    """The class, built once Pipecat is imported (OpenRing imports it lazily)."""
    global _STRATEGY
    if _STRATEGY is not None:
        return _STRATEGY
    from pipecat.turns.user_stop.turn_analyzer_user_turn_stop_strategy import TurnAnalyzerUserTurnStopStrategy

    class FreshTranscriptStopStrategy(TurnAnalyzerUserTurnStopStrategy):
        """Ends the caller's turn only with the transcript of their last words in hand (see the module)."""

        def __init__(self, **kwargs: Any):
            super().__init__(**kwargs)
            self._earlier = ""  # what the caller said before their latest speech
            self._speech_began: float | None = None  # when the latest speech was heard (time.time())
            self._speech_ended: float | None = None  # when it ended: the VAD's stop less its stop_secs
            self._text_heard: float | None = None  # when the transcript in hand was pushed
            self._grace: asyncio.Task | None = None
            self._newer: asyncio.Task | None = None  # waiting for a transcript sent after the speech ended

        async def _cancel_grace(self) -> None:
            if self._grace is not None:
                await self.task_manager.cancel_task(self._grace)
                self._grace = None

        async def _cancel_newer(self) -> None:
            if self._newer is not None:
                await self.task_manager.cancel_task(self._newer)
                self._newer = None

        async def _reset(self):
            await self._cancel_grace()
            await self._cancel_newer()
            self._earlier = ""
            self._speech_began = self._speech_ended = self._text_heard = None
            await super()._reset()

        async def _maybe_report_user_turn_stopped(self):
            if self._newer is not None:  # the rest of what the caller said is on its way
                return
            await super()._maybe_report_user_turn_stopped()

        async def _handle_vad_user_started_speaking(self, frame):
            await super()._handle_vad_user_started_speaking(frame)
            await self._cancel_grace()
            await self._cancel_newer()
            self._speech_began, self._speech_ended = frame.timestamp, None
            if self._text:  # speech again: the turn now needs this speech's transcript
                self._earlier, self._text = _joined(self._earlier, self._text), ""

        async def _handle_transcription(self, frame):
            await self._cancel_grace()
            heard = frame.metadata.get(HEARD, time.time())
            if self._speech_began is not None and heard < self._speech_began + LATE_SECONDS:
                self._earlier = _joined(self._earlier, frame.text)  # the end of what the caller said before
                if not self._vad_user_speaking and self._vad_stopped and not self._text:
                    self._grace = self.task_manager.create_task(self._after_grace(), f"{self}::grace")
                return
            if self._speech_ended is not None and heard >= self._speech_ended:
                await self._cancel_newer()  # sent after the caller stopped: the turn may end on it
            self._text_heard = heard
            await super()._handle_transcription(frame)

        async def _handle_vad_user_stopped_speaking(self, frame):
            self._speech_ended = frame.timestamp - frame.stop_secs
            if self._text and self._text_heard is not None and self._text_heard < self._speech_ended:
                await self._cancel_newer()  # what is in hand was sent while the caller was still talking
                self._newer = self.task_manager.create_task(self._after_newer(), f"{self}::newer")
            await super()._handle_vad_user_stopped_speaking(frame)
            if not self._text and self._earlier:
                self._grace = self.task_manager.create_task(self._after_grace(), f"{self}::grace")

        async def _after_newer(self) -> None:
            try:
                await asyncio.sleep(NEWER_SECONDS)
            except asyncio.CancelledError:
                return
            self._newer = None
            await self._maybe_report_user_turn_stopped()  # nothing more came: what was in hand was all of it

        async def _after_grace(self) -> None:
            try:
                await asyncio.sleep(GRACE_SECONDS)
            except asyncio.CancelledError:
                return
            self._grace = None
            if not self._text and self._earlier:  # nothing was said this time: go on with the earlier words
                self._text, self._transcript_finalized = self._earlier, True
                await self._maybe_report_user_turn_stopped()

    _STRATEGY = FreshTranscriptStopStrategy
    return _STRATEGY


def _joined(before: str, after: str) -> str:
    return f"{before} {after}".strip()
