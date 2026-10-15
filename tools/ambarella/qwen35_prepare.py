#!/usr/bin/env python3
#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""The changes install_qwen35_vllm.sh makes outside pip, kept apart so that
they can be checked on any machine.

  python3 tools/ambarella/qwen35_prepare.py amba-ext <transformers_amba_ext>
  python3 tools/ambarella/qwen35_prepare.py model <Qwen3.5-4B package>
  python3 tools/ambarella/qwen35_prepare.py model <package> --check

amba-ext  transformers_amba_ext's AutoModelForVision picks the model class by
          the model_type in config.json (models/auto/modeling_auto.py). Two
          changes there:
          - qwen3_5_text and qwen3_5 map to VLMForCausalLM, as the vendor's
            guide says;
          - a type not in the mapping still gets VLMForCausalLM when the
            config's architectures name Qwen3_5. The guide calls this
            optional. It is not: config.json, below, has to say "qwen3" for
            transformers 4.52 to read it at all; vLLM's --hf-overrides never
            reach this call (cooper-vllm's transformers_amba_ext_vlm.py
            passes it the model path and nothing else); and "qwen3" is not
            in the mapping.

model     ranch/ in the package is what vllm serve loads:
          - tokenizer.json and prompt_config.json linked from info/, and the
            ViT binary linked from where the package keeps it. The guide's
            link, ../../vit/<bin> from ranch/, is a level off the layout it
            shows (ranch/ and vit/ side by side), so the binary is looked
            for, not assumed;
          - tokenizer_config.json, generation_config.json and
            preprocessor_config.json from Qwen/Qwen3-VL-4B-Instruct on
            hf-mirror.com, as the guide does;
          - config.json from info/config.json with model_type "qwen3" --
            transformers 4.52 does not know qwen3_5_text -- and the vision
            image size 384;
          - the preprocessor's size 384x384;
          - the chat template made to keep thinking off -- an empty
            <think></think> -- unless a request sets enable_thinking: true;
            a package with the earlier edit, off only when asked, is
            brought up to it.
          Every file it replaces is kept first in ranch/.before-qwen35/, once.
          A symlink is replaced, never written through: ranch/config.json may
          well be a link to info/config.json.

Each change is skipped when it is already made, so it can run again.
Standard library only. Exits non-zero, saying why, when something is not as
expected.
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
import urllib.request

# --- amba-ext ---------------------------------------------------------------

AUTO = os.path.join(
    "src", "transformers_amba_ext", "models", "auto", "modeling_auto.py"
)
MAPPING_ANCHOR = '\t"qwen3_vl": VLMForCausalLM,\n'
MAPPING_ADD = (
    '\t"qwen3_5_text": VLMForCausalLM,\n\t"qwen3_5": VLMForCausalLM,\n'
)
FALLBACK_ANCHOR = (
    "\t\tsupported_models = MODEL_FOR_VISION_LM_MAPPING_NAMES\n"
    "\t\tmodel_class = _get_model_class(model_type, supported_models)\n"
)
FALLBACK_MARK = "# Qwen3.5, by architecture"
FALLBACK_ADD = (
    f"\t\t{FALLBACK_MARK}: config.json says qwen3 so that transformers can\n"
    "\t\t# read it; architectures still names the model (install_qwen35_vllm.sh).\n"
    "\t\tif model_class is None and any(\n"
    '\t\t\t\t"Qwen3_5" in arch for arch in (getattr(config, "architectures", None) or [])):\n'
    "\t\t\tmodel_class = VLMForCausalLM\n"
)

# --- model ------------------------------------------------------------------

HF = "https://hf-mirror.com/Qwen/Qwen3-VL-4B-Instruct/resolve/main"
DOWNLOADS = (
    "tokenizer_config.json",
    "generation_config.json",
    "preprocessor_config.json",
)
VIT_NAME = "qwen3.5_4b_vl_vit_384x384_n1655_a16w8_cavalry.bin"
BACKUP = ".before-qwen35"
SIZE = {"height": 384, "width": 384}
# The generation prompt in Qwen3-VL-4B-Instruct's template, and what the
# guide makes of it. The \n inside the Jinja strings are a backslash and an
# n, as the template itself has them.
GEN_OLD = (
    "{%- if add_generation_prompt %}\n"
    "    {{- '<|im_start|>assistant\\n' }}\n"
    "{%- endif %}"
)
# The guide's edit, applied on 2026-10-14: thinking off only when a request
# says enable_thinking: false.
GEN_ASKED_OFF = (
    "{%- if add_generation_prompt %}\n"
    "    {{- '<|im_start|>assistant\\n' }}\n"
    "    {%- if enable_thinking is defined and enable_thinking is false %}\n"
    "        {{- '<think>\\n\\n</think>\\n\\n' }}\n"
    "    {%- endif %}\n"
    "{%- endif %}"
)
# Thinking off unless a request asks for it: openai_llm2_python cannot send
# chat_template_kwargs, so a template that waits to be told never is.
GEN_NEW = (
    "{%- if add_generation_prompt %}\n"
    "    {{- '<|im_start|>assistant\\n' }}\n"
    "    {%- if not (enable_thinking is defined and enable_thinking) %}\n"
    "        {{- '<think>\\n\\n</think>\\n\\n' }}\n"
    "    {%- endif %}\n"
    "{%- endif %}"
)


