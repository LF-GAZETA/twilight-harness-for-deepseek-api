"""Local user-requested actions for the attachment build.

Supported requests (explicit slash commands or natural-language triggers):
  /derpi <tags>       search safe Derpibooru images and download via derpicdn.net
  /upscale            Real-ESRGAN upscale the attached image
  /audio              separate the audio track from the attached video
  /transcode <format> transcode the attached video (mp4/webm/mkv/mov/gif)
  /vhs [240p|288p|360p] emulate a VHS tape: NTSC-QT filter, low-bitrate
                      compression and Pedalboard audio degradation

Downloaded files are written to the user's Downloads folder and are never
removed by the application's runtime cleanup.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import attachments
import derpi


class LocalActionError(Exception):
    pass


# Real-ESRGAN/Vulkan cannot reliably run multiple instances at once on a single
# GPU; serialize it and retry transient "invalid gpu device" failures.
_ESRGAN_LOCK = threading.Lock()


@dataclass
class LocalRequest:
    kind: str
    title: str
    attachment: attachments.Attachment | None = None
    query: str = ""
    target_format: str = "mp4"
    params: dict = field(default_factory=dict)
    error: str = ""
    images: list[str] = field(default_factory=list)


def _creation_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def downloads_dir() -> Path:
    """Resolve the user's Downloads known folder; never delete it."""
    return derpi.downloads_dir()


def _tool_dir(name: str) -> Path | None:
    meipass = getattr(sys, "_MEIPASS", None)
    candidates: list[Path] = []
    if meipass:
        candidates.append(Path(meipass) / name)
    candidates.append(Path(__file__).resolve().parent.parent / "tools" / "realesrgan")
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def paths_download_root() -> Path:
    base = Path(__file__).resolve().parent.parent
    return base


