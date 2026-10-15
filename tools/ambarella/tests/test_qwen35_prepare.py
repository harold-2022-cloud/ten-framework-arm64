#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""qwen35_prepare.py model: the chat template keeps thinking off unless a
request asks for it, and a package that has the first edit (off only when
asked) is brought up to it.

  python3 -m unittest discover -s tools/ambarella/tests -v
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import qwen35_prepare as prep  # noqa: E402  pylint: disable=wrong-import-position

try:
    import jinja2
except ImportError:  # the board's python3 may not have it
    jinja2 = None

HEAD = "{%- for m in messages %}<|im_start|>{{ m.role }}\n{{ m.content }}<|im_end|>\n{%- endfor %}\n"


def package(root, generation):
    """A Qwen3.5 package whose ranch/ needs nothing downloaded."""
    pkg = os.path.join(root, "qwen3.5-4b_n1655-video")
    info = os.path.join(pkg, "ranch", "info")
    os.makedirs(info)
    os.makedirs(os.path.join(pkg, "vit"))
    with open(os.path.join(info, "config.json"), "w", encoding="utf-8") as f:
        json.dump({"model_type": "qwen3_5_text",
                   "architectures": ["Qwen3_5ForConditionalGeneration"],
                   "vision_config": {"image_size": 448}}, f)
    for name in ("tokenizer.json", "prompt_config.json"):
        with open(os.path.join(info, name), "w", encoding="utf-8") as f:
            f.write("{}")
    with open(os.path.join(pkg, "vit", prep.VIT_NAME), "wb") as f:
        f.write(b"\0")
    ranch = os.path.join(pkg, "ranch")
    with open(os.path.join(ranch, "generation_config.json"), "w", encoding="utf-8") as f:
        f.write("{}")
    with open(os.path.join(ranch, "preprocessor_config.json"), "w", encoding="utf-8") as f:
        json.dump({"size": prep.SIZE}, f)
    with open(os.path.join(ranch, "tokenizer_config.json"), "w", encoding="utf-8") as f:
        json.dump({"chat_template": HEAD + generation}, f)
    return pkg


def template(pkg):
    with open(os.path.join(pkg, "ranch", "tokenizer_config.json"), encoding="utf-8") as f:
        return json.load(f)["chat_template"]


def quietly(fn, *args):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*args)


class Template(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_the_first_edit_is_brought_up_to_off_by_default(self):
        pkg = package(self.tmp.name, prep.GEN_ASKED_OFF)
        quietly(prep.model, pkg, False, prep.VIT_NAME)
        self.assertIn(prep.GEN_NEW, template(pkg))
        self.assertNotIn(prep.GEN_ASKED_OFF, template(pkg))

    def test_the_original_template_is_brought_up_too(self):
        pkg = package(self.tmp.name, prep.GEN_OLD)
        quietly(prep.model, pkg, False, prep.VIT_NAME)
        self.assertIn(prep.GEN_NEW, template(pkg))

    def test_a_second_run_changes_nothing(self):
        pkg = package(self.tmp.name, prep.GEN_ASKED_OFF)
        quietly(prep.model, pkg, False, prep.VIT_NAME)
        first = template(pkg)
        todo = quietly(prep.model, pkg, True, prep.VIT_NAME)
        self.assertEqual(first, template(pkg))
        self.assertEqual(todo, [])

    def test_check_says_what_is_to_do_and_changes_nothing(self):
        pkg = package(self.tmp.name, prep.GEN_ASKED_OFF)
        todo = quietly(prep.model, pkg, True, prep.VIT_NAME)
        self.assertIn(prep.GEN_ASKED_OFF, template(pkg))
        self.assertTrue(any("thinking" in t for t in todo), todo)

    def test_an_unknown_template_stops(self):
        pkg = package(self.tmp.name, "{{- 'no generation prompt here' }}")
        with self.assertRaises(prep.Stop):
            quietly(prep.model, pkg, False, prep.VIT_NAME)

    @unittest.skipUnless(jinja2, "jinja2 is not installed")
    def test_thinking_is_off_unless_a_request_turns_it_on(self):
        env = jinja2.Environment()
        tpl = env.from_string(HEAD + prep.GEN_NEW)
        msgs = [{"role": "user", "content": "hi"}]
        empty = "<think>\n\n</think>\n\n"
        self.assertTrue(tpl.render(messages=msgs, add_generation_prompt=True).endswith(empty))
        self.assertTrue(tpl.render(messages=msgs, add_generation_prompt=True,
                                   enable_thinking=False).endswith(empty))
        self.assertNotIn("<think>", tpl.render(messages=msgs, add_generation_prompt=True,
                                               enable_thinking=True))


if __name__ == "__main__":
    unittest.main()