class Stop(Exception):
    """Something is not as this script expects; the message says what."""


def ok(msg):
    print(f"  ok    {msg}", flush=True)


def note(msg):
    print(f"  note  {msg}", flush=True)


def write_text(path, text):
    """Whole or not at all; a symlink at path is replaced, not followed."""
    folder = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    # mkstemp makes it 0600; a config or a source file is read by others.
    mask = os.umask(0)
    os.umask(mask)
    os.chmod(tmp, 0o666 & ~mask)
    if os.path.islink(path):
        os.unlink(path)
    os.replace(tmp, path)


def write_json(path, value):
    write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as err:
        raise Stop(f"cannot read {path}: {err}") from err


def amba_ext(checkout):
    path = os.path.join(checkout, AUTO)
    try:
        with open(path, encoding="utf-8") as f:
            src = f.read()
    except OSError as err:
        raise Stop(f"cannot read {path}: {err}") from err
    changed = []
    if '"qwen3_5":' in src:
        ok("qwen3_5 and qwen3_5_text are in the vision mapping already")
    elif MAPPING_ANCHOR in src:
        src = src.replace(MAPPING_ANCHOR, MAPPING_ANCHOR + MAPPING_ADD, 1)
        changed.append("qwen3_5 and qwen3_5_text added to the vision mapping")
    else:
        raise Stop(
            f"{path}: no '\"qwen3_vl\": VLMForCausalLM,' line to add Qwen3.5 "
            "after; the file is not the cooper_2.5.4 one this was written for"
        )
    if FALLBACK_MARK in src:
        ok("the Qwen3.5 architecture fallback is there already")
    elif FALLBACK_ANCHOR in src:
        src = src.replace(FALLBACK_ANCHOR, FALLBACK_ANCHOR + FALLBACK_ADD, 1)
        changed.append("Qwen3.5 architectures fall back to VLMForCausalLM")
    else:
        raise Stop(
            f"{path}: AutoModelForVision.from_pretrained is not as expected; "
            "the file is not the cooper_2.5.4 one this was written for"
        )
    try:
        compile(src, path, "exec")
    except SyntaxError as err:
        raise Stop(f"{path} would not compile after the change: {err}") from err
    if changed:
        write_text(path, src)
        for c in changed:
            ok(c)


def find_vit(package, ranch, name):
    """The ViT binary: in the package, or beside it (the guide's link points
    one level above the package). Never the link in ranch/ itself."""
    roots = [package, os.path.dirname(package)]
    for root in roots:
        for folder, dirs, files in os.walk(root):
            depth = folder[len(root) :].count(os.sep)
            if depth >= 3:
                dirs[:] = []
            if name in files:
                found = os.path.join(folder, name)
                if os.path.realpath(folder) != os.path.realpath(ranch):
                    return found
    return None


def link(ranch, name, target, changes, check):
    """ranch/name -> target (relative to ranch/)."""
    dst = os.path.join(ranch, name)
    if (
        os.path.islink(dst)
        and os.readlink(dst) == target
        and os.path.exists(dst)
    ):
        ok(f"{name} -> {target}")
        return
    if check:
        changes.append(f"link {name} -> {target}")
        return
    if os.path.lexists(dst):
        os.unlink(dst)
    os.symlink(target, dst)
    if not os.path.exists(dst):
        raise Stop(f"{dst} -> {target} does not resolve")
    ok(f"linked {name} -> {target}")


def backup_once(ranch, names):
    keep = os.path.join(ranch, BACKUP)
    if os.path.isdir(keep):
        return
    os.makedirs(keep)
    kept = []
    for name in names:
        src = os.path.join(ranch, name)
        if os.path.lexists(src):
            if os.path.islink(src):
                os.symlink(os.readlink(src), os.path.join(keep, name))
            else:
                shutil.copy2(src, os.path.join(keep, name))
            kept.append(name)
    ok(f"kept what was there in {keep}: {', '.join(kept) or 'nothing'}")


