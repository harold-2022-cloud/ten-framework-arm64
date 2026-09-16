#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#

import copy
import os
from typing import Any, Dict

from pydantic import Field
from ten_ai_base.tts2_http import AsyncTTS2HttpConfig

from .const import (
    CHARS_PER_PIECE,
    DEFAULT_NUM_THREADS,
    MIN_CHARS_TO_SPLIT,
    OUTPUT_SAMPLE_RATE,
)


class SherpaOnnxTTSConfig(AsyncTTS2HttpConfig):
    """Configuration for a VITS voice run through sherpa-onnx."""

    # A directory, not a file: sherpa-onnx wants the model, its tokens and the
    # espeak-ng data together, which is why the voice comes from sherpa-onnx's
    # repackaged bundle rather than from Hugging Face.
    voice_dir: str = ""

    output_sample_rate: int = OUTPUT_SAMPLE_RATE
    num_threads: int = DEFAULT_NUM_THREADS
    speed: float = 1.0
    speaker_id: int = 0

    # Text longer than this is handed to the engine a clause at a time, so
    # the first audio does not wait for the last word. Zero disables it.
    min_chars_to_split: int = MIN_CHARS_TO_SPLIT
    # How much of the rest goes in each following piece.
    chars_per_piece: int = CHARS_PER_PIECE

    dump: bool = Field(default=False)
    dump_path: str = Field(default="/tmp")
    params: Dict[str, Any] = Field(default_factory=dict)

    def update_params(self) -> None:
        """Coerce the numeric pass-through params."""
        for key in ("num_threads", "speaker_id"):
            if key in self.params:
                self.params[key] = int(self.params[key])
        if "speed" in self.params:
            self.params["speed"] = float(self.params["speed"])

    def to_str(self, sensitive_handling: bool = True) -> str:
        """Render the config. A local voice has no credentials."""
        if not sensitive_handling:
            return f"{self}"
        return f"{copy.deepcopy(self)}"

    def validate(self) -> None:
        if not self.voice_dir:
            raise ValueError("voice_dir is required for sherpa-onnx TTS")
        if not os.path.isdir(self.voice_dir):
            raise ValueError(f"voice_dir does not exist: {self.voice_dir}")
