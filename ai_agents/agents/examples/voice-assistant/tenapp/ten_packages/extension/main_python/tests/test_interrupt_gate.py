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


# --- While the assistant is thinking ---------------------------------------


def test_a_partial_does_not_interrupt_while_the_assistant_thinks():
    """The window the first fix missed.

    From task_run.log 01:27:53-01:27:55: the question went to the LLM, and
    2.3 seconds later the partial ' 加法' -- the tail of the user's own
    sentence -- cancelled the turn before it had produced a character. Two of
    five questions died this way; the third was the user asking 你还在吗？
    """
    gate = InterruptGate()
    gate.question_sent()
    assert gate.should_interrupt(" 加法", final=False) is False


def test_a_final_while_thinking_replaces_the_question():
    """Queueing was tried and measured worse.

    ASR split one sentence into 你在 and 说什么听不懂。; queued, the model spent
    45 seconds on 你在 and everything after it waited behind. A completed
    sentence is the newest thing the user said, and is what to answer."""
    gate = InterruptGate()
    gate.question_sent()
    assert gate.should_interrupt("算了不用了", final=False) is False
    assert gate.should_interrupt("算了不用了", final=True) is True


def test_thinking_and_speaking_clear_independently():
    """Thinking ends when the answer starts; speaking ends when it stops."""
    gate = InterruptGate()
    gate.question_sent()
    gate.set_speaking(True)

    gate.answer_returned()
    assert gate.should_interrupt("還在講", final=False) is False

    gate.set_speaking(False)
    assert gate.should_interrupt("講完了", final=False) is True


def test_the_old_behaviour_covers_thinking_too():
    gate = InterruptGate(interrupt_on_partial_while_speaking=True)
    gate.question_sent()
    assert gate.should_interrupt(" 加法", final=False) is True


def test_the_logged_question_survives_its_own_tail():
    """The exact partials that killed Q2, with the assistant thinking."""
    gate = InterruptGate()
    gate.question_sent()
    observed = [(" 家", False), (" 加法", False), (" 家法", False)]
    assert [t for t, f in observed if gate.should_interrupt(t, f)] == []


# --- A new question must not starve the one before it ----------------------


def test_a_final_while_thinking_interrupts():
    gate = InterruptGate()
    gate.question_sent()
    assert gate.should_interrupt("你还在吗？", final=True) is True


def test_a_final_while_speaking_still_interrupts():
    """Cutting in on an answer you can hear is barge-in, and should work."""
    gate = InterruptGate()
    gate.answer_returned()
    gate.set_speaking(True)
    assert gate.should_interrupt("停，我問別的", final=True) is True


def test_a_final_interrupts_when_the_assistant_is_idle():
    gate = InterruptGate()
    assert gate.should_interrupt("你是谁？", final=True) is True


def test_a_final_while_both_thinking_and_speaking_interrupts():
    """The model is still writing while its first sentences play; the user
    can hear something, so cutting in is deliberate."""
    gate = InterruptGate()
    gate.question_sent()
    gate.set_speaking(True)
    assert gate.should_interrupt("停", final=True) is True


# --- Two questions in the queue --------------------------------------------


def test_the_second_question_is_still_protected_after_the_first_answers():
    """The hole a boolean left.

    LLMExec answers one question at a time, so the first answer coming back
    does not mean the assistant is free: the second is already at the model.
    A flag cleared there reopened the gate underneath it.
    """
    gate = InterruptGate()
    gate.question_sent()
    gate.question_sent()

    gate.answer_returned()
    assert gate.should_interrupt("使用者還在講", final=False) is False

    gate.answer_returned()
    assert gate.should_interrupt("現在真的閒著了", final=False) is True


def test_audio_ending_does_not_free_a_question_still_being_thought_about():
    gate = InterruptGate()
    gate.question_sent()
    gate.set_speaking(True)
    gate.question_sent()

    gate.answer_returned()
    gate.set_speaking(False)  # the first answer finished playing

    assert gate.should_interrupt("第二題還在想", final=False) is False


def test_an_interrupt_clears_everything_outstanding():
    """flush() empties the queue and cancels the turn, and a turn cancelled
    before it produced text emits no final -- so nothing else would clear."""
    gate = InterruptGate()
    gate.question_sent()
    gate.question_sent()

    gate.questions_dropped()
    assert gate.should_interrupt("閘門要重新打開", final=False) is True


def test_the_count_does_not_go_negative():
    gate = InterruptGate()
    gate.answer_returned()
    gate.answer_returned()
    gate.question_sent()
    assert gate.should_interrupt("還在想", final=False) is False


def test_a_question_that_never_returns_stops_holding_the_gate():
    """LLMExec swallows an exception from a turn without emitting a final,
    so an error would otherwise wedge the gate shut for the session."""
    now = [1000.0]
    gate = InterruptGate(clock=lambda: now[0])
    gate.question_sent()
    assert gate.should_interrupt("還在想", final=False) is False

    now[0] += InterruptGate.MAX_PENDING_SECONDS + 1
    assert gate.should_interrupt("早就該回來了", final=False) is True


def test_the_timeout_does_not_fire_during_a_slow_turn():
    now = [1000.0]
    gate = InterruptGate(clock=lambda: now[0])
    gate.question_sent()
    now[0] += 40.0  # the slowest turn measured on this board was 43.5 s
    assert gate.should_interrupt("還在想", final=False) is False


def test_a_question_goes_stale_before_the_request_does():
    """The extension gives up on the board at 120 s. A gate that waits longer
    stays shut after the turn has already been abandoned."""
    assert InterruptGate.MAX_PENDING_SECONDS < 120.0
