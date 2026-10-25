"""Derpibooru image search/download helper shared by both builds.

Downloads only derpicdn.net media into the user's Downloads folder.  The
application never deletes Downloads.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

ALLOWED_RATINGS = ("safe", "suggestive", "semi-grimdark")
_RATING_SET = set(ALLOWED_RATINGS)


class DerpiError(Exception):
    pass


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
        raise DerpiError(f"cannot access Downloads folder: {exc}") from exc
    return candidate


def save_dir() -> Path:
    folder = downloads_dir() / "TwilightHarness"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _unique_output(folder: Path, stem: str, suffix: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._") or "image"
    candidate = folder / f"{safe}{suffix}"
    if not candidate.exists():
        return candidate
    for index in range(2, 1000):
        candidate = folder / f"{safe}_{index}{suffix}"
        if not candidate.exists():
            return candidate
    return folder / f"{safe}_{int(time.time())}{suffix}"


def extract_query(text: str) -> str:
    """Turn a user/tool request into a clean Derpibooru tag query.

    Handles both explicit `/derpi ...` commands and natural Chinese/English
    requests such as:
      "帮我在derpibooru里搜集最新的sunny starcout的图片，谢谢！"
    """
    raw = (text or "").strip()
    raw = re.sub(r"^\s*/?(?:derpibooru|derpi)\b", " ", raw, flags=re.IGNORECASE)
    # Keep only the part after a Derpibooru trigger when one is present.
    match = re.search(r"(?:derpibooru|derpi|呆站)", raw, flags=re.IGNORECASE)
    if match:
        raw = raw[match.end() :]
    # Strip request words, connectors and output nouns.  Longest tokens first
    # so "搜集" is not partially reduced to "集" by the "搜" rule.
    tokens = [
        "derpibooru", "derpi", "呆站",
        "帮我", "帮忙", "麻烦", "请", "给我", "我想要", "我想", "我要", "想要",
        "找一下", "找几张", "找一张", "找", "搜一下", "搜索", "搜集", "收集", "搜",
        "下载", "抓取", "获取", "来几张", "来一张", "看看", "查看", "查找",
        "最新的", "最新", "热门的", "热门", "好看的", "可爱的",
        "的图片", "的图", "图片", "图像", "插画", "壁纸", "照片",
        "几张", "一张", "一些", "一下", "谢谢", "多谢", "感谢", "请",
        "关于", "在", "里", "上", "中", "的",
        "latest", "newest", "please", "thanks", "thank you",
        "find", "search", "download", "get", "some", "pictures", "picture",
        "images", "image", "pics", "pic",
    ]
    for token in tokens:
        raw = re.sub(re.escape(token), " ", raw, flags=re.IGNORECASE)
    raw = re.sub(r"[，。！？!?.,;；:：、]+", " ", raw)
    raw = re.sub(r"\s+", " ", raw).strip(" \t\r\n")
    if not raw:
        return "twilight sparkle"
    aliases = {
        "sunny starcout": "sunny starscout",
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
    for chinese, english in aliases.items():
        raw = raw.replace(chinese, english)
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw or "twilight sparkle"
def _is_allowed(image: dict) -> bool:
    tags = {str(tag).lower() for tag in image.get("tags") or []}
    if not tags & _RATING_SET:
        # Some API filters omit the rating tag; inspect the CDN filename too.
        view_url = str(image.get("view_url") or "")
        if not any(f"__{rating}_" in view_url for rating in ALLOWED_RATINGS):
            return False
    return not ({"explicit", "questionable"} & tags)


def is_request(text: str) -> bool:
    raw = (text or "").strip()
    if re.match(r"^/?(?:derpi|derpibooru)", raw, flags=re.IGNORECASE):
        return True
    lower = raw.lower()
    if any(trigger in lower for trigger in ("derpibooru", "derpi", "呆站")):
        return any(verb in lower for verb in ("找", "搜", "下载", "抓取", "来", "给我", "find", "search", "download"))
    return False


def search_download(query: str, limit: int = 4) -> list[Path]:
    q = extract_query(query)
    allowed = " OR ".join(ALLOWED_RATINGS)
    url = "https://derpibooru.org/api/v1/json/search/images?" + urllib.parse.urlencode(
        {
            "q": f"({q}) AND ({allowed})",
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
        raise DerpiError(f"Derpibooru search failed: {exc}") from exc
    images = payload.get("images") if isinstance(payload, dict) else None
    if not images:
        raise DerpiError(f"Derpibooru returned no images for: {q}")
    folder = save_dir()
    saved: list[Path] = []
    for image in images:
        if len(saved) >= limit:
            break
        if not _is_allowed(image):
            continue
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
            req = urllib.request.Request(view_url, headers={"User-Agent": "TwilightHarness/1.0"})
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
        raise DerpiError(f"no downloadable derpicdn.net images were returned for: {q}")
    return saved
