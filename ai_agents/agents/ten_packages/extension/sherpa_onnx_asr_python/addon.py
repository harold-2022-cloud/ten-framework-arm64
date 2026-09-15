#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
from ten_runtime import (
    Addon,
    TenEnv,
    register_addon_as_extension,
)

from .extension import SherpaOnnxASRExtension


@register_addon_as_extension("sherpa_onnx_asr_python")
class SherpaOnnxASRExtensionAddon(Addon):

    def on_create_instance(self, ten_env: TenEnv, name: str, context) -> None:
        ten_env.log_info("SherpaOnnxASRExtensionAddon on_create_instance")
        ten_env.on_create_instance_done(SherpaOnnxASRExtension(name), context)
