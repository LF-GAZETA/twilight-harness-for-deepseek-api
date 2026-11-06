"""SD1.5 guided image edit for the standard client.

The bundled SD1.5 pack only contains a VAE decoder, so true latent image-to-image
is not possible with those ONNX files.  This adapter uses the local vision model
to read the source image, combines that description with the edit instruction,
and regenerates with SD1.5.  It is intentionally described as a guided edit, not
as pixel-preserving image-to-image.
"""
from __future__ import annotations

import time
from pathlib import Path

from ai_resources import outputs_dir


def available() -> bool:
    try:
        import ai_sd15
        return bool(ai_sd15)
    except Exception:
        return False


def edit(instruction: str, source: str | Path, size: str | tuple[int, int] = "512x512",
         progress=None) -> Path:
    instruction = (instruction or "").strip()
    if not instruction:
        raise ValueError("请说明想改哪里")
    if progress:
        progress(0, 4, "读取原图")
    caption = ""
    try:
        import vision_local
        caption = vision_local.answer(
            "Describe this image in one concise sentence. Mention the subject, colors, "
            "composition, background and any visible text.", path=str(source))
    except Exception:
        caption = ""
    finally:
        try:
            import vision_local
            vision_local.release()
        except Exception:
            pass
    if progress:
        progress(2, 4, "SD1.5 编辑中")
    prompt = "Edit the image: " + instruction
    if caption:
        prompt += ". Original scene: " + caption
    import ai_sd15
    started = time.perf_counter()
    path = ai_sd15.SD15Engine().generate(prompt, "", size=size)
    if progress:
        progress(4, 4, "SD1.5 编辑完成")
    print(f"[sd15-edit] {time.perf_counter() - started:.1f}s -> {path}", flush=True)
    return Path(path)
