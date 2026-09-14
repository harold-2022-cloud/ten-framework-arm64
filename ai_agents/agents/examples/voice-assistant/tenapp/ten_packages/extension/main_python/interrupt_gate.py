#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""Decide which ASR results should stop the bot mid-sentence."""


class InterruptGate:
    """Tracks what has already interrupted, so the same thing does not again."""

    def __init__(self) -> None:
        self._last_text: str = ""

    # Below this, a transcript is noise rather than speech worth cutting the
    # bot off for.
    MIN_CHARS = 2

    def should_interrupt(self, text: str, final: bool) -> bool:
        if final:
            # The turn is over, so the next one may open with the same words
            # and must still count as new speech.
            self._last_text = ""
            return True
        if len(text) <= self.MIN_CHARS:
            return False
        if text == self._last_text:
            return False
        self._last_text = text
        return True
