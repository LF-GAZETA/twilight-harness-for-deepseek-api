"""MiniMax-H3 video generation runtime.

The runtime is intentionally lazy: the 61.7 GB transformer and 62.1 GB Qwen3-VL
conditioner are loaded per workflow and kept off the accelerator with a
ComponentsManager. INT4 quantization is applied to the two large components
before workflow loading; the video/audio VAEs stay resident on CUDA.
"""
from __future__ import annotations

import gc
import os
import threading
import time
from pathlib import Path

_LOCK = threading.Lock()
_PIPES: dict[str, object] = {}


def model_dir() -> Path:
    configured = os.environ.get("TWILIGHT_MINIMAX_H3_DIR", "").strip()
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parent.parent / "runtime" / "portable" / "ai" / "models" / "MiniMax-H3"


def available() -> bool:
    try:
        import torch
        root = model_dir()
        def has_weights(folder: Path) -> bool:
            index = folder / "diffusion_pytorch_model.safetensors.index.json"
            if not index.is_file():
                index = folder / "model.safetensors.index.json"
            if not index.is_file():
                return False
            import json
            data = json.loads(index.read_text(encoding="utf-8"))
            shards = set((data.get("weight_map") or {}).values())
            if not shards:
                return False
            return all((folder / shard).is_file() and (folder / shard).stat().st_size > 1024**2
                       for shard in shards)
        return ((root / "model_index.json").is_file()
                and has_weights(root / "transformer")
                and has_weights(root / "text_encoder")
                and torch.cuda.is_available())
    except Exception:
        return False


def reset() -> None:
    _PIPES.clear()
    gc.collect()
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def _quant_mode() -> str:
    return os.environ.get("TWILIGHT_H3_QUANT", "int4").strip().lower()


def _quant_config():
    mode = _quant_mode()
    if mode in {"bf16", "none", "off"}:
        return None
    from transformers import BitsAndBytesConfig
    import torch
    if mode in {"int8", "8bit"}:
        return BitsAndBytesConfig(load_in_8bit=True)
    return BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.bfloat16,
    )


def _load(workflow: str):
    if workflow in _PIPES:
        return _PIPES[workflow]
    import torch
    from diffusers import ComponentsManager, ModularPipeline
    root = model_dir()
    manager = ComponentsManager()

    def load():
        pipe = ModularPipeline.from_pretrained(str(root), workflow=workflow,
                                               components_manager=manager)
        try:
            from diffusers import MiniMaxH3Transformer3DModel
            from transformers import Qwen3VLForConditionalGeneration
            quant = _quant_config()
            kwargs = {"torch_dtype": torch.bfloat16, "low_cpu_mem_usage": True,
                      "local_files_only": True}
            if quant is not None:
                kwargs["quantization_config"] = quant
            transformer = MiniMaxH3Transformer3DModel.from_pretrained(
                str(root / "transformer"), **kwargs)
            text = Qwen3VLForConditionalGeneration.from_pretrained(
                str(root / "text_encoder"), **kwargs)
            pipe.update_components(transformer=transformer, text_encoder=text)
        except Exception as exc:
            print("[h3] quantization failed, using bf16:", exc, flush=True)
        pipe.load_components(workflow=workflow, dtype=torch.bfloat16)
        manager.enable_auto_cpu_offload(device="cuda")
        try:
            pipe.transformer.requires_grad_(False)
            pipe.text_encoder.requires_grad_(False)
        except Exception:
            pass
        try:
            from diffusers.hooks import apply_group_offloading
            pipe.transformer.enable_group_offload(
                offload_type="block_level", num_blocks_per_group=1,
                onload_device=torch.device("cuda"), offload_device=torch.device("cpu"),
                use_stream=True,
            )
            apply_group_offloading(
                pipe.text_encoder.model, offload_type="leaf_level",
                onload_device=torch.device("cuda"), offload_device=torch.device("cpu"),
                use_stream=False,
            )
        except Exception as exc:
            print("[h3] group offload unavailable:", exc, flush=True)
        for name in ("vae", "audio_vae"):
            try:
                getattr(pipe, name).to("cuda")
            except Exception:
                pass
        return pipe

    pipe = load()
    _PIPES[workflow] = pipe
    return pipe


def generate(prompt: str, *, image=None, last_image=None, reference=None,
             num_frames: int = 124, height: int = 544, width: int = 960,
             seed: int | None = None, progress=None, output_dir=None) -> Path:
    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("视频提示词为空")
    workflow = "ref2va" if reference is not None else ("fl2va" if image is not None else "t2va")
    with _LOCK:
        if progress:
            progress(0, 8, "加载 MiniMax H3")
        pipe = _load(workflow)
        import torch
        generator = torch.Generator("cpu").manual_seed(
            int(seed if seed is not None else (time.time() * 1000) & 0x7FFFFFFF))
        kwargs = {
            "prompt": prompt,
            "num_frames": max(124, min(360, int(num_frames))),
            "height": max(256, int(height) // 32 * 32),
            "width": max(256, int(width) // 32 * 32),
            "generator": generator,
            "output": ["videos", "audio", "sampling_rate"],
        }
        if image is not None:
            from diffusers.utils import load_image
            kwargs["image"] = load_image(str(image)) if not hasattr(image, "size") else image
        if last_image is not None:
            from diffusers.utils import load_image
            kwargs["last_image"] = load_image(str(last_image)) if not hasattr(last_image, "size") else last_image
        if reference is not None:
            from diffusers.modular_pipelines.minimax_h3 import MiniMaxH3ImageReference
            kwargs["references"] = [MiniMaxH3ImageReference.from_file(str(reference))]
        if progress:
            progress(1, 10, "H3 采样")
        result = pipe(**kwargs)
        from ai_resources import ai_home
        folder = Path(output_dir) if output_dir else (ai_home() / "outputs" / "videos")
        folder.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        raw = folder / f"h3_{stamp}.mp4"
        from diffusers.utils.export_utils import encode_video
        encode_video(result["videos"][0], fps=24, output_path=str(raw),
                     audio=result["audio"][0], audio_sample_rate=result["sampling_rate"])
        if progress:
            progress(8, 10, "封装 MP4")
        final = _finalize_mp4(raw)
        if progress:
            progress(10, 10, "视频完成")
        return final


def _finalize_mp4(path: Path) -> Path:
    """Ensure H.264 + AAC + faststart MP4, preserving a thumbnail sidecar."""
    import subprocess
    ffmpeg = _ffmpeg()
    if not ffmpeg:
        return path
    target = path.with_name(path.stem + "_h264.mp4")
    try:
        subprocess.run([
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(target),
        ], check=True, timeout=1800,
           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if target.is_file() and target.stat().st_size > 0:
            path.unlink(missing_ok=True)
            return target
    except Exception:
        pass
    return path


def _ffmpeg() -> str:
    try:
        import local_actions
        value = local_actions._ffmpeg()
        if value:
            return str(value)
    except Exception:
        pass
    import shutil
    return shutil.which("ffmpeg") or ""
