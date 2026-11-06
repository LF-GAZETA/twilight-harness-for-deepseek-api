"""Qwen-Image-2.1-Turbo runtime used by the local `qwen_image` tool.

The pipeline is lazy-loaded and cached.  The text encoder stays on CPU, the
saved INT4 transformer and VAE stay on CUDA.  This is the same configuration
that measured ~6 GB peak and ~7.7 s at 512×512 on an RTX 3080.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

_PIPELINE = None
_LOCK = threading.Lock()
_WARMING = threading.Event()


def _model_dir() -> Path:
    configured = os.environ.get("TWILIGHT_QWEN_DIR", "").strip()
    if configured:
        return Path(configured)
    ai_home = os.environ.get("TWILIGHT_AI_HOME", "").strip()
    if ai_home:
        return Path(ai_home) / "models" / "Qwen-Image-2.1-Turbo-universal"
    return Path(__file__).resolve().parent.parent / "runtime" / "portable" / "ai" / "models" / "Qwen-Image-2.1-Turbo-universal"


def reset() -> None:
    """Release the cached pipeline before falling back to another engine."""
    global _PIPELINE
    _PIPELINE = None
    import gc
    gc.collect()
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def available() -> bool:
    try:
        import torch
        return torch.cuda.is_available() and (_model_dir() / "transformer").is_dir()
    except Exception:
        return False


_PAUSED_DOWNLOADS: list = []


def _pause_h3_downloads() -> None:
    try:
        import psutil
        for process in psutil.process_iter(["cmdline"]):
            cmdline = " ".join(process.info.get("cmdline") or [])
            if ("h3_curl_finish" in cmdline or "h3_curl_8h" in cmdline
                    or ("h3_one_chunk" in cmdline)
                    or ("curl.exe" in cmdline and "MiniMax-H3" in cmdline)):
                if process.pid == __import__("os").getpid():
                    continue
                if process.is_running() and process.status() != psutil.STATUS_STOPPED:
                    try:
                        process.suspend()
                        _PAUSED_DOWNLOADS.append(process)
                    except Exception:
                        pass
    except Exception:
        pass


def _resume_h3_downloads() -> None:
    while _PAUSED_DOWNLOADS:
        process = _PAUSED_DOWNLOADS.pop()
        try:
            process.resume()
        except Exception:
            pass


def _load(progress=None):
    global _PIPELINE
    if _PIPELINE is not None:
        return _PIPELINE
    from diffusers import AutoencoderKLQwenImage21, FlowMatchEulerDiscreteScheduler, QwenImage21Pipeline, QwenImage21Transformer2DModel
    from transformers import Qwen3VLForConditionalGeneration, Qwen3VLProcessor
    import json
    import torch

    model = _model_dir()
    dtype = torch.bfloat16
    if progress:
        progress(1, 10, "加载文本编码器")
    processor = Qwen3VLProcessor.from_pretrained(model, subfolder="processor", local_files_only=True)
    scheduler = FlowMatchEulerDiscreteScheduler.from_pretrained(model, subfolder="scheduler", local_files_only=True)
    dual_env = os.environ.get("TWILIGHT_QWEN_DUAL_INT4")
    if dual_env is None:
        try:
            import torch as _torch
            dual = _torch.cuda.is_available() and _torch.cuda.get_device_properties(0).total_memory / 1024**3 >= 9.5
        except Exception:
            dual = False
    else:
        dual = dual_env.lower() in {"1", "true", "yes"}
    if progress:
        progress(2, 10, "量化文本编码器")
    if dual:
        from qwen_text_loader import QwenTextLoader
        text_dir = Path(os.environ.get("TWILIGHT_QWEN_TEXT_DIR", model / "text_encoder"))
        text = QwenTextLoader(text_dir, device="cuda", embedding_bits=8).load()
    else:
        text = Qwen3VLForConditionalGeneration.from_pretrained(
            model, subfolder="text_encoder", torch_dtype=dtype, local_files_only=True,
            device_map="cpu", low_cpu_mem_usage=True,
        )
    if progress:
        progress(5, 10, "加载 Qwen Transformer")
    transformer = QwenImage21Transformer2DModel.from_pretrained(
        model, subfolder="transformer", torch_dtype=dtype, local_files_only=True,
        device_map="cuda", low_cpu_mem_usage=True,
    )
    if progress:
        progress(7, 10, "加载 Qwen VAE")
    vae = AutoencoderKLQwenImage21.from_pretrained(
        model, subfolder="vae", torch_dtype=dtype, local_files_only=True,
    ).to("cuda")
    index = json.loads((model / "model_index.json").read_text(encoding="utf-8"))
    if progress:
        progress(8, 10, "组装 Qwen 管线")
    pipe = QwenImage21Pipeline(scheduler=scheduler, vae=vae, text_encoder=text, processor=processor,
                               transformer=transformer, sample_sigmas=index.get("sample_sigmas"))
    from diffusers.image_processor import VaeImageProcessor
    pipe.image_processor = VaeImageProcessor()
    if not dual:
        original = pipe._get_qwen_prompt_embeds
        def _cpu_prompt(prompt, image, device):
            embeds, mask, image_mask = original(prompt, image, torch.device("cpu"))
            return embeds.to(device), mask.to(device), image_mask.to(device)
        pipe._get_qwen_prompt_embeds = _cpu_prompt
    else:
        try:
            pipe.vae.enable_slicing()
            pipe.vae.enable_tiling()
        except Exception:
            pass
    _PIPELINE = pipe
    return pipe


def _size(value) -> tuple[int, int]:
    if isinstance(value, (tuple, list)) and len(value) == 2:
        width, height = int(value[0]), int(value[1])
    else:
        text = str(value or "768x768").lower().replace("*", "x")
        if "x" in text:
            width, height = (int(part) for part in text.split("x", 1))
        else:
            width = height = int(text)
    cap = 1024
    try:
        import torch
        if torch.cuda.is_available() and torch.cuda.get_device_properties(0).total_memory / 1024**3 <= 10.5:
            cap = 512
    except Exception:
        pass
    width = max(256, min(cap, width // 16 * 16))
    height = max(256, min(cap, height // 16 * 16))
    return width, height


def generate(prompt: str, negative: str = "", size="768x768", progress=None,
             reference_image: str | Path | None = None) -> Path:
    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("Qwen image prompt is empty")
    reference = None
    if reference_image:
        try:
            # The trimmed INT4 text encoder has no vision tower, so image
            # conditioning is expressed as a cached local vision caption.
            import vision_local
            caption = vision_local.caption(path=Path(reference_image))
            if caption:
                prompt = prompt + chr(10) + "参考图：" + caption
            try:
                vision_local.release()
            except Exception:
                pass
        except Exception:
            reference = None
    width, height = _size(size)
    if progress and _WARMING.is_set() and _PIPELINE is None:
        for step in range(0, 10):
            progress(step, 10, "Qwen 预热中")
            if _WARMING.wait(1.0) or _PIPELINE is not None:
                break
    with _LOCK:
        if progress:
            progress(0, 10, "准备加载 Qwen")
        _pause_h3_downloads()
        started = time.perf_counter()
        try:
            pipe = _load(progress)
        finally:
            _resume_h3_downloads()
        if progress:
            progress(1, 10, "Qwen 采样")
        import torch
        generator = torch.Generator("cpu").manual_seed(int(time.time() * 1000) & 0x7FFFFFFF)
        def step(_pipe, index, _timestep, callback_kwargs):
            if progress:
                progress(index + 1, 10, "Qwen 采样")
            return callback_kwargs
        result = pipe(
            prompt=prompt,
            negative_prompt=negative or None,
            num_inference_steps=8,
            height=height,
            width=width,
            true_cfg_scale=1.0,
            generator=generator,
            callback_on_step_end=step,
        )
        from ai_resources import ai_home
        folder = ai_home() / "outputs" / "images"
        folder.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = folder / f"qwen_{stamp}.png"
        suffix = 1
        while target.exists():
            target = folder / f"qwen_{stamp}_{suffix}.png"
            suffix += 1
        result.images[0].save(target)
        print(f"[qwen] {width}x{height} {time.perf_counter()-started:.1f}s -> {target}", flush=True)
        if progress:
            progress(10, 10, "Qwen 完成")
        return target
