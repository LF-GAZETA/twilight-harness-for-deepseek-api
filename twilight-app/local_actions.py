"""Local user-requested actions for the attachment build.

Supported requests (explicit slash commands or natural-language triggers):
  /derpi <tags>       search safe Derpibooru images and download via derpicdn.net
  /upscale            Real-ESRGAN upscale the attached image
  /audio              separate the audio track from the attached video
  /transcode <format> transcode the attached video (mp4/webm/mkv/mov/gif)

Downloaded files are written to the user's Downloads folder and are never
removed by the application's runtime cleanup.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import attachments
import derpi


class LocalActionError(Exception):
    pass


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
    candidates.append(paths_download_root() / name)
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


def _upscale(image: Path) -> list[Path]:
    tool = _real_esrgan_dir()
    output = _unique_output(_save_dir(), f"{image.stem}_realesrgan_x4", ".png")
    commands = [
        [
            str(tool / "realesrgan-ncnn-vulkan.exe"),
            "-i",
            str(image),
            "-o",
            str(output),
            "-n",
            "realesrgan-x4plus-anime",
            "-s",
            "4",
            "-f",
            "png",
        ],
        [
            str(tool / "realesrgan-ncnn-vulkan.exe"),
            "-i",
            str(image),
            "-o",
            str(output),
            "-n",
            "realesrgan-x4plus-anime",
            "-s",
            "4",
            "-f",
            "png",
            "-g",
            "-1",
        ],
    ]
    errors: list[str] = []
    for command in commands:
        try:
            result = subprocess.run(
                command,
                cwd=str(tool),
                capture_output=True,
                timeout=600,
                creationflags=_creation_flags(),
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))
            continue
        if result.returncode == 0 and output.is_file() and output.stat().st_size > 0:
            return [output]
        errors.append(result.stderr.decode("utf-8", errors="replace")[-500:])
    raise LocalActionError("Real-ESRGAN failed: " + (" | ".join(errors[-2:])))


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
    command = ["-hide_banner", "-loglevel", "error", "-y", "-i", str(video), *codec, str(output)]
    try:
        result = subprocess.run(
            [_ffmpeg(), *command],
            capture_output=True,
            timeout=900,
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

    # Natural-language convenience triggers.
    if any(word in lower for word in ("derpibooru", "derpi", "呆站")):
        if any(word in lower for word in ("找", "搜", "下载", "抓取", "来", "给我", "find", "search", "download")):
            return LocalRequest(kind="derpi", title="Derpibooru", query=derpi.extract_query(raw))
    if any(word in lower for word in ("超分", "超分辨率", "放大", "upscale", "esrgan")):
        image = _first_image(items)
        if image is not None:
            return LocalRequest(kind="upscale", title="Real-ESRGAN", attachment=image)
    if any(word in lower for word in ("提取音频", "分离音频", "音频分离", "extract audio", "demux audio")):
        video = _first_video(items)
        if video is not None:
            return LocalRequest(kind="audio", title="Audio", attachment=video)
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


def run(request: LocalRequest) -> list[Path]:
    if request.error == "image":
        raise LocalActionError("please attach an image first")
    if request.error == "video":
        raise LocalActionError("please attach a video first")
    if request.kind == "derpi":
        return derpi.search_download(request.query)
    if request.attachment is None:
        raise LocalActionError("missing attachment")
    if request.kind == "upscale":
        return _upscale(request.attachment.path)
    if request.kind == "audio":
        return _extract_audio(request.attachment.path)
    if request.kind == "transcode":
        return _transcode(request.attachment.path, request.target_format, request.params)
    raise LocalActionError("unknown local action")
