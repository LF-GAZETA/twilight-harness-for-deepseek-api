"""Headless "视频 VHS 化" (VHS-ify video) pipeline for Twilight Harness.

Ties together the ported NTSC-QT renderer (``vhs_ntscqt``), the pedalboard audio
chain (``vhs_audio``) and an external ffmpeg binary:

    probe -> extract + process audio -> decode frames at render size
          -> NTSC-QT VHS effect per frame -> libx264 video only
          -> mux with AAC audio (or video only) -> <stem>_vhs_<mode>.mp4

The public entry point is :func:`run_vhs_video`.  Progress is reported with the
same event dict shape the app progress board already consumes (see
``progress_window.ProgressWindow.report`` and ``local_actions._emit_progress``)::

    {"id": "vhs-ntscqt-<stem>", "kind": "ntscqt", "name": "<stem>",
     "fraction": 0.0..1.0, "state": "running"|"done"|"error"}

Three rows are reported per job: ``vhs-ffmpeg-<stem>`` (ffmpeg work),
``vhs-pedalboard-<stem>`` (audio chain) and ``vhs-ntscqt-<stem>`` (frame render).

Dependencies: numpy, Pillow (renderer), pedalboard (audio, optional when the
input has no audio) and an ffmpeg executable (bundled via imageio-ffmpeg or
passed in by the caller).
"""

from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import numpy

from vhs_ntscqt import VHS_MODES, make_renderer

__all__ = [
    "VHS_MODES",
    "MODE_TARGET_HEIGHTS",
    "VIDEO_BITRATE",
    "AUDIO_BITRATE",
    "VHSVideoError",
    "VHSCancelled",
    "locate_ffmpeg",
    "probe_video",
    "run_vhs_video",
]

#: Render height per VHS mode.  NOTE: the product specification literally maps
#: the "360p" mode to a 288 px render height as well (it is a *tuning* mode, not
#: a literal resolution), so this table is intentionally not 240/288/360.
MODE_TARGET_HEIGHTS = {
    "240p": 240,
    "288p": 288,
    "360p": 288,
}

VIDEO_BITRATE = "128k"
AUDIO_BITRATE = "64k"

#: x264 output frame rate fallback when ffmpeg's probe line cannot be parsed.
_DEFAULT_FPS = 25.0

#: How often (in decoded frames) frame progress is pushed to the progress board.
_PROGRESS_EVERY = 4


class VHSVideoError(RuntimeError):
    """Raised when ffmpeg (or the input file) cannot be processed."""


class VHSCancelled(RuntimeError):
    """Raised when ``cancel_event`` was set; no output file is produced."""


# ---------------------------------------------------------------------------
# progress board helpers
# ---------------------------------------------------------------------------
def _emit_progress(progress, kind: str, name: str, fraction: float, state: str = "running") -> None:
    if progress is None:
        return
    try:
        progress({
            "id": f"vhs-{kind}-{name}",
            "kind": kind,
            "name": name,
            "fraction": max(0.0, min(1.0, float(fraction))),
            "state": state,
        })
    except Exception:  # noqa: BLE001  (progress board must never break a render)
        pass


def _cancelled(cancel_event) -> bool:
    return cancel_event is not None and bool(cancel_event.is_set())


# ---------------------------------------------------------------------------
# ffmpeg helpers
# ---------------------------------------------------------------------------
def locate_ffmpeg() -> str:
    """Return a usable ffmpeg executable path (imageio-ffmpeg first, PATH next)."""
    try:
        import imageio_ffmpeg  # noqa: PLC0415

        executable = imageio_ffmpeg.get_ffmpeg_exe()
        if executable and Path(executable).is_file():
            return str(executable)
    except Exception:  # noqa: BLE001
        pass
    found = shutil.which("ffmpeg")
    if found:
        return found
    for candidate in (Path(__file__).resolve().parent.parent / "ffmpeg-bin" / "ffmpeg.exe",
                      Path(__file__).resolve().parent / "ffmpeg-bin" / "ffmpeg.exe"):
        if candidate.is_file():
            return str(candidate)
    raise VHSVideoError("ffmpeg is not available")


def _command(ffmpeg: str, *args: str) -> list[str]:
    return [ffmpeg, "-hide_banner", "-nostdin", *args]


