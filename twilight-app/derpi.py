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

import tag_table

ALLOWED_RATINGS = ("safe", "suggestive", "semi-grimdark")
_RATING_TAGS = {"safe", "suggestive", "semi-grimdark", "questionable", "explicit"}

_KNOWN_TAGS = [
    "twilight sparkle (alicorn)",
    "twilight sparkle (unicorn)",
    "twilight sparkle (human)",
    "princess twilight sparkle",
    "twilight sparkle",
    "sunny starscout",
    "rainbow dash",
    "pinkie pie",
    "applejack",
    "fluttershy",
    "princess celestia",
    "princess luna",
    "princess cadance",
    "shining armor",
    "sunset shimmer",
    "starlight glimmer",
    "queen chrysalis",
    "king sombra",
    "cozy glow",
    "ms. peachbottom",
    "what?",
    "whoa!",
    "o.o",
    "my little pony",
    "friendship is magic",
    "alicorn",
    "unicorn",
    "earth pony",
    "pegasus",
    "g4",
    "g5",
    "equestria girls",
]

_CHINESE_TAGS = {
    "天角暮光": "twilight sparkle (alicorn)",
    "天角紫悦": "twilight sparkle (alicorn)",
    "暮光闪闪": "twilight sparkle",
    "暮光": "twilight sparkle",
    "紫悦": "twilight sparkle",
    "云宝": "rainbow dash",
    "碧琪": "pinkie pie",
    "苹果嘉儿": "applejack",
    "苹果杰克": "applejack",
    "珍奇": "rarity",
    "柔柔": "fluttershy",
    "小蝶": "fluttershy",
    "天角兽": "alicorn",
    "独角兽": "unicorn",
    "飞马": "pegasus",
}

_FILLER = {
    "帮我", "帮忙", "麻烦", "请", "给我", "我想要", "我想", "我要", "想要",
    "找一下", "找几张", "找一张", "找", "搜一下", "搜索", "搜集", "收集", "搜",
    "下载", "抓取", "获取", "下点", "来点", "来几张", "来一张", "来", "整点",
    "看看", "查看", "查找", "最新的", "最新", "热门的", "热门", "好看的", "可爱的",
    "图片", "图像", "插画", "壁纸", "照片", "的图片", "的图",
    "几张", "一张", "一些", "一下", "谢谢", "多谢", "感谢",
    "关于", "在", "里", "上", "中", "的", "呆站", "derpibooru", "derpi",
    "latest", "newest", "please", "thanks", "thank", "find", "search", "download",
    "get", "some", "picture", "pictures", "image", "images", "pics", "pic",
}


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


def _clean_tag(tag: str) -> str:
    tag = (tag or "").strip().strip(",;；")
    tag = re.sub(r"\s+", " ", tag)
    tag = tag.strip("，。；、")
    if not tag or tag.lower() in _RATING_TAGS:
        return ""
    return tag


def extract_tags(text: str) -> list[str]:
    raw = (text or "").strip()
    raw = re.sub(r"^\s*/?(?:derpibooru|derpi)\b", " ", raw, flags=re.IGNORECASE)
    match = re.search(r"(?:derpibooru|derpi|呆站)", raw, flags=re.IGNORECASE)
    if match:
        raw = raw[match.end():]
    raw = raw.replace("（", "(").replace("）", ")")
    raw = re.sub(r"^[\s:：,，、的里上中在]+", " ", raw)
    # A trailing thank-you / sentence punctuation is not a tag separator.
    raw = re.sub(
        r"[，,。！？!?.;；]?\s*(?:谢谢|多谢|感谢|thanks?|please)\s*[！!。.]*\s*$",
        " ",
        raw,
        flags=re.IGNORECASE,
    )
    raw = raw.strip(" ,，。；:：")
    raw = raw.replace("sunny starcout", "sunny starscout")

    for chinese, english in _CHINESE_TAGS.items():
        raw = raw.replace(chinese, f" {english} ")

    # If explicit comma-separated tags are supplied, preserve those boundaries
    # and all punctuation inside each tag.
    tags: list[str] = []
    table = tag_table.load()
    if "," in raw or "，" in raw:
        for piece in re.split(r"[,，]", raw):
            cleaned = _clean_tag(piece)
            if cleaned:
                if table is not None:
                    cleaned = table.canonicalize(cleaned)
                tags.append(cleaned)
    else:
        for filler in sorted(_FILLER, key=len, reverse=True):
            if re.search(r"[\u3400-\u9fff]", filler):
                raw = raw.replace(filler, " ")
            else:
                raw = re.sub(
                    rf"(?<![\w]){re.escape(filler)}(?![\w])",
                    " ",
                    raw,
                    flags=re.IGNORECASE,
                )
        raw = re.sub(r"\s+", " ", raw).strip(" ,，。.;；")
        table_tags: list[str] = []
        if table is not None:
            matches = table.find_phrases(raw)
            if matches:
                words = raw.split()
                for start, end, canonical in matches:
                    words[start] = f" __TAGTABLE_{len(table_tags)}__ "
                    for index in range(start + 1, end):
                        words[index] = ""
                    table_tags.append(canonical)
                raw = " ".join(word for word in words if word)
        placeholders: list[str] = []
        for known in sorted(_KNOWN_TAGS, key=len, reverse=True):
            pattern = re.compile(re.escape(known), re.IGNORECASE)
            while pattern.search(raw):
                raw = pattern.sub(f" __KNOWN_{len(placeholders)}__ ", raw, count=1)
                placeholders.append(known)
        for token in raw.split():
            if token.startswith("__TAGTABLE_"):
                try:
                    idx = int(token.strip("_").split("_")[-1])
                    tags.append(table_tags[idx])
                except (ValueError, IndexError):
                    continue
                continue
            if token.startswith("__KNOWN_"):
                try:
                    idx = int(token.strip("_").split("_")[-1])
                    tags.append(placeholders[idx])
                except (ValueError, IndexError):
                    continue
                continue
            token = token.strip("，。；、,;")
            if token and token.lower() not in _FILLER and token.lower() not in _RATING_TAGS:
                tags.append(token)
    result: list[str] = []
    for tag in tags:
        if tag not in result:
            result.append(tag)
    return result


