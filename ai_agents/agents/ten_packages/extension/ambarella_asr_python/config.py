#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#

from typing import Any, Dict, List

from pydantic import BaseModel, Field

# asr_d wants whole words, not ISO codes: --language chinese, not --language zh.
_DAEMON_LANGUAGE = {
    "zh": "chinese",
    "zh-CN": "chinese",
    "zh-TW": "chinese",
    "en": "english",
    "en-US": "english",
    "en-GB": "english",
}

# Reported back on ASRResult, mirroring whisper_stt_python.normalized_language.
_TEN_LANGUAGE = {
    "chinese": "zh-CN",
    "english": "en-US",
}

# --cap_dev would enable INFER_MIC, which bypasses the TEN audio pipeline.
# This extension is file-mode only, so the flag is dropped rather than honoured.
_DROPPED_PARAMS = ("cap_dev",)


class AmbarellaASRConfig(BaseModel):
    """Configuration for the on-board asr_d daemon."""

    bin_path: str = ""
    model_dir: str = ""
    model_type: str = "tiny"
    tmp_dir: str = "/tmp"

    # Audio shorter than this never reaches the VP; asr_d answers
    # ERR audio_too_short and the round trip is wasted. The daemon's real
    # file-mode floor is undocumented -- see spec section 9.
    min_audio_ms: int = 200

    load_timeout_s: float = 180.0
    infer_timeout_s: float = 30.0
    quit_timeout_s: float = 5.0

    # Every restart pays a full model load, so the retry loop is capped.
    restart_max_attempts: int = 3

    params: Dict[str, Any] = Field(default_factory=dict)

    @property
    def daemon_language(self) -> str:
        """The --language value asr_d expects."""
        # pylint: disable=no-member
        raw = str(self.params.get("language", "chinese"))
        return _DAEMON_LANGUAGE.get(raw, raw)

    @property
    def normalized_language(self) -> str:
        """The language code reported back to the pipeline."""
        return _TEN_LANGUAGE.get(self.daemon_language, self.daemon_language)

    def daemon_flags(self) -> List[str]:
        """Build asr_d's argv from model_dir plus the params pass-through."""
        flags = [
            "--cavalry_dir",
            self.model_dir,
            "--type",
            self.model_type,
        ]
        # pylint: disable=no-member
        params = {
            key: value
            for key, value in self.params.items()
            if key not in _DROPPED_PARAMS
        }
        params["language"] = self.daemon_language
        params.setdefault("log", 1)
        for key in sorted(params):
            flags.extend([f"--{key}", str(params[key])])
        return flags