def _unique_output(folder: Path, stem: str, suffix: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._") or "output"
    candidate = folder / f"{safe}{suffix}"
    if not candidate.exists():
        return candidate
    for index in range(2, 1000):
        candidate = folder / f"{safe}_{index}{suffix}"
        if not candidate.exists():
            return candidate
    return folder / f"{safe}_{int(time.time())}{suffix}"


def _save_dir() -> Path:
    return derpi.save_dir()


def _ffmpeg() -> str:
    executable = attachments.ffmpeg_executable()
    if not executable:
        raise LocalActionError("ffmpeg is not available")
    return executable


def _real_esrgan_dir() -> Path:
    directory = _tool_dir("realesrgan-bin")
    if directory is None:
        directory = Path(__file__).resolve().parent.parent / "tools" / "realesrgan"
    if not (directory / "realesrgan-ncnn-vulkan.exe").is_file():
        raise LocalActionError("Real-ESRGAN runtime is not available")
    return directory


# Real-ESRGAN is shipped as ncnn Vulkan.  The official binary accepts INT8
# models when they are present next to the fp16 ones; we optionally prefer an
# INT8 model to reduce VRAM/bandwidth, with an automatic fp16 fallback so a
# missing/incompatible INT8 model can never break upscaling.
_ESRGAN_MODEL_DIRS = ("models", "")
_ESRGAN_FP16_MODEL = "realesrgan-x4plus-anime"
_ESRGAN_INT8_MODEL = "realesrgan-x4plus-anime-int8"


def _esrgan_model_file(tool: Path, model_name: str) -> Path | None:
    for sub in _ESRGAN_MODEL_DIRS:
        base = (tool / sub) if sub else tool
        param = base / f"{model_name}.param"
        weights = base / f"{model_name}.bin"
        if param.is_file() and weights.is_file():
            return base / model_name
    return None


def esrgan_int8_status(tool: Path | None = None) -> dict:
    """Return a small status dict used by ``/ai`` and the embedding report."""
    try:
        directory = tool or _real_esrgan_dir()
    except Exception:  # noqa: BLE001
        return {"available": False, "reason": "Real-ESRGAN runtime is not available"}
    int8_path = _esrgan_model_file(directory, _ESRGAN_INT8_MODEL)
    fp16_path = _esrgan_model_file(directory, _ESRGAN_FP16_MODEL)
    env_force = os.environ.get("TWILIGHT_ESRGAN_INT8", "").strip().lower()
    chosen = _esrgan_model_candidates(directory)
    return {
        "available": True,
        "dir": str(directory),
        "int8_model": str(int8_path) if int8_path else "",
        "fp16_model": str(fp16_path) if fp16_path else "",
        "env_force": env_force,
        "chosen": chosen[0][0] if chosen else _ESRGAN_FP16_MODEL,
        "int8_active": bool(chosen and chosen[0][0] == _ESRGAN_INT8_MODEL),
    }


def _esrgan_model_candidates(tool: Path) -> list[tuple[str, str]]:
    """Model names in preference order (INT8 first when enabled+present)."""
    LOGGER = logging.getLogger("twilight_harness")
    explicit = os.environ.get("TWILIGHT_ESRGAN_MODEL", "").strip()
    force = os.environ.get("TWILIGHT_ESRGAN_INT8", "").strip().lower()
    int8_present = _esrgan_model_file(tool, _ESRGAN_INT8_MODEL) is not None
    fp16_present = _esrgan_model_file(tool, _ESRGAN_FP16_MODEL) is not None
    if explicit:
        LOGGER.info("Real-ESRGAN model explicitly requested: %s", explicit)
        return [(explicit, "explicit")]
    candidates: list[tuple[str, str]] = []
    if int8_present and force not in {"0", "false", "off"}:
        candidates.append((_ESRGAN_INT8_MODEL, "int8"))
    if fp16_present or not candidates:
        candidates.append((_ESRGAN_FP16_MODEL, "fp16"))
    LOGGER.info(
        "Real-ESRGAN model preference: %s (int8_present=%s fp16_present=%s)",
        [name for name, _ in candidates],
        int8_present,
        fp16_present,
    )
    return candidates


def _emit_progress(progress, row_id: str, kind: str, name: str,
                   fraction: float, state: str = "running") -> None:
    if progress is None:
        return
    try:
        progress({
            "id": row_id,
            "kind": kind,
            "name": name,
            "fraction": max(0.0, min(1.0, fraction)),
            "state": state,
        })
    except Exception:  # noqa: BLE001
        pass


def _run_realesrgan(tool: Path, input_path: Path, output_path: Path, progress=None,
                    gpu: int | None = None, use_lock: bool = True,
                    env_extra: dict | None = None) -> None:
    errors: list[str] = []
    LOGGER = logging.getLogger("twilight_harness")
    import contextlib
    lock = _ESRGAN_LOCK if use_lock else contextlib.nullcontext()
    with lock:
        for model_name, label in _esrgan_model_candidates(tool):
            command = [
                str(tool / "realesrgan-ncnn-vulkan.exe"),
                "-i",
                str(input_path),
                "-o",
                str(output_path),
                "-n",
                model_name,
                "-s",
                "4",
                "-f",
                "png",
                "-j",
                "2:4:2",
            ]
            if gpu is not None:
                command.extend(["-g", str(gpu)])
            for attempt in range(2):
                if output_path.is_dir():
                    try:
                        for item in output_path.iterdir():
                            item.unlink(missing_ok=True)
                    except OSError:
                        pass
                try:
                    child_env = dict(os.environ)
                    if env_extra:
                        child_env.update({k: str(v) for k, v in env_extra.items()})
                    result = subprocess.run(
                        command,
                        cwd=str(tool),
                        capture_output=True,
                        timeout=7200,
                        creationflags=_creation_flags(),
                        env=child_env,
                    )
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{label}: {exc}")
                    time.sleep(1.0 + attempt)
                    continue
                stderr = result.stderr.decode("utf-8", errors="replace")
                valid = output_path.is_file() and output_path.stat().st_size > 0
                if output_path.is_dir():
                    valid = any(output_path.glob("*.png"))
                if result.returncode == 0 and valid:
                    if label == "int8":
                        LOGGER.info("Real-ESRGAN ran with INT8 model %s", model_name)
                    return
                errors.append(f"{label}: {stderr[-400:]}")
                time.sleep(1.0 + attempt)
            if label == "int8":
                LOGGER.warning(
                    "Real-ESRGAN INT8 model %s failed; falling back to fp16 model",
                    model_name,
                )
    raise LocalActionError("Real-ESRGAN failed: " + (" | ".join(errors[-3:])))


def _upscale_tiled(image: Path, tool: Path, progress=None) -> Path:
    """Split into overlapping left/right tiles, run one ESRGAN per GPU, stitch."""
    from PIL import Image
    import concurrent.futures
    import tempfile
    source = Image.open(image).convert("RGBA")
    width, height = source.size
    half = width // 2
    overlap = 128
    left_box = (0, 0, min(width, half + overlap), height)
    right_box = (max(0, half - overlap), 0, width, height)
    folder = Path(tempfile.mkdtemp(prefix="twilight_esrgan_shard_"))
    left_in = folder / "left.png"
    right_in = folder / "right.png"
    source.crop(left_box).save(left_in)
    source.crop(right_box).save(right_in)
    left_out = folder / "left_x4.png"
    right_out = folder / "right_x4.png"

    def work(input_path: Path, output_path: Path, gpu: int):
        _run_realesrgan(tool, input_path, output_path, gpu=gpu, use_lock=False)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(work, left_in, left_out, 0),
                   pool.submit(work, right_in, right_out, 1)]
        for future in futures:
            future.result()
    left = Image.open(left_out).convert("RGBA")
    right = Image.open(right_out).convert("RGBA")
    output = Image.new("RGBA", (width * 4, height * 4))
    import numpy as _np
    left_arr = _np.asarray(left, dtype=_np.float32)
    right_arr = _np.asarray(right, dtype=_np.float32)
    canvas = _np.zeros((height * 4, width * 4, 4), dtype=_np.float32)
    canvas[:, :left_arr.shape[1]] = left_arr
    # Feather the overlap instead of a hard vertical seam.
    start = (half - overlap) * 4
    end = (half + overlap) * 4
    end = min(end, width * 4, left_arr.shape[1], right_arr.shape[1])
    if end > start:
        span = max(1, end - start)
        for x in range(start, end):
            t = (x - start) / span
            t = t * t * (3.0 - 2.0 * t)
            canvas[:, x, :] = left_arr[:, x, :] * (1.0 - t) + right_arr[:, x - start, :] * t
    if end < width * 4:
        canvas[:, end:width * 4] = right_arr[:, end - start:width * 4 - start]
    output = Image.fromarray(_np.clip(canvas, 0, 255).round().astype(_np.uint8))
    target = _unique_output(_save_dir(), f"{image.stem}_realesrgan_x4", ".png")
    output.save(target)
    try:
        import shutil as _shutil
        _shutil.rmtree(folder, ignore_errors=True)
    except Exception:
        pass
    return target


