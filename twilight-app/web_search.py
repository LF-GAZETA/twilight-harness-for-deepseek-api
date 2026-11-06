"""Minimal internet search helper (Bing HTML endpoint)."""
from __future__ import annotations

import html
import re
import urllib.parse
import urllib.request


def search(query: str, limit: int = 8) -> list[dict]:
    query = (query or "").strip()
    if not query:
        return []
    url = "https://www.bing.com/search?q=" + urllib.parse.quote(query)
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 TwilightHarness/1.0"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        body = response.read().decode("utf-8", errors="replace")
    blocks = re.findall(r'<li class="b_algo".*?</li>', body, re.I | re.S)
    results = []
    for block in blocks:
        link = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.I | re.S)
        if not link:
            continue
        href = html.unescape(link.group(1))
        title = re.sub(r"<[^>]+>", "", html.unescape(link.group(2))).strip()
        snippet_match = re.search(r"<p[^>]*>(.*?)</p>", block, re.I | re.S)
        snippet = ""
        if snippet_match:
            snippet = re.sub(r"<[^>]+>", "", html.unescape(snippet_match.group(1))).strip()
        if title and href.startswith("http"):
            results.append({"title": title, "url": href, "snippet": snippet})
        if len(results) >= limit:
            break
    return results