def _run(cmd: list[str], timeout: float = 120.0) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise VHSVideoError(f"ffmpeg executable not found: {cmd[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise VHSVideoError("ffmpeg timed out") from exc


def _tail(text: str, limit: int = 400) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return "..." + text[-limit:]


def _log_tail(path: Path) -> str:
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError:
        return ""
    return _tail(data.decode("utf-8", "replace"))


def probe_video(video_path: str | Path, ffmpeg: str | None = None) -> dict:
    """Probe ``video_path`` with ``ffmpeg -i`` (no ffprobe dependency).

    Returns a dict with ``width``, ``height``, ``fps``, ``duration`` (seconds,
    ``None`` when unknown), ``has_audio`` and ``path``.  Raises
    :class:`VHSVideoError` when ffmpeg cannot read the file.
    """
    executable = ffmpeg or locate_ffmpeg()
    path = Path(video_path)
    if not path.is_file():
        raise VHSVideoError(f"input video does not exist: {path}")
    result = _run(_command(executable, "-i", str(path)), timeout=60.0)
    info = result.stderr or ""
    video_line = next(
        (line for line in info.splitlines() if "Stream #" in line and "Video:" in line),
        None,
    )
    if video_line is None:
        raise VHSVideoError(f"ffmpeg could not read video streams from {path.name}: {_tail(info, 300)}")

    width = height = 0
    resolution = re.search(r"(\d{2,5})x(\d{2,5})", video_line)
    if resolution:
        width, height = int(resolution.group(1)), int(resolution.group(2))

    fps = _DEFAULT_FPS
    fps_match = re.search(r"(\d+(?:\.\d+)?)\s*fps", video_line)
    if fps_match is None:
        fps_match = re.search(r"(\d+(?:\.\d+)?)\s*tbr", video_line)
    if fps_match:
        try:
            value = float(fps_match.group(1))
            if value > 0:
                fps = value
        except ValueError:
            pass

    duration = None
    duration_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", info)
    if duration_match:
        hours, minutes, seconds = duration_match.groups()
        duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)

    has_audio = any("Stream #" in line and "Audio:" in line for line in info.splitlines())

    if width <= 0 or height <= 0:
        raise VHSVideoError(f"could not determine video size for {path.name}")

    return {
        "path": path,
        "width": width,
        "height": height,
        "fps": fps,
        "duration": duration,
        "has_audio": has_audio,
    }


def _scaled_frame_size(ffmpeg: str, video_path: Path, target_height: int) -> tuple[int, int]:
    """Return the (width, height) ffmpeg's ``scale=-2:H`` will produce.

    A one-frame PNG is piped through the exact same scale expression used for
    decoding, then the PNG header is parsed; this keeps the rawvideo frame size
    in sync with ffmpeg's aspect rounding for every input size.
    """
    cmd = _command(
        ffmpeg,
        "-v", "error",
        "-i", str(video_path),
        "-map", "0:v:0",
        "-frames:v", "1",
        "-vf", f"scale=-2:{int(target_height)}",
        "-f", "image2pipe",
        "-c:v", "png",
        "-",
    )
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=120.0)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise VHSVideoError("ffmpeg failed to probe the scaled frame size") from exc
    data = result.stdout or b""
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
        if width > 0 and height > 0:
            return width, height
    raise VHSVideoError(f"could not determine scaled frame size: {_log_tail_text(result.stderr)}")


def _log_tail_text(data: bytes) -> str:
    return _tail((data or b"").decode("utf-8", "replace"))