def _upscale(image: Path, progress=None) -> list[Path]:
    tool = _real_esrgan_dir()
    row_id = f"realesrgan-{image.stem}"
    _emit_progress(progress, row_id, "realesrgan", image.stem, 0.05)
    output = _unique_output(_save_dir(), f"{image.stem}_realesrgan_x4", ".png")
    try:
        if os.environ.get("TWILIGHT_ESRGAN_SHARD", "0") not in {"0", "false", "no"}:
            try:
                import gpu_scheduler
                if len(gpu_scheduler.visible_devices()) > 1:
                    output = _upscale_tiled(image, tool, progress=progress)
                    _emit_progress(progress, row_id, "realesrgan", image.stem, 1.0, "done")
                    return [output]
            except Exception as exc:
                logging.getLogger("twilight_harness").warning("ESRGAN tile shard failed: %s", exc)
        _run_realesrgan(tool, image, output)
    except Exception:
        _emit_progress(progress, row_id, "realesrgan", image.stem, 0.0, "error")
        raise
    _emit_progress(progress, row_id, "realesrgan", image.stem, 1.0, "done")
    return [output]

def _video_fps(video: Path) -> float:
    ffmpeg = _ffmpeg()
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", str(video)],
            capture_output=True,
            timeout=30,
            creationflags=_creation_flags(),
        )
    except Exception as exc:  # noqa: BLE001
        raise LocalActionError(f"video metadata failed: {exc}") from exc
    stderr = result.stderr.decode("utf-8", errors="replace")
    match = re.search(r"(\d+(?:\.\d+)?)\s*fps\b", stderr)
    if match:
        try:
            value = float(match.group(1))
            if 1 <= value <= 240:
                return value
        except ValueError:
            pass
    return 30.0


def _cuda_unit_count() -> int:
    """SM count of one GPU, used as the CU-group count.

    The user-specified policy is one Real-ESRGAN process per SM (for an
    RTX 3080 that is 68 SM -> up to 68 process groups).  Override with
    TWILIGHT_ESRGAN_CU only when a manual value is required.
    """
    override = os.environ.get("TWILIGHT_ESRGAN_CU", "").strip()
    if override.isdigit() and int(override) > 0:
        return int(override)
    try:
        import torch
        if torch.cuda.is_available():
            return max(1, int(torch.cuda.get_device_properties(0).multi_processor_count))
    except Exception:
        pass
    return 8


def _auto_video_concurrency(gpu_count: int) -> int:
    """Single GPU -> 6; two identical GPUs -> 4; otherwise 2."""
    if gpu_count <= 1:
        return 6
    try:
        import hardware_index
        devices = hardware_index.index().get("nvidia", [])
        identities = {(str(item.get("name", "")), int(item.get("memory_mb", 0) or 0)) for item in devices}
        if len(devices) >= 2 and len(identities) == 1 and gpu_count == 2:
            return 4
    except Exception:
        pass
    return 2


