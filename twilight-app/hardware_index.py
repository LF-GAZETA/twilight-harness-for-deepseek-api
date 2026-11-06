"""Local hardware index and a small, dependency-free performance score."""
from __future__ import annotations

import ctypes
import json
import os
import platform
import shutil
import subprocess
import time
from pathlib import Path


def _registry_cpu() -> str:
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                             r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
        value, _ = winreg.QueryValueEx(key, "ProcessorNameString")
        return str(value).strip()
    except Exception:  # noqa: BLE001
        return platform.processor() or "Unknown CPU"


def _memory_gb() -> float:
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(status)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    return round(status.ullTotalPhys / (1024 ** 3), 1)


def _nvidia() -> list[dict]:
    executable = shutil.which("nvidia-smi")
    if not executable:
        return []
    try:
        result = subprocess.run(
            [executable, "--query-gpu=name,memory.total,memory.used,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        devices = []
        for line in result.stdout.splitlines():
            parts = [part.strip() for part in line.split(",")]
            if len(parts) >= 4:
                devices.append({"name": parts[0], "memory_mb": int(float(parts[1])),
                                "used_mb": int(float(parts[2])), "util_percent": int(float(parts[3])),
                                "vendor": "NVIDIA"})
        return devices
    except Exception:  # noqa: BLE001
        return []


def _display_adapters() -> list[str]:
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


def _cpu_benchmark(seconds: float = 0.6) -> float:
    """Tiny integer workload; returns thousands of operations per second."""
    started = time.perf_counter()
    operations = 0
    value = 1
    while time.perf_counter() - started < seconds:
        for _ in range(20000):
            value = (value * 1103515245 + 12345) & 0x7FFFFFFF
        operations += 20000
    return round(operations / max(0.001, time.perf_counter() - started) / 1000.0, 1)


def index() -> dict:
    nvidia = _nvidia()
    adapters = _display_adapters()
    cpu_score = _cpu_benchmark()
    memory = _memory_gb()
    cpu_pt = max(1.0, min(1000.0, cpu_score / 10.0))
    gpu_pt = 0.0
    for device in nvidia:
        gpu_pt += min(1000.0, device["memory_mb"] / 24.576)
    if not gpu_pt and adapters:
        # Integrated / non-CUDA adapters get a conservative placeholder, not a fake CUDA score.
        gpu_pt = 20.0
    ram_pt = min(100.0, memory / 64.0 * 100.0)
    total = round(cpu_pt * 0.4 + gpu_pt * 0.4 + ram_pt * 0.2, 1)
    return {
        "cpu": _registry_cpu(),
        "cpu_threads": os.cpu_count(),
        "cpu_score": cpu_score,
        "cpu_pt": round(cpu_pt, 1),
        "memory_gb": memory,
        "ram_pt": round(ram_pt, 1),
        "nvidia": nvidia,
        "adapters": adapters,
        "gpu_pt": round(gpu_pt, 1),
        "total_pt": total,
        "weights": {"cpu": 0.4, "gpu": 0.4, "ram": 0.2},
        "cuda_available": bool(nvidia),
        "note": "AMD/Intel GPUs are detected but the bundled torch runtime is CUDA-first; "
                "see gpu_scheduler.py for conservative fallback behavior.",
    }


if __name__ == "__main__":
    print(json.dumps(index(), ensure_ascii=False, indent=2))
