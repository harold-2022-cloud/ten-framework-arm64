#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#

from pydantic import BaseModel


class MeetingSegmenterConfig(BaseModel):
    """Where topics are cut, and the model that hears speech."""

    # Silence that ends a topic. A starting point, not a measurement: reading
    # a document runs well past ten seconds, and a new subject seldom starts
    # sooner than thirty. Real meetings will say where it belongs.
    segment_silence_s: float = 30.0
    min_segment_s: float = 5.0
    # The memory ceiling. Diarization holds a whole topic at once, so the
    # longest topic, not the meeting, sets how much the board needs.
    max_segment_s: float = 600.0

    # sherpa-onnx's TEN VAD (ten-vad.onnx), not the ten-vad pip package:
    # that one ships a Linux library for x86_64 only and raises
    # NotImplementedError on the aarch64 board.
    vad_model: str = ""
    vad_threshold: float = 0.5
