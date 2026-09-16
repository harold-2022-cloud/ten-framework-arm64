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


# --- While the assistant is speaking ---------------------------------------


def test_a_growing_partial_interrupts_on_every_character():
    """The hole the repeat check never covered.

    Replayed from task_run.log 00:13:41-00:14:09: the user, having waited
    fifteen seconds, asked again, and each character of the growing transcript
    cut off the answer that had just started.
    """
    gate = InterruptGate()
    assert gate.should_interrupt(" 如果", final=False) is True
    assert gate.should_interrupt(" 如果要", final=False) is True
    assert gate.should_interrupt(" 如果用于", final=False) is True


def test_a_partial_does_not_interrupt_while_the_assistant_speaks():
    gate = InterruptGate()
    gate.set_speaking(True)
    assert gate.should_interrupt(" 如果", final=False) is False
    assert gate.should_interrupt(" 如果要", final=False) is False
    assert gate.should_interrupt(" 如果用于", final=False) is False


def test_a_final_still_interrupts_while_the_assistant_speaks():
    """A completed sentence is the user actually saying something."""
    gate = InterruptGate()
    gate.set_speaking(True)
    assert gate.should_interrupt("停一下", final=True) is True


def test_partials_interrupt_again_once_the_assistant_stops():
    gate = InterruptGate()
    gate.set_speaking(True)
    assert gate.should_interrupt("你好嗎", final=False) is False
    gate.set_speaking(False)
    assert gate.should_interrupt("你好嗎在嗎", final=False) is True


def test_the_old_behaviour_is_available():
    """Barge-in on a partial is what a fast pipeline wants; this one is not."""
    gate = InterruptGate(interrupt_on_partial_while_speaking=True)
    gate.set_speaking(True)
    assert gate.should_interrupt(" 如果", final=False) is True


def test_the_logged_sequence_no_longer_cuts_the_answer_off():
    """The whole exchange from the log, with the assistant speaking."""
    observed = [
        (" 如果", False),
        (" 如果要", False),
        (" 如果用于", False),
        (" 如果用于", False),
        (" 如果用于", False),
        (" 如果用于", False),
        (" 如果用于", False),
        (" 如果用于", False),
        (" 如果用于。", False),
        (" 如果用于。", False),
        (" 如果用于。", False),
        (" 如果用于。", False),
        (" 如果用于。", False),
        (" 如果用于。", True),
    ]
    gate = InterruptGate()
    gate.set_speaking(True)
    fired = [t for t, f in observed if gate.should_interrupt(t, f)]

    # Four interrupts before; now only the final one, which is the user
    # genuinely having spoken.
    assert fired == [" 如果用于。"]