def _upscale_cpu_frames(frames: list[Path], output_dir: Path, concurrency: int) -> None:
    if not frames:
        return
    import concurrent.futures
    import shutil
    work_root = output_dir.parent / "cpu_chunks"
    worker = Path(__file__).resolve().with_name("cpu_esrgan_worker.py")
    size = (len(frames) + concurrency - 1) // concurrency
    jobs = []
    for index in range(concurrency):
        batch = frames[index * size:(index + 1) * size]
        if not batch:
            continue
        batch_dir = work_root / f"in_{index:02d}"
        batch_dir.mkdir(parents=True, exist_ok=True)
        for frame in batch:
            try:
                os.link(frame, batch_dir / frame.name)
            except OSError:
                shutil.copy2(frame, batch_dir / frame.name)
        out_dir = work_root / f"out_{index:02d}"
        out_dir.mkdir(parents=True, exist_ok=True)
        jobs.append((batch_dir, out_dir))

    def run_job(job):
        batch_dir, out_dir = job
        completed = subprocess.run(
            [str(Path(sys.executable)), str(worker), "--input", str(batch_dir), "--output", str(out_dir)],
            capture_output=True, timeout=7200, creationflags=_creation_flags(),
        )
        if completed.returncode != 0:
            raise LocalActionError("CPU Real-ESRGAN failed: " + completed.stderr.decode("utf-8", "replace")[-500:])

    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        for future in concurrent.futures.as_completed([pool.submit(run_job, job) for job in jobs]):
            future.result()
    for _, out_dir in jobs:
        for produced in sorted(out_dir.glob("*")):
            shutil.copy2(produced, output_dir / produced.name)
    shutil.rmtree(work_root, ignore_errors=True)


def _upscale_video_frames(tool: Path, frames: list[Path], output_dir: Path, progress=None,
                          stem: str = "") -> None:
    """Whole-directory frame chunks with 4/6/8/12 process concurrency.

    No SM/CU grouping: frames are split into N contiguous chunks and each chunk
    is a whole-directory Real-ESRGAN batch.  ``TWILIGHT_ESRGAN_GPU_MODE`` selects
    single GPU, multi GPU, or CPU (the ncnn binary has no CPU inference backend).
    """
    if not frames:
        return
    try:
        import gpu_scheduler
        gpu_count = max(1, len(gpu_scheduler.visible_devices()))
    except Exception:
        gpu_count = 1
    raw = os.environ.get("TWILIGHT_ESRGAN_CONCURRENCY", "").strip().lower()
    if raw in {"", "auto"}:
        concurrency = _auto_video_concurrency(gpu_count)
    else:
        try:
            concurrency = max(1, int(raw))
        except ValueError:
            concurrency = _auto_video_concurrency(gpu_count)
    concurrency = min(concurrency, len(frames))
    mode = os.environ.get("TWILIGHT_ESRGAN_GPU_MODE", "auto").strip().lower()
    if mode == "auto":
        mode = "single" if gpu_count <= 1 else "multi"
    if mode == "cpu":
        # CPU default concurrency is fixed at four workers.
        if raw in {"", "auto"}:
            concurrency = 4
        concurrency = min(concurrency, len(frames))
        _upscale_cpu_frames(frames, output_dir, concurrency)
        return
    if mode == "single" or gpu_count <= 1:
        gpu_ids = [0] * concurrency
    else:
        gpu_ids = [index % gpu_count for index in range(concurrency)]
    size = (len(frames) + concurrency - 1) // concurrency
    import concurrent.futures
    import shutil
    work_root = output_dir.parent / "video_chunks"
    jobs = []
    for chunk_index in range(concurrency):
        batch = frames[chunk_index * size:(chunk_index + 1) * size]
        if not batch:
            continue
        batch_dir = work_root / f"chunk_{chunk_index:02d}"
        batch_dir.mkdir(parents=True, exist_ok=True)
        for frame in batch:
            target = batch_dir / frame.name
            try:
                if not target.exists():
                    os.link(frame, target)
            except OSError:
                shutil.copy2(frame, target)
        batch_out = work_root / f"out_{chunk_index:02d}"
        batch_out.mkdir(parents=True, exist_ok=True)
        jobs.append((batch_dir, batch_out, gpu_ids[chunk_index]))

    def run_job(job):
        batch_dir, batch_out, gpu = job
        _run_realesrgan(tool, batch_dir, batch_out, gpu=gpu, use_lock=False,
                        env_extra={"CUDA_MPS_ACTIVE_THREAD_PERCENTAGE": str(max(1, 100 // concurrency))})

    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        for future in concurrent.futures.as_completed([pool.submit(run_job, job) for job in jobs]):
            future.result()
    for _, batch_out, _ in jobs:
        for produced in sorted(batch_out.glob("*")):
            shutil.copy2(produced, output_dir / produced.name)
    shutil.rmtree(work_root, ignore_errors=True)

def _video_upscale(video: Path, progress=None) -> list[Path]:
    """Upscale every video frame with Real-ESRGAN, then restore the audio."""
    tool = _real_esrgan_dir()
    ffmpeg = _ffmpeg()
    fps = _video_fps(video)
    esrgan_row = f"realesrgan-{video.stem}"
    ffmpeg_row = f"ffmpeg-{video.stem}"
    _emit_progress(progress, esrgan_row, "realesrgan", video.stem, 0.0)
    _emit_progress(progress, ffmpeg_row, "ffmpeg", video.stem, 0.0)
    output = _unique_output(_save_dir(), f"{video.stem}_realesrgan_x4", ".mp4")
    with tempfile.TemporaryDirectory(prefix=".twilight-video-esrgan-", dir=str(_save_dir())) as directory:
        root = Path(directory)
        input_dir = root / "frames"
        output_dir = root / "upscaled"
        input_dir.mkdir()
        output_dir.mkdir()
        extract_command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(video),
            "-vsync",
            "0",
            "-qscale:v",
            "2",
            str(input_dir / "%06d.png"),
        ]
        try:
            result = subprocess.run(
                extract_command,
                capture_output=True,
                timeout=3600,
                creationflags=_creation_flags(),
            )
        except Exception as exc:  # noqa: BLE001
            raise LocalActionError(f"video frame extraction failed: {exc}") from exc
        frames = sorted(input_dir.glob("*.png"))
        _emit_progress(progress, ffmpeg_row, "ffmpeg", video.stem, 0.25)
        audio_path = root / "audio.m4a"
        audio_result = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(video),
             "-vn", "-c:a", "aac", "-b:a", "192k", str(audio_path)],
            capture_output=True, timeout=600, creationflags=_creation_flags(),
        )
        if result.returncode != 0 or not frames:
            raise LocalActionError(
                "video frame extraction failed: "
                + result.stderr.decode("utf-8", errors="replace")[-500:]
            )
        try:
            _upscale_video_frames(tool, frames, output_dir, progress=progress, stem=video.stem)
        except Exception:
            _emit_progress(progress, esrgan_row, "realesrgan", video.stem, 0.0, "error")
            _emit_progress(progress, ffmpeg_row, "ffmpeg", video.stem, 0.0, "error")
            raise
        _emit_progress(progress, esrgan_row, "realesrgan", video.stem, 1.0, "done")
        _emit_progress(progress, ffmpeg_row, "ffmpeg", video.stem, 0.65)
        encode_command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-framerate",
            f"{fps:g}",
            "-i",
            str(output_dir / "%06d.png"),
            "-i",
            str(audio_path if audio_path.is_file() else video),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0?",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-shortest",
            "-movflags",
            "+faststart",
            str(output),
        ]
        try:
            result = subprocess.run(
                encode_command,
                capture_output=True,
                timeout=3600,
                creationflags=_creation_flags(),
            )
        except Exception as exc:  # noqa: BLE001
            raise LocalActionError(f"video encode failed: {exc}") from exc
        if result.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
            _emit_progress(progress, ffmpeg_row, "ffmpeg", video.stem, 0.0, "error")
            raise LocalActionError(
                "video encode failed: "
                + result.stderr.decode("utf-8", errors="replace")[-500:]
            )
        _emit_progress(progress, ffmpeg_row, "ffmpeg", video.stem, 1.0, "done")
    return [output]


