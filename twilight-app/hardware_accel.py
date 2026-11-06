"""Accelerator vendor detection and practical backend selection.

Windows notes:
  * NVIDIA -> CUDA
  * AMD     -> DirectML on Windows (ROCm is Linux-only for mainstream PyTorch)
  * Intel   -> DirectML on Windows / XPU on Linux with oneAPI builds
  * CPU     -> universal fallback
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from pathlib import Path


def _adapters() -> list[str]:
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name"],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]
    except Exception:  # noqa: BLE001
        return []


def _nvidia_present() -> bool:
    return shutil.which("nvidia-smi") is not None


def vendors() -> dict:
    adapters = _adapters()
    text = " ".join(adapters).lower()
    return {
        "adapters": adapters,
        "nvidia": "nvidia" in text or _nvidia_present(),
        "amd": "amd" in text or "radeon" in text,
        "intel": "intel" in text,
        "os": platform.system(),
        "mixed": ("nvidia" in text or _nvidia_present()) and ("amd" in text or "intel" in text),
    }


def onnx_providers() -> list[str]:
    try:
        import onnxruntime
        return list(onnxruntime.get_available_providers())
    except Exception:  # noqa: BLE001
        return []


def preferred_backend() -> dict:
    info = vendors()
    providers = onnx_providers()
    if info["nvidia"]:
        backend, provider = "cuda", "CUDAExecutionProvider"
    elif info["amd"]:
        backend = "rocm" if info["os"] == "Linux" else "directml"
        provider = "ROCMExecutionProvider" if backend == "rocm" else "DmlExecutionProvider"
    elif info["intel"]:
        backend = "xpu" if info["os"] == "Linux" else "directml"
        provider = "XpuExecutionProvider" if backend == "xpu" else "DmlExecutionProvider"
    else:
        backend, provider = "cpu", "CPUExecutionProvider"
    return {
        "vendor": info,
        "backend": backend,
        "provider": provider,
        "provider_available": provider in providers,
        "providers": providers,
        "windows_rocm_note": "ROCm PyTorch/ONNX wheels are Linux-first; Windows AMD uses DirectML.",
    }


def torch_device() -> str:
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
        try:
            import torch_directml
            return str(torch_directml.device())
        except Exception:
            return "cpu"
    except Exception:
        return "cpu"


if __name__ == "__main__":
    print(json.dumps(preferred_backend(), ensure_ascii=False, indent=2))
