#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""Every graph the board runs asks Qwen3.5-4B: none is left on the 7B, and
the voice graphs keep their own prompts, name the key and the model (an
unset ${env:...} with no default stops a graph at its start), and ask with
no repetition penalty -- the extension's 0.9 is OpenAI's, and in Chinese
it penalises the commonest characters.

  python3 -m unittest discover -s tools/ambarella/tests -v
"""

import json
import os
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
APPS = ("voice-assistant", "voice-assistant-ambarella")
QWEN = {
    "base_url": "${env:QWEN_LLM_BASE_URL|http://127.0.0.1:8000/v1}",
    "api_key": "EMPTY",
    "model": "qwen3.5_4B",
}
SPOKEN_EN = "You are a helpful assistant running on an Ambarella N1-655 board. Keep answers short and spoken."
SPOKEN_ZH = "用中文简短口语回答，不要条列或符号。"
VOICE = {
    ("voice-assistant", "voice_assistant_soniox_ambarella_llm"): SPOKEN_EN,
    ("voice-assistant", "voice_assistant_sherpa_tts"): SPOKEN_ZH,
    ("voice-assistant", "voice_assistant_sherpa_full"): SPOKEN_ZH,
    ("voice-assistant-ambarella", "voice_assistant"): SPOKEN_EN,
}


def graphs(app):
    path = os.path.join(REPO, "ai_agents", "agents", "examples", app, "tenapp", "property.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)["ten"]["predefined_graphs"]


def llm(app, name):
    graph = next(g for g in graphs(app) if g["name"] == name)
    return next(n for n in graph["graph"]["nodes"] if n["name"] == "llm")


class BoardGraphs(unittest.TestCase):
    def test_no_graph_is_left_on_the_7b(self):
        left = [
            (app, g["name"])
            for app in APPS
            for g in graphs(app)
            for n in g["graph"]["nodes"]
            if n.get("addon") == "ambarella_llm2_python"
        ]
        self.assertEqual(left, [])

    def test_the_voice_graphs_ask_qwen_with_their_own_prompts(self):
        for (app, name), prompt in VOICE.items():
            node = llm(app, name)
            self.assertEqual(node["addon"], "openai_llm2_python", name)
            self.assertEqual(
                node["property"],
                dict(QWEN, prompt=prompt, max_tokens=512, frequency_penalty=0.0),
                name,
            )


if __name__ == "__main__":
    unittest.main()
