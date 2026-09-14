#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""Which ASR results should stop the bot mid-sentence.

An interrupt cancels the in-flight LLM turn, the TTS request and the RTC
send buffer, so it has to mean "the user said something new" -- not merely
"a transcript arrived".
"""

from main_python.interrupt_gate import InterruptGate


def test_a_repeated_identical_partial_interrupts_only_once():
    # Soniox re-sends an unchanged partial as a keepalive: 'Who are you'
    # arrived three times in one turn on 2026-09-14, and each one cancelled
    # whatever the bot was saying.
    gate = InterruptGate()
    assert gate.should_interrupt("Who are you", final=False) is True
    assert gate.should_interrupt("Who are you", final=False) is False
    assert gate.should_interrupt("Who are you", final=False) is False


def test_a_very_short_partial_does_not_interrupt():
    # Two characters or fewer is noise, not speech worth cutting the bot off
    # for. '?' arrived ten times in the same session.
    gate = InterruptGate()
    assert gate.should_interrupt("?", final=False) is False
    assert gate.should_interrupt("ab", final=False) is False


def test_a_final_always_interrupts():
    # A final ends the user's turn, so the bot has to stop whatever it is
    # doing even when the text is short or was seen before.
    gate = InterruptGate()
    gate.should_interrupt("Who are you", final=False)
    assert gate.should_interrupt("Who are you", final=True) is True
    assert gate.should_interrupt("?", final=True) is True


def test_the_next_turn_can_repeat_the_previous_words():
    # A final closes the turn, so saying the same thing again is new speech
    # and has to interrupt.
    gate = InterruptGate()
    assert gate.should_interrupt("say that again", final=False) is True
    assert gate.should_interrupt("say that again", final=True) is True
    assert gate.should_interrupt("say that again", final=False) is True


def test_a_growing_partial_keeps_interrupting():
    # Each one is new speech, so the bot should keep yielding. Redundant once
    # it has already stopped, but harmless -- and suppressing it would delay a
    # real interruption.
    gate = InterruptGate()
    assert gate.should_interrupt("Who", final=False) is True
    assert gate.should_interrupt("Who are", final=False) is True
    assert gate.should_interrupt("Who are you", final=False) is True


def test_the_sequence_recorded_on_the_board():
    # Replayed from /tmp/task_run.log, 2026-09-14. Eleven ASR results for one
    # exchange; three were the same partial re-sent, and each of those
    # cancelled the reply that was being spoken.
    recorded = [
        ("Who", False),
        ("Who are", False),
        ("Who are y", False),
        ("Who are you", False),
        ("Who are you", False),
        ("Who are you", False),
        ("Who are you?", False),
        ("Who are", True),
        (" you?", False),
        (" you?", False),
        (" you?", True),
    ]
    gate = InterruptGate()
    fired = sum(gate.should_interrupt(t, final=f) for t, f in recorded)
    assert fired == 8, "every repeat of an unchanged partial must be dropped"
