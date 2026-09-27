#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""The record is the product. Everything else exists to fill it in."""

import json

import pytest

from main_python.record import MeetingRecord

STARTED_AT = 1774598700.0  # 2026-03-27 14:05:00 local


def test_the_record_carries_both_clocks():
    """Absolute time is for a person reading it; the offset is for finding the
    moment in the recording."""
    record = MeetingRecord()
    record.add_segment(
        "seg-1",
        started_at=STARTED_AT + 30,
        utterances=[
            {
                "start_s": 2.0,
                "end_s": 6.0,
                "speaker": 1,
                "text": "下週要出版本。",
            },
        ],
    )
    lines = record.as_prompt_lines(started_at=STARTED_AT)
    assert "說話人1: 下週要出版本。" in lines
    assert "/ 00:32]" in lines, "the offset from the meeting's own start"


def test_a_failed_segment_does_not_take_the_others_with_it():
    """The reason for cutting a meeting into topics at all."""
    record = MeetingRecord()
    record.add_segment(
        "seg-1",
        started_at=STARTED_AT,
        utterances=[
            {"start_s": 1.0, "end_s": 2.0, "speaker": 0, "text": "一。"}
        ],
    )
    record.mark_failed(
        "seg-2",
        "diarization ran out of memory",
        started_at=STARTED_AT + 300,
    )
    record.add_segment(
        "seg-3",
        started_at=STARTED_AT + 600,
        utterances=[
            {"start_s": 1.0, "end_s": 2.0, "speaker": 1, "text": "三。"}
        ],
    )

    lines = record.as_prompt_lines(started_at=STARTED_AT)
    assert "一。" in lines and "三。" in lines
    assert "seg-2 段未能處理" in lines
    assert (
        lines.index("一。")
        < lines.index("seg-2 段未能處理")
        < lines.index("三。")
    ), "a failed segment keeps its place in time, not at the front"
    assert not record.is_empty


def test_segments_are_ordered_by_when_they_happened_not_when_they_finished():
    """A short segment can finish transcribing after a long earlier one."""
    record = MeetingRecord()
    record.add_segment(
        "late-but-earlier",
        started_at=STARTED_AT,
        utterances=[
            {"start_s": 0.0, "end_s": 1.0, "speaker": 0, "text": "先。"}
        ],
    )
    record.add_segment(
        "early-but-later",
        started_at=STARTED_AT + 300,
        utterances=[
            {"start_s": 0.0, "end_s": 1.0, "speaker": 0, "text": "後。"}
        ],
    )
    assert [s.segment_id for s in record.ordered()] == [
        "late-but-earlier",
        "early-but-later",
    ]


def test_a_record_with_nothing_in_it_says_so():
    record = MeetingRecord()
    record.mark_failed("seg-1", "everything went wrong")
    assert record.is_empty, "a record of failures is not a meeting record"


import asyncio
from unittest.mock import AsyncMock, MagicMock

from ten_runtime import StatusCode

from main_python.config import MeetingControlConfig
from main_python.extension import MEETING_SUMMARY_KEY, MeetingControlExtension


def make_extension(**overrides):
    ext = MeetingControlExtension("main_control")
    ext.ten_env = MagicMock()
    ext.ten_env.log_info = MagicMock()
    ext.ten_env.log_error = MagicMock()
    ext._close_segment = AsyncMock()
    ext._assemble = AsyncMock()
    ext.config = MeetingControlConfig(**overrides)
    ext.meeting_started_at = STARTED_AT
    ext._tick_s = 0.02  # the test's clock, not a meeting's
    ext._ticker = asyncio.create_task(ext._tick())
    return ext


async def stop(ext):
    ext._stopped = True
    ext._ticker.cancel()


