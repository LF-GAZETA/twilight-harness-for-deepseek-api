"""Portable bundled-font installation.

The release carries all font files in a sibling `fonts/` folder.  On startup we
silently install only files that are not already present in the Windows or
per-user font directories; no dialogs, admin rights or reboots are required.
"""

from __future__ import annotations

import ctypes
import logging
import os
import shutil
import sys
from pathlib import Path

import paths

LOGGER = logging.getLogger("twilight_harness")

_IS_WINDOWS = os.name == "nt"
_FONT_REGS = [
    {
        "family": "Uni Neue",
        "files": [
            ("UniNeue-Regular.ttf", "Uni Neue Regular (TrueType)"),
            ("UniNeue-Bold.ttf", "Uni Neue Bold (TrueType)"),
            ("UniNeue-RegularItalic.ttf", "Uni Neue Regular Italic (TrueType)"),
            ("UniNeue-BoldItalic.ttf", "Uni Neue Bold Italic (TrueType)"),
        ],
    },
    {
        "family": "微软雅黑",
        "files": [
            ("msyh.ttc", "Microsoft YaHei & Microsoft YaHei UI (TrueType)"),
        ],
    },
    {
        "family": "Source Han Sans JP",
        "files": [
            ("SourceHanSansJP-Normal.otf", "Source Han Sans JP Normal (OpenType)"),
        ],
    },
]


def _font_dirs() -> tuple[Path, Path]:
    windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    windows_fonts = windir / "Fonts"
    local = os.environ.get("LOCALAPPDATA")
    user_fonts = (
        Path(local) / "Microsoft" / "Windows" / "Fonts"
        if local
        else Path.home() / "AppData" / "Local" / "Microsoft" / "Windows" / "Fonts"
    )
    return windows_fonts, user_fonts


def _add_font_resource(path: Path) -> bool:
    if not _IS_WINDOWS:
        return False
    try:
        gdi32 = ctypes.windll.gdi32
        gdi32.AddFontResourceW.argtypes = [ctypes.c_wchar_p]
        gdi32.AddFontResourceW.restype = ctypes.c_int
        return bool(gdi32.AddFontResourceW(str(path)))
    except Exception:  # noqa: BLE001
        return False


def _broadcast_font_change() -> None:
    if not _IS_WINDOWS:
        return
    try:
        user32 = ctypes.windll.user32
        user32.SendMessageTimeoutW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_uint,
            ctypes.c_void_p,
        ]
        user32.SendMessageTimeoutW.restype = ctypes.c_void_p
        result = ctypes.c_void_p()
        user32.SendMessageTimeoutW(
            0xFFFF,  # HWND_BROADCAST
            0x001D,  # WM_FONTCHANGE
            0,
            0,
            0x0002,  # SMTO_ABORTIFHUNG
            1000,
            ctypes.byref(result),
        )
    except Exception:  # noqa: BLE001
        pass


def _register_font(path: Path, registry_name: str) -> bool:
    if not _IS_WINDOWS:
        return False
    try:
        import winreg

        key = winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER,
            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts",
            0,
            winreg.KEY_SET_VALUE,
        )
        try:
            winreg.SetValueEx(key, registry_name, 0, winreg.REG_SZ, str(path))
        finally:
            winreg.CloseKey(key)
        return True
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("Could not register font %s: %s", path.name, exc)
        return False


def ensure_fonts() -> None:
    """Install bundled fonts that the system does not already have.

    Runs before Tk is created so `tkfont.families` sees the newly registered
    faces.  Errors are logged and never shown to the user.
    """
    source_dir = paths.fonts_dir()
    if not source_dir.is_dir():
        return
    windows_fonts, user_fonts = _font_dirs()
    try:
        user_fonts.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    added_any = False
    for group in _FONT_REGS:
        for filename, registry_name in group["files"]:
            source = source_dir / filename
            if not source.is_file():
                continue
            system_copy = windows_fonts / filename
            if system_copy.is_file():
                # The typeface is already supplied by Windows.
                continue
            user_copy = user_fonts / filename
            try:
                same = user_copy.is_file() and user_copy.stat().st_size == source.stat().st_size
                if not same:
                    shutil.copyfile(source, user_copy)
                    LOGGER.info("Installed bundled font %s -> %s", filename, user_copy)
            except OSError as exc:
                LOGGER.warning("Could not copy font %s: %s", filename, exc)
                # Still make it available for this process if possible.
                if _add_font_resource(source):
                    added_any = True
                continue
            if same:
                # Already present in the per-user font directory; just make
                # sure this process has it.  Rewriting the registry key is not
                # necessary and can be blocked by managed Windows profiles.
                if _add_font_resource(user_copy):
                    added_any = True
                continue
            _register_font(user_copy, registry_name)
            if _add_font_resource(user_copy):
                added_any = True
    if added_any:
        _broadcast_font_change()
