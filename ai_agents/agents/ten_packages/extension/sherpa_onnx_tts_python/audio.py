#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""Turning what the voice produces into what the pipeline was promised.

sherpa-onnx hands back float32 samples at the model's own rate. agora_rtc
wants 16-bit PCM at the rate the RTSA service was told about when it started,
and that rate is fixed for the life of the connection.
"""

import math

import numpy as np
from scipy.signal import resample_poly


def float_to_pcm16(samples: np.ndarray) -> bytes:
    """Scale float samples in [-1, 1] to 16-bit PCM, clipping overshoot.

    A voice can exceed the nominal range on a loud syllable; left alone that
    wraps around in int16 and is heard as a click.
    """
    scaled = np.asarray(samples, dtype=np.float32) * 32767.0
    return np.clip(scaled, -32767.0, 32767.0).astype(np.int16).tobytes()


def resample_pcm16(pcm: bytes, source_rate: int, target_rate: int) -> bytes:
    """Convert 16-bit mono PCM between two sample rates."""
    if source_rate == target_rate:
        return pcm

    samples = np.frombuffer(pcm, dtype=np.int16)
    divisor = math.gcd(target_rate, source_rate)
    # resample_poly applies an FIR anti-aliasing filter. This is a downsample
    # -- 22050 to 16000 -- so without one the content above the target's 8 kHz
    # Nyquist limit folds back into the audible band.
    resampled = resample_poly(
        samples.astype(np.float32),
        target_rate // divisor,
        source_rate // divisor,
    )
    return np.clip(resampled, -32767.0, 32767.0).astype(np.int16).tobytes()
