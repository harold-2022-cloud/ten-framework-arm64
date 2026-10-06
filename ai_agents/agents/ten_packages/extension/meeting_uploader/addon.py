#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
from ten_runtime import Addon, TenEnv, register_addon_as_extension

from .extension import MeetingUploaderExtension


@register_addon_as_extension("meeting_uploader")
class MeetingUploaderAddon(Addon):
    def on_create_instance(self, ten_env: TenEnv, name: str, context) -> None:
        ten_env.on_create_instance_done(MeetingUploaderExtension(name), context)
