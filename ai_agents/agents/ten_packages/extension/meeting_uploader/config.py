#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#

from pydantic import BaseModel


class MeetingUploaderConfig(BaseModel):
    """Where meetings land, what is let in, and how the worker stays up."""

    listen_port: int = 8765
    meetings_dir: str = "/tmp/meetings"

    # An hour of 16 kHz mono Ogg-Opus is about 6.4 MB; 64 leaves room for a
    # long meeting at a higher bitrate and still turns away a stray WAV.
    max_upload_mb: float = 64.0
    # Room the processing needs after the upload lands: work/audio.pcm alone
    # is 115 MB an hour.
    min_free_mb: float = 250.0

    # Empty: no check. Set: every request must carry
    # "Authorization: Bearer <auth_token>".
    auth_token: str = ""

    # The Go server injects channel into any node that declares it; the
    # uploader pings /ping with it while a meeting is being processed, so
    # the worker is not reaped an hour in. Empty ping_url means the server
    # this worker was started by: 127.0.0.1 on $SERVER_PORT.
    channel: str = ""
    ping_url: str = ""
    keepalive_s: float = 20.0

    # How often a meeting waiting its turn is looked at, to be handed on
    # once the one before it ends.
    queue_poll_s: float = 2.0