def _unique_output(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    index = 2
    while True:
        candidate = parent / f"{stem}_{index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def _fmt_fps(fps: float) -> str:
    return f"{fps:.6g}"


def _terminate(process: subprocess.Popen | None) -> None:
    if process is None:
        return
    try:
        if process.poll() is None:
            process.kill()
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# public pipeline
# ---------------------------------------------------------------------------
def run_vhs_video(
    video_path: Path,
    mode: str,
    output_dir: Path,
    ffmpeg: str,
    progress=None,
    cancel_event=None,
) -> Path:
    """Render ``video_path`` with the NTSC-QT VHS effect and mux VHS audio.

    :param video_path: source video (any container ffmpeg can decode)
    :param mode: ``"240p"``, ``"288p"`` or ``"360p"`` (see
        :data:`MODE_TARGET_HEIGHTS`)
    :param output_dir: folder for ``<stem>_vhs_<mode>.mp4`` (created if missing)
    :param ffmpeg: ffmpeg executable path (use :func:`locate_ffmpeg`)
    :param progress: optional callable receiving progress event dicts
    :param cancel_event: optional ``threading.Event``; when set the render
        stops and :class:`VHSCancelled` is raised
    :return: path of the rendered mp4 file
    :raises VHSVideoError: on ffmpeg/input failures
    :raises VHSCancelled: when ``cancel_event`` is set
    """
    if mode not in MODE_TARGET_HEIGHTS:
        raise ValueError(f"unknown VHS mode {mode!r}; expected one of {VHS_MODES}")

    source = Path(video_path)
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    executable = ffmpeg or locate_ffmpeg()
    stem = source.stem
    target_height = MODE_TARGET_HEIGHTS[mode]
    started = time.monotonic()

    _emit_progress(progress, "ffmpeg", stem, 0.0)
    _emit_progress(progress, "ntscqt", stem, 0.0)
    _emit_progress(progress, "pedalboard", stem, 0.0)

    temporary_dir = Path(tempfile.mkdtemp(prefix=f"twilight_vhs_{stem}_"))
    decode_process: subprocess.Popen | None = None
    encode_process: subprocess.Popen | None = None
    try:
        info = probe_video(source, executable)
        frame_rate = info["fps"]
        duration = info["duration"]
        has_audio = bool(info["has_audio"])
        if _cancelled(cancel_event):
            raise VHSCancelled("VHS render cancelled")

        # ---- audio: extract -> pedalboard chain ------------------------------
        vhs_audio_path: Path | None = None
        if has_audio:
            source_wav = temporary_dir / "audio_source.wav"
            vhs_audio_path = temporary_dir / "audio_vhs.wav"
            _emit_progress(progress, "ffmpeg", stem, 0.05)
            _emit_progress(progress, "pedalboard", stem, 0.05)
            result = _run(_command(
                executable,
                "-v", "error",
                "-i", str(source),
                "-map", "0:a:0",
                "-vn",
                "-ac", "2",
                "-c:a", "pcm_s16le",
                "-y",
                str(source_wav),
            ))
            if result.returncode != 0 or not source_wav.is_file():
                raise VHSVideoError(f"ffmpeg failed to extract audio: {_tail(result.stderr)}")
            if _cancelled(cancel_event):
                raise VHSCancelled("VHS render cancelled")
            _emit_progress(progress, "pedalboard", stem, 0.5)
            from vhs_audio import process_audio_wav  # noqa: PLC0415  (lazy: pedalboard)

            process_audio_wav(source_wav, vhs_audio_path)
            _emit_progress(progress, "pedalboard", stem, 1.0, "done")
        else:
            _emit_progress(progress, "pedalboard", stem, 1.0, "done")
        if _cancelled(cancel_event):
            raise VHSCancelled("VHS render cancelled")

        # ---- video: decode bgr24 -> VHS frames -> libx264 --------------------
        frame_width, frame_height = _scaled_frame_size(executable, source, target_height)
        frame_size = frame_width * frame_height * 3
        temporary_video = temporary_dir / "video_only.mp4"
        decode_log_path = temporary_dir / "decode.log"
        encode_log_path = temporary_dir / "encode.log"
        renderer = make_renderer(mode, frame_height)

        decode_cmd = _command(
            executable,
            "-v", "error",
            "-i", str(source),
            "-map", "0:v:0",
            "-vf", f"scale=-2:{target_height}",
            "-f", "rawvideo",
            "-pix_fmt", "bgr24",
            "-",
        )
        encode_cmd = _command(
            executable,
            "-v", "error",
            "-f", "rawvideo",
            "-pix_fmt", "bgr24",
            "-s", f"{frame_width}x{frame_height}",
            "-r", _fmt_fps(frame_rate),
            "-i", "-",
            "-an",
            "-c:v", "libx264",
            "-b:v", VIDEO_BITRATE,
            "-pix_fmt", "yuv420p",
            "-y",
            str(temporary_video),
        )
        total_frames = int(round(duration * frame_rate)) if duration else 0
        rendered = 0
        with open(decode_log_path, "wb") as decode_log, open(encode_log_path, "wb") as encode_log:
            decode_process = subprocess.Popen(
                decode_cmd, stdout=subprocess.PIPE, stderr=decode_log, bufsize=frame_size * 2
            )
            encode_process = subprocess.Popen(
                encode_cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=encode_log
            )
            assert decode_process.stdout is not None and encode_process.stdin is not None
            try:
                while True:
                    if _cancelled(cancel_event):
                        raise VHSCancelled("VHS render cancelled")
                    raw = decode_process.stdout.read(frame_size)
                    if not raw or len(raw) < frame_size:
                        break
                    frame = numpy.frombuffer(raw, dtype=numpy.uint8).reshape(frame_height, frame_width, 3)
                    rendered_frame = renderer.process_frame(frame)
                    encode_process.stdin.write(rendered_frame.tobytes())
                    rendered += 1
                    if total_frames > 0:
                        fraction = rendered / total_frames
                    else:  # unknown duration: asymptotic progress capped below 1
                        fraction = 1.0 - math.exp(-rendered / 400.0)
                    if rendered == 1 or rendered % _PROGRESS_EVERY == 0:
                        _emit_progress(progress, "ntscqt", stem, fraction)
                        _emit_progress(progress, "ffmpeg", stem, 0.1 + 0.75 * min(fraction, 0.999))
            except BrokenPipeError as exc:
                raise VHSVideoError(
                    f"ffmpeg encoder stopped early: {_log_tail(encode_log_path)}"
                ) from exc
            finally:
                try:
                    encode_process.stdin.close()
                except Exception:  # noqa: BLE001
                    pass
                # Let both ffmpeg processes finish gracefully: the encoder must
                # flush the final frames and write the moov atom before it can
                # be considered successful.  Only kill on timeout.
                try:
                    decode_process.wait(timeout=30)
                except Exception:  # noqa: BLE001
                    _terminate(decode_process)
                try:
                    encode_process.wait(timeout=300)
                except Exception:  # noqa: BLE001
                    _terminate(encode_process)

        if _cancelled(cancel_event):
            raise VHSCancelled("VHS render cancelled")
        if rendered == 0:
            raise VHSVideoError(f"ffmpeg decoded no video frames: {_log_tail(decode_log_path)}")
        if decode_process.returncode not in (0, None) or encode_process.returncode not in (0, None):
            detail = _log_tail(decode_log_path) or _log_tail(encode_log_path)
            raise VHSVideoError(f"ffmpeg failed while rendering video: {detail}")
        _emit_progress(progress, "ntscqt", stem, 1.0, "done")
        _emit_progress(progress, "ffmpeg", stem, 0.85)

        # ---- mux -------------------------------------------------------------
        final_path = _unique_output(destination / f"{stem}_vhs_{mode}.mp4")
        if has_audio and vhs_audio_path is not None:
            mux_cmd = _command(
                executable,
                "-v", "error",
                "-i", str(temporary_video),
                "-i", str(vhs_audio_path),
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-c:v", "copy",
                "-c:a", "aac",
                "-b:a", AUDIO_BITRATE,
                "-shortest",
                "-movflags", "+faststart",
                "-y",
                str(final_path),
            )
        else:
            mux_cmd = _command(
                executable,
                "-v", "error",
                "-i", str(temporary_video),
                "-map", "0:v:0",
                "-c", "copy",
                "-movflags", "+faststart",
                "-y",
                str(final_path),
            )
        result = _run(mux_cmd)
        if result.returncode != 0 or not final_path.is_file():
            raise VHSVideoError(f"ffmpeg failed to mux output: {_tail(result.stderr)}")
        _emit_progress(progress, "ffmpeg", stem, 1.0, "done")
        _ = time.monotonic() - started  # elapsed time is available to callers via logging
        return final_path
    except VHSCancelled:
        _terminate(decode_process)
        _terminate(encode_process)
        for kind in ("ntscqt", "ffmpeg", "pedalboard"):
            _emit_progress(progress, kind, stem, 0.0, "error")
        raise
    except Exception:
        _terminate(decode_process)
        _terminate(encode_process)
        for kind in ("ntscqt", "ffmpeg", "pedalboard"):
            _emit_progress(progress, kind, stem, 0.0, "error")
        raise
    finally:
        _terminate(decode_process)
        _terminate(encode_process)
        shutil.rmtree(temporary_dir, ignore_errors=True)
