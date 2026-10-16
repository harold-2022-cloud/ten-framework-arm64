#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""One meeting from upload to archive, with the other extensions faked.

The flow only talks through four ports -- ask for topics, ask for a
transcript, ask the LLM, log -- so a test plays the segmenter, the
transcriber and the board's LLM, and reads what lands on disk.
"""

import json

import pytest

from meeting_control_python.config import MeetingControlConfig
from meeting_control_python.flow import MeetingFlow

A = [1.0, 0.0]
B = [0.0, 1.0]


class Board:
    """Plays every other extension in the graph."""

    def __init__(self, answers=None, work_dir=None):
        self.segment_requests = []
        self.transcribe_requests = []
        self.prompts = []
        self.answers = list(answers or [])
        self.work_dir = work_dir
        self.state_when_first_asked = None
        self.logs = []

    async def segment_audio(self, ogg_path, work_dir):
        self.segment_requests.append((ogg_path, work_dir))

    async def transcribe(
        self, pcm_path, segment_id, start_s, duration_s, speakers
    ):
        self.transcribe_requests.append(
            (pcm_path, segment_id, start_s, duration_s, speakers)
        )

    async def ask_llm(self, prompt):
        if self.state_when_first_asked is None and self.work_dir:
            self.state_when_first_asked = state(self.work_dir)
        self.prompts.append(prompt)
        return self.answers.pop(0) if self.answers else ""

    def log(self, message):
        self.logs.append(message)


def state(work_dir):
    return json.loads((work_dir / "state.json").read_text(encoding="utf-8"))


def said(start, end, speaker, text, voice):
    return {
        "start_s": start,
        "end_s": end,
        "speaker": speaker,
        "text": text,
        "embedding": voice,
    }


@pytest.fixture
def meeting(tmp_path):
    folder = tmp_path / "m1"
    work = folder / "work"
    work.mkdir(parents=True)
    (folder / "audio.ogg").write_bytes(b"ogg")
    (work / "audio.pcm").write_bytes(b"\x00\x00" * 16000)
    return folder


def upload(folder, speakers=2, script=None):
    payload = {
        "meeting_id": "m1",
        "ogg_path": str(folder / "audio.ogg"),
        "work_dir": str(folder / "work"),
        "title": "週會",
        "speakers": speakers,
    }
    if script:
        payload["script"] = script
    return payload


def topics_ready(folder, *spans):
    return {
        "pcm_path": str(folder / "work" / "audio.pcm"),
        "duration_s": spans[-1][1] if spans else 0.0,
        "segments": [
            {"id": f"t{i:02d}", "start_s": s, "end_s": e}
            for i, (s, e) in enumerate(spans, 1)
        ],
        "error": None,
    }


def transcribed(segment_id, *utterances, error=None):
    return {
        "segment_id": segment_id,
        "utterances": list(utterances),
        "error": error,
    }


def progress(segment_id, done, total, utterance=None, **extra):
    payload = {
        "segment_id": segment_id,
        "step": "recognising",
        "turns_done": done,
        "turns_total": total,
        **extra,
    }
    if utterance is not None:
        payload["utterance"] = utterance
    return payload


def heard(start, end, speaker, text):
    return {"start_s": start, "end_s": end, "speaker": speaker, "text": text}


def record_texts(folder):
    record = json.loads((folder / "record.json").read_text(encoding="utf-8"))
    return [u["text"] for u in record["topics"][0]["utterances"]]


def flow(board):
    return MeetingFlow(MeetingControlConfig(), board)


async def run_two_topics(folder, board, script="traditional"):
    # Traditional, so the record reads as these fixtures are written; the
    # script itself is test_script.py's and the two tests that choose it.
    f = flow(board)
    await f.start(upload(folder, script=script))
    await f.on_segments_ready(
        topics_ready(folder, (0.0, 300.0), (300.0, 600.0))
    )
    await f.on_transcribed(
        transcribed(
            "t01", said(1, 5, 0, "我先說。", A), said(6, 9, 1, "好。", B)
        )
    )
    # Topic 2's diarizer numbered the two people the other way round.
    await f.on_transcribed(transcribed("t02", said(1, 5, 0, "我來回應。", B)))
    return f


@pytest.mark.asyncio
async def test_an_upload_starts_decoding_and_asks_for_topics(meeting):
    board = Board()

    await flow(board).start(upload(meeting))

    assert state(meeting / "work")["state"] == "decoding"
    assert board.segment_requests == [
        (str(meeting / "audio.ogg"), str(meeting / "work"))
    ]


@pytest.mark.asyncio
async def test_topics_are_transcribed_one_at_a_time_as_slices(meeting):
    board = Board()
    f = flow(board)
    await f.start(upload(meeting, speakers=3))
    await f.on_segments_ready(
        topics_ready(meeting, (0.0, 300.0), (300.0, 600.0))
    )

    pcm = str(meeting / "work" / "audio.pcm")
    assert board.transcribe_requests == [(pcm, "t01", 0.0, 300.0, 3)]

    await f.on_transcribed(transcribed("t01", said(1, 5, 0, "甲", A)))

    assert board.transcribe_requests[-1] == (pcm, "t02", 300.0, 300.0, 3)


@pytest.mark.asyncio
async def test_summaries_wait_for_every_topic_and_use_meeting_numbers(meeting):
    board = Board(answers=["一的重點", "二的重點", "結論"])
    f = flow(board)
    await f.start(upload(meeting))
    await f.on_segments_ready(
        topics_ready(meeting, (0.0, 300.0), (300.0, 600.0))
    )
    await f.on_transcribed(
        transcribed(
            "t01", said(1, 5, 0, "我先說。", A), said(6, 9, 1, "好。", B)
        )
    )
    assert board.prompts == []  # nothing is summarised before linking

    await f.on_transcribed(transcribed("t02", said(1, 5, 0, "我來回應。", B)))

    # Topic 2's own speaker 0 is the meeting's second voice.
    assert "说话人2：我來回應。" in board.prompts[1]
    assert "[00:" not in board.prompts[1]  # no clock for the LLM (record.py)


@pytest.mark.asyncio
async def test_the_conclusion_reads_the_summaries_without_the_clock(meeting):
    board = Board(answers=["一的重點", "二的重點", "結論"])

    await run_two_topics(meeting, board)

    conclusion = board.prompts[-1]
    assert "第1段：一的重點" in conclusion
    assert "第2段：二的重點" in conclusion
    assert "05:00" not in conclusion


@pytest.mark.asyncio
async def test_the_meeting_ends_archived_with_record_and_minutes(meeting):
    board = Board(answers=["一的重點", "二的重點", "下週出版本。"])

    await run_two_topics(meeting, board)

    assert state(meeting / "work")["state"] == "archived"
    record = json.loads((meeting / "record.json").read_text(encoding="utf-8"))
    assert [t["summary"] for t in record["topics"]] == ["一的重點", "二的重點"]
    assert record["summary"] == "下週出版本。"
    assert record["speaker_count"] == 2
    assert "下週出版本。" in (meeting / "minutes.txt").read_text(
        encoding="utf-8"
    )
    assert not (meeting / "work" / "audio.pcm").exists()


@pytest.mark.asyncio
async def test_the_record_is_written_in_the_script_the_upload_chose(meeting):
    board = Board(answers=["一的重点", "二的重点", "下周出版本。"])
    f = flow(board)
    chosen = upload(meeting)
    chosen["script"] = "traditional"
    await f.start(chosen)
    await f.on_segments_ready(
        topics_ready(meeting, (0.0, 300.0), (300.0, 600.0))
    )
    await f.on_transcribed(transcribed("t01", said(1, 5, 0, "我先说。", A)))
    await f.on_transcribed(transcribed("t02", said(1, 5, 0, "这样吧。", B)))

    record = json.loads((meeting / "record.json").read_text(encoding="utf-8"))
    minutes = (meeting / "minutes.txt").read_text(encoding="utf-8")
    assert record["topics"][0]["utterances"][0]["text"] == "我先說。"
    assert record["topics"][0]["summary"] == "一的重點"
    assert record["summary"] == "下週出版本。"
    assert "結論" in minutes and "說話人1：我先說。" in minutes
    # The prompts stay Simplified: that is what the board's 7B was measured on.
    assert "说话人1：我先说。" in board.prompts[0]


@pytest.mark.asyncio
async def test_without_a_choice_the_boards_default_applies(meeting):
    board = Board(answers=["一的重点", "二的重点", "结论"])

    await run_two_topics(meeting, board, script=None)

    minutes = (meeting / "minutes.txt").read_text(encoding="utf-8")
    assert "结论" in minutes and "说话人1：我先说。" in minutes
    assert "結論" not in minutes and "說話人" not in minutes


@pytest.mark.asyncio
async def test_a_broken_board_default_falls_back_to_simplified(meeting):
    # The default comes from ${env:MEETING_OUTPUT_SCRIPT|simplified} in the
    # graph; a typo there must not leave every meeting stuck at "decoding".
    board = Board(answers=["一的重点", "二的重点", "结论"])
    f = MeetingFlow(MeetingControlConfig(output_script="Traditional "), board)
    logged = []
    board.log = logged.append
    await f.start(upload(meeting))
    await f.on_segments_ready(
        topics_ready(meeting, (0.0, 300.0), (300.0, 600.0))
    )
    await f.on_transcribed(transcribed("t01", said(1, 5, 0, "我先说。", A)))
    await f.on_transcribed(transcribed("t02", said(1, 5, 0, "这样吧。", B)))

    assert state(meeting / "work")["state"] == "archived"
    assert "说话人1：我先说。" in (meeting / "minutes.txt").read_text(
        encoding="utf-8"
    )
    assert any("Traditional " in line for line in logged)


@pytest.mark.asyncio
async def test_progress_restarts_from_zero_for_the_summaries(meeting):
    board = Board(work_dir=meeting / "work")

    await run_two_topics(meeting, board)

    first = board.state_when_first_asked
    assert (first["state"], first["topics_done"], first["topics_total"]) == (
        "summarising",
        0,
        2,
    )


@pytest.mark.asyncio
async def test_a_topic_that_failed_does_not_stop_the_meeting(meeting):
    board = Board(answers=["二的重點", "結論"])
    f = flow(board)
    await f.start(upload(meeting, script="traditional"))
    await f.on_segments_ready(
        topics_ready(meeting, (0.0, 300.0), (300.0, 600.0))
    )

    await f.on_transcribed(transcribed("t01", error="out of memory"))
    await f.on_transcribed(transcribed("t02", said(1, 5, 0, "還在。", A)))

    record = json.loads((meeting / "record.json").read_text(encoding="utf-8"))
    assert record["topics"][0]["error"] == "out of memory"
    assert record["topics"][1]["summary"] == "二的重點"
    assert len(board.prompts) == 2  # no summary asked for the failed one
    assert state(meeting / "work")["state"] == "archived"


@pytest.mark.asyncio
async def test_a_summary_that_never_came_leaves_that_topic_without_one(meeting):
    board = Board(answers=["", "二的重點", "結論"])  # "" is a timed-out turn

    await run_two_topics(meeting, board)

    record = json.loads((meeting / "record.json").read_text(encoding="utf-8"))
    assert [t["summary"] for t in record["topics"]] == ["", "二的重點"]
    assert state(meeting / "work")["state"] == "archived"


@pytest.mark.asyncio
async def test_an_undecodable_upload_ends_failed_with_the_reason(meeting):
    board = Board()
    f = flow(board)
    await f.start(upload(meeting))

    await f.on_segments_ready(
        {
            "pcm_path": "",
            "duration_s": 0.0,
            "segments": [],
            "error": "ValueError: 48000 Hz",
        }
    )

    final = state(meeting / "work")
    assert (final["state"], final["error"]) == (
        "failed",
        "ValueError: 48000 Hz",
    )
    record = json.loads((meeting / "record.json").read_text(encoding="utf-8"))
    assert record["error"] == "ValueError: 48000 Hz"
    assert board.transcribe_requests == []
    assert (meeting / "audio.ogg").exists()


@pytest.mark.asyncio
async def test_a_meeting_with_no_speech_ends_empty_with_a_record(meeting):
    board = Board()
    f = flow(board)
    await f.start(upload(meeting))

    await f.on_segments_ready(topics_ready(meeting))

    assert state(meeting / "work")["state"] == "empty"
    assert (meeting / "record.json").exists()
    assert (meeting / "minutes.txt").exists()


class Broken(Board):
    async def ask_llm(self, prompt):
        self.prompts.append(prompt)
        raise RuntimeError("llm extension went away")


@pytest.mark.asyncio
async def test_an_llm_that_raises_costs_summaries_not_the_meeting(meeting):
    board = Broken()

    await run_two_topics(meeting, board)

    record = json.loads((meeting / "record.json").read_text(encoding="utf-8"))
    assert [t["summary"] for t in record["topics"]] == ["", ""]
    assert len(record["topics"][0]["utterances"]) == 2  # transcript intact
    assert state(meeting / "work")["state"] == "archived"


@pytest.mark.asyncio
async def test_a_second_upload_while_one_is_running_is_turned_away(
    meeting, tmp_path
):
    other = tmp_path / "m2"
    (other / "work").mkdir(parents=True)
    (other / "audio.ogg").write_bytes(b"ogg")
    board = Board()
    f = flow(board)
    await f.start(upload(meeting))

    second = upload(other)
    second["meeting_id"] = "m2"
    await f.start(second)

    turned_away = state(other / "work")
    assert turned_away["state"] == "failed"
    assert "m1" in turned_away["error"]
    assert state(meeting / "work")["state"] == "decoding"
    assert len(board.segment_requests) == 1


@pytest.mark.asyncio
async def test_a_start_that_cannot_set_up_ends_failed_and_takes_the_next(
    meeting, tmp_path, monkeypatch
):
    # A package the board lacks (opencc, before install_board_arm64.sh)
    # once left the meeting at "received" for good: the controller stayed
    # busy, the uploader answered 409 and pinged forever, and the worker
    # was never reaped.
    def missing(_script):
        raise ModuleNotFoundError("No module named 'opencc'")

    monkeypatch.setattr("meeting_control_python.flow.converter", missing)
    board = Board()
    f = flow(board)
    await f.start(upload(meeting))

    failed = state(meeting / "work")
    assert failed["state"] == "failed"
    assert "opencc" in failed["error"]
    assert board.segment_requests == []

    monkeypatch.undo()
    other = tmp_path / "m2"
    (other / "work").mkdir(parents=True)
    (other / "audio.ogg").write_bytes(b"ogg")
    second = upload(other)
    second["meeting_id"] = "m2"
    await f.start(second)
    assert state(other / "work")["state"] == "decoding"


@pytest.mark.asyncio
async def test_whatever_raises_later_ends_the_meeting_failed(meeting, tmp_path):
    board = Board()
    f = flow(board)
    await f.start(upload(meeting))

    await f.abandon("on_segments_ready: KeyError: 'pcm_path'")

    failed = state(meeting / "work")
    assert failed["state"] == "failed"
    assert "pcm_path" in failed["error"]
    assert (meeting / "record.json").exists()

    other = tmp_path / "m2"
    (other / "work").mkdir(parents=True)
    (other / "audio.ogg").write_bytes(b"ogg")
    second = upload(other)
    second["meeting_id"] = "m2"
    await f.start(second)
    assert state(other / "work")["state"] == "decoding"


@pytest.mark.asyncio
async def test_abandoning_with_nothing_in_hand_does_nothing(meeting):
    await flow(Board()).abandon("late")

    assert not (meeting / "work" / "state.json").exists()


@pytest.mark.asyncio
async def test_each_topic_starts_by_telling_its_speakers_apart(meeting):
    f = flow(Board())
    await f.start(upload(meeting))
    await f.on_segments_ready(
        topics_ready(meeting, (0.0, 300.0), (300.0, 600.0))
    )

    now = state(meeting / "work")
    assert (now["state"], now["step"], now["topic_s"]) == (
        "transcribing",
        "diarizing",
        300.0,
    )
    assert (now["turns_done"], now["turns_total"]) == (0, 0)
    assert isinstance(now["step_started_at"], float)


@pytest.mark.asyncio
async def test_recognition_counts_turns_and_shows_the_words_so_far(meeting):
    f = flow(Board())
    await f.start(upload(meeting, script="traditional"))
    await f.on_segments_ready(topics_ready(meeting, (0.0, 300.0)))

    await f.on_progress(progress("t01", 0, 2, diarize_s=140.0))
    now = state(meeting / "work")
    assert (now["step"], now["turns_done"], now["turns_total"]) == (
        "recognising",
        0,
        2,
    )

    await f.on_progress(progress("t01", 1, 2, heard(1, 5, 0, "我先說。")))
    assert state(meeting / "work")["turns_done"] == 1
    assert record_texts(meeting) == ["我先說。"]


@pytest.mark.asyncio
async def test_words_so_far_are_saved_at_most_once_a_second(meeting):
    now = [1000.0]
    f = MeetingFlow(MeetingControlConfig(), Board(), clock=lambda: now[0])
    await f.start(upload(meeting, script="traditional"))
    await f.on_segments_ready(topics_ready(meeting, (0.0, 300.0)))

    await f.on_progress(progress("t01", 1, 3, heard(1, 5, 0, "一。")))
    now[0] += 0.4
    await f.on_progress(progress("t01", 2, 3, heard(6, 9, 1, "二。")))
    assert record_texts(meeting) == ["一。"]
    now[0] += 0.4
    # The last turn is always written.
    await f.on_progress(progress("t01", 3, 3, heard(10, 12, 0, "三。")))
    assert record_texts(meeting) == ["一。", "二。", "三。"]


@pytest.mark.asyncio
async def test_the_finished_topic_replaces_its_words_so_far(meeting):
    board = Board()
    f = flow(board)
    await f.start(upload(meeting, script="traditional"))
    await f.on_segments_ready(
        topics_ready(meeting, (0.0, 300.0), (300.0, 600.0))
    )
    await f.on_progress(progress("t01", 1, 2, heard(1, 5, 0, "我先說。")))

    timing = {"diarize_s": 140.0, "recognise_s": 90.0, "embed_s": 20.0}
    timing["turns"] = 2
    await f.on_transcribed(
        {
            **transcribed(
                "t01", said(1, 5, 0, "我先說。", A), said(6, 9, 1, "好。", B)
            ),
            "timing": timing,
        }
    )

    record = json.loads((meeting / "record.json").read_text(encoding="utf-8"))
    topic = record["topics"][0]
    assert [u["text"] for u in topic["utterances"]] == ["我先說。", "好。"]
    assert topic["timing"] == timing
    assert any("diarized 140.0 s" in line for line in board.logs)
    now = state(meeting / "work")
    assert (now["step"], now["topic_s"], now["topics_done"]) == (
        "diarizing",
        300.0,
        1,
    )


@pytest.mark.asyncio
async def test_progress_for_another_topic_or_after_the_last_is_dropped(
    meeting,
):
    f = flow(Board())
    await f.start(upload(meeting))
    await f.on_segments_ready(topics_ready(meeting, (0.0, 300.0)))

    await f.on_progress(progress("t02", 5, 9))
    assert state(meeting / "work")["step"] == "diarizing"

    await f.on_transcribed(transcribed("t01", said(1, 5, 0, "甲", A)))
    ended = state(meeting / "work")
    await f.on_progress(progress("t01", 1, 1, heard(1, 5, 0, "甲")))
    assert state(meeting / "work") == ended


@pytest.mark.asyncio
async def test_a_broken_progress_report_is_dropped_not_fatal(meeting):
    board = Board()
    f = flow(board)
    await f.start(upload(meeting))
    await f.on_segments_ready(topics_ready(meeting, (0.0, 300.0)))

    await f.on_progress({"segment_id": "t01", "turns_done": "many"})

    assert state(meeting / "work")["state"] == "transcribing"
    assert any("progress dropped" in line for line in board.logs)
