#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""Decide which ASR results should stop the bot mid-sentence."""

import time


class InterruptGate:
    """Tracks what has already interrupted, so the same thing does not again.

    An interrupt cancels the in-flight LLM turn, the TTS request and the RTC
    send buffer. It has to mean "the user said something new", not merely "a
    transcript arrived".
    """

    # An outstanding question that never comes back would hold the gate shut
    # for the rest of the session. LLMExec swallows an exception from a turn
    # without emitting a final (llm_exec.py:119 emits one only when the turn
    # produced text), so nothing else would clear it.
    #
    # It has to be shorter than the request's own timeout, not longer: the
    # extension gives up on the board at 120 s (ambarella.py total_timeout_s),
    # so a question still outstanding after that is not coming back. Set
    # above it, as it was, the gate stayed shut for another minute after the
    # turn had already been abandoned.
    MAX_PENDING_SECONDS = 90.0

    def __init__(
        self,
        interrupt_on_partial_while_speaking: bool = False,
        clock=None,
    ) -> None:
        self._last_text: str = ""
        self._speaking: bool = False
        # How many questions have gone to the model and not come back. A
        # count, not a flag: LLMExec queues questions and answers them one at
        # a time, so the first answer arriving does not mean the assistant is
        # free -- the next question is already being thought about, and a flag
        # cleared there would reopen the gate underneath it.
        self._pending: int = 0
        self._pending_since: float = 0.0
        # The question the model is working on. Soniox can finalise the same
        # words twice: on 2026-09-16 it closed 讲一个笑话。 and opened a new
        # segment with the deferred remainder in the same millisecond, and
        # that remainder finalised 2.0 s later carrying the same words.
        self._asked: str = ""
        self._clock = clock or time.monotonic
        self._reason: str = ""
        # Partial results are unreliable exactly when the assistant is
        # talking: the microphone hears the speaker, and a user who thinks
        # they were not heard repeats themselves. Both arrive as partials and
        # both cancel a reply that is already playing. Set True for a pipeline
        # fast enough that neither happens, and where cutting in the instant
        # the user opens their mouth is worth more.
        self._interrupt_on_partial_while_speaking = (
            interrupt_on_partial_while_speaking
        )

    # Below this, a transcript is noise rather than speech worth cutting the
    # bot off for.
    MIN_CHARS = 2

    def set_speaking(self, speaking: bool) -> None:
        """Called when the assistant starts and stops producing audio."""
        self._speaking = speaking

    def question_sent(self, text: str = "") -> None:
        """A question has gone to the model."""
        if self._pending == 0:
            self._pending_since = self._clock()
        self._pending += 1
        self._asked = text.strip()

    def repeats_the_question_in_flight(self, text: str) -> bool:
        """Is this the same question the model is already working on?

        Not a new question and not barge-in: one spoken sentence that ASR
        finalised in two pieces. Acting on it cancels the answer being
        written and asks for it again, which the board refuses because it is
        still generating the first.
        """
        return bool(
            self._pending and self._asked and text.strip() == self._asked
        )

    def answer_returned(self) -> None:
        """The model finished a turn, whether it produced an answer or not."""
        self._pending = max(0, self._pending - 1)
        if self._pending == 0:
            # Asked again after the answer, the same words are a real repeat.
            self._asked = ""

    def questions_dropped(self) -> None:
        """Everything queued was thrown away.

        flush() empties LLMExec's input queue and cancels the turn in flight,
        so after an interrupt nothing is outstanding. A turn cancelled before
        it produced text emits no final either, so this is the only signal
        that clears those.
        """
        self._pending = 0
        self._asked = ""

    @property
    def _thinking(self) -> bool:
        if self._pending <= 0:
            return False
        if self._clock() - self._pending_since > self.MAX_PENDING_SECONDS:
            # Whatever happened to it, it is not coming back.
            self._pending = 0
            return False
        return True

    @property
    def _busy(self) -> bool:
        # Occupied from the moment a question leaves until its answer has been
        # heard. On this board the thinking half is the longer one -- 37 s
        # against 14 s of speech, measured on 2026-09-16.
        return self._speaking or self._thinking

    def explain(self) -> str:
        """The state a decision was made in, for the log."""
        return (
            f"pending={self._pending} speaking={self._speaking} "
            f"last={self._last_text!r}"
        )

    def last_reason(self) -> str:
        """Why the last decision went the way it did."""
        return self._reason

    def can_commit_stable_partial(self, text: str) -> bool:
        """Whether a repeated partial may stand in for a missing ASR final."""
        return not self._busy and len(text.strip()) > self.MIN_CHARS

    def should_interrupt(self, text: str, final: bool) -> bool:
        if final:
            # The turn is over, so the next one may open with the same words
            # and must still count as new speech.
            self._last_text = ""
            # A completed sentence always interrupts, whatever the
            # assistant is doing. Queueing it instead was tried on
            # 2026-09-16 and was worse: ASR split one sentence into 你在 and
            # 说什么听不懂。, the model spent 45 seconds answering 你在, and
            # every later fragment waited its own 45 seconds behind it. The
            # newest thing the user said is the thing to answer.
            self._reason = "final"
            return True
        if self._busy and not self._interrupt_on_partial_while_speaking:
            # Measured on 2026-09-16, once while speaking and once while
            # thinking. Speaking: the four partials of a repeated question --
            # 如果, 如果要, 如果用于, 如果用于。 -- cut the answer off 2.1 s in.
            # Thinking: ' 加法', the tail of the user's own sentence, killed
            # the turn 2.3 s after it was sent, before a character came back.
            # Each partial differed from the one before, so the check below
            # never fired once.
            self._reason = (
                "partial while speaking"
                if self._speaking
                else "partial while thinking"
            )
            return False
        if len(text) <= self.MIN_CHARS:
            self._reason = f"partial shorter than {self.MIN_CHARS + 1} chars"
            return False
        if text == self._last_text:
            self._reason = "partial unchanged"
            return False
        self._last_text = text
        self._reason = "partial changed while idle"
        return True
