#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#

import os

from pydantic import BaseModel


class MeetingTranscriberConfig(BaseModel):
    """Two models and how hard to work them."""

    segmentation_model: str = ""
    embedding_model: str = ""
    # SenseVoice rather than the streaming Zipformer the assistant uses:
    # it punctuates. A meeting transcript without punctuation is three
    # thousand characters in one breath, and the summariser gets it too.
    asr_model_dir: str = ""

    # Diarization and recognition run after a topic ends, when the board is
    # otherwise idle, so they may have more of it than the live path takes.
    num_threads: int = 2

    # Given the count, clustering is right; without it the same person comes
    # back as several. Measured on the board: four speakers found as seven.
    speakers: int = -1
    cluster_threshold: float = 0.5

    def validate_models(self) -> None:
        """Fail when the pipeline is wired, not at the first silence."""
        for name, path in (
            ("segmentation_model", self.segmentation_model),
            ("embedding_model", self.embedding_model),
        ):
            if not path or not os.path.isfile(path):
                raise ValueError(f"{name} is not a file: {path!r}")
        if not self.asr_model_dir or not os.path.isdir(self.asr_model_dir):
            raise ValueError(
                f"asr_model_dir is not a directory: {self.asr_model_dir!r}"
            )
