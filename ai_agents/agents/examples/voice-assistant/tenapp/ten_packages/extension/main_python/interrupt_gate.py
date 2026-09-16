#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""Decide which ASR results should stop the bot mid-sentence."""


class InterruptGate:
    """Tracks what has already interrupted, so the same thing does not again.

    An interrupt cancels the in-flight LLM turn, the TTS request and the RTC
    send buffer. It has to mean "the user said something new", not merely "a
    transcript arrived".
    """

    def __init__(
        self, interrupt_on_partial_while_speaking: bool = False
    ) -> None:
        self._last_text: str = ""
        self._speaking: bool = False
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

    def should_interrupt(self, text: str, final: bool) -> bool:
        if final:
            # The turn is over, so the next one may open with the same words
            # and must still count as new speech.
            self._last_text = ""
            return True
        if self._speaking and not self._interrupt_on_partial_while_speaking:
            # Measured on 2026-09-16: the board took fifteen seconds to
            # answer, the user asked again, and the four partials of that
            # repeat -- 如果, 如果要, 如果用于, 如果用于。 -- cut the answer off
            # 2.1 seconds in. Each was different from the one before, so the
            # check below never fired once.
            return False
        if len(text) <= self.MIN_CHARS:
            return False
        if text == self._last_text:
            return False
        self._last_text = text
        return True
