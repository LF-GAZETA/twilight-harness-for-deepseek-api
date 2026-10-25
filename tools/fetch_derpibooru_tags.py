"""Fetch a local Derpibooru tag table for Twilight Harness.

The Derpibooru API exposes tags via /api/v1/json/search/tags.  We walk the
image-count-ranked result pages, exclude rating/NSFW/other sensitive tags, and
keep exactly 20,000 tags including artist:, oc: and fanfic: entries.

Run from the repository root:
    python tools/fetch_derpibooru_tags.py
"""

from __future__ import annotations

import gzip
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://derpibooru.org/api/v1/json/search/tags"
PER_PAGE = 50
TARGET = 20_000
OUTPUT = Path(__file__).resolve().parent.parent / "assets" / "derpibooru-tags.json.gz"
USER_AGENT = "TwilightHarness/1.0 (fan project; local desktop client)"

# Rating metadata is deliberately not part of the content-tag table.
EXCLUDED_CATEGORIES = {"rating"}

SENSITIVE_TERMS = [
    # Explicit sexual / anatomy
    "explicit", "questionable", "sex", "sexual", "sexuality", "sexy",
    "nude", "nudity", "naked", "breast", "breasts", "boob", "boobs",
    "nipple", "nipples", "areola", "penis", "vagina", "pussy", "cock",
    "dick", "cum", "semen", "sperm", "anal", "oral", "erection", "orgasm",
    "masturbation", "bdsm", "bondage", "fetish", "fetishized", "fetishism",
    "genital", "genitals", "ass", "butt", "buttocks", "cameltoe",
    "lingerie", "underwear", "panties", "bikini", "swimsuit",
    # Extreme / violence / horror
    "gore", "gory", "blood", "bloody", "guts", "dismember", "dismemberment",
    "death", "dead", "dying", "kill", "killing", "murder", "suicide",
    "torture", "rape", "incest", "pedo", "pedophilia", "foalcon", "cub",
    "vore", "scat", "urine", "piss", "diaper", "guro", "necrophilia",
    "bestiality", "zoophilia", "self harm", "self-harm",
    # Slurs / harassment
    "nigger", "nigga", "faggot", "fag", "retard", "tranny", "kike",
    "spic", "chink", "wetback", "coon", "slut", "whore", "bitch", "cunt",
    # Other adult concepts
    "hentai", "porn", "pornography", "nsfw", "lewd", "erotic", "erotica",
    "molest", "molestation", "rape", "incest", "bestiality",
]

_TERM_RE = re.compile(
    r"(?<![a-z0-9])(?:" + "|".join(re.escape(t) for t in sorted(SENSITIVE_TERMS, key=len, reverse=True)) + r")(?![a-z0-9])",
    re.IGNORECASE,
)


def is_sensitive(value: str) -> bool:
    text = (value or "").strip().lower()
    if not text:
        return False
    return _TERM_RE.search(text) is not None


def clean_aliases(aliases: list[str]) -> list[str]:
    result: list[str] = []
    for alias in aliases or []:
        alias = str(alias).replace("+", " ").strip()
        if alias and not is_sensitive(alias) and alias not in result:
            result.append(alias)
    return result


def fetch_page(page: int) -> dict:
    params = {
        "q": "*",
        "per_page": PER_PAGE,
        "page": page,
        "sf": "images",
        "sd": "desc",
    }
    url = API + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    delay = 2.0
    for attempt in range(5):
        try:
            with urllib.request.urlopen(request, timeout=40) as response:
                return json.loads(response.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                time.sleep(delay)
                delay *= 2
                continue
            raise
        except Exception:
            time.sleep(1.5)
    raise RuntimeError(f"Could not fetch Derpibooru tag page {page}")


def main() -> int:
    tags: list[dict] = []
    seen: set[str] = set()
    page = 1
    started = time.time()
    while len(tags) < TARGET:
        payload = fetch_page(page)
        batch = payload.get("tags") or []
        if not batch:
            break
        for item in batch:
            name = str(item.get("name") or "").strip()
            if not name or name in seen:
                continue
            category = str(item.get("category") or "")
            if category in EXCLUDED_CATEGORIES:
                continue
            if is_sensitive(name):
                continue
            aliases = clean_aliases(item.get("aliases") or [])
            seen.add(name)
            tags.append(
                {
                    "name": name,
                    "images": int(item.get("images") or 0),
                    "category": category or "",
                    "namespace": item.get("namespace") or "",
                    "aliases": aliases,
                }
            )
            if len(tags) >= TARGET:
                break
        if page % 20 == 0 or len(tags) >= TARGET:
            elapsed = time.time() - started
            print(f"page={page} kept={len(tags)} elapsed={elapsed:.0f}s", flush=True)
        page += 1
        time.sleep(0.12)
    if len(tags) < TARGET:
        print(f"Only found {len(tags)} allowed tags after {page - 1} pages", flush=True)
    tags.sort(key=lambda item: (-int(item.get("images") or 0), item.get("name") or ""))
    payload = {
        "source": "https://derpibooru.org/api/v1/json/search/tags",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "criteria": {
            "target": TARGET,
            "excluded_categories": sorted(EXCLUDED_CATEGORIES),
            "sensitive_terms": SENSITIVE_TERMS,
            "ranking": "images descending",
        },
        "count": len(tags),
        "tags": tags,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(OUTPUT, "wt", encoding="utf-8", compresslevel=9) as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
    print(f"Wrote {OUTPUT} ({OUTPUT.stat().st_size} bytes) count={len(tags)}", flush=True)
    return 0 if len(tags) >= TARGET else 1


if __name__ == "__main__":
    raise SystemExit(main())