def _frame_interpolate(video: Path, factor: int, progress=None) -> list[Path]:
    """RIFE /frame pipeline: split audio, extract frames, interpolate, re-encode."""
    try:
        factor = int(factor)
    except (TypeError, ValueError):
        raise LocalActionError("frame factor must be an integer")
    if factor < 2 or factor > 30:
        raise LocalActionError("frame factor must be between 2 and 30")
    import rife_interpolate
    from PIL import Image
    ffmpeg = _ffmpeg()
    fps = _video_fps(video)
    output = _unique_output(_save_dir(), f"{video.stem}_rife_x{factor}", ".mp4")
    row = f"rife-{video.stem}"
    _emit_progress(progress, row, "rife", video.stem, 0.0)
    with tempfile.TemporaryDirectory(prefix=".twilight-rife-", dir=str(_save_dir())) as directory:
        root = Path(directory)
        input_dir = root / "frames"
        output_dir = root / "interpolated"
        input_dir.mkdir()
        output_dir.mkdir()
        audio = root / "audio.m4a"
        subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(video),
             "-vn", "-c:a", "aac", "-b:a", "192k", str(audio)],
            capture_output=True, timeout=600, creationflags=_creation_flags(),
        )
        extracted = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(video),
             "-vsync", "0", "-qscale:v", "2", str(input_dir / "%06d.png")],
            capture_output=True, timeout=3600, creationflags=_creation_flags(),
        )
        frames = sorted(input_dir.glob("*.png"))
        if extracted.returncode != 0 or not frames:
            _emit_progress(progress, row, "rife", video.stem, 0.0, "error")
            raise LocalActionError("frame extraction failed: " + extracted.stderr.decode("utf-8", "replace")[-500:])
        import concurrent.futures
        import numpy as _np
        try:
            import gpu_scheduler
            gpu_count = max(1, len(gpu_scheduler.visible_devices()))
        except Exception:
            gpu_count = 1
        workers = max(1, min(gpu_count, len(frames) - 1))
        engines = [rife_interpolate.RifeEngine(index) for index in range(workers)]
        pairs = [(number, frames[number], frames[number + 1]) for number in range(len(frames) - 1)]
        total = max(1, len(pairs))

        def process_pair(job):
            number, first_path, second_path = job
            first = Image.open(first_path).convert("RGBA")
            second = Image.open(second_path).convert("RGBA")
            a = _np.asarray(first.convert("RGB"), dtype=_np.float32) / 255.0
            b = _np.asarray(second.convert("RGB"), dtype=_np.float32) / 255.0
            mids = engines[number % workers].between(a, b, factor)
            return number, mids, first.getchannel("A")

        results = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(process_pair, pair) for pair in pairs]
            for done, future in enumerate(concurrent.futures.as_completed(futures), 1):
                number, mids, alpha = future.result()
                results[number] = (mids, alpha)
                if done % max(1, total // 20) == 0 or done == total:
                    _emit_progress(progress, row, "rife", video.stem, 0.05 + 0.85 * done / total)
        for number in range(len(pairs)):
            mids, alpha = results[number]
            Image.open(frames[number]).save(output_dir / f"{number * factor:06d}.png")
            for offset, mid in enumerate(mids, 1):
                image = Image.fromarray((_np.clip(mid, 0, 1) * 255).astype(_np.uint8))
                if alpha is not None:
                    image = image.convert("RGBA")
                    image.putalpha(alpha)
                image.save(output_dir / f"{number * factor + offset:06d}.png")
        Image.open(frames[-1]).save(output_dir / f"{(len(frames) - 1) * factor:06d}.png")
        _emit_progress(progress, row, "rife", video.stem, 0.92)
        encode = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-framerate", f"{fps * factor:g}", "-i", str(output_dir / "%06d.png"),
            "-i", str(audio if audio.is_file() else video),
            "-map", "0:v:0", "-map", "1:a:0?",
            "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
            "-shortest", "-movflags", "+faststart", str(output),
        ]
        encoded = subprocess.run(encode, capture_output=True, timeout=7200, creationflags=_creation_flags())
        if encoded.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
            _emit_progress(progress, row, "rife", video.stem, 0.0, "error")
            raise LocalActionError("frame encode failed: " + encoded.stderr.decode("utf-8", "replace")[-500:])
        _emit_progress(progress, row, "rife", video.stem, 1.0, "done")
    return [output]


