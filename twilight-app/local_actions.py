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
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import attachments


class LocalActionError(Exception):
    pass


@dataclass
class LocalRequest:
    kind: str
    title: str
    attachment: attachments.Attachment | None = None
    query: str = ""
    target_format: str = "mp4"
    error: str = ""
    images: list[str] = field(default_factory=list)


def _creation_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def downloads_dir() -> Path:
    """Resolve the user's Downloads known folder; never delete it."""
    candidate: Path | None = None
    if os.name == "nt":
        try:
            import winreg

            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
            )
            value, _ = winreg.QueryValueEx(
                key, "{374DE290-123F-4565-9164-39C4925E467B}"
            )
            candidate = Path(os.path.expandvars(value))
        except Exception:  # noqa: BLE001
            candidate = None
    if candidate is None:
        candidate = Path.home() / "Downloads"
    try:
        candidate.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise LocalActionError(f"cannot access Downloads folder: {exc}") from exc
    return candidate


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
    folder = downloads_dir() / "TwilightHarness"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _ffmpeg() -> str:
    executable = attachments.ffmpeg_executable()
    if not executable:
        raise LocalActionError("ffmpeg is not available")
    return executable


def _derpi_query(query: str) -> str:
    query = re.sub(r"\s+", " ", (query or "").strip())
    mapping = {
        "紫悦": "twilight sparkle",
        "暮光闪闪": "twilight sparkle",
        "暮光": "twilight sparkle",
        "云宝": "rainbow dash",
        "碧琪": "pinkie pie",
        "苹果嘉儿": "applejack",
        "苹果杰克": "applejack",
        "珍奇": "rarity",
        "柔柔": "fluttershy",
        "小蝶": "fluttershy",
    }
    for chinese, english in mapping.items():
        query = query.replace(chinese, english)
    return query or "twilight sparkle"


def _download_derpi(query: str, limit: int = 4) -> list[Path]:
    q = _derpi_query(query)
    query_string = q if "safe" in q.lower().split() else f"{q} AND safe"
    url = "https://derpibooru.org/api/v1/json/search/images?" + urllib.parse.urlencode(
        {
            "q": query_string,
            "per_page": max(1, min(limit, 8)),
            "sf": "created_at",
            "sd": "desc",
        }
    )
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "TwilightHarness/1.0 (fan project; local desktop client)"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001
        raise LocalActionError(f"Derpibooru search failed: {exc}") from exc
    images = payload.get("images") if isinstance(payload, dict) else None
    if not images:
        raise LocalActionError("Derpibooru returned no safe images")
    folder = _save_dir()
    saved: list[Path] = []
    for image in images:
        if len(saved) >= limit:
            break
        view_url = str(image.get("view_url") or image.get("image") or "")
        parsed = urllib.parse.urlparse(view_url)
        if parsed.scheme != "https" or not (
            parsed.netloc == "derpicdn.net" or parsed.netloc.endswith(".derpicdn.net")
        ):
            continue
        suffix = Path(parsed.path).suffix.lower()
        if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
            suffix = ".png"
        image_id = str(image.get("id") or int(time.time() * 1000))
        target = _unique_output(folder, f"derpibooru_{image_id}", suffix)
        try:
            req = urllib.request.Request(
                view_url,
                headers={"User-Agent": "TwilightHarness/1.0"},
            )
            with urllib.request.urlopen(req, timeout=60) as response:
                data = response.read(30 * 1024 * 1024 + 1)
            if len(data) > 30 * 1024 * 1024:
                continue
            target.write_bytes(data)
        except Exception:  # noqa: BLE001
            continue
        if target.is_file() and target.stat().st_size > 0:
            saved.append(target)
    if not saved:
        raise LocalActionError("no downloadable derpicdn.net images were returned")
    return saved


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


def _transcode(video: Path, target_format: str) -> list[Path]:
    fmt = (target_format or "mp4").lower().lstrip(".")
    if fmt not in {"mp4", "webm", "mkv", "mov", "gif"}:
        raise LocalActionError("unsupported transcode format: " + fmt)
    output = _unique_output(_save_dir(), f"{video.stem}_transcoded", "." + fmt)
    if fmt == "mp4":
        codec = ["-c:v", "libx264", "-preset", "medium", "-crf", "23", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]
    elif fmt == "webm":
        codec = ["-c:v", "libvpx-vp9", "-crf", "32", "-b:v", "0", "-c:a", "libopus", "-b:a", "128k"]
    elif fmt == "mkv":
        codec = ["-c", "copy"]
    elif fmt == "mov":
        codec = ["-c:v", "libx264", "-preset", "medium", "-crf", "23", "-c:a", "aac", "-b:a", "192k"]
    else:
        codec = ["-vf", "fps=12,scale='min(960,iw)':-1:flags=lanczos,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse", "-loop", "0"]
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
        )

    # Natural-language convenience triggers.
    if any(word in lower for word in ("derpibooru", "derpi", "呆站")):
        if any(word in lower for word in ("找", "搜", "下载", "抓取", "来", "给我", "find", "search", "download")):
            query = raw
            for word in ("derpibooru", "derpi", "呆站", "帮我", "请", "找", "搜索", "搜", "下载", "抓取", "来一张", "来几张", "来", "给我", "一张", "几张", "图片", "图像", "的", "find", "search", "download", "some", "pictures", "images"):
                query = re.sub(re.escape(word), " ", query, flags=re.IGNORECASE)
            return LocalRequest(kind="derpi", title="Derpibooru", query=query.strip() or "twilight sparkle")
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
            )
    return None


def run(request: LocalRequest) -> list[Path]:
    if request.error == "image":
        raise LocalActionError("please attach an image first")
    if request.error == "video":
        raise LocalActionError("please attach a video first")
    if request.kind == "derpi":
        return _download_derpi(request.query)
    if request.attachment is None:
        raise LocalActionError("missing attachment")
    if request.kind == "upscale":
        return _upscale(request.attachment.path)
    if request.kind == "audio":
        return _extract_audio(request.attachment.path)
    if request.kind == "transcode":
        return _transcode(request.attachment.path, request.target_format)
    raise LocalActionError("unknown local action")