async def stream_frames(ext, seconds, every=0.005):
    """An upload that is still there. Frames arrive whatever the VAD is
    saying -- the client streams, it does not gate on voice."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + seconds
    while loop.time() < deadline:
        await ext.on_audio_frame(None, None)
        await asyncio.sleep(every)


@pytest.mark.asyncio
async def test_a_pause_ends_a_topic_and_not_the_meeting():
    """The mistake an earlier design would have made: a meeting has silences
    in it, and treating the first one as the end throws the rest away."""
    ext = make_extension(segment_silence_s=0.05, meeting_silence_s=10.0)
    ext.speech_started()
    ext.speech_stopped()
    await asyncio.sleep(0.15)
    await stop(ext)

    assert ext._close_segment.await_count == 1, "the topic did not close"
    assert ext._assemble.await_count == 0, "the meeting was declared over"


@pytest.mark.asyncio
async def test_a_long_enough_silence_ends_the_meeting():
    ext = make_extension(segment_silence_s=0.05, meeting_silence_s=0.15)
    ext.speech_started()
    ext.speech_stopped()
    await asyncio.sleep(0.30)
    await stop(ext)

    assert ext._assemble.await_count == 1


@pytest.mark.asyncio
async def test_speaking_again_puts_the_topic_off():
    ext = make_extension(segment_silence_s=0.10, meeting_silence_s=1.0)
    ext.speech_started()
    ext.speech_stopped()
    await asyncio.sleep(0.04)
    ext.speech_started()  # somebody carried on
    await asyncio.sleep(0.06)
    await stop(ext)

    assert ext._close_segment.await_count == 0
    assert ext._assemble.await_count == 0


@pytest.mark.asyncio
async def test_a_monologue_longer_than_the_topic_silence_is_not_cut_in_half():
    """ten_vad_python emits start_of_sentence once and end_of_sentence once,
    and nothing at all in between. Reading time since the last VAD *event*
    as elapsed silence closed the topic mid-word on any unbroken run longer
    than segment_silence_s, cleared _segment_pending, and stranded the rest
    of that speech in a segment nothing ever closed."""
    ext = make_extension(
        segment_silence_s=0.05, meeting_silence_s=0.15, upload_gone_s=0.10
    )
    ext.speech_started()  # and carries straight on talking
    await stream_frames(ext, 0.30)
    await stop(ext)

    assert (
        ext._close_segment.await_count == 0
    ), "the topic was closed while the speaker was still talking"
    assert (
        ext._assemble.await_count == 0
    ), "the record was assembled while the speaker was still talking"


@pytest.mark.asyncio
async def test_the_monologue_closes_once_the_speaker_finally_stops():
    """The other half of the same behaviour: gating the clock on VAD state
    must not stop the clock from ever running."""
    ext = make_extension(
        segment_silence_s=0.05, meeting_silence_s=10.0, upload_gone_s=0.10
    )
    ext.speech_started()
    await stream_frames(ext, 0.20)
    ext.speech_stopped()
    await stream_frames(ext, 0.20)  # the upload is still there, just quiet
    await stop(ext)

    assert ext._close_segment.await_count == 1
    assert ext._assemble.await_count == 0


@pytest.mark.asyncio
async def test_the_upload_dying_mid_sentence_still_produces_a_record():
    """A network drop sends no end_of_sentence -- websocket_server only logs
    a client disconnect and emits nothing into the graph. A clock gated
    purely on VAD state would wait there for ever. The frames are what
    answers this instead: they stop arriving, said so or not."""
    ext = make_extension(
        segment_silence_s=0.05, meeting_silence_s=0.15, upload_gone_s=0.05
    )
    ext.speech_started()
    await stream_frames(ext, 0.10)  # and then the socket dies, mid-sentence
    await asyncio.sleep(0.40)
    await stop(ext)

    assert ext._close_segment.await_count == 1
    assert ext._assemble.await_count == 1, "a disconnect left no record"


@pytest.mark.asyncio
async def test_a_meeting_that_resumes_can_be_assembled_again():
    """The long threshold marks the end of a lull, not the meeting: if
    talking resumes, a later lull must produce another record rather than
    being absorbed silently for ever."""
    ext = make_extension(segment_silence_s=0.05, meeting_silence_s=0.15)
    ext.speech_started()
    ext.speech_stopped()
    await asyncio.sleep(0.30)  # first long silence: assembles once
    ext.speech_started()
    ext.speech_stopped()
    await asyncio.sleep(0.30)  # second long silence: assembles again
    await stop(ext)

    assert ext._assemble.await_count == 2


@pytest.mark.asyncio
async def test_each_segment_is_summarised_as_it_lands():
    """Work spread across the meeting, not piled up at the end."""
    ext = make_extension()
    ext.agent = AsyncMock()
    ext.record.add_segment(
        "seg-1",
        started_at=STARTED_AT,
        utterances=[
            {"start_s": 1.0, "end_s": 4.0, "speaker": 0, "text": "下週出。"}
        ],
    )
    # _summarise_segment now waits for the answer, so it is run alongside
    # the answer that unblocks it rather than awaited to completion first.
    task = asyncio.create_task(ext._summarise_segment("seg-1"))
    await asyncio.sleep(0.01)
    ext.on_llm_text("重點：版本下週出。")
    await task

    ext.agent.queue_llm_input.assert_awaited_once()
    sent = ext.agent.queue_llm_input.await_args.args[0]
    assert "下週出。" in sent
    assert ext.config.segment_prompt.strip()[:6] in sent
    await stop(ext)


@pytest.mark.asyncio
async def test_the_summary_comes_back_into_the_record():
    """Queued and forgotten is the same as never asked."""
    ext = make_extension()
    ext.agent = AsyncMock()
    ext.record.add_segment(
        "seg-1",
        started_at=STARTED_AT,
        utterances=[
            {"start_s": 1.0, "end_s": 4.0, "speaker": 0, "text": "下週出。"}
        ],
    )
    task = asyncio.create_task(ext._summarise_segment("seg-1"))
    await asyncio.sleep(0.01)
    ext.on_llm_text("重點：版本下週出。")
    await task

    entry = next(s for s in ext.record.ordered() if s.segment_id == "seg-1")
    assert entry.summary == "重點：版本下週出。"
    await stop(ext)


@pytest.mark.asyncio
async def test_an_answer_with_no_segment_waiting_is_dropped_not_misfiled():
    """The board can finish a turn after its segment was already assembled."""
    ext = make_extension()
    ext.agent = AsyncMock()
    ext.on_llm_text("重點：沒有人在等這個。")

    assert all(s.summary == "" for s in ext.record.ordered())
    await stop(ext)


@pytest.mark.asyncio
async def test_two_segments_summarised_back_to_back_each_get_their_own_answer():
    """A second topic transcribed while the first is still being summarised
    must not steal the first topic's still-outstanding answer: queuing a
    prompt never blocks, so only holding _summary_lock across the whole
    ask-and-wait actually serialises this."""
    ext = make_extension()
    ext.agent = AsyncMock()
    ext.record.add_segment(
        "seg-a",
        started_at=STARTED_AT,
        utterances=[
            {"start_s": 1.0, "end_s": 2.0, "speaker": 0, "text": "A。"}
        ],
    )
    ext.record.add_segment(
        "seg-b",
        started_at=STARTED_AT + 60,
        utterances=[
            {"start_s": 1.0, "end_s": 2.0, "speaker": 0, "text": "B。"}
        ],
    )

    task_a = asyncio.create_task(ext._summarise_segment("seg-a"))
    await asyncio.sleep(0.01)
    assert ext._awaiting_summary == "seg-a"

    # B's segment_transcribed lands while A's turn is still outstanding --
    # exactly the backlogged-short-segment-behind-a-long-one scenario the
    # review demonstrated stealing A's answer.
    task_b = asyncio.create_task(ext._summarise_segment("seg-b"))
    await asyncio.sleep(0.01)
    assert not task_b.done()
    assert (
        ext._awaiting_summary == "seg-a"
    ), "B must not overwrite A's slot while A is still outstanding"

    ext.on_llm_text("SUMMARY A")
    await task_a
    await asyncio.sleep(0.01)
    assert ext._awaiting_summary == "seg-b"

    ext.on_llm_text("SUMMARY B")
    await task_b

    entry_a = next(s for s in ext.record.ordered() if s.segment_id == "seg-a")
    entry_b = next(s for s in ext.record.ordered() if s.segment_id == "seg-b")
    assert entry_a.summary == "SUMMARY A"
    assert entry_b.summary == "SUMMARY B"
    await stop(ext)


@pytest.mark.asyncio
async def test_a_hung_board_times_out_and_frees_the_slot_for_the_next_topic():
    """A hung board must not stop every later topic from being summarised."""
    ext = make_extension(summary_timeout_s=0.05)
    ext.agent = AsyncMock()
    ext.record.add_segment(
        "seg-1",
        started_at=STARTED_AT,
        utterances=[
            {"start_s": 1.0, "end_s": 2.0, "speaker": 0, "text": "一。"}
        ],
    )

    await ext._summarise_segment("seg-1")  # nobody ever answers

    entry = next(s for s in ext.record.ordered() if s.segment_id == "seg-1")
    assert entry.summary == ""
    assert ext._awaiting_summary is None, "the slot must be freed, not stuck"
    await stop(ext)


@pytest.mark.asyncio
async def test_the_meeting_summary_is_asked_from_the_segment_summaries():
    """Assembly sends one more turn: the per-segment summaries in, the whole
    meeting's conclusion out, alongside the transcript rather than instead
    of it."""
    ext = make_extension()
    ext._assemble = MeetingControlExtension._assemble.__get__(ext)
    ext.agent = AsyncMock()
    ext.ten_env.send_data = AsyncMock()
    ext.record.add_segment(
        "seg-1",
        started_at=STARTED_AT,
        utterances=[
            {"start_s": 1.0, "end_s": 2.0, "speaker": 0, "text": "一。"}
        ],
    )
    ext.record.add_summary("seg-1", "重點一。")

    task = asyncio.create_task(ext._assemble())
    await asyncio.sleep(0.01)
    ext.on_llm_text("結論：一。待辦：無。")
    await task

    ext.agent.queue_llm_input.assert_awaited_once()
    sent = ext.agent.queue_llm_input.await_args.args[0]
    assert "重點一。" in sent, "the segment summary is the input"
    assert "一。" not in sent.replace(
        "重點一。", ""
    ), "the full transcript is not what gets sent to the LLM"
    assert ext.config.meeting_prompt.strip()[:6] in sent

    ext.ten_env.send_data.assert_awaited_once()
    payload = json.loads(
        ext.ten_env.send_data.await_args.args[0].get_property_to_json(None)[0]
    )
    assert payload["meeting_summary"] == "結論：一。待辦：無。"
    assert "transcript" in payload and payload["transcript"]
    await stop(ext)


@pytest.mark.asyncio
async def test_the_meeting_prompt_reflects_a_segment_still_finishing_when_assembly_starts():
    """If the meeting-ending silence fires while a segment's own turn is
    still outstanding, assembly's turn waits behind it on the same lock --
    and by the time it actually runs, that segment's summary is already in
    the record. The prompt has to be built then, not before, or the
    meeting's conclusions could silently omit exactly the segment that
    contained them."""
    ext = make_extension()
    ext._assemble = MeetingControlExtension._assemble.__get__(ext)
    ext.agent = AsyncMock()
    ext.ten_env.send_data = AsyncMock()
    ext.record.add_segment(
        "seg-a",
        started_at=STARTED_AT,
        utterances=[
            {"start_s": 1.0, "end_s": 2.0, "speaker": 0, "text": "A。"}
        ],
    )

    # seg-a's own turn is still outstanding -- holding _summary_lock --
    # when the meeting-ending silence fires.
    task_a = asyncio.create_task(ext._summarise_segment("seg-a"))
    await asyncio.sleep(0.01)
    assert ext._awaiting_summary == "seg-a"

    task_assemble = asyncio.create_task(ext._assemble())
    await asyncio.sleep(0.01)
    assert not task_assemble.done(), "assembly must wait for seg-a's turn"
    assert (
        ext._awaiting_summary == "seg-a"
    ), "assembly must not have jumped the queue"

    ext.on_llm_text("SEG A SUMMARY")
    await task_a
    entry_a = next(s for s in ext.record.ordered() if s.segment_id == "seg-a")
    assert entry_a.summary == "SEG A SUMMARY"

    await asyncio.sleep(0.01)
    assert ext._awaiting_summary == MEETING_SUMMARY_KEY
    ext.on_llm_text("MEETING CONCLUSION")
    await task_assemble

    # The second queue_llm_input call is the meeting turn's; its prompt
    # must contain the segment summary that only landed while it waited.
    meeting_prompt = ext.agent.queue_llm_input.await_args_list[-1].args[0]
    assert (
        "SEG A SUMMARY" in meeting_prompt
    ), "the meeting prompt must reflect the segment that just finished"

    ext.ten_env.send_data.assert_awaited_once()
    payload = json.loads(
        ext.ten_env.send_data.await_args.args[0].get_property_to_json(None)[0]
    )
    assert payload["meeting_summary"] == "MEETING CONCLUSION"
    await stop(ext)


@pytest.mark.asyncio
async def test_a_failed_meeting_summary_turn_still_emits_the_transcript():
    """The spec: a failed summary must not cost the transcript."""
    ext = make_extension(summary_timeout_s=0.05)
    ext._assemble = MeetingControlExtension._assemble.__get__(ext)
    ext.agent = AsyncMock()
    ext.ten_env.send_data = AsyncMock()
    ext.record.add_segment(
        "seg-1",
        started_at=STARTED_AT,
        utterances=[
            {"start_s": 1.0, "end_s": 2.0, "speaker": 0, "text": "一。"}
        ],
    )
    ext.record.add_summary("seg-1", "重點一。")

    await ext._assemble()  # nobody ever answers the meeting-level turn

    ext.ten_env.send_data.assert_awaited_once()
    payload = json.loads(
        ext.ten_env.send_data.await_args.args[0].get_property_to_json(None)[0]
    )
    assert payload["meeting_summary"] == ""
    assert "transcript" in payload and payload["transcript"]
    await stop(ext)


@pytest.mark.asyncio
async def test_the_meeting_record_is_written_next_to_its_own_audio(tmp_path):
    """The graph gives meeting_record nowhere to route to, so a file next
    to the segment audio is its only real destination."""
    ext = make_extension(summary_timeout_s=0.05)
    ext._assemble = MeetingControlExtension._assemble.__get__(ext)
    ext.agent = AsyncMock()
    ext.ten_env.send_data = AsyncMock()
    segment_dir = tmp_path / "segments"
    segment_dir.mkdir()
    ext._last_segment_dir = str(segment_dir)
    ext.record.add_segment(
        "seg-1",
        started_at=STARTED_AT,
        utterances=[
            {"start_s": 1.0, "end_s": 2.0, "speaker": 0, "text": "一。"}
        ],
    )
    ext.record.add_summary("seg-1", "重點一。")

    await ext._assemble()

    written = segment_dir / "meeting_record.json"
    assert written.exists()
    payload = json.loads(written.read_text(encoding="utf-8"))
    assert payload["segments"][0]["summary"] == "重點一。"
    await stop(ext)


class _FakeCmdResult:
    """Duck-types the three CmdResult methods _send_to_llm actually calls,
    so a fake board's answer can be fed through the real LLMExec without
    needing a live graph to construct a genuine one."""

    def __init__(self, payload_json, final=False):
        self._payload_json = payload_json
        self._final = final

    def is_final(self):
        return self._final

    def get_status_code(self):
        return StatusCode.OK

    def get_property_to_json(self, _path):
        return self._payload_json, None


def _message_done_json(content, response_id="r"):
    return json.dumps(
        {
            "response_id": response_id,
            "type": "message_content_done",
            "role": "assistant",
            "content": content,
        }
    )


@pytest.mark.asyncio
async def test_the_real_agent_delivers_a_real_llm_response_to_the_record():
    """The tests above mock ext.agent, so none of them exercise on_init's
    dir(self) registration scan, Agent._dispatch's isinstance match,
    _on_llm_response's is_final/type guard, or _summarise_segment actually
    filing _ask_llm's return value -- exactly where a mistake in the
    trimmed agent/ package, or in the refactor that moved filing out of
    on_llm_text, would surface. This drives _summarise_segment as a real
    task against the real Agent/LLMExec, answered by a real
    LLMResponseReasoningDone/Delta/Done fed through _handle_llm_response."""
    # pylint: disable=protected-access
    from ten_ai_base.struct import (
        LLMResponseMessageDelta,
        LLMResponseMessageDone,
        LLMResponseReasoningDone,
    )

    class FakeTenEnv:
        async def get_property_to_json(self, _path):
            return MeetingControlConfig().model_dump_json(), None

        async def send_cmd(self, _cmd):
            return None, None

        async def send_cmd_ex(self, _cmd):
            # _summarise_segment's own queue_llm_input reaches
            # LLMExec._send_to_llm for real; this test answers by calling
            # _handle_llm_response directly instead, so the board itself
            # is never asked anything here.
            return
            yield  # pragma: no cover - makes this an async generator

        def log_info(self, _msg):
            pass

        def log_error(self, _msg):
            pass

        def log_debug(self, _msg):
            pass

        def log_warn(self, _msg):
            pass

    ten_env = FakeTenEnv()
    ext = MeetingControlExtension("main_control")
    await ext.on_init(ten_env)
    try:
        registered = [
            h.__name__ for hs in ext.agent._callbacks.values() for h in hs
        ]
        assert (
            "_on_llm_response" in registered
        ), "on_init's registration scan did not find the handler"

        ext.record.add_segment(
            "seg-1",
            started_at=STARTED_AT,
            utterances=[
                {"start_s": 1.0, "end_s": 2.0, "speaker": 0, "text": "X。"}
            ],
        )

        task = asyncio.create_task(ext._summarise_segment("seg-1"))
        for _ in range(200):
            if ext._awaiting_summary == "seg-1":
                break
            await asyncio.sleep(0.01)
        assert ext._awaiting_summary == "seg-1"

        await ext.agent.llm_exec._handle_llm_response(
            LLMResponseReasoningDone(
                response_id="r1", role="assistant", content="推理過程"
            )
        )
        await ext.agent.llm_exec._handle_llm_response(
            LLMResponseMessageDelta(
                response_id="r1", role="assistant", delta="重", content="重"
            )
        )
        await ext.agent.llm_exec._handle_llm_response(
            LLMResponseMessageDone(
                response_id="r1", role="assistant", content="重點：X。"
            )
        )
        await task

        entry = next(s for s in ext.record.ordered() if s.segment_id == "seg-1")
        assert entry.summary == "重點：X。"

        # A stray final after the slot has already been filed must not
        # overwrite it.
        await ext.agent.llm_exec._handle_llm_response(
            LLMResponseMessageDone(
                response_id="r2", role="assistant", content="漏接的回答"
            )
        )
        await asyncio.sleep(0.05)
        entry = next(s for s in ext.record.ordered() if s.segment_id == "seg-1")
        assert entry.summary == "重點：X。"
    finally:
        await ext.on_stop(ten_env)
        ext._stopped = True
        if ext._ticker:
            ext._ticker.cancel()


@pytest.mark.asyncio
async def test_a_late_answer_after_a_timeout_does_not_reach_a_later_turn():
    """The residual of the same race: a timed-out turn's answer carries no
    correlation id, so nothing in on_llm_text can tell it apart from
    whatever turn asks next. The only sound fix is to make the late answer
    not exist -- _ask_llm's flush_llm() on timeout aborts the board's turn
    and cancels the task reading its response, so even a "board" still
    willing to answer late can no longer reach a later turn's slot."""
    # pylint: disable=protected-access
    a_may_respond = asyncio.Event()

    class FakeTenEnv:
        def __init__(self):
            self.calls = 0

        async def get_property_to_json(self, _path):
            return (
                MeetingControlConfig(summary_timeout_s=0.05).model_dump_json(),
                None,
            )

        async def send_cmd(self, _cmd):
            return None, None

        async def send_cmd_ex(self, _cmd):
            self.calls += 1
            if self.calls == 1:
                # Segment A's turn: the board never actually replies
                # before A's own turn is aborted for timing out.
                await a_may_respond.wait()
                return
            # Segment B's turn: a normal, prompt answer.
            payload = _message_done_json("SUMMARY B", response_id="r-b")
            yield _FakeCmdResult(payload, final=False), None
            yield _FakeCmdResult(payload, final=True), None

        def log_info(self, _msg):
            pass

        def log_error(self, _msg):
            pass

        def log_debug(self, _msg):
            pass

        def log_warn(self, _msg):
            pass

    ten_env = FakeTenEnv()
    ext = MeetingControlExtension("main_control")
    await ext.on_init(ten_env)
    try:
        ext.record.add_segment(
            "seg-a",
            started_at=STARTED_AT,
            utterances=[
                {"start_s": 1.0, "end_s": 2.0, "speaker": 0, "text": "A。"}
            ],
        )
        ext.record.add_segment(
            "seg-b",
            started_at=STARTED_AT + 60,
            utterances=[
                {"start_s": 1.0, "end_s": 2.0, "speaker": 0, "text": "B。"}
            ],
        )

        await ext._summarise_segment("seg-a")  # times out; flush_llm cancels it
        await ext._summarise_segment("seg-b")  # a normal, prompt turn

        entry_a = next(
            s for s in ext.record.ordered() if s.segment_id == "seg-a"
        )
        entry_b = next(
            s for s in ext.record.ordered() if s.segment_id == "seg-b"
        )
        assert entry_a.summary == "", "A timed out; it has no answer to carry"
        assert entry_b.summary == "SUMMARY B"

        # Even if the "board" belatedly wants to answer A now, A's own
        # task was already cancelled by flush_llm and cannot deliver it --
        # so this must change nothing.
        a_may_respond.set()
        await asyncio.sleep(0.05)
        entry_a = next(
            s for s in ext.record.ordered() if s.segment_id == "seg-a"
        )
        entry_b = next(
            s for s in ext.record.ordered() if s.segment_id == "seg-b"
        )
        assert entry_a.summary == ""
        assert (
            entry_b.summary == "SUMMARY B"
        ), "a late answer must not overwrite a later turn's own"
    finally:
        await ext.on_stop(ten_env)
        ext._stopped = True
        if ext._ticker:
            ext._ticker.cancel()
