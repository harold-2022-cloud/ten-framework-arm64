#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""A voice that is not there should stop the extension, not the first sentence.

Loading happens in the background so the first spoken sentence does not wait
for it, which means a missing file would otherwise surface minutes later as
silence, in the middle of a conversation.
"""

import pytest

from sherpa_onnx_tts_python.config import SherpaOnnxTTSConfig


def test_a_missing_voice_directory_is_refused(tmp_path):
    config = SherpaOnnxTTSConfig(voice_dir=str(tmp_path / "nope"))
    with pytest.raises(ValueError, match="voice_dir"):
        config.validate()
