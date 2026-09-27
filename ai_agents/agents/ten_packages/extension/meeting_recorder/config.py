#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#

from pydantic import BaseModel


class MeetingRecorderConfig(BaseModel):
    """Where the segments go, and how much room they need."""

    # One directory per deployment, not per meeting: the file names carry the
    # time, and a meeting that crashed leaves its segments where the next run
    # can still find them.
    output_dir: str = "/tmp/meeting_segments"
    # Refuse to start rather than truncate an hour in. 16 kHz PCM16 is
    # 115 MB/hour; this is a little over two hours.
    min_free_mb: int = 250
