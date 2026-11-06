"""Derpibooru image search/download helper shared by both builds.

Downloads only derpicdn.net media into the user's Downloads folder.  The
application never deletes Downloads.
"""

from __future__ import annotations

import concurrent.futures
import http.client
import ipaddress
import json
import os
import random
import re
import shutil
import socket
import ssl
import subprocess
import threading
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import paths
import tag_table

ALLOWED_RATINGS = ("safe", "suggestive", "semi-grimdark")
API_HOST = "derpibooru.org"
# Cloudflare edge IPs for derpibooru.org.  The system DNS on some networks is
# poisoned to an unreachable address; these are tried first, with the hostname
# still used for SNI and certificate verification.
API_FALLBACK_IPS = ("104.26.15.212", "104.26.14.212", "172.67.71.84")
API_IP_ENV = "TWILIGHT_DERPIBOORU_IPS"
_WORKING_API_IP: str | None = None
MAX_IMAGE_DOWNLOADS = 5000
MAX_IMAGE_BYTES = 30 * 1024 * 1024
PER_PAGE = 50
IMAGE_FORMATS = {"png", "jpg", "jpeg", "webp", "gif"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
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
    """Only an explicit /derpi or /derpibooru slash command bypasses the model."""
    raw = (text or "").strip()
    return bool(re.match(r"^/(?:derpi|derpibooru)\b", raw, flags=re.IGNORECASE))

def _is_allowed(image: dict) -> bool:
    tags = {str(tag).lower() for tag in image.get("tags") or []}
    if not tags & set(ALLOWED_RATINGS):
        view_url = str(image.get("view_url") or "")
        if not any(f"__{rating}_" in view_url for rating in ALLOWED_RATINGS):
            return False
    if {"explicit", "questionable"} & tags:
        return False
    fmt = str(image.get("format") or "").lower()
    if fmt and fmt not in IMAGE_FORMATS:
        return False
    # Derpibooru search also returns short videos/animations; `format` above
    # filters those out.  Do not use `duration` here: ordinary images also
    # report a small duration field.
    return True


def _date_term(value: str) -> str:
    value = (value or "").strip()
    match = re.match(r"(\d{4}-\d{2}-\d{2})", value)
    return match.group(1) if match else ""


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection to a fixed IP while keeping the real hostname for SNI."""

    def __init__(self, host: str, ip: str, **kwargs) -> None:
        super().__init__(host, **kwargs)
        self._pinned_ip = ip

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._pinned_ip, self.port), self.timeout, self.source_address
        )
        server_hostname = self._tunnel_host or self.host
        if self._tunnel_host:
            self._tunnel()
        self.sock = self._context.wrap_socket(
            self.sock, server_hostname=server_hostname
        )


def _api_candidate_ips() -> list[str]:
    """Available derpibooru.org edge IPs: env override, CDN DNS, then defaults."""
    candidates: list[str] = []
    for part in re.split(r"[,;\s]+", os.environ.get(API_IP_ENV, "").strip()):
        if part and part not in candidates:
            candidates.append(part)
    # derpicdn.net uses the same Cloudflare edge and its DNS is usually healthy.
    try:
        for info in socket.getaddrinfo("derpicdn.net", 443, type=socket.SOCK_STREAM):
            ip = info[4][0]
            if ip not in candidates:
                candidates.append(ip)
    except OSError:
        pass
    for ip in API_FALLBACK_IPS:
        if ip not in candidates:
            candidates.append(ip)
    valid: list[str] = []
    for ip in candidates:
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            continue
        valid.append(ip)
    # Prefer IPv4; it is the least surprising on Windows.
    return [ip for ip in valid if ":" not in ip] + [ip for ip in valid if ":" in ip]


def _pinned_api_json(url: str, ip: str, timeout: float = 8.0) -> dict:
    parsed = urllib.parse.urlsplit(url)
    path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    connection = _PinnedHTTPSConnection(
        API_HOST, ip, timeout=timeout, context=ssl.create_default_context()
    )
    try:
        connection.request(
            "GET",
            path,
            headers={
                "User-Agent": "TwilightHarness/1.0 (fan project; local desktop client)",
                "Accept": "application/json",
            },
        )
        response = connection.getresponse()
        body = response.read()
        if response.status != 200:
            raise urllib.error.HTTPError(
                url, response.status, response.reason, response.headers, None
            )
        return json.loads(body.decode("utf-8", errors="replace"))
    finally:
        try:
            connection.close()
        except OSError:
            pass


def _api_json(url: str, timeout: float = 8.0) -> dict:
    """Call the API through a working pinned IP, then fall back to normal DNS."""
    global _WORKING_API_IP
    errors: list[str] = []
    candidates: list[str] = []
    if _WORKING_API_IP:
        candidates.append(_WORKING_API_IP)
    for ip in _api_candidate_ips():
        if ip not in candidates:
            candidates.append(ip)
    for ip in candidates:
        try:
            data = _pinned_api_json(url, ip, timeout=timeout)
            _WORKING_API_IP = ip
            return data
        except urllib.error.HTTPError:
            # Rate limits / HTTP errors are server-wide: let the caller retry.
            raise
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{ip}: {type(exc).__name__}: {exc}")
            if _WORKING_API_IP == ip:
                _WORKING_API_IP = None
    # Last resort: normal urllib path (honors a system HTTPS proxy and DNS).
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "TwilightHarness/1.0 (fan project; local desktop client)"},
    )
    try:
        with urllib.request.urlopen(request, timeout=max(timeout, 10.0)) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001
        errors.append(f"DNS: {type(exc).__name__}: {exc}")
    raise DerpiError("Derpibooru API unreachable: " + "; ".join(errors[-4:]))


def _search_payload(q: str, page: int = 1, sort: str = "created_at") -> dict:
    params = {
        "q": q,
        "per_page": PER_PAGE,
        "page": max(1, int(page)),
        "sf": sort,
        "sd": "desc",
    }
    url = "https://" + API_HOST + "/api/v1/json/search/images?" + urllib.parse.urlencode(params)
    last_error: Exception | None = None
    for attempt in range(5):
        try:
            return _api_json(url, timeout=8.0)
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


def _probe_writable(folder: Path) -> None:
    probe = folder / f".twilight_write_{os.getpid()}"
    try:
        probe.write_bytes(b"x")
        probe.unlink(missing_ok=True)
    except OSError as exc:
        raise DerpiError(
            f"Download folder is not writable: {folder} ({exc.strerror or exc})"
        ) from exc


def _safe_image_urls(image: dict) -> list[str]:
    """Prefer the full view URL, then CDN representation fallbacks."""
    urls: list[str] = []
    view_url = str(image.get("view_url") or image.get("image") or "")
    if view_url:
        urls.append(view_url)
    representations = image.get("representations") or {}
    if isinstance(representations, dict):
        for key in ("full", "large", "medium", "small", "thumb"):
            value = representations.get(key)
            if isinstance(value, str) and value:
                urls.append(value)
    result: list[str] = []
    for url in urls:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or not (
            parsed.netloc == "derpicdn.net" or parsed.netloc.endswith(".derpicdn.net")
        ):
            continue
        suffix = Path(parsed.path).suffix.lower()
        if suffix and suffix not in IMAGE_SUFFIXES:
            continue
        if url not in result:
            result.append(url)
    return result


def _curl_download(url: str, target: Path) -> tuple[bool, str]:
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if not curl:
        return False, "curl unavailable"
    part = target.with_name(target.name + ".part")
    command = [
        curl,
        "-L",
        "--fail",
        "--silent",
        "--show-error",
        "--retry",
        "2",
        "--max-time", "45",
        "--connect-timeout",
        "15",
        "--max-filesize",
        str(MAX_IMAGE_BYTES),
        "-A",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36",
        "-e",
        "https://derpibooru.org/",
        "-o",
        str(part),
        url,
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            timeout=150,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"curl: {exc}"
    if result.returncode == 0 and part.is_file() and 0 < part.stat().st_size <= MAX_IMAGE_BYTES:
        try:
            part.replace(target)
            return True, ""
        except OSError as exc:
            return False, f"write: {exc}"
    try:
        part.unlink(missing_ok=True)
    except OSError:
        pass
    detail = result.stderr.decode("utf-8", errors="replace").strip()[-160:]
    return False, f"curl HTTP {result.returncode}: {detail}"


def _emit_progress(progress, event: dict) -> None:
    if progress is None:
        return
    try:
        progress(event)
    except Exception:  # noqa: BLE001
        pass


def _batch_fields(batch: dict | None) -> dict:
    if not batch:
        return {}
    lock = batch.get("lock")
    if lock is None:
        return {"batch_done": int(batch.get("done") or 0),
                "batch_total": int(batch.get("total") or 0)}
    with lock:
        return {"batch_done": int(batch.get("done") or 0),
                "batch_total": int(batch.get("total") or 0)}


def _batch_step(batch: dict | None) -> dict:
    if not batch:
        return {}
    lock = batch.get("lock")
    if lock is not None:
        with lock:
            batch["done"] = int(batch.get("done") or 0) + 1
    else:
        batch["done"] = int(batch.get("done") or 0) + 1
    return _batch_fields(batch)


def _download_one(image: dict, folder: Path, progress=None, batch: dict | None = None) -> Path | None:
    image_id = str(image.get("id") or int(time.time() * 1000))
    row_id = f"download-{image_id}"
    label = f"derpibooru-{image_id}"
    total_hint = int(image.get("size") or 0)
    _emit_progress(
        progress,
        {"id": row_id, "kind": "download", "name": label,
         "fraction": 0.0, "done": 0, "total": total_hint, "state": "running",
         **_batch_fields(batch)},
    )
    urls = _safe_image_urls(image)
    if not urls:
        _LAST_DOWNLOAD_ERRORS.append(f"{image_id}: no derpicdn.net URL")
        _emit_progress(
            progress,
            {"id": row_id, "kind": "download", "name": label,
             "fraction": 0.0, "done": 0, "total": total_hint, "state": "error",
             **_batch_step(batch)},
        )
        return None
    suffix = Path(urllib.parse.urlparse(urls[0]).path).suffix.lower()
    fmt = str(image.get("format") or "").lower()
    if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        suffix = "." + fmt if fmt in {"png", "jpg", "jpeg", "webp", "gif"} else ".png"
    target = _unique_output(folder, f"derpibooru_{image_id}", suffix)
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36",
        "Referer": "https://derpibooru.org/",
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    last_error = ""
    for url in urls:
        too_large = False
        for attempt in range(2):
            try:
                request = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(request, timeout=20) as response:
                    total = int(
                        response.headers.get("Content-Length")
                        or total_hint
                        or 0
                    )
                    chunks: list[bytes] = []
                    done = 0
                    if total > MAX_IMAGE_BYTES:
                        # Do not download a huge body just to reject it; move on
                        # to the large/medium/small representation.
                        too_large = True
                    else:
                        while True:
                            chunk = response.read(64 * 1024)
                            if not chunk:
                                break
                            done += len(chunk)
                            if done > MAX_IMAGE_BYTES:
                                too_large = True
                                break
                            chunks.append(chunk)
                            fraction = (done / total) if total > 0 else 0.0
                            _emit_progress(
                                progress,
                                {"id": row_id, "kind": "download", "name": label,
                                 "fraction": min(0.99, fraction), "done": done,
                                 "total": total, "state": "running",
                                 **_batch_fields(batch)},
                            )
                    payload = b"".join(chunks)
                if not too_large and 0 < len(payload) <= MAX_IMAGE_BYTES:
                    part.write_bytes(payload)
                    part.replace(target)
                    _emit_progress(
                        progress,
                        {"id": row_id, "kind": "download", "name": label,
                         "fraction": 1.0, "done": len(payload),
                         "total": len(payload), "state": "done",
                         **_batch_step(batch)},
                    )
                    return target
                if too_large:
                    last_error = f"image too large (> {MAX_IMAGE_BYTES // (1024 * 1024)} MB): {url}"
                else:
                    last_error = f"HTTP body size {len(payload)}"
            except Exception as exc:  # noqa: BLE001
                last_error = f"{type(exc).__name__}: {exc}"
            if attempt == 0:
                time.sleep(0.4)
        if too_large:
            # Try the next representation (large/medium/small) instead of
            # writing a truncated file or calling curl for the same URL.
            continue
        _emit_progress(
            progress,
            {"id": row_id, "kind": "download", "name": label,
             "fraction": 0.25, "done": 0, "total": total_hint, "state": "running",
             **_batch_fields(batch)},
        )
        ok, curl_error = _curl_download(url, target)
        if ok:
            try:
                size = target.stat().st_size
            except OSError:
                size = 0
            _emit_progress(
                progress,
                {"id": row_id, "kind": "download", "name": label,
                 "fraction": 1.0, "done": size, "total": size, "state": "done",
                 **_batch_step(batch)},
            )
            return target
        last_error = curl_error or last_error
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass
    _LAST_DOWNLOAD_ERRORS.append(f"{image_id}: {last_error or 'download failed'}")
    _emit_progress(
        progress,
        {"id": row_id, "kind": "download", "name": label,
         "fraction": 0.0, "done": 0, "total": total_hint, "state": "error",
         **_batch_step(batch)},
    )
    return None


_LAST_DOWNLOAD_ERRORS: list[str] = []


def _download_images(images: list[dict], limit: int, progress=None) -> list[Path]:
    _LAST_DOWNLOAD_ERRORS.clear()
    folder = save_dir()
    _probe_writable(folder)
    selected = list(images)[:limit]
    saved: list[Path] = []
    if not selected:
        return saved
    batch = {"total": len(selected), "done": 0, "lock": threading.Lock()}
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=6)
    pending = {
        executor.submit(_download_one, image, folder, progress, batch): image
        for image in selected
    }
    try:
        for future in concurrent.futures.as_completed(pending, timeout=90):
            try:
                path = future.result()
            except Exception as exc:  # noqa: BLE001
                _LAST_DOWNLOAD_ERRORS.append(str(exc))
                _batch_step(batch)
                continue
            if path is not None:
                saved.append(path)
    except concurrent.futures.TimeoutError:
        _LAST_DOWNLOAD_ERRORS.append("Derpibooru download timed out after 90s")
        for future in pending:
            future.cancel()
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    return saved

def download_selected(images: list[dict], limit: int, progress=None) -> list[Path]:
    saved = _download_images(images, max(1, min(MAX_IMAGE_DOWNLOADS, int(limit or 10))), progress)
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
    progress=None,
) -> list[Path]:
    """Backward-compatible convenience wrapper: select and download."""
    images = find_images(query, limit=limit, after=after, before=before)
    saved = download_selected(images, max(1, min(MAX_IMAGE_DOWNLOADS, int(limit or 10))), progress)
    if not saved:
        raise DerpiError("no downloadable derpicdn.net images were returned for the selected image IDs")
    return saved
