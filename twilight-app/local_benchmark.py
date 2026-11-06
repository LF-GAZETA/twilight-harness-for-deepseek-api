"""Small reproducible local benchmark with weighted point scores.

Quick mode covers CPU, PIL and ffmpeg.  ``--full`` additionally attempts
Real-ESRGAN, NTSC-VHS and SD1.5 on small inputs; those are intentionally
optional because they need model weights and can take minutes.
"""
from __future__ import annotations

import argparse
import io
import json
import subprocess
import tempfile
import time
from pathlib import Path

import hardware_index


def _time(fn) -> float:
    started = time.perf_counter()
    fn()
    return max(0.001, time.perf_counter() - started)


def _cpu() -> float:
    def work():
        value = 1
        for _ in range(4_000_000):
            value = (value * 1103515245 + 12345) & 0x7FFFFFFF
    return _time(work)


def _pil() -> float:
    from PIL import Image
    image = Image.new("RGB", (1024, 1024), (80, 40, 120))

    def work():
        for _ in range(6):
            image.resize((512, 512)).resize((1024, 1024))
    return _time(work)


def _ffmpeg() -> float:
    import attachments
    ffmpeg = attachments.ffmpeg_executable()
    if not ffmpeg:
        return 0.0
    target = Path(tempfile.gettempdir()) / "twilight_bench.mp4"

    def work():
        subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi", "-i",
             "testsrc2=size=960x540:rate=30", "-t", "2", "-c:v", "libx264",
             "-preset", "veryfast", "-crf", "28", str(target)],
            check=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    elapsed = _time(work)
    try:
        target.unlink()
    except OSError:
        pass
    return elapsed


def _sd15() -> float:
    import ai_sd15
    engine = ai_sd15.SD15Engine()
    started = time.perf_counter()
    engine.generate("benchmark simple geometric shapes", "", size=(256, 256))
    return max(0.001, time.perf_counter() - started)


def _esrgan() -> float:
    from PIL import Image
    import local_actions
    source = Path(tempfile.gettempdir()) / "twilight_bench_esrgan.png"
    Image.new("RGB", (128, 128), (90, 120, 160)).save(source)
    elapsed = _time(lambda: local_actions._upscale(source))
    try:
        source.unlink()
    except OSError:
        pass
    return elapsed


def _h3() -> float:
    import minimax_h3_runtime
    if not minimax_h3_runtime.available():
        raise RuntimeError("MiniMax H3 weights unavailable")
    started = time.perf_counter()
    minimax_h3_runtime.generate(
        "benchmark: a simple geometric shape rotating slowly",
        num_frames=124, width=960, height=544,
    )
    return max(0.001, time.perf_counter() - started)


def _qwen() -> float:
    import qwen_image_runtime
    if not qwen_image_runtime.available():
        raise RuntimeError("Qwen-Image weights unavailable")
    started = time.perf_counter()
    qwen_image_runtime.generate("benchmark: simple geometric shapes", size="512x512")
    return max(0.001, time.perf_counter() - started)


def _ntsc() -> float:
    import attachments
    import local_actions
    import vhs_video
    ffmpeg = attachments.ffmpeg_executable()
    if not ffmpeg:
        return 0.0
    source = Path(tempfile.gettempdir()) / "twilight_bench_vhs.mp4"
    subprocess.run(
        [ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi", "-i",
         "testsrc2=size=96x96:rate=12", "-t", "1", "-c:v", "libx264",
         "-pix_fmt", "yuv420p", str(source)],
        check=True, timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    started = time.perf_counter()
    vhs_video.run_vhs_video(source, "240p", local_actions._save_dir(), ffmpeg)
    elapsed = max(0.001, time.perf_counter() - started)
    try:
        source.unlink()
    except OSError:
        pass
    return elapsed


def _score(elapsed: float, baseline: float) -> float:
    return round(min(1000.0, baseline / max(0.001, elapsed) * 100.0), 1)


PROFILES = {
    "plus": {
        "weights": {"qwen": 0.30, "esrgan": 0.15, "ntsc": 0.10, "ffmpeg": 0.10, "cpu": 0.20, "pil": 0.15},
        "tests": (("qwen", _qwen, 30.0), ("esrgan", _esrgan, 3.0), ("ntsc", _ntsc, 5.0)),
    },
    "standard": {
        "weights": {"sd15": 0.35, "esrgan": 0.15, "ntsc": 0.10, "ffmpeg": 0.10, "cpu": 0.15, "pil": 0.15},
        "tests": (("sd15", _sd15, 40.0), ("esrgan", _esrgan, 3.0), ("ntsc", _ntsc, 5.0)),
    },
    "benchmark": {
        "weights": {"qwen": 0.35, "esrgan": 0.15, "ntsc": 0.10,
                    "ffmpeg": 0.10, "cpu": 0.15, "pil": 0.15},
        "tests": (("qwen", _qwen, 30.0), ("esrgan", _esrgan, 3.0), ("ntsc", _ntsc, 5.0)),
    },
}


def run(full: bool = False, variant: str | None = None) -> dict:
    import feature_flags
    profile_name = (variant or feature_flags.VARIANT or "plus").lower()
    if profile_name == "debug":
        profile_name = "plus"
    profile = PROFILES.get(profile_name)
    if profile is None:
        raise RuntimeError("当前版本不提供 Benchmark")
    cpu_time = _cpu()
    pil_time = _pil()
    ffmpeg_time = _ffmpeg()
    result = {
        "variant": profile_name,
        "profile_weights": profile["weights"],
        "hardware": hardware_index.index(),
        "timings_seconds": {"cpu": cpu_time, "pil": pil_time, "ffmpeg": ffmpeg_time},
        "pt": {
            "cpu": _score(cpu_time, 1.0),
            "pil": _score(pil_time, 1.2),
            "ffmpeg": _score(ffmpeg_time, 2.0),
        },
        "weights": profile["weights"],
    }
    if full:
        for key, function, baseline in profile["tests"]:
            try:
                elapsed = _time(function)
                result["timings_seconds"][key] = elapsed
                result["pt"][key] = _score(elapsed, baseline)
            except Exception as exc:  # noqa: BLE001
                result["pt"][key] = 0.0
                result.setdefault("errors", {})[key] = str(exc)[:300]
    scores = result["pt"]
    weights = result["weights"]
    if full:
        result["total_pt"] = round(sum(float(scores.get(key, 0.0)) * weight for key, weight in weights.items()), 1)
    result["partial_total_pt"] = round(
        scores["cpu"] * 0.4 + scores["pil"] * 0.3 + scores["ffmpeg"] * 0.3, 1)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(args.full), ensure_ascii=False, indent=2))
