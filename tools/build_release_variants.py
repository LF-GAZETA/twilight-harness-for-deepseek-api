"""Stage the five isolated Twilight Harness client trees.

Models and the portable Python runtime are hard-linked when possible; this
creates separate release folders without duplicating tens of gigabytes on disk.
Archives are created separately by pack_release_variants.py.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

ROOT = Path(r"H:/Github/twilight-harness-for-deepseek-api")
PORTABLE = ROOT / "runtime" / "portable"
STAGE = Path(r"H:/Github/_ai_install/release/rc-261106")
AI_INSTALL = Path(r"H:/Github/_ai_install")
EXE = ROOT / "native" / "build" / "twilight-native.exe"

VARIANTS = {
    "plus": {
        "folder": "twilight-harness-plus", "exe": "twilight-harness-plus.exe",
        "models": ["Qwen-Image-2.1-Turbo-universal", "vision", "gpt_sovits", "realesrgan", "rife"],
        "tts": True,
    },
    "debug": {
        "folder": "twilight-harness-plus-debug", "exe": "twilight-harness-plus-debug.exe",
        "models": ["Qwen-Image-2.1-Turbo-universal", "vision", "gpt_sovits", "realesrgan", "rife"],
        "tts": True,
    },
    "standard": {
        "folder": "twilight-harness", "exe": "twilight-harness.exe",
        "models": ["sd15", "vision", "gpt_sovits", "realesrgan", "rife"],
        "tts": True,
    },
    "chat": {
        "folder": "twilight-harness-chat", "exe": "twilight-harness-chat.exe",
        "models": ["gpt_sovits"],
        "tts": True,
    },
    "benchmark": {
        "folder": "twilight-harness-benchmark", "exe": "twilight-harness-benchmark.exe",
        "models": ["Qwen-Image-2.1-Turbo-universal", "realesrgan", "rife"],
        "tts": False,
    },
}


def link_tree(source: Path, target: Path) -> None:
    if not source.exists():
        return
    for item in source.rglob("*"):
        if "__pycache__" in item.parts or item.name.endswith(".pyc"):
            continue
        rel = item.relative_to(source)
        destination = target / rel
        if item.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            continue
        try:
            os.link(item, destination)
        except OSError:
            try:
                shutil.copy2(item, destination)
            except OSError:
                pass


def copy_app(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for item in source.rglob("*"):
        if not item.is_file() or "__pycache__" in item.parts or item.suffix == ".pyc":
            continue
        if item.suffix not in {".py", ".json", ".txt", ".md", ".onnx", ".yaml", ".yml"}:
            continue
        rel = item.relative_to(source)
        dest = target / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, dest)


def write_tts_config(root: Path) -> None:
    models = root / "ai" / "models" / "gpt_sovits"
    ref_zh = "ai/models/gpt_sovits/frozen/reference_audio/twi-zh-clean-norm.wav"
    ref_en = "ai/models/gpt_sovits/frozen/reference_audio/twi-en-demucs-norm.wav"
    data = {
        "version": 1,
        "zh": {"api_url": "http://127.0.0.1:9881", "reference": ref_zh,
               "s1": "cn/twiclean-e40.ckpt", "s2": "cn/twiclean_e40_s920.pth"},
        "en": {"api_url": "http://127.0.0.1:9883", "reference": ref_en,
               "s1": "en/twien3-e40.ckpt", "s2": "en/twien3_e20_s500.pth"},
    }
    (root / "ai" / "tts-runtime.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    for name, lang in (("cn.yaml", "cn"), ("en.yaml", "en")):
        path = models / "api" / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        s1 = models / "frozen" / lang / ("twiclean-e40.ckpt" if lang == "cn" else "twien3-e40.ckpt")
        s2 = models / "frozen" / lang / ("twiclean_e40_s920.pth" if lang == "cn" else "twien3_e20_s500.pth")
        lines = []
        for line in text.splitlines():
            if line.strip().startswith("t2s_weights_path:"):
                lines.append(f"  t2s_weights_path: {s1.as_posix()}")
            elif line.strip().startswith("vits_weights_path:"):
                lines.append(f"  vits_weights_path: {s2.as_posix()}")
            else:
                lines.append(line)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def stage(name: str, spec: dict) -> Path:
    root = STAGE / spec["folder"]
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    shutil.copy2(EXE, root / spec["exe"])
    (root / "variant.txt").write_text(name + "\n", encoding="utf-8")
    copy_app(ROOT / "sidecar", root / "sidecar")
    link_tree(ROOT / "sidecar" / "assets", root / "sidecar" / "assets")
    copy_app(ROOT / "twilight-app", root / "twilight-app")
    link_tree(ROOT / "native" / "assets", root / "assets")
    link_tree(AI_INSTALL / "native-slim" / "ffmpeg-bin", root / "ffmpeg-bin")
    link_tree(PORTABLE / "tools", root / "tools")
    link_tree(ROOT / "tools" / "realesrgan", root / "tools" / "realesrgan")
    link_tree(PORTABLE / "python", root / "python")
    if spec["tts"]:
        link_tree(PORTABLE / "api" / "GPT-SoVITS", root / "api" / "GPT-SoVITS")
    for model in spec["models"]:
        source = PORTABLE / "ai" / "models" / model
        link_tree(source, root / "ai" / "models" / model)
    for sub in ("images", "audio", "effects", "tts", "render"):
        (root / "ai" / "outputs" / sub).mkdir(parents=True, exist_ok=True)
    if spec["tts"]:
        write_tts_config(root)
    launcher = root / "启动 Twilight Harness.bat"
    launcher.write_text(
        "@echo off\r\ncd /d \"%~dp0\"\r\nstart \"\" \"%~dp0" + spec["exe"] + "\"\r\n",
        encoding="utf-8")
    print("staged", name, root, flush=True)
    return root


def main() -> int:
    STAGE.mkdir(parents=True, exist_ok=True)
    for name, spec in VARIANTS.items():
        stage(name, spec)
    print("all variants staged", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
