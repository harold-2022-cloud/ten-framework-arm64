#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#

import copy
import os
from typing import Any, Dict

from pydantic import BaseModel, Field

from .const import DEFAULT_NUM_THREADS


class SherpaOnnxASRConfig(BaseModel):
    """Configuration for a streaming Zipformer run through sherpa-onnx."""

    # A directory, not a file: the transducer is four files that have to match
    # each other, so they are named relative to one bundle.
    model_dir: str = ""

    num_threads: int = DEFAULT_NUM_THREADS

    # The engine's own endpoint rules, kept under their own names so the
    # meaning survives the trip through property.json.
    enable_endpoint_detection: bool = True
    rule1_min_trailing_silence: float = 2.4
    rule2_min_trailing_silence: float = 1.2
    rule3_min_utterance_length: float = 20.0

    # The model is bilingual; this is what goes out on the result, for
    # consumers that care, not an instruction to the model.
    language: str = "zh-CN"

    dump: bool = Field(default=False)
    dump_path: str = Field(default="/tmp")
    params: Dict[str, Any] = Field(default_factory=dict)

    def to_str(self, sensitive_handling: bool = True) -> str:
        """Render the config. A local model has no credentials."""
        if not sensitive_handling:
            return f"{self}"
        return f"{copy.deepcopy(self)}"

    def validate_model(self) -> None:
        """Fail at start, not at the first spoken word."""
        if not self.model_dir:
            raise ValueError("model_dir is required for sherpa-onnx ASR")
        if not os.path.isdir(self.model_dir):
            raise ValueError(f"model_dir does not exist: {self.model_dir}")
