#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#

import pytest

from ambarella_tts_python.config import AmbarellaTTSConfig
from ambarella_tts_python.const import (
    CHUNK_BYTES,
    NATIVE_SAMPLE_RATE,
    OUTPUT_SAMPLE_RATE,
)


def test_fixed_rates():
    assert NATIVE_SAMPLE_RATE == 22050
    assert OUTPUT_SAMPLE_RATE == 16000
    assert CHUNK_BYTES == 640


def test_defaults():
    config = AmbarellaTTSConfig(
        bin_path="/opt/tts_d", model_dir="/models/openvoice"
    )
    assert config.tmp_dir == "/tmp"
    assert config.max_chars == 200
    assert config.output_sample_rate == 16000
    assert config.load_timeout_s == 180.0
    assert config.infer_timeout_s == 30.0
    assert config.quit_timeout_s == 5.0
    assert config.restart_max_attempts == 3


def test_flags_carry_model_dir():
    config = AmbarellaTTSConfig(
        bin_path="/opt/tts_d", model_dir="/models/openvoice"
    )
    flags = config.daemon_flags()
    assert flags[:2] == ["--model_dir", "/models/openvoice"]


def test_flags_expand_params_deterministically():
    config = AmbarellaTTSConfig(
        bin_path="/b",
        model_dir="/m",
        params={"speaker_id": 3, "rand_seed": 42},
    )
    assert config.daemon_flags() == [
        "--model_dir",
        "/m",
        "--log",
        "1",
        "--rand_seed",
        "42",
        "--speaker_id",
        "3",
    ]


def test_log_defaults_to_error_level():
    config = AmbarellaTTSConfig(bin_path="/b", model_dir="/m")
    flags = config.daemon_flags()
    assert flags[flags.index("--log") + 1] == "1"


def test_validate_requires_bin_path_and_model_dir():
    with pytest.raises(ValueError, match="bin_path"):
        AmbarellaTTSConfig(model_dir="/m").validate()
    with pytest.raises(ValueError, match="model_dir"):
        AmbarellaTTSConfig(bin_path="/b").validate()


def test_validate_accepts_a_complete_config():
    AmbarellaTTSConfig(bin_path="/b", model_dir="/m").validate()
