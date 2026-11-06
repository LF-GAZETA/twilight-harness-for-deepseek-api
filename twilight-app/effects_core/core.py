"""Beold processing core.

All media work is delegated to the bundled command-line engines:
ffmpeg / ffprobe, SoX, NTSC-RS and ImageMagick.
"""

from __future__ import annotations

import ctypes
import json
import math
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Callable, Iterable, Optional

from PIL import Image

APP_NAME = "Beold"
APP_VERSION = "0027"

VIDEO_EXTS = {
    ".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".wmv", ".flv",
    ".mts", ".m2ts", ".ts", ".mpg", ".mpeg", ".3gp", ".ogv", ".vob",
}
IMAGE_EXTS = {
    ".jpg", ".jpeg", ".jfif", ".png", ".apng", ".bmp", ".webp", ".tif", ".tiff",
    ".gif", ".ppm", ".pgm", ".tga",
}

LogCallback = Callable[[str, str], None]
ProgressCallback = Callable[[Optional[int], str], None]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class BeoldError(RuntimeError):
    """A user-facing processing error."""


class Cancelled(BeoldError):
    """The user cancelled the job."""


class PtyUnavailable(BeoldError):
    """ConPTY is not available on this system."""


class ToolMissingError(BeoldError):
    """One or more external engines are missing."""


class ProcessFailure(BeoldError):
    """A child process returned a non-zero status."""

    def __init__(self, stage: str, code: int, detail: str) -> None:
        self.stage = stage
        self.code = code
        self.detail = detail
        super().__init__(
            f"{stage} 失败（退出码 {code}）。\n{detail or '引擎没有返回详细信息。'}"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resource_path(name: str) -> Path:
    roots = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass))
    roots.append(app_dir())
    for root in roots:
        candidate = root / "assets" / name
        if candidate.exists():
            return candidate
    return app_dir() / "assets" / name


def media_kind(path: os.PathLike[str] | str) -> Optional[str]:
    ext = Path(path).suffix.lower()
    if ext in VIDEO_EXTS:
        return "video"
    if ext in IMAGE_EXTS:
        return "image"
    return None


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def image_frame_count(path: os.PathLike[str] | str) -> int:
    try:
        with Image.open(path) as image:
            return int(getattr(image, "n_frames", 1) or 1)
    except Exception:
        return 1


def is_animated_image(path: os.PathLike[str] | str) -> bool:
    return media_kind(path) == "image" and image_frame_count(path) > 1


@lru_cache(maxsize=1)
def cpu_group_sizes() -> tuple[int, ...]:
    """Return logical processor counts per Windows processor group.

    Python 3.12's os.cpu_count() can report only the current 64-CPU group on
    large Windows machines. GetActiveProcessorCount(ALL_PROCESSOR_GROUPS)
    returns the real total.
    """
    if os.name != "nt":
        count = max(1, os.cpu_count() or 1)
        return (count,)
    try:
        kernel32 = ctypes.windll.kernel32
        group_count = int(kernel32.GetActiveProcessorGroupCount())
        groups = []
        for index in range(max(1, group_count)):
            count = int(kernel32.GetActiveProcessorCount(index))
            if count > 0:
                groups.append(count)
        if groups:
            return tuple(groups)
    except Exception:
        pass
    count = max(1, os.cpu_count() or 1)
    return (count,)


def host_cpu_count() -> int:
    return max(1, sum(cpu_group_sizes()))


def vhs_thread_split(total: Optional[int] = None) -> tuple[int, int]:
    """Return (video_threads, audio_threads).

    SoX is effectively single-threaded, so give the audio chain 5–10% of the
    host as a ceiling and leave the rest to NTSC-RS.
    """
    count = max(1, int(total if total is not None else host_cpu_count()))
    if count <= 1:
        return 1, 1
    audio = max(1, min(12, round(count * 0.08)))
    video = max(1, count - audio)
    return video, audio


def level_to_fraction(level: int) -> float:
    """Slider 1..100 -> 0..1 intensity, inclusive at both ends.

    VHS uses this directly (higher = more decay). SQUEEZE and PATINA invert it
    because their formula parameters are defined in the opposite direction:
    higher t = better quality, higher s = less resampling / higher JPEG quality.
    """
    return clamp((int(level) - 1) / 99.0, 0.0, 1.0)


def level_to_formula_fraction(level: int) -> float:
    """Slider intensity -> the formula's t/s parameter.

    For SQUEEZE/PATINA, level 100 must mean maximum crush/patina, which is
    t=0 / s=0. Keeping this conversion in one place keeps the UI honest.
    """
    return 1.0 - level_to_fraction(level)


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 1000):
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise BeoldError(f"无法创建唯一的输出文件名：{path.name}")


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} GB"


def human_duration(seconds: float) -> str:
    seconds = max(0.0, float(seconds or 0))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def format_cmd(cmd: Iterable[object]) -> str:
    return " ".join(shlex.quote(str(part)) for part in cmd)


def creation_flags() -> int:
    if os.name == "nt":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return 0


