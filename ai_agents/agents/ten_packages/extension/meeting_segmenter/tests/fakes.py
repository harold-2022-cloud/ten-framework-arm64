#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""A stand-in for sherpa-onnx's VadModel.

The real one is a 332 KB ONNX model that lives on the board. This keeps the
contract segmenter.py relies on: window_size() samples in, one bool out,
samples as float in [-1, 1].
"""

import numpy as np

WINDOW = 1600  # 0.1 s at 16 kHz, so expected seconds read cleanly


class ScriptedVad:
    """Answers each window from a script and keeps what it was fed."""

    def __init__(self, flags):
        self.flags = list(flags)
        self.windows = []

    def window_size(self):
        return WINDOW

    def is_speech(self, samples):
        self.windows.append(np.asarray(samples))
        return self.flags.pop(0) if self.flags else False