def _extract_audio(video: Path) -> list[Path]:
    output = _unique_output(_save_dir(), f"{video.stem}_audio", ".m4a")
    command = [
        _ffmpeg(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(video),
        "-vn",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        str(output),
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            timeout=600,
            creationflags=_creation_flags(),
        )
    except Exception as exc:  # noqa: BLE001
        raise LocalActionError(f"audio extraction failed: {exc}") from exc
    if result.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
        raise LocalActionError(
            "audio extraction failed: "
            + result.stderr.decode("utf-8", errors="replace")[-500:]
        )
    return [output]


def _transcode(video: Path, target_format: str, params: dict | None = None) -> list[Path]:
    params = params or {}
    fmt = (target_format or "mp4").lower().lstrip(".")
    if fmt not in {"mp4", "webm", "mkv", "mov", "gif"}:
        raise LocalActionError("unsupported transcode format: " + fmt)
    output = _unique_output(_save_dir(), f"{video.stem}_transcoded", "." + fmt)
    video_bitrate = str(params.get("video_bitrate") or params.get("vbitrate") or "").strip()
    audio_bitrate = str(params.get("audio_bitrate") or params.get("abitrate") or "").strip()
    resolution = str(params.get("resolution") or "").strip()
    fps = str(params.get("fps") or "").strip()
    vf_parts: list[str] = []
    if resolution:
        lower_res = resolution.lower()
        if lower_res.endswith("p") and lower_res[:-1].isdigit():
            vf_parts.append(f"scale=-2:{lower_res[:-1]}")
        elif "x" in lower_res:
            width, height = lower_res.split("x", 1)
            if width.isdigit() and height.isdigit():
                vf_parts.append(f"scale={width}:{height}")
    if fps:
        try:
            fps_value = float(fps)
            if 1 <= fps_value <= 120:
                vf_parts.append(f"fps={fps_value:g}")
        except ValueError:
            pass
    if fmt == "gif":
        if not fps:
            vf_parts.append("fps=12")
        if not resolution:
            vf_parts.append("scale='min(960,iw)':-1:flags=lanczos")
        vf_parts.append("split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse")
        codec = ["-vf", ",".join(vf_parts), "-loop", "0"]
    else:
        codec = []
        if fmt == "mp4":
            codec += ["-c:v", "libx264", "-preset", "medium"]
            if video_bitrate:
                codec += ["-b:v", video_bitrate]
            else:
                codec += ["-crf", "23"]
            codec += ["-c:a", "aac", "-b:a", audio_bitrate or "192k", "-movflags", "+faststart"]
        elif fmt == "webm":
            codec += ["-c:v", "libvpx-vp9"]
            if video_bitrate:
                codec += ["-b:v", video_bitrate]
            else:
                codec += ["-crf", "32", "-b:v", "0"]
            codec += ["-c:a", "libopus", "-b:a", audio_bitrate or "128k"]
        elif fmt == "mkv":
            if vf_parts:
                # Filters and stream copy are mutually exclusive in ffmpeg;
                # re-encode when the user asked for scaling / frame rate.
                codec += ["-c:v", "libx264", "-preset", "medium"]
                if video_bitrate:
                    codec += ["-b:v", video_bitrate]
                else:
                    codec += ["-crf", "23"]
                codec += ["-c:a", "aac", "-b:a", audio_bitrate or "192k"]
            else:
                codec += ["-c", "copy"]
        elif fmt == "mov":
            codec += ["-c:v", "libx264", "-preset", "medium"]
            if video_bitrate:
                codec += ["-b:v", video_bitrate]
            else:
                codec += ["-crf", "23"]
            codec += ["-c:a", "aac", "-b:a", audio_bitrate or "192k"]
        if vf_parts:
            codec = ["-vf", ",".join(vf_parts)] + codec
    command = ["-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(video), *codec, str(output)]
    try:
        result = subprocess.run(
            [_ffmpeg(), *command],
            capture_output=True,
            timeout=900,
            stdin=subprocess.DEVNULL,
            creationflags=_creation_flags(),
        )
    except Exception as exc:  # noqa: BLE001
        raise LocalActionError(f"video transcode failed: {exc}") from exc
    if result.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
        raise LocalActionError(
            "video transcode failed: "
            + result.stderr.decode("utf-8", errors="replace")[-500:]
        )
    return [output]


def _first_image(items: list[attachments.Attachment]) -> attachments.Attachment | None:
    for item in items:
        if item.suffix in attachments._IMAGE_SUFFIXES:
            return item
    return None


def _first_video(items: list[attachments.Attachment]) -> attachments.Attachment | None:
    for item in items:
        if item.suffix in attachments._VIDEO_SUFFIXES:
            return item
    return None


# Three user-selectable VHS presets.  The app always asks the user to choose
# one; these names travel through LocalRequest.params["mode"].
_VHS_MODES = ("240p", "288p", "360p")


def _vhs_mode(text: str) -> str:
    match = re.search(r"\b(240p|288p|360p)\b", (text or "").lower())
    return match.group(1) if match else ""


def parse_transcode_params(text: str) -> dict:
    lower = text.lower()
    params: dict = {}
    bitrate = re.search(r"(?:视频码率|video[_-]?bitrate|vbitrate|vbr|\b码率)\s*[=:：]?\s*([0-9.]+\s*[kKmMgG]?)", lower)
    if bitrate:
        params["video_bitrate"] = bitrate.group(1).replace(" ", "").upper()
    audio = re.search(r"(?:音频码率|audio[_-]?bitrate|abitrate|abr)\s*[=:：]?\s*([0-9.]+\s*[kKmMgG]?)", lower)
    if audio:
        params["audio_bitrate"] = audio.group(1).replace(" ", "").upper()
    resolution = re.search(r"(?:分辨率|resolution)\s*[=:：]?\s*([0-9]{3,4}p|[0-9]{3,5}x[0-9]{3,5})", lower)
    if not resolution:
        resolution = re.search(r"\b([0-9]{3,4}p|[0-9]{3,5}x[0-9]{3,5})\b", lower)
    if resolution:
        params["resolution"] = resolution.group(1).lower()
    fps = re.search(r"(?:fps|帧率)\s*[=:：]?\s*([0-9]{1,3}(?:\.[0-9]+)?)", lower)
    if fps:
        params["fps"] = fps.group(1)
    return params


def parse_request(text: str, items: list[attachments.Attachment]) -> LocalRequest | None:
    raw = (text or "").strip()
    lower = raw.lower()
    m = re.match(r"^/(derpi|derpibooru)\b\s*(.*)$", raw, re.IGNORECASE | re.DOTALL)
    if m:
        return LocalRequest(kind="derpi", title="Derpibooru", query=m.group(2).strip() or "twilight sparkle")
    if re.match(r"^/vupscale\b", lower):
        video = _first_video(items)
        if video is None:
            return LocalRequest(kind="video_upscale", title="Real-ESRGAN Video", error="video")
        return LocalRequest(kind="video_upscale", title="Real-ESRGAN Video", attachment=video)
    if re.match(r"^/upscale\b", lower):
        image = _first_image(items)
        if image is None:
            return LocalRequest(kind="upscale", title="Real-ESRGAN", error="image")
        return LocalRequest(kind="upscale", title="Real-ESRGAN", attachment=image)
    if re.match(r"^/(audio|extract-audio)\b", lower):
        video = _first_video(items)
        if video is None:
            return LocalRequest(kind="audio", title="Audio", error="video")
        return LocalRequest(kind="audio", title="Audio", attachment=video)
    if re.match(r"^/vhs(?:化)?(?:\s|$)", lower):
        video = _first_video(items)
        mode = _vhs_mode(raw)
        intensity = None
        m_value = re.search(r"(?:^|\s)([0-9]+(?:\.[0-9]+)?)\s*%?", raw)
        if m_value:
            try:
                intensity = float(m_value.group(1))
                if intensity > 1.0:
                    intensity /= 100.0
                intensity = max(0.0, min(1.0, intensity))
            except ValueError:
                intensity = None
        if video is None:
            return LocalRequest(kind="vhs", title="VHS", error="video")
        if intensity is None and not mode:
            intensity = 0.7
        return LocalRequest(kind="vhs", title="VHS", attachment=video,
                            params={"mode": mode, "intensity": intensity})
    m = re.match(r"^/transcode\b(?:\s+([A-Za-z0-9]+))?", lower)
    if m:
        video = _first_video(items)
        if video is None:
            return LocalRequest(kind="transcode", title="Transcode", error="video", target_format=m.group(1) or "mp4")
        return LocalRequest(
            kind="transcode",
            title="Transcode",
            attachment=video,
            target_format=m.group(1) or "mp4",
            params=parse_transcode_params(raw),
        )

    # Natural-language convenience triggers (Derpibooru is model-translated).
    if any(word in lower for word in ("视频超分", "逐帧超分", "逐帧放大", "video upscale", "vupscale")):
        video = _first_video(items)
        if video is not None:
            return LocalRequest(kind="video_upscale", title="Real-ESRGAN Video", attachment=video)
    if any(word in lower for word in ("超分", "超分辨率", "放大", "upscale", "esrgan")):
        video = _first_video(items)
        if video is not None:
            return LocalRequest(kind="video_upscale", title="Real-ESRGAN Video", attachment=video)
        image = _first_image(items)
        if image is not None:
            return LocalRequest(kind="upscale", title="Real-ESRGAN", attachment=image)
    if any(word in lower for word in ("提取音频", "分离音频", "音频分离", "extract audio", "demux audio")):
        video = _first_video(items)
        if video is not None:
            return LocalRequest(kind="audio", title="Audio", attachment=video)
    if "压片" in lower or "极限压缩" in lower or lower.startswith("/compress"):
        video = _first_video(items)
        if video is not None:
            return LocalRequest(
                kind="transcode",
                title="Compress",
                attachment=video,
                target_format="mp4",
                params={
                    "resolution": "240p",
                    "video_bitrate": "42k",
                    "audio_bitrate": "14k",
                },
            )
    if ("vhs" in lower or "录像带" in lower):
        video = _first_video(items)
        if video is not None:
            return LocalRequest(
                kind="vhs",
                title="VHS",
                attachment=video,
                params={"mode": _vhs_mode(raw)},
            )
    if any(word in lower for word in ("转码", "transcode")):
        video = _first_video(items)
        if video is not None:
            fmt_match = re.search(r"\b(mp4|webm|mkv|mov|gif)\b", lower)
            return LocalRequest(
                kind="transcode",
                title="Transcode",
                attachment=video,
                target_format=fmt_match.group(1) if fmt_match else "mp4",
                params=parse_transcode_params(raw),
            )
    return None


def run(request: LocalRequest, progress=None) -> list[Path]:
    if request.error == "image":
        raise LocalActionError("please attach an image first")
    if request.error == "video":
        raise LocalActionError("please attach a video first")
    if request.kind == "derpi":
        return derpi.search_download(request.query, progress=progress)
    if request.attachment is None:
        raise LocalActionError("missing attachment")
    if request.kind == "vhs":
        intensity = request.params.get("intensity")
        if intensity is not None:
            import effects_bridge
            return [effects_bridge.run("vhs", "q", float(intensity), request.attachment.path, progress=progress)]
        mode = str(request.params.get("mode") or "")
        if mode not in _VHS_MODES:
            raise LocalActionError("VHS mode was not selected")
        import vhs_video
        return [
            vhs_video.run_vhs_video(
                request.attachment.path,
                mode,
                _save_dir(),
                _ffmpeg(),
                progress=progress,
            )
        ]
    if request.kind == "upscale":
        return _upscale(request.attachment.path, progress=progress)
    if request.kind == "video_upscale":
        return _video_upscale(request.attachment.path, progress=progress)
    if request.kind == "audio":
        return _extract_audio(request.attachment.path)
    if request.kind == "transcode":
        return _transcode(request.attachment.path, request.target_format, request.params)
    raise LocalActionError("unknown local action")
