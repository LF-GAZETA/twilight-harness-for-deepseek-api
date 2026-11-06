"""Conservative multi-GPU planning for local AI jobs.

True device fusion/splitting is model-specific; this module schedules existing
single-device workers across visible CUDA devices and exposes a deterministic
plan for the sidecar.
"""
from __future__ import annotations

import itertools
import os
import threading

import hardware_index

_LOCK = threading.Lock()
_ROTATION = itertools.count()


def visible_devices() -> list[dict]:
    devices = hardware_index.index().get("nvidia", [])
    if devices:
        return devices
    try:
        import torch_directml
        return [{"name": f"DirectML GPU {i}", "vendor": "DirectML"}
                for i in range(int(torch_directml.device_count()))]
    except Exception:
        return []


def assign(task: str = "local-ai") -> dict:
    devices = visible_devices()
    if not devices:
        return {"task": task, "cuda_visible_devices": "", "device_index": None,
                "mode": "cpu", "reason": "no CUDA device detected"}
    with _LOCK:
        index = next(_ROTATION) % len(devices)
    # Prefer the least-utilized device when utilization data is present.
    index = min(range(len(devices)), key=lambda i: devices[i].get("util_percent", 0))
    return {
        "task": task,
        "cuda_visible_devices": str(index),
        "device_index": index,
        "mode": "single-gpu",
        "reason": f"{devices[index]['name']} selected by lowest utilization",
    }


def plan(tasks: list[str]) -> list[dict]:
    devices = visible_devices()
    if not devices:
        return [{"task": task, "cuda_visible_devices": "", "mode": "cpu"} for task in tasks]
    result = []
    for position, task in enumerate(tasks):
        index = position % len(devices)
        result.append({
            "task": task,
            "cuda_visible_devices": str(index),
            "device_index": index,
            "mode": "parallel" if len(tasks) > 1 and len(devices) > 1 else "single-gpu",
        })
    return result


def apply_environment(task: str = "local-ai") -> dict:
    choice = assign(task)
    if choice["cuda_visible_devices"]:
        os.environ["CUDA_VISIBLE_DEVICES"] = choice["cuda_visible_devices"]
    return choice