def extract_query(text: str) -> str:
    tags = extract_tags(text)
    return ", ".join(tags) if tags else "twilight sparkle"


def is_request(text: str) -> bool:
    """Only an explicit /derpi command bypasses model translation."""
    raw = (text or "").strip()
    return bool(re.match(r"^/?(?:derpi|derpibooru)\b", raw, flags=re.IGNORECASE))

def _is_allowed(image: dict) -> bool:
    tags = {str(tag).lower() for tag in image.get("tags") or []}
    if not tags & set(ALLOWED_RATINGS):
        view_url = str(image.get("view_url") or "")
        if not any(f"__{rating}_" in view_url for rating in ALLOWED_RATINGS):
            return False
    return not ({"explicit", "questionable"} & tags)


def _date_term(value: str) -> str:
    value = (value or "").strip()
    match = re.match(r"(\d{4}-\d{2}-\d{2})", value)
    return match.group(1) if match else ""


def _search_payload(q: str, limit: int, sort: str) -> dict:
    params = {
        "q": q,
        "per_page": 50,
        "sf": sort,
        "sd": "desc",
    }
    url = "https://derpibooru.org/api/v1/json/search/images?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "TwilightHarness/1.0 (fan project; local desktop client)"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001
        raise DerpiError(f"Derpibooru search failed: {exc}") from exc


def _download_images(images: list[dict], limit: int) -> list[Path]:
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
    return saved


def search_download(
    query,
    limit: int = 4,
    after: str | None = None,
    before: str | None = None,
) -> list[Path]:
    """Search Derpibooru and download images.

    - No time range: return the newest available images.
    - Time range with more results than the limit: request a random ordering
      and take the first *limit*.
    - Time range with fewer results: take all available results.
    """
    limit = max(1, min(8, int(limit or 4)))
    raw_tags = query if isinstance(query, (list, tuple)) else extract_tags(str(query))
    tags: list[str] = []
    for tag in raw_tags:
        cleaned = _clean_tag(str(tag))
        if cleaned and cleaned.lower() not in _RATING_TAGS and cleaned not in tags:
            tags.append(cleaned)
    if not tags:
        tags = ["twilight sparkle"]
    after_date = _date_term(after or "")
    before_date = _date_term(before or "")
    allowed = " OR ".join(ALLOWED_RATINGS)
    terms = list(tags)
    if after_date:
        terms.append(f"created_at.gte:{after_date}")
    if before_date:
        terms.append(f"created_at.lt:{before_date}")
    terms.append(f"({allowed})")
    q = ", ".join(terms)

    # First look up the newest results (and get the total for the date range).
    newest = _search_payload(q, limit, "created_at")
    total = int(newest.get("total") or 0)
    images = newest.get("images") or []
    if not images:
        raise DerpiError(f"Derpibooru returned no images for tags: {q}")
    if (after_date or before_date) and total > limit:
        # A time range contains more images than the requested count: randomize.
        images = _search_payload(q, limit, "random").get("images") or images
    saved = _download_images(images, limit)
    if not saved:
        raise DerpiError(
            f"no downloadable derpicdn.net images were returned for tags: {q} "
            f"(total={total}, limit={limit})"
        )
    return saved
