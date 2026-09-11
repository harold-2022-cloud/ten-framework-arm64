#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#

import copy
from typing import Any, Dict, List

from pydantic import Field
from ten_ai_base.tts2_http import AsyncTTS2HttpConfig

from .const import OUTPUT_SAMPLE_RATE


class AmbarellaTTSConfig(AsyncTTS2HttpConfig):
    """Configuration for the on-board tts_d daemon."""

    bin_path: str = ""
    model_dir: str = ""
    tmp_dir: str = "/tmp"

    # One over-long sentence is split on punctuation so the first audio does
    # not wait for the whole synthesis.
    max_chars: int = 200

    # Declared to the pipeline. tts_d always emits 22050; the resampling ratio
    # is derived from the WAV header on every response.
    output_sample_rate: int = OUTPUT_SAMPLE_RATE

    load_timeout_s: float = 180.0
    infer_timeout_s: float = 30.0
    quit_timeout_s: float = 5.0
    restart_max_attempts: int = 3

    dump: bool = Field(default=False)
    dump_path: str = Field(default="/tmp")
    params: Dict[str, Any] = Field(default_factory=dict)

    def daemon_flags(self) -> List[str]:
        """Build tts_d's argv from model_dir plus the params pass-through."""
        flags = ["--model_dir", self.model_dir]
        params = dict(self.params)
        params.setdefault("log", 1)
        for key in sorted(params):
            flags.extend([f"--{key}", str(params[key])])
        return flags

    def update_params(self) -> None:
        """Coerce the numeric pass-through params tts_d validates."""
        for key in ("speaker_id", "rand_seed", "log"):
            if key in self.params:
                self.params[key] = int(self.params[key])

    def to_str(self, sensitive_handling: bool = True) -> str:
        """Render the config. The board interface has no credentials."""
        if not sensitive_handling:
            return f"{self}"
        return f"{copy.deepcopy(self)}"

    def validate(self) -> None:
        if not self.bin_path:
            raise ValueError("bin_path is required for Ambarella TTS")
        if not self.model_dir:
            raise ValueError("model_dir is required for Ambarella TTS")
