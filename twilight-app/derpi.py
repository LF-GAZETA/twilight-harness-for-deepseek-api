"""Derpibooru image search/download helper shared by both builds.

Downloads only derpicdn.net media into the user's Downloads folder.  The
application never deletes Downloads.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import random
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import paths
import tag_table

ALLOWED_RATINGS = ("safe", "suggestive", "semi-grimdark")
MAX_IMAGE_DOWNLOADS = 5000
PER_PAGE = 50
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


def _is_writable(folder: Path) -> bool:
    probe = folder / f".twilight_write_{os.getpid()}"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe.write_bytes(b"x")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def save_dir() -> Path:
    """Return a writable Downloads-based output folder.

    The normal target is Downloads/TwilightHarness.  If that folder is not
    writable (ACL, antivirus, controlled-folder access, ...), try a sibling
    folder, then Downloads itself, then Pictures or LocalAppData.  No folder is
    ever deleted.
    """
    primary = downloads_dir()
    candidates = [
        primary / "TwilightHarness",
        primary / "TwilightHarness-2",
        primary / "TwilightHarness-3",
        primary,
        Path.home() / "Pictures" / "TwilightHarness",
        paths.persistent_dir() / "downloads",
        paths.resource_base() / "downloads",
        Path(tempfile.gettempdir()) / "TwilightHarness",
    ]
    for folder in candidates:
        try:
            if _is_writable(folder):
                return folder
        except OSError:
            continue
    raise DerpiError(
        "no writable download folder found; tried Downloads/TwilightHarness, "
        "Pictures/TwilightHarness, LocalAppData, the application folder and TEMP"
    )


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


def _search_payload(q: str, page: int = 1, sort: str = "created_at") -> dict:
    params = {
        "q": q,
        "per_page": PER_PAGE,
        "page": max(1, int(page)),
        "sf": sort,
        "sd": "desc",
    }
    url = "https://derpibooru.org/api/v1/json/search/images?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "TwilightHarness/1.0 (fan project; local desktop client)"},
    )
    last_error: Exception | None = None
    for attempt in range(5):
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                return json.loads(response.read().decode("utf-8", errors="replace"))
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            retry_after = 0.8 * (attempt + 1)
            if isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
                retry_after = max(retry_after, 2.0 * (attempt + 1))
            if attempt < 4:
                time.sleep(retry_after)
    raise DerpiError(f"Derpibooru search failed: {last_error}")


def _fetch_pages(q: str, pages: list[int], sort: str = "created_at") -> list[dict]:
    pages = [page for page in dict.fromkeys(pages) if page >= 1]
    if not pages:
        return []
    results: dict[int, list[dict]] = {}
    failed: list[int] = []
    # Sequential page fetching is slower than a thread pool but keeps the
    # 5000-image path below Derpibooru's API rate limits and avoids silently
    # returning a short result.
    for page in pages:
        for attempt in range(4):
            try:
                results[page] = _search_payload(q, page, sort).get("images") or []
                break
            except Exception:  # noqa: BLE001
                if attempt == 3:
                    failed.append(page)
                else:
                    time.sleep(1.0 * (attempt + 1))
        time.sleep(0.12)
    if failed:
        raise DerpiError(
            "Derpibooru page fetch failed for page(s): "
            + ", ".join(str(page) for page in failed[:8])
        )
    images: list[dict] = []
    seen: set[str] = set()
    for page in pages:
        for image in results.get(page, []):
            key = str(image.get("id") or "")
            if key and key not in seen:
                seen.add(key)
                images.append(image)
    return images
def _find_images_paged(
    q: str,
    limit: int,
    after_date: str,
    before_date: str,
    total: int,
    first_images: list[dict],
) -> list[dict]:
    page_count = max(1, (max(total, len(first_images)) + PER_PAGE - 1) // PER_PAGE)
    if not (after_date or before_date):
        # No time limit: newest items, in publication order.
        needed_pages = max(1, (limit + PER_PAGE - 1) // PER_PAGE)
        pages = list(range(1, min(page_count, needed_pages) + 1))
        images = first_images if pages == [1] else _fetch_pages(q, pages, "created_at")
        return images[:limit]

    if total <= limit:
        # The whole time range fits; fetch all of it.
        pages = list(range(1, page_count + 1))
        images = first_images if page_count == 1 else _fetch_pages(q, pages, "created_at")
        return images[:limit]

    # More results than requested: pick random pages from the time range.
    needed_pages = max(1, (limit + PER_PAGE - 1) // PER_PAGE)
    chosen = random.sample(range(1, page_count + 1), min(page_count, needed_pages))
    images = _fetch_pages(q, chosen, "created_at")
    if len(images) < limit and len(chosen) < page_count:
        remaining = [page for page in range(1, page_count + 1) if page not in chosen]
        random.shuffle(remaining)
        extra_needed = max(1, (limit - len(images) + PER_PAGE - 1) // PER_PAGE)
        images.extend(_fetch_pages(q, remaining[:extra_needed], "created_at"))
    return images[:limit]


def find_images(
    query,
    limit: int = 10,
    after: str | None = None,
    before: str | None = None,
) -> list[dict]:
    """Return selected image records (with IDs) before downloading."""
    limit = max(1, min(MAX_IMAGE_DOWNLOADS, int(limit or 10)))
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

    first = _search_payload(q, 1, "created_at")
    total = int(first.get("total") or 0)
    first_images = first.get("images") or []
    if not first_images:
        raise DerpiError(f"Derpibooru returned no images for tags: {q}")
    return _find_images_paged(q, limit, after_date, before_date, total, first_images)
def _download_images(images: list[dict], limit: int) -> list[Path]:
    _LAST_DOWNLOAD_ERRORS.clear()
    folder = save_dir()
    _probe_writable(folder)
    selected = [image for image in images if _is_allowed(image)][:limit]
    saved: list[Path] = []
    if not selected:
        return saved
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        pending = {
            executor.submit(_download_one, image, folder): image
            for image in selected
        }
        for future in concurrent.futures.as_completed(pending):
            try:
                path = future.result()
            except Exception as exc:  # noqa: BLE001
                _LAST_DOWNLOAD_ERRORS.append(str(exc))
                continue
            if path is not None:
                saved.append(path)
    return saved

def download_selected(images: list[dict], limit: int) -> list[Path]:
    saved = _download_images(images, max(1, min(MAX_IMAGE_DOWNLOADS, int(limit or 10))))
    if not saved:
        detail = "; ".join(_LAST_DOWNLOAD_ERRORS[-4:])
        raise DerpiError(
            "no downloadable derpicdn.net images were returned for the selected image IDs"
            + (f" ({detail})" if detail else "")
        )
    return saved


def search_download(
    query,
    limit: int = 10,
    after: str | None = None,
    before: str | None = None,
) -> list[Path]:
    """Backward-compatible convenience wrapper: select and download."""
    images = find_images(query, limit=limit, after=after, before=before)
    saved = download_selected(images, max(1, min(MAX_IMAGE_DOWNLOADS, int(limit or 10))))
    if not saved:
        raise DerpiError("no downloadable derpicdn.net images were returned for the selected image IDs")
    return saved
