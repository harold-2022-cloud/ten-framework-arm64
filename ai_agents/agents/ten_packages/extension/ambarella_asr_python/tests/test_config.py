#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#

from ambarella_asr_python.config import AmbarellaASRConfig
from ambarella_asr_python.const import MAX_BUFFER_BYTES


def test_defaults():
    config = AmbarellaASRConfig()
    assert config.model_type == "tiny"
    assert config.tmp_dir == "/tmp"
    assert config.min_audio_ms == 200
    assert config.load_timeout_s == 180.0
    assert config.infer_timeout_s == 30.0
    assert config.quit_timeout_s == 5.0
    assert config.restart_max_attempts == 3
    assert config.params == {}


def test_buffer_ceiling_matches_thirty_seconds():
    assert MAX_BUFFER_BYTES == 960_000


def test_flags_carry_model_dir_and_type():
    config = AmbarellaASRConfig(
        bin_path="/opt/asr_d", model_dir="/models/whisper"
    )
    flags = config.daemon_flags()
    assert flags[:4] == ["--cavalry_dir", "/models/whisper", "--type", "tiny"]


def test_flags_expand_params_deterministically():
    config = AmbarellaASRConfig(
        model_dir="/m",
        params={"beam_size": 5, "no_speech_thres": 0.6, "language": "chinese"},
    )
    flags = config.daemon_flags()
    assert flags == [
        "--cavalry_dir",
        "/m",
        "--type",
        "tiny",
        "--beam_size",
        "5",
        "--language",
        "chinese",
        "--log",
        "1",
        "--no_speech_thres",
        "0.6",
    ]


def test_log_defaults_to_error_level():
    config = AmbarellaASRConfig(model_dir="/m")
    flags = config.daemon_flags()
    assert flags[flags.index("--log") + 1] == "1"


def test_explicit_log_level_wins():
    config = AmbarellaASRConfig(model_dir="/m", params={"log": 4})
    flags = config.daemon_flags()
    assert flags[flags.index("--log") + 1] == "4"


def test_cap_dev_is_dropped_so_infer_mic_stays_unavailable():
    config = AmbarellaASRConfig(model_dir="/m", params={"cap_dev": "default"})
    assert "--cap_dev" not in config.daemon_flags()


def test_ten_language_codes_map_to_daemon_words():
    for code in ("zh", "zh-CN", "zh-TW"):
        assert AmbarellaASRConfig(
            params={"language": code}
        ).daemon_language == ("chinese")
    for code in ("en", "en-US", "en-GB"):
        assert AmbarellaASRConfig(
            params={"language": code}
        ).daemon_language == ("english")


def test_auto_language_passes_through():
    config = AmbarellaASRConfig(params={"language": "auto"})
    assert config.daemon_language == "auto"


def test_language_defaults_to_chinese():
    assert AmbarellaASRConfig().daemon_language == "chinese"


def test_normalized_language_reports_ten_codes():
    assert AmbarellaASRConfig(
        params={"language": "zh"}
    ).normalized_language == ("zh-CN")
    assert (
        AmbarellaASRConfig(params={"language": "english"}).normalized_language
        == "en-US"
    )
