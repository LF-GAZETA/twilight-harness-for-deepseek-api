"""RAID-0-style work striping across CUDA devices.

This is deliberately a *data-parallel* striping layer: independent work items
are distributed across visible GPUs and gathered in input order.  It is not a
model-parallel tensor split, which would require model-specific partitioning.
"""
from __future__ import annotations

import concurrent.futures
from typing import Callable, Iterable, TypeVar

import gpu_scheduler

T = TypeVar("T")
R = TypeVar("R")


def device_ids() -> list[int]:
    devices = gpu_scheduler.visible_devices()
    if devices:
        return list(range(len(devices)))
    try:
        import torch_directml
        count = int(torch_directml.device_count())
        return list(range(max(0, count)))
    except Exception:
        return []


def stripe(items: Iterable[T], gpu_ids: list[int] | None = None) -> list[tuple[T, int]]:
    values = list(items)
    devices = gpu_ids or device_ids()
    if not devices:
        return [(item, -1) for item in values]
    return [(item, devices[index % len(devices)]) for index, item in enumerate(values)]


def raid0_map(worker: Callable[[T, int], R], items: Iterable[T],
              max_workers: int | None = None) -> list[R]:
    """Run worker(item, gpu_id) in parallel, preserving input order."""
    assignments = stripe(items)
    if not assignments:
        return []
    workers = max_workers or min(len(assignments), max(1, len(device_ids())))
    if workers <= 1:
        return [worker(item, gpu) for item, gpu in assignments]
    results: list[R | None] = [None] * len(assignments)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(worker, item, gpu): index
            for index, (item, gpu) in enumerate(assignments)
        }
        for future in concurrent.futures.as_completed(futures):
            index = futures[future]
            results[index] = future.result()
    return [result for result in results if result is not None]  # type: ignore[list-item]


def split_lanes(count: int, gpu_count: int | None = None) -> list[int]:
    """Assign N concurrent jobs to GPUs, reusing a GPU when jobs exceed lanes."""
    lanes = gpu_count if gpu_count is not None else max(1, len(device_ids()))
    if lanes <= 0:
        return [-1] * count
    return [index % lanes for index in range(count)]