class _Follow308(urllib.request.HTTPRedirectHandler):
    """hf-mirror.com answers 308; urllib follows it only from Python 3.11."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(
            req, fp, 307 if code == 308 else code, msg, headers, newurl
        )

    http_error_308 = urllib.request.HTTPRedirectHandler.http_error_302


def download(url, path):
    opener = urllib.request.build_opener(_Follow308)
    with opener.open(url, timeout=60) as r:
        data = r.read()
    json.loads(data)  # a mirror's error page is not a config
    write_text(path, data.decode("utf-8"))


def model(package, check, vit_name):
    package = os.path.realpath(os.path.expanduser(package))
    ranch = os.path.join(package, "ranch")
    info = os.path.join(ranch, "info")
    source = load_json(os.path.join(info, "config.json"))
    arch = source.get("architectures") or []
    ok(
        f"package {package}: info/config.json says model_type "
        f"{source.get('model_type')!r}, architectures {arch}"
    )
    if not any("Qwen3_5" in a for a in arch):
        raise Stop(
            "info/config.json's architectures do not name Qwen3_5: this is "
            "not the Qwen3.5 package (qwen3.5-4b_n1655-video)"
        )
    for name in ("tokenizer.json", "prompt_config.json"):
        if not os.path.isfile(os.path.join(info, name)):
            raise Stop(f"no info/{name} in {ranch}")
    vit = find_vit(package, ranch, vit_name)
    if vit is None:
        raise Stop(
            f"no {vit_name} in {package} or beside it; "
            "set VIT_NAME if the package names it otherwise"
        )
    ok(f"ViT binary {vit}")
    for other in ("chat_template.json", "chat_template.jinja"):
        if os.path.lexists(os.path.join(ranch, other)):
            note(
                f"ranch/{other} exists and may be used instead of the "
                "template in tokenizer_config.json, which is the one changed here"
            )

    changes = []
    if not check:
        backup_once(
            ranch,
            ("config.json", "tokenizer.json", "prompt_config.json", vit_name)
            + DOWNLOADS,
        )
    link(
        ranch,
        "tokenizer.json",
        os.path.join("info", "tokenizer.json"),
        changes,
        check,
    )
    link(
        ranch,
        "prompt_config.json",
        os.path.join("info", "prompt_config.json"),
        changes,
        check,
    )
    link(ranch, vit_name, os.path.relpath(vit, ranch), changes, check)

    for name in DOWNLOADS:
        path = os.path.join(ranch, name)
        if os.path.isfile(path) and not os.path.islink(path):
            ok(f"{name} is there")
        elif check:
            changes.append(f"download {name}")
        else:
            try:
                download(f"{HF}/{name}", path)
            except Exception as err:  # pylint: disable=broad-except
                raise Stop(
                    f"could not download {name} from {HF}: {err}"
                ) from err
            ok(f"downloaded {name}")

    config_path = os.path.join(ranch, "config.json")
    current = (
        load_json(config_path)
        if os.path.isfile(config_path) and not os.path.islink(config_path)
        else None
    )
    if current and current.get("model_type") == "qwen3":
        ok("config.json says model_type qwen3")
    elif check:
        changes.append("write config.json with model_type qwen3")
    else:
        new = dict(source)
        new["model_type"] = "qwen3"
        if isinstance(new.get("vision_config"), dict):
            new["vision_config"] = dict(new["vision_config"], image_size=384)
        else:
            note("info/config.json has no vision_config; image_size not set")
        write_json(config_path, new)
        ok("wrote config.json: model_type qwen3, vision image size 384")

    pre_path = os.path.join(ranch, "preprocessor_config.json")
    if os.path.isfile(pre_path):
        pre = load_json(pre_path)
        if pre.get("size") == SIZE:
            ok("preprocessor size is 384x384")
        elif check:
            changes.append("set the preprocessor size to 384x384")
        else:
            pre["size"] = SIZE
            write_json(pre_path, pre)
            ok("set the preprocessor size to 384x384")

    tok_path = os.path.join(ranch, "tokenizer_config.json")
    if os.path.isfile(tok_path):
        tok = load_json(tok_path)
        template = tok.get("chat_template") or ""
        if GEN_NEW in template:
            ok("the chat template keeps thinking off unless a request asks")
        else:
            found = next(
                (old for old in (GEN_ASKED_OFF, GEN_OLD) if old in template),
                None,
            )
            if found is None:
                raise Stop(
                    "tokenizer_config.json's chat template has no generation "
                    "prompt in a form this script knows; thinking cannot be "
                    "turned off"
                )
            if check:
                changes.append("keep thinking off unless a request asks")
            else:
                tok["chat_template"] = template.replace(found, GEN_NEW, 1)
                write_json(tok_path, tok)
                ok("the chat template now keeps thinking off unless a request asks")

    for c in changes:
        note(f"to do: {c}")
    return changes


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="what", required=True)
    a = sub.add_parser(
        "amba-ext", help="change transformers_amba_ext for Qwen3.5"
    )
    a.add_argument("checkout")
    m = sub.add_parser(
        "model", help="prepare the package's ranch/ for vllm serve"
    )
    m.add_argument("package")
    m.add_argument(
        "--check", action="store_true", help="say what is to do; change nothing"
    )
    m.add_argument("--vit-name", default=os.environ.get("VIT_NAME") or VIT_NAME)
    args = p.parse_args()
    try:
        if args.what == "amba-ext":
            amba_ext(args.checkout)
        else:
            model(args.package, args.check, args.vit_name)
    except Stop as err:
        print(f"  FAIL  {err}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
