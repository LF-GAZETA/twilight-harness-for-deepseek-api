"""Qwen-Image-2.1 instruction editing with a full CPU text encoder.

Text-to-image uses a trimmed INT4 text encoder for low VRAM. Image editing
needs the vision tower, so this runtime keeps the full Qwen3-VL text encoder on
CPU and only moves the transformer and VAE to CUDA. That keeps the 10 GB
profile usable at the cost of a slower prompt-encoding step.
"""
from __future__ import annotations

import gc
import os
import threading
import time
from pathlib import Path

_LOCK = threading.Lock()
_PIPELINE = None


def model_dir() -> Path:
    configured = os.environ.get("TWILIGHT_QWEN_EDIT_DIR", "").strip()
    if configured:
        return Path(configured)
    import qwen_image_runtime
    return qwen_image_runtime._model_dir()


def available() -> bool:
    try:
        import torch
        return model_dir().is_dir() and (model_dir() / "text_encoder").is_dir() and torch.cuda.is_available()
    except Exception:
        return False


def reset() -> None:
    global _PIPELINE
    _PIPELINE = None
    gc.collect()
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def _size(value) -> tuple[int, int]:
    if isinstance(value, (tuple, list)) and len(value) == 2:
        width, height = int(value[0]), int(value[1])
    else:
        text = str(value or "512x512").lower().replace("*", "x")
        if "x" in text:
            width, height = (int(part) for part in text.split("x", 1))
        else:
            width = height = int(text)
    cap = 512
    try:
        import torch
        if torch.cuda.is_available():
            cap = 512 if torch.cuda.get_device_properties(0).total_memory / 1024**3 <= 10.5 else 768
    except Exception:
        pass
    width = max(256, min(cap, width // 16 * 16))
    height = max(256, min(cap, height // 16 * 16))
    return width, height


def _load():
    global _PIPELINE
    if _PIPELINE is not None:
        return _PIPELINE
    import torch
    from diffusers import (AutoencoderKLQwenImage21, FlowMatchEulerDiscreteScheduler,
                           QwenImage21Pipeline, QwenImage21Transformer2DModel)
    from diffusers.image_processor import VaeImageProcessor
    from transformers import Qwen3VLForConditionalGeneration, Qwen3VLProcessor

    model = model_dir()
    dtype = torch.bfloat16
    processor = Qwen3VLProcessor.from_pretrained(str(model / "processor"), local_files_only=True)
    scheduler = FlowMatchEulerDiscreteScheduler.from_pretrained(
        model, subfolder="scheduler", local_files_only=True)
    text = Qwen3VLForConditionalGeneration.from_pretrained(
        model, subfolder="text_encoder", torch_dtype=dtype,
        device_map="cpu", low_cpu_mem_usage=True, local_files_only=True,
    ).eval()
    transformer = QwenImage21Transformer2DModel.from_pretrained(
        model, subfolder="transformer", torch_dtype=dtype,
        device_map="cuda", low_cpu_mem_usage=True, local_files_only=True,
    )
    vae = AutoencoderKLQwenImage21.from_pretrained(
        model, subfolder="vae", torch_dtype=dtype, local_files_only=True,
    ).to("cuda")
    import json
    index = json.loads((model / "model_index.json").read_text(encoding="utf-8"))
    pipe = QwenImage21Pipeline(scheduler=scheduler, vae=vae, text_encoder=text,
                               processor=processor, transformer=transformer,
                               sample_sigmas=index.get("sample_sigmas"))
    pipe.image_processor = VaeImageProcessor()
    original_prompt_embeds = pipe._get_qwen_prompt_embeds
    def _cpu_prompt_embeds(prompt, image, device):
        import torch as _torch
        embeds, mask, image_mask = original_prompt_embeds(prompt, image, _torch.device("cpu"))
        return embeds.to(device), mask.to(device), image_mask.to(device)
    pipe._get_qwen_prompt_embeds = _cpu_prompt_embeds
    try:
        pipe.vae.enable_slicing()
        pipe.vae.enable_tiling()
    except Exception:
        pass
    _PIPELINE = pipe
    return pipe


def edit(prompt: str, image: str | Path, size="512x512", progress=None) -> Path:
    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("编辑指令为空")
    from PIL import Image
    source = Image.open(image).convert("RGB")
    width, height = _size(size)
    with _LOCK:
        if progress:
            progress(0, 8, "加载编辑模型")
        try:
            import qwen_image_runtime
            qwen_image_runtime._pause_h3_downloads()
        except Exception:
            pass
        try:
            # Do not race the sidecar's Qwen text-to-image warm-up.
            try:
                import qwen_image_runtime
                while getattr(qwen_image_runtime, "_WARMING", None) is not None and qwen_image_runtime._WARMING.is_set():
                    time.sleep(1)
            except Exception:
                pass
            # Free the text-to-image pipeline before loading the full encoder.
            try:
                import qwen_image_runtime
                qwen_image_runtime.reset()
            except Exception:
                pass
            pipe = _load()
        finally:
            try:
                import qwen_image_runtime
                qwen_image_runtime._resume_h3_downloads()
            except Exception:
                pass
        import torch
        generator = torch.Generator("cpu").manual_seed(int(time.time() * 1000) & 0x7FFFFFFF)
        if progress:
            progress(1, 10, "理解编辑指令")
        result = pipe(
            prompt=prompt,
            image=source,
            num_inference_steps=8,
            height=height,
            width=width,
            true_cfg_scale=1.0,
            generator=generator,
        )
        from ai_resources import ai_home
        folder = ai_home() / "outputs" / "images"
        folder.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = folder / f"qwen_edit_{stamp}.png"
        suffix = 1
        while target.exists():
            target = folder / f"qwen_edit_{stamp}_{suffix}.png"
            suffix += 1
        result.images[0].save(target)
        if progress:
            progress(10, 10, "编辑完成")
        return target
