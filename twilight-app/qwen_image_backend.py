"""Adaptive Qwen-Image-2.1-Turbo backend planner.

Providers
---------
cuda
    NVIDIA path.  Uses the saved INT4 NF4 transformer in the slim package,
    keeps the BF16 text encoder on CPU and optionally shards independent
    replicas across GPUs (data parallel).

dml
    Reserved DirectML path for AMD / Intel GPUs.  It requires ``torch-directml``
    and a BF16 transformer; bitsandbytes INT4 is CUDA-only and is deliberately
    not used.  DirectML support is probed, never assumed.

cpu
    Portable fallback.  It loads the BF16 pipeline on CPU; this is functional
    but much slower than a GPU.

The module only plans.  It never silently changes the user's backend or
downloads weights.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import psutil


@dataclass(frozen=True)
class QwenProvider:
    backend: str
    name: str
    vendor: str
    device: str
    vram_gb: float
    available: bool
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class QwenPlan:
    backend: str
    strategy: str
    replicas: int
    transformer: str
    text_encoder: str
    vae: str
    text_offload: str
    reason: str
    provider: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _cuda_providers() -> list[QwenProvider]:
    try:
        import torch
        if not torch.cuda.is_available():
            return []
        out = []
        for i in range(torch.cuda.device_count()):
            props = torch.cuda.get_device_properties(i)
            out.append(QwenProvider(
                "cuda", torch.cuda.get_device_name(i), "nvidia", f"cuda:{i}",
                props.total_memory / 1024**3, True,
            ))
        return out
    except Exception:
        return []


def _dml_providers() -> list[QwenProvider]:
    try:
        import torch_directml  # type: ignore
        count = int(torch_directml.device_count())
        out = []
        for i in range(count):
            # DirectML exposes an adapter name but not a portable VRAM API.
            # Use the Windows WMI value only as a hint; planning stays conservative.
            out.append(QwenProvider(
                "dml", str(torch_directml.device_name(i)), "amd/intel", f"privateuseone:{i}",
                0.0, True,
                "VRAM is not exposed by torch-directml; BF16 transformer needs ~14GB free.",
            ))
        return out
    except Exception:
        return []


def providers() -> list[QwenProvider]:
    return _cuda_providers() + _dml_providers() + [
        QwenProvider("cpu", "CPU", "cpu", "cpu", 0.0, True,
                     "BF16 on CPU is portable but impractically slow for 8-step 512px.")
    ]


def detect_provider(preferred: str | None = None) -> QwenProvider:
    all_providers = providers()
    preferred = (preferred or os.environ.get("TWILIGHT_QWEN_BACKEND") or "").lower().strip()
    if preferred:
        for provider in all_providers:
            if provider.backend == preferred and provider.available:
                return provider
        raise RuntimeError(f"requested Qwen backend is unavailable: {preferred}")
    for provider in all_providers:
        if provider.backend in {"cuda", "dml"} and provider.available:
            return provider
    return all_providers[-1]


def _find_model(model_dir: str | Path | None) -> Path:
    if model_dir:
        return Path(model_dir)
    return Path(os.environ.get(
        "TWILIGHT_QWEN_DIR",
        Path(__file__).resolve().parent.parent / "runtime" / "portable"
        / "ai" / "models" / "Qwen-Image-2.1-Turbo-universal",
    ))


def choose_plan(model_dir: str | Path | None = None, preferred: str | None = None) -> QwenPlan:
    model = _find_model(model_dir)
    ram_gb = psutil.virtual_memory().total / 1024**3
    try:
        provider = detect_provider(preferred)
    except RuntimeError as exc:
        return QwenPlan("unavailable", "unavailable", 0, "none", "none", "none", "none", str(exc))

    if not (model / "transformer").is_dir():
        return QwenPlan(provider.backend, "unavailable", 0, "none", "none", "none", "none",
                        f"Qwen slim model not found: {model}", provider.to_dict())
    if ram_gb < 24:
        return QwenPlan(provider.backend, "unavailable", 0, "int4", "none", "none", "none",
                        "Less than 24 GB RAM: the 16.3 GB BF16 text encoder cannot be loaded safely.",
                        provider.to_dict())

    if provider.backend == "cpu":
        return QwenPlan("cpu", "cpu", 0, "bf16", "bf16-cpu", "cpu", "all",
                        "No CUDA/DirectML GPU; CPU is a compatibility path, not a benchmark path.",
                        provider.to_dict())

    if provider.backend == "dml":
        if provider.vram_gb and provider.vram_gb < 14:
            return QwenPlan("dml", "unavailable", 0, "bf16", "bf16-cpu", "none", "none",
                            "DirectML BF16 transformer needs about 14 GB VRAM; INT4 bitsandbytes is CUDA-only.",
                            provider.to_dict())
        return QwenPlan("dml", "single", 1, "bf16", "bf16-cpu", "dml", "text-cpu",
                        "Reserved DirectML path: BF16 transformer on the DML adapter, text encoder on CPU. "
                        "Operator coverage must be validated per AMD/Intel driver.",
                        provider.to_dict())

    cuda = [p for p in providers() if p.backend == "cuda" and p.available]
    vram = max(p.vram_gb for p in cuda)
    total_vram = sum(p.vram_gb for p in cuda)
    if len(cuda) >= 2 and total_vram >= 16 and ram_gb >= 40:
        return QwenPlan("cuda", "data_parallel", min(2, len(cuda)), "int4", "bf16-cpu", "gpu", "text-cpu",
                        "Two CUDA GPUs: independent INT4 replicas maximize image throughput; "
                        "model/tensor parallel was slower for one image.", provider.to_dict())
    if vram >= 16:
        return QwenPlan("cuda", "single", 1, "bf16", "bf16-cpu", "gpu", "text-cpu",
                        "At least 16 GB VRAM: BF16 transformer fits; text encoder stays on CPU.",
                        provider.to_dict())
    if vram >= 6.5:
        return QwenPlan("cuda", "single", 1, "int4", "bf16-cpu", "gpu", "text-cpu",
                        "6.5-16 GB VRAM: saved INT4 transformer plus BF16 text encoder on CPU; ~6 GB peak.",
                        provider.to_dict())
    return QwenPlan("cuda", "single", 1, "int4", "bf16-cpu", "cpu", "sequential",
                    "Less than 6.5 GB VRAM: sequential CPU offload is required; expect low speed.",
                    provider.to_dict())


if __name__ == "__main__":
    import json
    print(json.dumps({
        "providers": [p.to_dict() for p in providers()],
        "plan": choose_plan().to_dict(),
    }, ensure_ascii=False, indent=2))