def _decode(data: bytes) -> str:
    for encoding in ("utf-8", "gb18030", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# Tool discovery
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GpuDevice:
    backend: str
    index: int
    name: str
    encoder: str
    min_total_kbps: int = 0

    @property
    def label(self) -> str:
        return f"{self.name} · {self.encoder}"


@lru_cache(maxsize=1)
def detect_gpu_devices() -> tuple[GpuDevice, ...]:
    """Probe working hardware H.264 encoders.

    This performs a tiny 320x240 encode. It is intentionally conservative:
    a backend is only reported when ffmpeg can actually open it.
    """
    try:
        tools = Toolchain.discover()
    except Exception:
        return ()
    ffmpeg = str(tools.ffmpeg)

    def works(encoder: str) -> bool:
        cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=black:s=320x240:d=0.2:r=10",
            "-c:v", encoder, "-f", "null", "-",
        ]
        try:
            proc = subprocess.run(
                cmd, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=creation_flags(), timeout=12,
            )
            return proc.returncode == 0
        except Exception:
            return False

    devices: list[GpuDevice] = []
    if works("h264_nvenc"):
        indices = [0]
        smi = shutil.which("nvidia-smi")
        candidates = [
            smi,
            r"C:\Windows\System32\nvidia-smi.exe",
            r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
        ]
        for candidate in candidates:
            if not candidate or not Path(candidate).exists():
                continue
            try:
                out = subprocess.run(
                    [candidate, "--query-gpu=index,name", "--format=csv,noheader"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    creationflags=creation_flags(), timeout=8,
                )
                if out.returncode == 0:
                    parsed = []
                    for line in _decode(out.stdout).splitlines():
                        parts = [part.strip() for part in line.split(",", 1)]
                        if len(parts) == 2 and parts[0].isdigit():
                            parsed.append((int(parts[0]), parts[1]))
                    if parsed:
                        indices = [index for index, _name in parsed]
                        for index, name in parsed:
                            devices.append(GpuDevice("nvenc", index, name, "h264_nvenc", 48))
                        break
            except Exception:
                pass
        if not devices:
            for index in indices:
                devices.append(GpuDevice("nvenc", index, f"NVIDIA GPU {index}", "h264_nvenc", 48))

    if works("h264_qsv"):
        devices.append(GpuDevice("qsv", 0, "Intel Quick Sync", "h264_qsv", 160))
    if works("h264_amf"):
        devices.append(GpuDevice("amf", 0, "AMD AMF", "h264_amf", 64))
    return tuple(devices)


@dataclass(frozen=True)
class Toolchain:
    ffmpeg: Path
    ffprobe: Path
    sox: Path
    magick: Path
    ntsc: Path
    ntsc_home: Path

    @classmethod
    def discover(cls) -> "Toolchain":
        root = app_dir()
        tools = Path(
            os.environ.get("BEOLD_TOOLS_DIR", str(root / "tools"))
        ).resolve()

        def pick(relative: str, command: str) -> Optional[Path]:
            bundled = tools / relative
            if bundled.exists():
                return bundled
            found = shutil.which(command)
            return Path(found) if found else None

        ffmpeg = pick("ffmpeg/bin/ffmpeg.exe", "ffmpeg")
        ffprobe = pick("ffmpeg/bin/ffprobe.exe", "ffprobe")
        sox = pick("sox/sox.exe", "sox")
        magick = pick("imagemagick/magick.exe", "magick")
        ntsc = pick("ntsc-rs/bin/ntsc-rs-cli.exe", "ntsc-rs-cli")
        if not ntsc:
            ntsc = pick("ntsc-rs/ntsc-rs-cli.exe", "ntsc-rs-cli")

        missing = [
            name
            for name, value in (
                ("ffmpeg", ffmpeg),
                ("ffprobe", ffprobe),
                ("SoX", sox),
                ("ImageMagick", magick),
                ("NTSC-RS", ntsc),
            )
            if value is None
        ]
        if missing or ffmpeg is None or ffprobe is None or sox is None or magick is None or ntsc is None:
            raise ToolMissingError(
                "缺少核心引擎：" + "、".join(missing) + "。请确认 tools 文件夹完整。"
            )

        ntsc_home = ntsc.parent.parent
        return cls(
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            sox=sox,
            magick=magick,
            ntsc=ntsc,
            ntsc_home=ntsc_home,
        )


# ---------------------------------------------------------------------------
# Media probing
# ---------------------------------------------------------------------------


@dataclass
class MediaInfo:
    path: Path
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    has_video: bool = False
    has_audio: bool = False
    video_codec: str = ""
    audio_codec: str = ""
    bit_rate: int = 0
    size: int = 0
    format_name: str = ""

    @property
    def summary(self) -> str:
        bits = []
        if self.width and self.height:
            bits.append(f"{self.width}×{self.height}")
        if self.duration:
            bits.append(human_duration(self.duration))
        if self.size:
            bits.append(human_size(self.size))
        return "  ·  ".join(bits)


def _fraction(value: str) -> float:
    try:
        if "/" in value:
            num, den = value.split("/", 1)
            return float(num) / float(den) if float(den) else 0.0
        return float(value)
    except (ValueError, TypeError, ZeroDivisionError):
        return 0.0


def probe_media(path: os.PathLike[str] | str, toolchain: Optional[Toolchain] = None) -> MediaInfo:
    source = Path(path)
    if not source.exists():
        raise BeoldError(f"找不到文件：{source}")
    tools = toolchain or Toolchain.discover()
    cmd = [
        str(tools.ffprobe), "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(source),
    ]
    try:
        proc = subprocess.run(
            cmd, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=creation_flags(), timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        raise BeoldError("读取媒体信息超时（60 秒）。") from exc
    if proc.returncode != 0:
        detail = _decode(proc.stderr).strip()
        raise BeoldError(f"无法读取媒体信息：{detail or 'ffprobe 失败'}")
    try:
        payload = json.loads(_decode(proc.stdout) or "{}")
    except json.JSONDecodeError as exc:
        raise BeoldError("ffprobe 返回了无法解析的数据。") from exc

    fmt = payload.get("format") or {}
    streams = payload.get("streams") or []
    info = MediaInfo(path=source)
    info.duration = float(fmt.get("duration") or 0.0)
    info.size = int(fmt.get("size") or source.stat().st_size)
    info.bit_rate = int(fmt.get("bit_rate") or 0)
    info.format_name = str(fmt.get("format_name") or "")

    for stream in streams:
        if stream.get("codec_type") == "video" and not info.has_video:
            info.has_video = True
            info.width = int(stream.get("width") or 0)
            info.height = int(stream.get("height") or 0)
            info.fps = _fraction(str(stream.get("avg_frame_rate") or "0/0"))
            if info.fps <= 0:
                info.fps = _fraction(str(stream.get("r_frame_rate") or "0/0"))
            info.video_codec = str(stream.get("codec_name") or "")
            if not info.duration:
                info.duration = float(stream.get("duration") or 0.0)
        elif stream.get("codec_type") == "audio" and not info.has_audio:
            info.has_audio = True
            info.audio_codec = str(stream.get("codec_name") or "")

    return info


# ---------------------------------------------------------------------------
# Process runner
# ---------------------------------------------------------------------------


class ProcessRunner:
    """Runs child processes with cancellation and progress extraction."""

    def __init__(
        self,
        log: LogCallback,
        cancel_event: threading.Event,
    ) -> None:
        self.log = log
        self.cancel_event = cancel_event
        self._procs: set[subprocess.Popen[str]] = set()
        self._pty_procs: set[object] = set()
        self._lock = threading.Lock()

    def _register(self, proc: subprocess.Popen[str]) -> None:
        with self._lock:
            self._procs.add(proc)

    def _unregister(self, proc: subprocess.Popen[str]) -> None:
        with self._lock:
            self._procs.discard(proc)

    def cancel_all(self) -> None:
        self.cancel_event.set()
        with self._lock:
            procs = list(self._procs)
            ptys = list(self._pty_procs)
        for proc in procs:
            self._terminate(proc)
        for proc in ptys:
            try:
                proc.terminate()
            except Exception:
                pass

    @staticmethod
    def _terminate(proc: subprocess.Popen[str]) -> None:
        if proc.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=creation_flags(),
                )
            else:
                proc.terminate()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def run(
        self,
        cmd: Iterable[object],
        *,
        stage: str,
        cwd: Optional[Path] = None,
        env: Optional[dict[str, str]] = None,
        on_line: Optional[Callable[[str], None]] = None,
        check: bool = True,
        tail_lines: int = 48,
    ) -> int:
        if self.cancel_event.is_set():
            raise Cancelled("任务已取消。")
        args = [str(part) for part in cmd]
        self.log(f"$ {format_cmd(args)}", "command")

        popen_env = os.environ.copy()
        if env:
            popen_env.update(env)
        # SoX 14.4.2 on Windows cannot write to an MSYS-style /tmp path.
        tmp_root = Path(tempfile.gettempdir())
        popen_env.setdefault("TMP", str(tmp_root))
        popen_env.setdefault("TEMP", str(tmp_root))

        proc = subprocess.Popen(
            args,
            cwd=str(cwd) if cwd else None,
            env=popen_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creation_flags(),
        )
        self._register(proc)
        tail: list[str] = []
        code = 0
        try:
            assert proc.stdout is not None
            try:
                for raw in proc.stdout:
                    if self.cancel_event.is_set():
                        self._terminate(proc)
                        break
                    line = raw.rstrip("\r\n")
                    if not line:
                        continue
                    tail.append(line)
                    if len(tail) > tail_lines:
                        del tail[0]
                    if on_line:
                        try:
                            on_line(line)
                        except Exception:
                            pass
            except OSError as exc:
                # Windows can invalidate a redirected pipe handle just as the
                # child exits. The output file is validated by the caller; do
                # not turn that benign close race into a job failure.
                self.log(f"{stage}: 读取进程输出时句柄已失效（{exc}），继续收尾。", "warn")
            try:
                code = proc.wait()
            except OSError as exc:
                self.log(f"{stage}: 进程句柄已失效（{exc}），按已完成处理。", "warn")
                code = 0
        finally:
            self._unregister(proc)
            if proc.stdout:
                try:
                    proc.stdout.close()
                except Exception:
                    pass

        if self.cancel_event.is_set():
            raise Cancelled("任务已取消。")
        if check and code != 0:
            detail = "\n".join(tail[-18:]).strip()
            raise ProcessFailure(stage, code, detail)
        return code

    def run_pty(
        self,
        cmd: Iterable[object],
        *,
        stage: str,
        cwd: Optional[Path] = None,
        env: Optional[dict[str, str]] = None,
        on_progress: Optional[Callable[[int], None]] = None,
        check: bool = True,
    ) -> int:
        """Run a child inside ConPTY so NTSC-RS emits its live progress bar.

        NTSC-RS suppresses the progress UI when stdout is not a terminal. A
        pseudo-console makes it think it is interactive, so we can parse the
        exact percentage instead of estimating.
        """
        if os.name != "nt":
            raise PtyUnavailable("ConPTY 仅在 Windows 可用。")
        try:
            from winpty import PtyProcess
        except Exception as exc:
            raise PtyUnavailable(f"pywinpty 不可用：{exc}") from exc
        if self.cancel_event.is_set():
            raise Cancelled("任务已取消。")

        args = [str(part) for part in cmd]
        self.log(f"$ {format_cmd(args)}", "command")
        popen_env = os.environ.copy()
        if env:
            popen_env.update(env)
        tmp_root = Path(tempfile.gettempdir())
        popen_env.setdefault("TMP", str(tmp_root))
        popen_env.setdefault("TEMP", str(tmp_root))

        try:
            proc = PtyProcess.spawn(
                args,
                cwd=str(cwd) if cwd else None,
                env=popen_env,
                dimensions=(24, 120),
            )
        except Exception as exc:
            raise PtyUnavailable(f"无法创建 ConPTY：{exc}") from exc

        with self._lock:
            self._pty_procs.add(proc)
        self.log("NTSC-RS 进度：ConPTY 已连接，实时读取百分比。", "info")
        tail: list[str] = []
        ansi = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
        normal_exit = False
        try:
            while True:
                if self.cancel_event.is_set():
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                    raise Cancelled("任务已取消。")
                try:
                    chunk = proc.read(256)
                except EOFError:
                    break
                except OSError as exc:
                    # A closed/invalid ConPTY handle must not be reported as a
                    # clean EOF. Let the caller re-run without the PTY bridge.
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                    raise PtyUnavailable(f"ConPTY 读取失败：{exc}") from exc
                if chunk:
                    tail.append(chunk)
                    if len(tail) > 16:
                        del tail[0]
                    if on_progress:
                        for match in re.finditer(r"(\d{1,3})%", chunk):
                            try:
                                on_progress(clamp(int(match.group(1)), 0, 100))
                            except Exception:
                                pass
                if self.cancel_event.is_set():
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                    raise Cancelled("任务已取消。")
                if not proc.isalive():
                    break
            normal_exit = True
        finally:
            with self._lock:
                self._pty_procs.discard(proc)
            if not normal_exit:
                # Cancelled/failed before EOF: close here instead of leaving
                # a ConPTY handle open for the lifetime of the process.
                try:
                    proc.close()
                except Exception:
                    pass

        # Read the exit status before ClosePseudoConsole/hProc. Accessing
        # wait()/exitstatus after close can raise Windows error 6
        # ("句柄无效" / invalid handle) on some pywinpty builds.
        code = getattr(proc, "exitstatus", None)
        if code is None:
            try:
                code = proc.wait()
            except Exception:
                code = 0
        try:
            proc.close()
        except Exception:
            pass
        code = int(code or 0)
        if check and code != 0:
            detail = ansi.sub("", "".join(tail)).strip()
            raise ProcessFailure(stage, code, detail[-4000:])
        return code


# ---------------------------------------------------------------------------
# Progress tracker
# ---------------------------------------------------------------------------


class Progress:
    def __init__(self, callback: ProgressCallback) -> None:
        self.callback = callback
        self.value = 0
        self._lock = threading.Lock()

    def set(self, value: Optional[int], stage: str) -> None:
        with self._lock:
            if value is not None:
                value = int(clamp(value, 0, 100))
                self.value = max(self.value, value)
            display_value = self.value
        try:
            self.callback(display_value, stage)
        except Exception:
            pass

    def indeterminate(self, stage: str) -> None:
        try:
            self.callback(None, stage)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# VHS settings
# ---------------------------------------------------------------------------


@dataclass
class VhsRecipe:
    q: float
    composite_noise: float
    tracking_snow: float
    tracking_wave: float
    chroma_noise: float
    luma_smear: float
    composite_sharpening: float
    chroma_vert_blend: bool
    head_shift: float
    head_height: int
    tape_speed: str
    wow_speed: float
    wow_depth: float
    flutter_speed: float
    flutter_depth: float
    jitter: float
    overdrive_gain: float
    overdrive_colour: float
    lowpass_freq: int
    noise_level: float
    snow_p: float
    snow_intensity: float
    snow_anisotropy: float

    def preset(self) -> dict:
        return {
            "version": 1,
            "composite_noise": True,
            "composite_noise_intensity": round(self.composite_noise, 6),
            "tracking_noise": True,
            "tracking_noise_snow_intensity": round(self.tracking_snow, 6),
            "tracking_noise_wave_intensity": round(self.tracking_wave, 5),
            "chroma_noise": True,
            "chroma_noise_intensity": round(self.chroma_noise, 6),
            "luma_smear": round(self.luma_smear, 6),
            "composite_preemphasis": round(self.composite_sharpening, 6),
            "head_switching": True,
            "head_switching_horizontal_shift": round(self.head_shift, 4),
            "head_switching_height": int(self.head_height),
            "vhs_settings": True,
            "vhs_tape_speed": {"SP": 1, "LP": 2, "EP": 3}[self.tape_speed],
            "vhs_chroma_vert_blend": bool(self.chroma_vert_blend),
            "snow_intensity": round(self.snow_intensity, 4),
            "snow_anisotropy": round(self.snow_anisotropy, 4),
        }

    def detail_rows(self) -> list[tuple[str, str]]:
        return [
            ("TAPE", self.tape_speed),
            ("NOISE", f"{self.composite_noise:.2f}/{self.chroma_noise:.2f}"),
            ("WARP", f"+{self.head_shift:.0f}px"),
            ("AUDIO", f"{self.wow_speed:.1f}/{self.flutter_speed:.0f}Hz"),
            ("SNOW p", f"{self.snow_p:.2f}/{self.snow_intensity:.1f}"),
        ]


def vhs_recipe(level: int) -> VhsRecipe:
    q = level_to_fraction(level)
    if q < 0.3:
        tape = "SP"
    elif q <= 0.7:
        tape = "LP"
    else:
        tape = "EP"

    # Conventional VHS/VHS-C transport behaviour: wow is a slow 0.7–3 Hz
    # wander, flutter is a faster 10–24 Hz tremor, and both stay below ~1.5%
    # so the audio remains a tape artifact rather than a broken pitch-shifter.
    jitter = 0.8 + 0.3 * random.random()
    transport = {"SP": 1.0, "LP": 1.2, "EP": 1.4}[tape]
    wow_speed = 0.7 + 2.3 * q
    wow_depth = (0.0015 + 0.0085 * q) * jitter * transport
    flutter_speed = 10.0 + 14.0 * q
    flutter_depth = (0.0003 + 0.0017 * q) * jitter * transport
    snow_p = q
    snow_intensity = 8.0 * snow_p
    snow_anisotropy = 0.15 + 0.20 * snow_p

    return VhsRecipe(
        q=q,
        composite_noise=0.01 + 0.2 * q,
        tracking_snow=0.005 + 0.12 * q,
        tracking_wave=2.0 + 25.0 * q,
        chroma_noise=0.02 + 0.35 * q,
        luma_smear=clamp(0.15 + 1.2 * q, 0.0, 1.0),
        composite_sharpening=1.0 - 1.5 * q,
        # NTSC-RS exposes this as a boolean. It is switched on in the upper half.
        chroma_vert_blend=q >= 0.5,
        head_shift=20.0 + 80.0 * q,
        head_height=max(0, round(4 + 20 * q)) if q > 0.3 else 0,
        tape_speed=tape,
        wow_speed=wow_speed,
        wow_depth=wow_depth,
        flutter_speed=flutter_speed,
        flutter_depth=flutter_depth,
        jitter=jitter,
        overdrive_gain=2.0 + 10.0 * q,
        overdrive_colour=20.0 + 40.0 * q,
        lowpass_freq=round(12000.0 - 8000.0 * q),
        noise_level=0.001 + 0.01 * q,
        snow_p=snow_p,
        snow_intensity=snow_intensity,
        snow_anisotropy=snow_anisotropy,
    )


# ---------------------------------------------------------------------------
# Squeeze settings
# ---------------------------------------------------------------------------


@dataclass
class SqueezeRecipe:
    t: float
    height: int
    total_kbps: float
    video_kbps: float
    audio_kbps: float
    fps: float
    scale_filter: str = "lanczos"

    @property
    def resolution_label(self) -> str:
        return f"{self.height}p"

    def detail_rows(self) -> list[tuple[str, str]]:
        return [
            ("RES", self.resolution_label),
            ("VIDEO", f"{self.video_kbps:.1f}k"),
            ("AUDIO", f"{self.audio_kbps:.1f}k"),
            ("FPS", f"{self.fps:.1f}"),
            ("TOTAL", f"{self.total_kbps:.0f}k"),
        ]


def squeeze_recipe(level: int) -> SqueezeRecipe:
    # Level 100 = maximum crush -> t=0 (144p, 4 kbps, 10 fps).
    t = level_to_formula_fraction(level)
    heights = (144, 240, 288, 360, 480)
    height = heights[min(4, int(t * 5))]
    total = 4.0 + 380.0 * t
    return SqueezeRecipe(
        t=t,
        height=height,
        total_kbps=total,
        video_kbps=total * 0.75,
        audio_kbps=total * 0.25,
        fps=10.0 + 15.0 * t,
    )


# ---------------------------------------------------------------------------
# Patina settings
# ---------------------------------------------------------------------------


@dataclass
class PatinaRecipe:
    s: float
    shrink_pct: float
    upscale_pct: float
    jpeg_quality: int
    passes: int

    def detail_rows(self) -> list[tuple[str, str]]:
        return [
            ("SHRINK", f"{self.shrink_pct:.0f}%"),
            ("JPEG Q", str(self.jpeg_quality)),
            ("PASSES", f"{self.passes}×"),
        ]


def patina_recipe(level: int) -> PatinaRecipe:
    # Level 100 = maximum patina -> s=0 (25% shrink, 16 passes).
    # The per-pass JPEG quality is the high end of the specified range (Q30),
    # but 16 rounds at a quarter resolution accumulate far more damage.
    s = level_to_formula_fraction(level)
    shrink = 25.0 + 50.0 * s
    return PatinaRecipe(
        s=s,
        shrink_pct=shrink,
        upscale_pct=10000.0 / shrink,
        jpeg_quality=max(5, min(30, round(30.0 - 25.0 * s))),
        passes=max(4, min(16, round(16.0 - 12.0 * s))),
    )


# ---------------------------------------------------------------------------
# Job result
# ---------------------------------------------------------------------------


@dataclass
class JobResult:
    output: Path
    kind: str
    elapsed: float
    details: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Processor
# ---------------------------------------------------------------------------


class Processor:
    def __init__(
        self,
        log: LogCallback,
        progress: ProgressCallback,
        cancel_event: Optional[threading.Event] = None,
        toolchain: Optional[Toolchain] = None,
        use_gpu: bool = False,
        gpu_index: Optional[int] = None,
        gpu_position: Optional[int] = None,
    ) -> None:
        self.toolchain = toolchain or Toolchain.discover()
        self.cancel_event = cancel_event or threading.Event()
        self.log = log
        self.progress = Progress(progress)
        self.runner = ProcessRunner(log, self.cancel_event)
        self._cpu = host_cpu_count()
        self._cpu_groups = cpu_group_sizes()
        self.use_gpu = bool(use_gpu)
        self.gpu_devices = detect_gpu_devices() if self.use_gpu else ()
        self.gpu: Optional[GpuDevice] = None
        if self.gpu_devices:
            if gpu_position is not None and 0 <= gpu_position < len(self.gpu_devices):
                self.gpu = self.gpu_devices[gpu_position]
            if self.gpu is None and gpu_index is not None:
                for device in self.gpu_devices:
                    if device.index == gpu_index:
                        self.gpu = device
                        break
            if self.gpu is None:
                self.gpu = self.gpu_devices[0]
        if self.use_gpu and self.gpu is None:
            self.log("请求 GPU 加速，但没有检测到可用的硬件 H.264 编码器；回退 CPU。", "warn")

    # -- public API ---------------------------------------------------------

    def cancel(self) -> None:
        self.runner.cancel_all()

    def run(self, source: os.PathLike[str] | str, output_dir: os.PathLike[str] | str, mode: str, level: int) -> JobResult:
        started = time.monotonic()
        source_path = Path(source).resolve()
        out_dir = Path(output_dir).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        if not source_path.exists():
            raise BeoldError(f"找不到输入文件：{source_path}")
        animated = is_animated_image(source_path)
        if mode == "vhs":
            output = (self._run_vhs_animation(source_path, out_dir, level)
                      if animated else self._run_vhs(source_path, out_dir, level))
        elif mode == "squeeze":
            output = self._run_squeeze(source_path, out_dir, level)
        elif mode == "patina":
            output = (self._run_patina_animation(source_path, out_dir, level)
                      if animated else self._run_patina(source_path, out_dir, level))
        else:
            raise BeoldError(f"未知模式：{mode}")
        return JobResult(
            output=output,
            kind=mode,
            elapsed=time.monotonic() - started,
            details={"level": str(level)},
        )

    # -- Animated image helpers --------------------------------------------

    def _extract_animation(self, source: Path, job_dir: Path) -> tuple[list[Path], list[int]]:
        frames: list[Path] = []
        delays: list[int] = []
        with Image.open(source) as image:
            count = int(getattr(image, "n_frames", 1) or 1)
            for index in range(count):
                image.seek(index)
                duration = int(image.info.get("duration", 100) or 100)
                delays.append(max(10, duration))
                frame = image.convert("RGBA")
                path = job_dir / f"frame_{index:05d}.png"
                frame.save(path)
                frames.append(path)
        return frames, delays

    def _run_vhs_animation(self, source: Path, out_dir: Path, level: int) -> Path:
        info = probe_media(source, self.toolchain)
        fps = info.fps or 10.0
        job_dir = Path(tempfile.mkdtemp(prefix=".beold_vhs_anim_", dir=out_dir))
        try:
            intermediate = job_dir / "animation.mp4"
            self.runner.run(
                [str(self.toolchain.ffmpeg), "-y", "-loglevel", "error",
                 "-i", str(source), "-an",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", str(intermediate)],
                stage="动画转视频",
            )
            video = self._run_vhs(intermediate, out_dir, level)
            suffix = source.suffix.lower()
            animated_suffix = ".apng" if suffix == ".apng" else (".gif" if suffix == ".gif" else ".png")
            final = unique_path(out_dir / f"{source.stem}_VHS_{level:02d}{animated_suffix}")
            if animated_suffix == ".gif":
                cmd = [
                    str(self.toolchain.ffmpeg), "-y", "-loglevel", "error",
                    "-i", str(video), "-vf",
                    f"fps={fps:.6f},split[a][b];[a]palettegen[p];[b][p]paletteuse",
                    "-loop", "0", str(final),
                ]
            else:
                cmd = [
                    str(self.toolchain.ffmpeg), "-y", "-loglevel", "error",
                    "-i", str(video), "-plays", "0", "-f", "apng",
                    "-pix_fmt", "rgba", str(final),
                ]
            self.runner.run(cmd, stage="生成动画")
            try:
                video.unlink(missing_ok=True)
            except OSError:
                pass
            if not final.exists() or final.stat().st_size == 0:
                raise BeoldError("没有生成动画文件。")
            self.progress.set(100, "ANIMATION AGED")
            self.log(f"完成：{final.name} · {human_size(final.stat().st_size)}", "success")
            return final
        finally:
            self.runner.cancel_event.clear()
            shutil.rmtree(job_dir, ignore_errors=True)

    def _run_patina_animation(self, source: Path, out_dir: Path, level: int) -> Path:
        job_dir = Path(tempfile.mkdtemp(prefix=".beold_patina_anim_", dir=out_dir))
        try:
            frames, delays = self._extract_animation(source, job_dir)
            aged: list[Path] = []
            for index, frame in enumerate(frames):
                if self.cancel_event.is_set():
                    raise Cancelled("任务已取消。")
                aged.append(self._run_patina(frame, job_dir, level))
                self.progress.set(
                    2 + int((index + 1) / max(1, len(frames)) * 92),
                    f"PATINA FRAME {index + 1}/{len(frames)}",
                )
            suffix = source.suffix.lower()
            animated_suffix = ".apng" if suffix == ".apng" else (".gif" if suffix == ".gif" else ".png")
            final = unique_path(out_dir / f"{source.stem}_PATINA_{level:02d}{animated_suffix}")
            if animated_suffix == ".gif":
                delay_arg = ",".join(str(max(1, round(d / 10.0))) for d in delays)
                cmd = [
                    str(self.toolchain.magick), "-delay", delay_arg, "-loop", "0",
                    *[str(p) for p in aged], "-layers", "Optimize", str(final),
                ]
            else:
                seq = job_dir / "seq"
                seq.mkdir(exist_ok=True)
                for index, path in enumerate(aged):
                    shutil.copyfile(path, seq / f"{index:05d}.jpg")
                fps = 1000.0 / max(10.0, sum(delays) / max(1, len(delays)))
                cmd = [
                    str(self.toolchain.ffmpeg), "-y", "-loglevel", "error",
                    "-framerate", f"{fps:.6f}",
                    "-i", str(seq / "%05d.jpg"),
                    "-plays", "0", "-f", "apng", "-pix_fmt", "rgba",
                    str(final),
                ]
            self.runner.run(cmd, stage="生成动画")
            if not final.exists() or final.stat().st_size == 0:
                raise BeoldError("没有生成动画文件。")
            self.progress.set(100, "ANIMATION AGED")
            self.log(f"完成：{final.name} · {human_size(final.stat().st_size)}", "success")
            return final
        finally:
            self.runner.cancel_event.clear()
            shutil.rmtree(job_dir, ignore_errors=True)

    # -- VHS ----------------------------------------------------------------

    def _run_vhs(self, source: Path, out_dir: Path, level: int) -> Path:
        recipe = vhs_recipe(level)
        self.log(
            f"VHS profile q={recipe.q:.3f} · {recipe.tape_speed} · "
            f"head={recipe.head_height}px · snow p={recipe.snow_p:.2f}/"
            f"{recipe.snow_intensity:.1f}",
            "stage",
        )
        info = probe_media(source, self.toolchain)
        if not info.has_video:
            raise BeoldError("这个文件里没有可处理的视频流。")
        self.progress.set(3, "ANALYZING SOURCE")

        # SoX is effectively single-threaded. Give audio ~8% as a ceiling and
        # leave the rest to the NTSC-RS video renderer.
        video_threads, audio_threads = vhs_thread_split(self._cpu)
        scale_height = 480 if recipe.q > 0.5 else 576
        groups = "+".join(str(size) for size in self._cpu_groups)
        self.log(
            f"CPU topology: {self._cpu} logical / {len(self._cpu_groups)} group(s) [{groups}] "
            f"· VHS {video_threads} / audio {audio_threads}",
            "info",
        )
        self.log(
            f"VHS pre-scale: {scale_height}p (p={recipe.q:.3f} -> "
            f"{'480p' if recipe.q > 0.5 else '576p'})",
            "info",
        )
        if self.use_gpu:
            self.log(
                "VHS: 检测 OpenCL；可用时走全分辨率 YIQ GPU 近似，失败自动回退 NTSC-RS CPU。",
                "info",
            )

        # Ensure SoX and all temporary media use a real Windows temp path.
        job_dir = Path(tempfile.mkdtemp(prefix=".beold_vhs_", dir=out_dir))
        try:
            video_out = job_dir / "video_vhs.mp4"
            audio_mod = job_dir / "audio_mod.wav"
            audio_out = job_dir / "audio_vhs.wav"
            preset_path = job_dir / "vhs.json"
            preset_path.write_text(
                json.dumps(recipe.preset(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            self.progress.set(6, "VHS READY")
            self.log("直接读取源视频进入 NTSC-RS（跳过预复制 MKV）。", "stage")

            self.progress.set(12, "AUDIO READY")
            if info.has_audio:
                # Decode directly from the source in the audio worker. This
                # avoids one intermediate 48 kHz WAV pass and its disk I/O.
                self.log("音频将在并行阶段直接从源文件处理。", "stage")
            else:
                self.log("源文件没有音频轨，跳过磁带音频处理。", "warn")

            self.progress.set(5, "VHS ENGINE · STARTING")
            errors: list[BaseException] = []

            def work_video() -> None:
                try:
                    if self.use_gpu:
                        try:
                            from .opencl_vhs import (
                                OpenCLVhsCancelled, available as opencl_available,
                                render_multi as opencl_render,
                            )
                            if not opencl_available():
                                raise RuntimeError("没有可用的 OpenCL GPU 设备")
                            self.log(
                                "VHS GPU: OpenCL 全分辨率 YIQ（近似），"
                                "失败自动回退 CPU。",
                                "stage",
                            )

                            def gpu_progress(pct: int) -> None:
                                self.progress.set(
                                    5 + int(clamp(pct, 0, 100) * 0.80),
                                    f"VHS GPU · {pct}%",
                                )

                            device_name = opencl_render(
                                source, video_out, level, scale_height,
                                int(info.width or 0), int(info.height or 0),
                                float(info.fps or 30.0), float(info.duration or 0.0),
                                gpu_progress, self.cancel_event,
                                device_count=int(os.environ.get("TWILIGHT_OPENCL_DEVICES", "0") or 0),
                            )
                            if "+" in device_name:
                                self.log(f"VHS GPU 多卡：{device_name}", "info")
                            self.log(f"VHS GPU 完成：{device_name}", "info")
                            return
                        except OpenCLVhsCancelled as exc:
                            raise Cancelled("任务已取消。") from exc
                        except Exception as exc:  # noqa: BLE001
                            if self.cancel_event.is_set():
                                raise Cancelled("任务已取消。") from exc
                            self.log(f"OpenCL VHS 不可用，回退 CPU：{exc}", "warn")
                            try:
                                video_out.unlink(missing_ok=True)
                            except OSError:
                                pass
                    self._ntsc_pass(
                        source, video_out, preset_path, video_threads, job_dir,
                        scale_height,
                    )
                except BaseException as exc:  # noqa: BLE001 - re-raised below
                    errors.append(exc)

            def work_audio() -> None:
                try:
                    if info.has_audio:
                        self._vhs_audio(
                            source, audio_mod, audio_out, recipe, job_dir,
                            info.duration, audio_threads,
                        )
                except BaseException as exc:  # noqa: BLE001 - re-raised below
                    errors.append(exc)

            if self._cpu <= 1:
                self.log("单线程主机：串行执行 VHS 与音频。", "warn")
                work_video()
                if not errors:
                    work_audio()
            else:
                self.log(
                    f"并行执行：VHS {video_threads} 线程 · 音频 {audio_threads} 线程",
                    "info",
                )
                workers = [
                    threading.Thread(target=work_video, name="beold-video", daemon=True),
                    threading.Thread(target=work_audio, name="beold-audio", daemon=True),
                ]
                for worker in workers:
                    worker.start()
                for worker in workers:
                    worker.join()

            if self.cancel_event.is_set():
                raise Cancelled("任务已取消。")
            if errors:
                first = errors[0]
                if isinstance(first, BeoldError):
                    raise first
                raise BeoldError(str(first)) from first

            self.progress.set(90, "MULTIPLEX")
            output = unique_path(out_dir / f"{source.stem}_VHS_{level:02d}.mp4")
            self.log("合成音视频 …", "stage")
            if info.has_audio:
                mux_cmd = [
                    self.toolchain.ffmpeg, "-y", "-loglevel", "error",
                    "-i", video_out, "-i", audio_out,
                    "-map", "0:v:0", "-map", "1:a:0",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                    "-ar", "48000", "-shortest", "-movflags", "+faststart",
                    output,
                ]
            else:
                mux_cmd = [
                    self.toolchain.ffmpeg, "-y", "-loglevel", "error",
                    "-i", video_out, "-map", "0:v:0", "-c:v", "copy",
                    "-movflags", "+faststart", output,
                ]
            self.runner.run(mux_cmd, stage="音视频合成")
            self.progress.set(100, "VHS LOCKED")
            self.log(f"完成：{output.name}", "success")
            return output
        finally:
            self.runner.cancel_event.clear()
            shutil.rmtree(job_dir, ignore_errors=True)

    def _transcode_video(self, source: Path, destination: Path, threads: Optional[int] = None) -> None:
        thread_count = max(1, int(threads or self._cpu))
        self.runner.run(
            [
                self.toolchain.ffmpeg, "-y", "-loglevel", "error",
                "-threads", str(thread_count),
                "-i", source, "-map", "0:v:0", "-an",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "10",
                "-threads", str(thread_count),
                "-pix_fmt", "yuv420p", "-f", "matroska", destination,
            ],
            stage="视频转码",
        )

    def _ntsc_pass(
        self,
        source: Path,
        destination: Path,
        preset_path: Path,
        threads: int,
        job_dir: Path,
        scale_height: int,
    ) -> None:
        env = os.environ.copy()
        bin_dir = self.toolchain.ntsc_home / "bin"
        plugin_dir = self.toolchain.ntsc_home / "lib" / "gstreamer-1.0"
        env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
        env["GST_PLUGIN_PATH_1_0"] = str(plugin_dir)
        env["RAYON_NUM_THREADS"] = str(max(1, threads))
        env["TMP"] = str(job_dir)
        env["TEMP"] = str(job_dir)
        # 0 = slowest/best, 8 = fastest. 480p tolerates a faster encoder.
        encoding_speed = 5 if scale_height >= 576 else 6
        self.log(
            f"NTSC-RS 渲染中（{threads} 线程，{scale_height}p 预缩放，"
            f"encoder speed {encoding_speed}）…",
            "info",
        )

        def attempt(input_file: Path, tries: int) -> None:
            last_error: Optional[BaseException] = None
            for attempt_no in range(1, tries + 1):
                if self.cancel_event.is_set():
                    raise Cancelled("任务已取消。")
                try:
                    destination.unlink(missing_ok=True)
                except OSError:
                    pass
                render_cmd = [
                    self.toolchain.ntsc,
                    "-i", input_file,
                    "-o", destination,
                    "-p", preset_path,
                    "-y",
                    "--chroma-subsampling",
                    "--quality", "28",
                    "--encoding-speed", str(encoding_speed),
                    "--scale", str(scale_height),
                    "--scale-filter", "bicubic",
                ]
                def report_progress(percent: int) -> None:
                    self.progress.set(
                        5 + int(clamp(percent, 0, 100) * 0.80),
                        f"VHS RENDER · {percent}%",
                    )

                try:
                    try:
                        self.runner.run_pty(
                            render_cmd,
                            stage="NTSC-RS 渲染",
                            cwd=self.toolchain.ntsc_home,
                            env=env,
                            on_progress=report_progress,
                        )
                    except PtyUnavailable as exc:
                        self.log(
                            f"ConPTY 不可用，回退普通子进程：{exc}",
                            "warn",
                        )
                        self.progress.set(5, "VHS ENGINE · CPU")
                        self.runner.run(
                            render_cmd,
                            stage="NTSC-RS 渲染",
                            cwd=self.toolchain.ntsc_home,
                            env=env,
                        )
                    if destination.exists() and destination.stat().st_size > 0:
                        return
                    raise BeoldError("NTSC-RS 没有生成视频文件。")
                except ProcessFailure as exc:
                    last_error = exc
                    # NTSC-RS 0.9.7 occasionally trips a GStreamer/rayon race
                    # and exits with an access-violation status. A fresh
                    # process almost always succeeds, so retry those crashes.
                    if exc.code not in (139, -1073741819, 3221225477):
                        raise
                    if attempt_no < tries:
                        self.log(
                            f"VHS 引擎异常退出（{exc.code}），第 {attempt_no + 1} 次重试 …",
                            "warn",
                        )
                        time.sleep(0.7)
                except BeoldError:
                    raise
            if last_error:
                raise last_error
            raise BeoldError("NTSC-RS 没有生成视频文件。")

        try:
            attempt(source, 3)
        except ProcessFailure as first:
            # Some exotic codecs are copied successfully but cannot be decoded
            # by GStreamer. Try one high-quality transcode before giving up.
            if self.cancel_event.is_set():
                raise
            self.log(f"VHS 引擎无法稳定渲染（{first.code}），转码后重试。", "warn")
            retry_in = job_dir / "video_retry.mkv"
            self._transcode_video(source, retry_in, threads)
            attempt(retry_in, 2)
        except BeoldError as first:
            if self.cancel_event.is_set():
                raise
            self.log(f"VHS 引擎无法读取该视频流：{first}", "warn")
            retry_in = job_dir / "video_retry.mkv"
            self._transcode_video(source, retry_in, threads)
            attempt(retry_in, 2)

        if not destination.exists() or destination.stat().st_size == 0:
            raise BeoldError("NTSC-RS 没有生成视频文件。")

    def _vhs_audio(
        self,
        source: Path,
        modulated: Path,
        destination: Path,
        recipe: VhsRecipe,
        job_dir: Path,
        duration: float,
        threads: int,
    ) -> None:
        # SoX 14.4.2 has no true pitch-vibrato effect. Use FFmpeg's vibrato
        # stage for the wow/flutter formulas, then run SoX overdrive + lowpass.
        wow_depth = clamp(recipe.wow_depth, 0.0, 0.95)
        flutter_depth = clamp(recipe.flutter_depth, 0.0, 0.95)
        af = (
            f"vibrato=f={recipe.wow_speed:.4f}:d={wow_depth:.6f},"
            f"vibrato=f={recipe.flutter_speed:.4f}:d={flutter_depth:.6f}"
        )
        noise = recipe.noise_level
        duration_args: list[str] = []
        if duration > 0:
            duration_args = ["-t", f"{duration:.6f}"]
        pink = (
            f"anoisesrc=color=pink:amplitude={noise:.6f}:sample_rate=48000,"
            "aformat=channel_layouts=stereo"
        )
        brown = (
            f"anoisesrc=color=brown:amplitude={noise:.6f}:sample_rate=48000,"
            "aformat=channel_layouts=stereo"
        )
        graph = (
            "[0:a]aformat=sample_rates=48000:channel_layouts=stereo,"
            f"{af}[wet];"
            "[wet][1:a][2:a]amix=inputs=3:duration=first:normalize=0,"
            "volume=0.95[out]"
        )
        self.log(
            f"磁带抖动 jitter={recipe.jitter:.3f} · wow {recipe.wow_speed:.2f}Hz/{wow_depth*100:.2f}% · "
            f"flutter {recipe.flutter_speed:.1f}Hz/{flutter_depth*100:.2f}% · "
            f"pink+brown noise {noise*100:.2f}%",
            "info",
        )
        self.runner.run(
            [
                self.toolchain.ffmpeg, "-y", "-loglevel", "error",
                "-threads", str(max(1, threads)),
                "-filter_threads", str(max(1, threads)),
                "-i", source, "-vn",
                "-f", "lavfi", *duration_args, "-i", pink,
                "-f", "lavfi", *duration_args, "-i", brown,
                "-filter_complex", graph, "-map", "[out]",
                "-ar", "48000", "-ac", "2",
                "-c:a", "pcm_s16le", "-shortest", modulated,
            ],
            stage="抖动与底噪混合",
            env={"TMP": str(job_dir), "TEMP": str(job_dir)},
        )
        self.runner.run(
            [
                self.toolchain.sox, modulated, destination,
                "overdrive", f"{recipe.overdrive_gain / 100.0:.4f}", f"{recipe.overdrive_colour / 100.0:.4f}",
                "lowpass", str(recipe.lowpass_freq),
                "gain", "-n", "-1",
            ],
            stage="SoX 磁带音染",
            env={"TMP": str(job_dir), "TEMP": str(job_dir)},
        )
        if not destination.exists():
            raise BeoldError("SoX 没有生成音频文件。")

    # -- Squeeze ------------------------------------------------------------

    def _run_squeeze(self, source: Path, out_dir: Path, level: int) -> Path:
        recipe = squeeze_recipe(level)
        self.log(
            f"SQUEEZE profile crush={1.0 - recipe.t:.3f} (t={recipe.t:.3f}) · "
            f"{recipe.height}p · {recipe.video_kbps:.1f}k video + "
            f"{recipe.audio_kbps:.1f}k audio · {recipe.fps:.2f}fps",
            "stage",
        )
        info = probe_media(source, self.toolchain)
        if not info.has_video:
            raise BeoldError("这个文件里没有可处理的视频流。")
        self.progress.set(3, "ANALYZING SOURCE")
        groups = "+".join(str(size) for size in self._cpu_groups)
        self.log(
            f"CPU topology: {self._cpu} logical / {len(self._cpu_groups)} group(s) [{groups}] "
            f"· encoder budget {self._cpu}",
            "info",
        )

        job_dir = Path(tempfile.mkdtemp(prefix=".beold_squeeze_", dir=out_dir))
        try:
            output = unique_path(out_dir / f"{source.stem}_SQUEEZE_{level:02d}.mkv")
            fps = f"{recipe.fps:.3f}".rstrip("0").rstrip(".")
            duration = max(0.1, info.duration)
            vf = f"scale=-2:{recipe.height}:flags=lanczos,fps={fps}"
            video_kbps = max(1, int(round(recipe.video_kbps)))
            audio_kbps = max(1, int(round(recipe.audio_kbps)))
            total_kbps = recipe.total_kbps
            # x264 cannot use 128 frame threads at 144p. Scale the encoder
            # budget with the target height instead of blindly oversubscribing.
            encode_threads = max(2, min(self._cpu, max(4, recipe.height // 4)))
            filter_threads = max(2, min(self._cpu, max(4, recipe.height // 16)))
            self.log(
                f"adaptive threads: filter={filter_threads} · x264={encode_threads} "
                f"(target {recipe.height}p)",
                "info",
            )

            audio_args = [
                "-c:a", "libopus", "-b:a", f"{audio_kbps}k",
                "-vbr", "off", "-ac", "1" if audio_kbps < 48 else "2",
                "-ar", "48000", "-frame_duration", "60" if audio_kbps < 24 else "20",
                "-application", "voip" if audio_kbps < 24 else "audio",
            ]
            if not info.has_audio:
                audio_args = ["-an"]

            self.progress.set(8, "ENCODING VIDEO")
            last_progress = 0.0

            def parse_line(line: str) -> None:
                nonlocal last_progress
                if line.startswith("out_time_us="):
                    try:
                        us = int(line.split("=", 1)[1])
                    except ValueError:
                        return
                    pct = 8 + (us / 1_000_000.0) / duration * 86.0
                    pct = clamp(pct, 8, 94)
                    if pct - last_progress >= 0.7:
                        last_progress = pct
                        self.progress.set(int(pct), "SQUEEZING")
                elif line.startswith("progress=end"):
                    self.progress.set(94, "FINALIZING")

            prefix = [
                self.toolchain.ffmpeg, "-y",
                "-loglevel", "error", "-nostats", "-progress", "pipe:1",
                "-filter_threads", str(filter_threads),
                "-i", source,
                "-vf", vf,
            ]
            tail = [
                "-b:v", f"{video_kbps}k",
                "-maxrate", f"{video_kbps}k",
                "-bufsize", f"{max(2 * video_kbps, 8)}k",
                "-pix_fmt", "yuv420p",
                "-g", str(max(2, round(recipe.fps * 2))),
                *audio_args,
                "-sn", "-dn",
                "-metadata", f"comment=Beold SQUEEZE total-target={total_kbps:.1f}kbps",
                output,
            ]
            cpu_cmd = prefix + [
                "-c:v", "libx264", "-preset", "medium",
                "-threads", str(encode_threads),
            ] + tail

            gpu_cmd = None
            if self.gpu is not None:
                if total_kbps >= self.gpu.min_total_kbps:
                    if self.gpu.backend == "nvenc":
                        codec_args = [
                            "-c:v", "h264_nvenc", "-preset", "p5",
                            "-tune", "hq", "-rc", "vbr",
                            "-gpu", str(self.gpu.index),
                        ]
                    elif self.gpu.backend == "amf":
                        codec_args = [
                            "-c:v", "h264_amf", "-quality", "balanced",
                            "-rc", "vbr_peak",
                        ]
                    else:
                        codec_args = ["-c:v", "h264_qsv", "-preset", "medium"]
                    gpu_cmd = prefix + codec_args + tail
                    self.log(
                        f"GPU encode: {self.gpu.label} · target {total_kbps:.0f}kbps",
                        "info",
                    )
                else:
                    self.log(
                        f"GPU {self.gpu.label} 的最低码率约 {self.gpu.min_total_kbps}kbps；"
                        f"当前目标 {total_kbps:.0f}kbps 过低，使用 CPU 以保持极压缩效果。",
                        "warn",
                    )

            if gpu_cmd is not None:
                try:
                    self.runner.run(gpu_cmd, stage="GPU 编码", on_line=parse_line)
                    if not output.exists() or output.stat().st_size == 0:
                        raise BeoldError("GPU 编码没有生成输出文件。")
                except BeoldError as exc:
                    if self.cancel_event.is_set():
                        raise
                    self.log(f"GPU 编码失败，回退 CPU：{exc}", "warn")
                    try:
                        output.unlink(missing_ok=True)
                    except OSError:
                        pass
                    self.runner.run(cpu_cmd, stage="极限压缩", on_line=parse_line)
            else:
                self.runner.run(cpu_cmd, stage="极限压缩", on_line=parse_line)
            if not output.exists() or output.stat().st_size == 0:
                raise BeoldError("压缩没有生成输出文件。")
            self.progress.set(100, "SIGNAL CRUSHED")
            self.log(f"完成：{output.name} · {human_size(output.stat().st_size)}", "success")
            return output
        finally:
            self.runner.cancel_event.clear()
            shutil.rmtree(job_dir, ignore_errors=True)

    # -- Patina -------------------------------------------------------------

    def _run_patina(self, source: Path, out_dir: Path, level: int) -> Path:
        recipe = patina_recipe(level)
        self.log(
            f"PATINA profile age={1.0 - recipe.s:.3f} (s={recipe.s:.3f}) · "
            f"shrink={recipe.shrink_pct:.1f}% · Q={recipe.jpeg_quality} · "
            f"{recipe.passes} passes · single-image sequential (1 thread)",
            "stage",
        )
        magick_env = None
        if self.use_gpu:
            magick_env = dict(os.environ)
            magick_env["MAGICK_OCL_DEVICE"] = "true"
            self.log("ImageMagick OpenCL 已请求；若驱动不可用会自动回退 CPU。", "info")
        try:
            size_out = subprocess.run(
                [
                    str(self.toolchain.magick), "identify",
                    "-format", "%w %h %[orientation]", str(source),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=magick_env,
                creationflags=creation_flags(), timeout=60,
            )
        except Exception as exc:
            raise BeoldError(f"无法启动 ImageMagick：{exc}") from exc
        if size_out.returncode != 0:
            raise BeoldError("ImageMagick 无法读取这张图片。\n" + _decode(size_out.stderr).strip())
        try:
            parts = _decode(size_out.stdout).split()
            width, height = int(parts[0]), int(parts[1])
            orientation = parts[2] if len(parts) > 2 else "TopLeft"
            if orientation in {"LeftTop", "RightTop", "RightBottom", "LeftBottom"}:
                width, height = height, width
        except (ValueError, IndexError):
            raise BeoldError("无法识别图片尺寸。")
        if width <= 0 or height <= 0:
            raise BeoldError("图片尺寸无效。")

        self.progress.set(2, "ANALYZING IMAGE")
        job_dir = Path(tempfile.mkdtemp(prefix=".beold_patina_", dir=out_dir))
        try:
            final = unique_path(out_dir / f"{source.stem}_PATINA_{level:02d}.jpg")
            current = source
            passes = recipe.passes
            for index in range(1, passes + 1):
                if self.cancel_event.is_set():
                    raise Cancelled("任务已取消。")
                target = final if index == passes else job_dir / f"pass_{index:02d}.jpg"
                self.log(
                    f"PASS {index:02d}/{passes:02d} · {recipe.shrink_pct:.0f}% → "
                    f"{recipe.upscale_pct:.0f}% · JPEG Q{recipe.jpeg_quality}",
                    "info",
                )
                # Shrink by L%, then restore the original pixel dimensions.
                # 100/L percent is mathematically the inverse; forcing the exact
                # source size avoids one-pixel drift over 16 generations.
                cmd = [
                    str(self.toolchain.magick), str(current),
                    "-auto-orient",
                    "-delete", "1--1",
                    "-background", "white", "-alpha", "remove", "-alpha", "off",
                    "-resize", f"{recipe.shrink_pct:.4f}%",
                    "-resize", f"{width}x{height}!",
                    "-strip",
                    "-sampling-factor", "4:2:0",
                    "-quality", str(recipe.jpeg_quality),
                    str(target),
                ]
                self.runner.run(
                    cmd,
                    stage=f"第 {index} 次包浆",
                    env={"MAGICK_OCL_DEVICE": "true"} if self.use_gpu else None,
                )
                current = target
                self.progress.set(4 + int(index / passes * 94), "PATINA")
            if not final.exists():
                raise BeoldError("ImageMagick 没有生成输出文件。")
            self.progress.set(100, "IMAGE AGED")
            self.log(
                f"完成：{final.name} · {recipe.passes} 次 · {human_size(final.stat().st_size)}",
                "success",
            )
            return final
        finally:
            self.runner.cancel_event.clear()
            shutil.rmtree(job_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Tool status (for the UI)
# ---------------------------------------------------------------------------


def tool_status() -> list[tuple[str, str, bool]]:
    try:
        tools = Toolchain.discover()
        return [
            ("FFMPEG", str(tools.ffmpeg), True),
            ("SOX", str(tools.sox), True),
            ("NTSC-RS", str(tools.ntsc), True),
            ("IMAGEMAGICK", str(tools.magick), True),
        ]
    except ToolMissingError:
        root = app_dir() / "tools"
        checks = [
            ("FFMPEG", root / "ffmpeg/bin/ffmpeg.exe"),
            ("SOX", root / "sox/sox.exe"),
            ("NTSC-RS", root / "ntsc-rs/bin/ntsc-rs-cli.exe"),
            ("IMAGEMAGICK", root / "imagemagick/magick.exe"),
        ]
        return [(name, str(path), path.exists()) for name, path in checks]
