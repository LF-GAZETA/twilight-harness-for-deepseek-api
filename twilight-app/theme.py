"""Visual language for Twilight Harness, based on the supplied GUI design.

Flat lavender canvas, black rules and type, white form fields with rounded
borders, small lilac pill buttons. Latin display text uses Uni Neue; Chinese UI
and body text use Microsoft YaHei (微软雅黑) with Windows fallbacks.
"""

from __future__ import annotations

import math
import tkinter as tk
from tkinter import font as tkfont

# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------
BG = "#F6EBFF"
WHITE = "#FFFFFF"
BLACK = "#000000"
INK = "#000000"
ACCENT = "#7030A0"
ACCENT_SOFT = "#9C4FC4"
BUTTON = "#E8CDFF"
BUTTON_HOVER = "#DDBBFF"
BUTTON_PRESSED = "#D2A6FF"
BUTTON_DISABLED = "#D9D9D9"
DISABLED_TEXT = "#BFBFBF"
PLACEHOLDER = "#BCB4C3"
ERROR = "#C00000"
LINE = "#000000"
SCROLL = "#595959"
SCROLL_HOVER = "#3A3A3A"
ONLINE = "#43B02A"
FIELD_BORDER = "#000000"
PANEL_HOVER = "#EAD7FF"
MUTED = "#706A77"
THINKING_TEXT = "#361759"
SETUP_TITLE_TEXT = "#361759"

# ---------------------------------------------------------------------------
# Font resolution
# ---------------------------------------------------------------------------
LATIN_FAMILY = "Uni Neue"
CJK_FAMILY = "微软雅黑"
CJK_BOLD_FAMILY = "微软雅黑"
OTHER_FAMILY = "Source Han Sans JP"
MONO_FAMILY = "Consolas"

_FONT_CACHE: dict[tuple[str, int, str, str, bool], tkfont.Font] = {}
_FONTS_READY = False


def _pick(families: set[str], candidates: list[str], fallback: str) -> str:
    for name in candidates:
        if name in families:
            return name
    return fallback


def init_fonts(root: tk.Misc) -> None:
    """Resolve the design fonts once a Tk root exists."""
    global LATIN_FAMILY, CJK_FAMILY, CJK_BOLD_FAMILY, OTHER_FAMILY, MONO_FAMILY, _FONTS_READY
    try:
        families = set(tkfont.families(root))
    except tk.TclError:
        families = set()
    LATIN_FAMILY = _pick(
        families,
        ["Uni Neue", "Uni Neue Book", "Segoe UI Variable Display", "Segoe UI"],
        "Segoe UI",
    )
    CJK_FAMILY = _pick(
        families,
        ["微软雅黑", "Microsoft YaHei UI", "Microsoft YaHei", "思源黑体"],
        "Microsoft YaHei",
    )
    CJK_BOLD_FAMILY = CJK_FAMILY
    OTHER_FAMILY = _pick(
        families,
        ["Source Han Sans JP", "Source Han Sans SC", "Source Han Sans CN", "思源黑体"],
        "Source Han Sans JP",
    )
    MONO_FAMILY = _pick(families, ["Consolas", "Cascadia Mono", "Courier New"], "Courier New")
    _FONTS_READY = True


def ui_family(locale: str = "zh") -> str:
    if locale.startswith("zh"):
        return CJK_FAMILY
    if locale in {"en", "fr", "de", "es", "pt", "it"}:
        return LATIN_FAMILY
    return OTHER_FAMILY


def body_family(locale: str = "zh") -> str:
    """Font for conversation text.

    Chinese uses Microsoft YaHei, Latin-script languages use Uni Neue, and all
    other languages (Japanese, Korean, Cyrillic, Arabic, ...) use Source Han
    Sans as requested.
    """
    if locale.startswith("zh"):
        return CJK_FAMILY
    if locale in {"en", "fr", "de", "es", "pt", "it", "latin"}:
        return LATIN_FAMILY
    return OTHER_FAMILY


def font(
    size: int = 11,
    weight: str = "normal",
    slant: str = "roman",
    family: str | None = None,
    underline: bool = False,
) -> tkfont.Font:
    family = family or CJK_FAMILY
    key = (family, int(size), weight, slant, underline)
    cached = _FONT_CACHE.get(key)
    if cached is None:
        cached = tkfont.Font(
            family=family, size=size, weight=weight, slant=slant, underline=underline
        )
        _FONT_CACHE[key] = cached
    return cached


def display(size: int = 24, weight: str = "normal") -> tkfont.Font:
    return font(size, weight=weight, family=LATIN_FAMILY)


def ui(
    size: int = 11,
    weight: str = "normal",
    locale: str = "zh",
    slant: str = "roman",
) -> tkfont.Font:
    family = ui_family(locale)
    if weight == "bold" and family == CJK_FAMILY:
        family = CJK_BOLD_FAMILY
    return font(size, weight=weight, slant=slant, family=family)


def body(
    size: int = 10,
    weight: str = "normal",
    locale: str = "zh",
    slant: str = "roman",
) -> tkfont.Font:
    family = body_family(locale)
    if weight == "bold" and family == CJK_FAMILY:
        family = CJK_BOLD_FAMILY
    return font(size, weight=weight, slant=slant, family=family)


# ---------------------------------------------------------------------------
# Drawing helpers
# ---------------------------------------------------------------------------
def rounded_rect(
    canvas: tk.Canvas,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    radius: float = 12,
    **kwargs,
) -> int:
    """Draw a rounded rectangle as one polygon (fill and outline)."""
    radius = max(0.0, min(radius, (x2 - x1) / 2.0, (y2 - y1) / 2.0))
    if radius <= 0:
        return canvas.create_rectangle(x1, y1, x2, y2, **kwargs)
    points: list[float] = []
    steps = 8
    corners = (
        (x1 + radius, y1 + radius, 180.0, 270.0),
        (x2 - radius, y1 + radius, 270.0, 360.0),
        (x2 - radius, y2 - radius, 0.0, 90.0),
        (x1 + radius, y2 - radius, 90.0, 180.0),
    )
    for cx, cy, start, end in corners:
        for index in range(steps + 1):
            angle = math.radians(start + (end - start) * index / steps)
            points.extend(
                (cx + radius * math.cos(angle), cy + radius * math.sin(angle))
            )
    return canvas.create_polygon(points, **kwargs)


def star_vertices(
    cx: float,
    cy: float,
    outer: float,
    inner: float,
    points: int = 6,
    rotation_deg: float = -90.0,
) -> list[float]:
    coords: list[float] = []
    for index in range(points * 2):
        radius = outer if index % 2 == 0 else inner
        angle = math.radians(rotation_deg + index * (180.0 / points))
        coords.extend((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    return coords


def draw_star(
    canvas: tk.Canvas,
    cx: float,
    cy: float,
    outer: float,
    inner: float | None = None,
    fill: str = ACCENT,
    outline: str = "",
    width: float = 0,
    points: int = 6,
    **kwargs,
) -> int:
    if inner is None:
        inner = outer * 0.42
    coords = star_vertices(cx, cy, outer, inner, points=points)
    return canvas.create_polygon(
        coords, fill=fill, outline=outline, width=width, **kwargs
    )
