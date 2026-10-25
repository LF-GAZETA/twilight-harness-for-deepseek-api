#!/usr/bin/env python3
"""UI automation + Win32 probing helpers for the Twilight Harness QA suite.

This module is deliberately *black box*: the SUT (``twilight-harness.exe``) is
copied to a temp folder, pointed at ``mock_deepseek.py`` only via its own
``api-key.ini``, launched as a normal user would launch it, and inspected via
screen pixels, Win32 window metadata and user-level input (pyautogui).

It is imported by ``run_e2e.py`` and ``selftest_harness.py``.  It never writes
to the delivery folder (``twilight-harness/``); only temp copies are mutated.

Requires: bundled Python with pyautogui, Pillow, numpy (all present in
``C:\\Users\\User\\.dsh\\dsh-runtimes\\dsh-primary-runtime\\dependencies\\python``).
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional, Sequence

# --------------------------------------------------------------------------- #
# Paths / constants
# --------------------------------------------------------------------------- #
HERE = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.dirname(HERE)
DELIVERY_DIR = os.path.join(WORKSPACE, "twilight-harness")
DELIVERY_EXE = "twilight-harness.exe"
DELIVERY_INI = "api-key.ini"
DELIVERY_CHARACTER = "twilight-character.md"
DELIVERY_FILES = (DELIVERY_INI, DELIVERY_EXE, DELIVERY_CHARACTER)

ARTIFACTS_DIR = os.path.join(HERE, "artifacts")
SCREENSHOTS_DIR = os.path.join(HERE, "screenshots")

BUNDLED_PYTHON = (
    r"C:\Users\User\.dsh\dsh-runtimes\dsh-primary-runtime"
    r"\dependencies\python\python.exe"
)

APP_TITLE = "Twilight Harness \u00b7 \u7d2b\u60a6"   # "Twilight Harness · 紫悦"
APP_TITLE_CORE = "Twilight Harness"
APP_TITLE_CN = "\u7d2b\u60a6"                       # 紫悦
APP_TITLE_VARIANTS = (
    APP_TITLE,
    "Twilight Harness \u00b7 \u7d2b\u60a6",
    "Twilight Harness \u2022 \u7d2b\u60a6",
    "Twilight Harness - \u7d2b\u60a6",
)

# The 24 language / dialect options the app is expected to offer (from the
# project requirements).  Used only for tolerant "a language appears in the
# request" assertions, never for driving the dropdown.
LANGUAGE_TOKENS = [
    "上海话", "广东粤语", "东北话", "四川话", "新疆话", "普通话", "北京官话",
    "港澳粤语", "台湾话", "新加坡及马来西亚地区华语",
    "美式英语", "英式英语",
    "法国方言", "加拿大魁北克方言",
    "德国方言", "奥地利方言",
    "意大利语", "西班牙语", "葡萄牙方言", "巴西方言",
    "日语", "韩语", "阿拉伯语", "希伯来语",
]
LANGUAGE_TOKENS_EN = [
    "English", "Chinese", "Mandarin", "Cantonese", "Japanese", "Korean",
    "Arabic", "Hebrew", "French", "German", "Italian", "Spanish",
    "Portuguese", "Shanghainese", "Sichuanese",
]

DEFAULT_MODEL = "deepseek-reasoner"
DEFAULT_TEST_KEY = "sk-test-qa"


class BuildNotReady(RuntimeError):
    """Raised when the delivery folder does not exist / is incomplete."""


class ProbeError(RuntimeError):
    """Raised when a UI probing primitive cannot do its job."""


# --------------------------------------------------------------------------- #
# DPI awareness (must happen before PIL ImageGrab / pyautogui / window rects)
# --------------------------------------------------------------------------- #
def _enable_dpi_awareness():
    try:  # Windows 10 1703+: per-monitor v2
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return "per_monitor_v2"
    except Exception:
        pass
    try:  # Windows 8.1+
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return "per_monitor"
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
        return "system"
    except Exception:
        return "none"


DPI_MODE = _enable_dpi_awareness()

import numpy as np  # noqa: E402  (after DPI setup on purpose)
from PIL import Image, ImageGrab  # noqa: E402

try:
    import pyautogui  # noqa: E402
    pyautogui.FAILSAFE = False
    pyautogui.PAUSE = 0.06
except Exception as _exc:  # pragma: no cover - reported by selftest
    pyautogui = None
    _PYAUTOGUI_IMPORT_ERROR = _exc
else:
    _PYAUTOGUI_IMPORT_ERROR = None

try:
    import pyperclip  # noqa: E402
except Exception:  # pragma: no cover
    pyperclip = None


# --------------------------------------------------------------------------- #
# Win32 bindings
# --------------------------------------------------------------------------- #
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

GWL_STYLE = -16
GWL_EXSTYLE = -20

WS_CAPTION = 0x00C00000
WS_SYSMENU = 0x00080000
WS_THICKFRAME = 0x00040000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
WS_BORDER = 0x00800000
WS_DLGFRAME = 0x00400000
WS_EX_APPWINDOW = 0x00040000
WS_EX_TOOLWINDOW = 0x00000080

SW_MINIMIZE = 6
SW_RESTORE = 9
SW_MAXIMIZE = 3
WM_CLOSE = 0x0010
GA_ROOT = 2
GW_OWNER = 4

HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040

MONITOR_DEFAULTTONEAREST = 2


class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

    def as_tuple(self):
        return (self.left, self.top, self.right, self.bottom)

    @property
    def width(self):
        return self.right - self.left

    @property
    def height(self):
        return self.bottom - self.top


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD),
                ("rcMonitor", RECT),
                ("rcWork", RECT),
                ("dwFlags", wintypes.DWORD)]


user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindow.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL
user32.IsZoomed.argtypes = [wintypes.HWND]
user32.IsZoomed.restype = wintypes.BOOL
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
user32.GetClientRect.restype = wintypes.BOOL
user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(POINT)]
user32.ClientToScreen.restype = wintypes.BOOL
user32.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(POINT)]
user32.ScreenToClient.restype = wintypes.BOOL
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                            ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetForegroundWindow.restype = wintypes.HWND
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.BringWindowToTop.restype = wintypes.BOOL
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD,
                                     wintypes.BOOL]
user32.AttachThreadInput.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                ctypes.c_uint]
user32.SetWindowPos.restype = wintypes.BOOL
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.restype = wintypes.BOOL
user32.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
user32.GetAncestor.restype = wintypes.HWND
user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.MonitorFromWindow.restype = wintypes.HMONITOR
user32.WindowFromPoint.argtypes = [POINT]
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR,
                                   ctypes.POINTER(MONITORINFO)]
user32.GetMonitorInfoW.restype = wintypes.BOOL
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int
kernel32.GetCurrentThreadId.restype = wintypes.DWORD

# --- process table (Toolhelp32) for strict "our launched process tree" logic --
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = 0xFFFFFFFFFFFFFFFF


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


try:
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD,
                                                  wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE,
                                         ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE,
                                        ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
except AttributeError:  # pragma: no cover - non-Windows
    pass
try:
    user32.GetDpiForWindow.argtypes = [wintypes.HWND]
    user32.GetDpiForWindow.restype = wintypes.UINT
except AttributeError:  # pragma: no cover - very old Windows
    pass
try:
    user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
except AttributeError:  # pragma: no cover
    pass


# --------------------------------------------------------------------------- #
# Generic helpers
# --------------------------------------------------------------------------- #
def ensure_dir(path):
    os.makedirs(path, exist_ok=True)
    return path


def sha256_file(path, chunk=1 << 20):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def wait_until(predicate, timeout=10.0, interval=0.1, desc="condition",
               on_timeout=None):
    """Poll ``predicate`` until truthy; returns (ok, last_value).

    On timeout returns (False, last_value) unless ``on_timeout`` is a string,
    in which case it raises ``ProbeError`` with that message.
    """
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = predicate()
        except Exception as exc:  # transient Win32 races are expected
            last = exc
        else:
            if last:
                return True, last
        time.sleep(interval)
    if isinstance(on_timeout, str):
        raise ProbeError(on_timeout)
    return False, last


# --------------------------------------------------------------------------- #
# Process tree helpers (strict ownership: only the process we launched)
# --------------------------------------------------------------------------- #
def process_table():
    """Return [{pid, ppid, exe}] for all processes, or [] on failure."""
    entries = []
    try:
        snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    except Exception:
        return entries
    if not snapshot:
        return entries
    try:
        if int(snapshot) == INVALID_HANDLE_VALUE:
            return entries
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            entries.append({
                "pid": int(entry.th32ProcessID),
                "ppid": int(entry.th32ParentProcessID),
                "exe": entry.szExeFile,
            })
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    except Exception:
        pass
    finally:
        try:
            kernel32.CloseHandle(snapshot)
        except Exception:
            pass
    return entries


def process_descendants(pid, entries=None):
    """Set of ``pid`` plus all descendant pids (PyInstaller onefile support)."""
    if not pid:
        return set()
    table = entries if entries is not None else process_table()
    children = {}
    for row in table:
        children.setdefault(row["ppid"], set()).add(row["pid"])
    result = {int(pid)}
    frontier = [int(pid)]
    while frontier:
        current = frontier.pop()
        for child in children.get(current, ()):
            if child not in result:
                result.add(child)
                frontier.append(child)
    return result


def process_details(pids):
    """Human-readable diagnostics for a set of pids."""
    table = process_table()
    out = []
    for row in table:
        if row["pid"] in pids:
            out.append(row)
    return sorted(out, key=lambda row: row["pid"])


def sleep(seconds):
    time.sleep(max(0.0, seconds))


# --------------------------------------------------------------------------- #
# Window discovery / metadata
# --------------------------------------------------------------------------- #
def _window_text(hwnd):
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _window_class(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _window_pid(hwnd):
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def enum_windows(visible_only=False):
    """Return raw window info for every top-level window."""
    found = []

    def _callback(hwnd, _lparam):
        if visible_only and not user32.IsWindowVisible(hwnd):
            return True
        title = _window_text(hwnd)
        found.append({
            "hwnd": int(hwnd),
            "title": title,
            "pid": _window_pid(hwnd),
            "class_name": _window_class(hwnd),
            "visible": bool(user32.IsWindowVisible(hwnd)),
            "iconic": bool(user32.IsIconic(hwnd)),
        })
        return True

    user32.EnumWindows(WNDENUMPROC(_callback), 0)
    return found


@dataclass
class AppWindow:
    hwnd: int
    title: str = ""
    pid: int = 0
    class_name: str = ""

    # -- live metadata ---------------------------------------------------- #
    @property
    def alive(self):
        return bool(user32.IsWindow(self.hwnd)) if self.hwnd else False

    @property
    def visible(self):
        return bool(user32.IsWindowVisible(self.hwnd))

    @property
    def iconic(self):
        return bool(user32.IsIconic(self.hwnd))

    @property
    def zoomed(self):
        return bool(user32.IsZoomed(self.hwnd))

    @property
    def rect(self):
        r = RECT()
        if not user32.GetWindowRect(self.hwnd, ctypes.byref(r)):
            raise ctypes.WinError(ctypes.get_last_error())
        return r.as_tuple()

    @property
    def size(self):
        r = self.rect
        return (r[2] - r[0], r[3] - r[1])

    @property
    def client_origin(self):
        p = POINT(0, 0)
        if not user32.ClientToScreen(self.hwnd, ctypes.byref(p)):
            raise ctypes.WinError(ctypes.get_last_error())
        return (p.x, p.y)

    @property
    def client_rect(self):
        """(left, top, right, bottom) in screen coordinates."""
        r = RECT()
        user32.GetClientRect(self.hwnd, ctypes.byref(r))
        ox, oy = self.client_origin
        return (ox + r.left, oy + r.top, ox + r.right, oy + r.bottom)

    @property
    def client_size(self):
        r = RECT()
        user32.GetClientRect(self.hwnd, ctypes.byref(r))
        return (r.right - r.left, r.bottom - r.top)

    @property
    def dpi(self):
        try:
            dpi = user32.GetDpiForWindow(self.hwnd)
            return int(dpi) if dpi else 96
        except Exception:
            return 96

    @property
    def scale(self):
        return self.dpi / 96.0

    def _style(self):
        return user32.GetWindowLongW(self.hwnd, GWL_STYLE) & 0xFFFFFFFF

    def _exstyle(self):
        return user32.GetWindowLongW(self.hwnd, GWL_EXSTYLE) & 0xFFFFFFFF

    @property
    def style(self):
        return self._style()

    @property
    def exstyle(self):
        return self._exstyle()

    @property
    def style_flags(self):
        style = self._style()
        return {
            "style_hex": "0x%08X" % style,
            "exstyle_hex": "0x%08X" % self._exstyle(),
            "has_caption": bool(style & WS_CAPTION),
            "has_border": bool(style & WS_BORDER),
            "has_dlgframe": bool(style & WS_DLGFRAME),
            "has_sysmenu": bool(style & WS_SYSMENU),
            "has_thickframe": bool(style & WS_THICKFRAME),
            "has_minimizebox": bool(style & WS_MINIMIZEBOX),
            "has_maximizebox": bool(style & WS_MAXIMIZEBOX),
            "frameless": not bool(style & WS_CAPTION),
            "appwindow": bool(self._exstyle() & WS_EX_APPWINDOW),
        }

    def client_to_screen(self, x, y):
        p = POINT(int(x), int(y))
        if not user32.ClientToScreen(self.hwnd, ctypes.byref(p)):
            raise ctypes.WinError(ctypes.get_last_error())
        return (p.x, p.y)

    # -- window actions --------------------------------------------------- #
    def restore(self):
        return bool(user32.ShowWindow(self.hwnd, SW_RESTORE))

    def minimize_win32(self):
        return bool(user32.ShowWindow(self.hwnd, SW_MINIMIZE))

    def maximize_win32(self):
        return bool(user32.ShowWindow(self.hwnd, SW_MAXIMIZE))

    def post_close(self):
        return bool(user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0))

    def foreground(self, settle=0.12, retries=4):
        return bring_to_foreground(self, settle=settle, retries=retries)

    @property
    def is_foreground(self):
        return int(user32.GetForegroundWindow()) == self.hwnd

    def work_area(self):
        """Monitor work area as (left, top, right, bottom)."""
        mon = user32.MonitorFromWindow(self.hwnd, MONITOR_DEFAULTTONEAREST)
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if user32.GetMonitorInfoW(mon, ctypes.byref(info)):
            return info.rcWork.as_tuple()
        return (0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))

    # -- description ------------------------------------------------------ #
    def describe(self):
        d = {
            "hwnd": self.hwnd,
            "title": self.title,
            "pid": self.pid,
            "class_name": self.class_name,
            "rect": self.rect,
            "size": self.size,
            "client_rect": self.client_rect,
            "client_size": self.client_size,
            "dpi": self.dpi,
            "scale": round(self.scale, 4),
            "logical_size": (round(self.size[0] / self.scale, 2),
                             round(self.size[1] / self.scale, 2)),
            "logical_client_size": (round(self.client_size[0] / self.scale, 2),
                                    round(self.client_size[1] / self.scale, 2)),
            "iconic": self.iconic,
            "zoomed": self.zoomed,
            "foreground": self.is_foreground,
            "work_area": self.work_area(),
        }
        d.update(self.style_flags)
        return d


def window_exists(hwnd):
    return bool(hwnd) and bool(user32.IsWindow(hwnd))


def snapshot_window_hwnds():
    """Set of every current top-level window handle (pre-launch baseline)."""
    return {info["hwnd"] for info in enum_windows(visible_only=False)}


def _matches_app(title):
    if APP_TITLE in title:
        return 3
    if APP_TITLE_CORE in title and APP_TITLE_CN in title:
        return 2
    if APP_TITLE_CORE in title:
        return 1
    return 0


def find_app_window(title_contains=APP_TITLE_CORE, pid=None, timeout=20.0,
                    interval=0.15, require_visible=True,
                    exclude_hwnds=None, allowed_pids=None,
                    pid_provider=None):
    """Wait for *our* app's top-level window and return an ``AppWindow``.

    Ownership is strict: when ``pid`` is given, only windows whose process is
    that pid **or one of its descendants** are considered (PyInstaller onefile
    bootloader spawns the real app as a child).  A window belonging to some
    other copy of the app on the shared desktop can never be adopted, even if
    its title matches exactly.  ``exclude_hwnds`` adds a pre-launch hwnd
    snapshot as a second guard.
    """
    exclude = set(exclude_hwnds or ())
    base_pid = int(pid) if pid else None

    def _allowed():
        if pid_provider is not None:
            return set(pid_provider())
        if allowed_pids is not None:
            return set(allowed_pids)
        if base_pid is not None:
            return process_descendants(base_pid)
        return None

    def _find():
        allowed = _allowed()
        candidates = []
        for info in enum_windows(visible_only=False):
            if info["hwnd"] in exclude:
                continue
            if require_visible and not info["visible"]:
                continue
            if allowed is not None and info["pid"] not in allowed:
                continue
            score = 0
            title_hit = bool(title_contains and title_contains in info["title"])
            if title_hit:
                score += 10
            if APP_TITLE in info["title"]:
                score += 5
            if base_pid is not None and info["pid"] == base_pid:
                score += 8
            if info["class_name"] in ("TkTopLevel", "TkChild"):
                score += 1
            if score:
                candidates.append((score, info))
        if not candidates:
            return None
        candidates.sort(key=lambda item: (
            -item[0], not item[1]["visible"], item[1]["hwnd"]))
        info = candidates[0][1]
        return AppWindow(hwnd=info["hwnd"], title=info["title"],
                         pid=info["pid"], class_name=info["class_name"])

    deadline = time.time() + timeout
    while True:
        value = _find()
        if value is not None:
            break
        if time.time() >= deadline:
            raise ProbeError(
                "app window %r not found within %.1fs for launched pid=%s "
                "(process tree=%r); other visible titles=%r"
                % (title_contains, timeout, base_pid,
                   process_details(process_descendants(base_pid))
                   if base_pid else [],
                   [w["title"] for w in enum_windows(visible_only=True)
                    if APP_TITLE_CORE in w["title"]]))
        time.sleep(interval)
    # final ownership assertion (defence in depth)
    if base_pid is not None:
        tree = process_descendants(base_pid)
        if value.pid not in tree:
            raise ProbeError(
                "refusing to use window pid=%s title=%r: it is not in the "
                "launched process tree %r (launched pid=%s)"
                % (value.pid, value.title, sorted(tree), base_pid))
    # refresh cached metadata right after discovery
    value.title = _window_text(value.hwnd)
    value.class_name = _window_class(value.hwnd)
    return value


def find_windows_for_pid(pid):
    return [w for w in enum_windows() if w["pid"] == pid]


# --------------------------------------------------------------------------- #
# Foreground / input
# --------------------------------------------------------------------------- #
def bring_to_foreground(win, settle=0.12, retries=4):
    """Best-effort SetForegroundWindow that survives foreground lock rules."""
    if not win.alive:
        return False
    for attempt in range(retries):
        if win.iconic:
            win.restore()
            sleep(0.25)
        if win.is_foreground:
            return True
        fg = int(user32.GetForegroundWindow())
        cur_tid = kernel32.GetCurrentThreadId()
        fg_tid = 0
        if fg:
            fg_tid = int(user32.GetWindowThreadProcessId(
                fg, ctypes.byref(wintypes.DWORD(0))))
        attached = False
        try:
            if fg_tid and fg_tid != cur_tid:
                attached = bool(user32.AttachThreadInput(cur_tid, fg_tid,
                                                         True))
            user32.BringWindowToTop(win.hwnd)
            user32.SetForegroundWindow(win.hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(cur_tid, fg_tid, False)
        sleep(settle)
        if win.is_foreground:
            return True
        if attempt == 1 and pyautogui is not None:
            # classic foreground-lock workaround: an ALT tap yields permission
            try:
                pyautogui.press("alt")
            except Exception:
                pass
    return win.is_foreground


def set_topmost(win, enabled=True, settle=0.1):
    """Pin the SUT window above other apps so QA screenshots/clicks are clean.

    This only changes z-order for the duration of the test; it does not alter
    the app's own behaviour and is reverted on close/termination.
    """
    if not win.alive:
        return False
    insert_after = HWND_TOPMOST if enabled else HWND_NOTOPMOST
    ok = bool(user32.SetWindowPos(
        win.hwnd, wintypes.HWND(insert_after), 0, 0, 0, 0,
        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW))
    sleep(settle)
    return ok


def root_window_at(x, y):
    """Top-level window handle under a screen point (0 when none)."""
    hwnd = user32.WindowFromPoint(POINT(int(x), int(y)))
    if not hwnd:
        return 0
    root = user32.GetAncestor(hwnd, GA_ROOT)
    return int(root) if root else int(hwnd)


def is_own_window(root_hwnd, win):
    """True when a root hwnd belongs to the SUT process (or a child window)."""
    if not root_hwnd:
        return True
    if root_hwnd == win.hwnd:
        return True
    return _window_pid(root_hwnd) == win.pid


def window_coverage(win, samples=None):
    """Check whether another app's window covers part of the SUT window.

    Windows belonging to the SUT process itself (e.g. a combobox popup) do not
    count as occlusion.
    """
    if not win.alive:
        return {"covered": True, "foreign": [{"reason": "window gone"}],
                "samples": 0}
    width, height = win.client_size
    if width < 10 or height < 10:
        return {"covered": True, "foreign": [{"reason": "degenerate size"}],
                "samples": 0}
    ox, oy = win.client_origin
    fractions = samples or [(0.5, 0.5), (0.25, 0.25), (0.75, 0.25),
                            (0.5, 0.75), (0.12, 0.5), (0.88, 0.5)]
    foreign = []
    for fx, fy in fractions:
        px, py = int(ox + fx * width), int(oy + fy * height)
        root = root_window_at(px, py)
        if not is_own_window(root, win):
            foreign.append({"point": [px, py], "hwnd": root,
                            "title": _window_text(root)[:80]})
    return {"covered": bool(foreign), "foreign": foreign,
            "samples": len(fractions)}


def ensure_usable(win, retries=4, settle=0.2):
    """Bring the SUT to front above everything, or raise ProbeError if occluded.

    QA must never type into or click through another application's window.
    """
    last = None
    for attempt in range(retries):
        bring_to_foreground(win, retries=2)
        set_topmost(win, True)
        bring_to_foreground(win, retries=2)
        last = window_coverage(win)
        if not last["covered"]:
            return {"ok": True, "attempt": attempt, "coverage": last,
                    "foreground": win.is_foreground}
        sleep(settle)
    raise ProbeError(
        "SUT window is occluded by another window; refusing to interact. "
        "coverage=%r" % (last,))


def ensure_foreground(win, retries=5):
    """Make sure the SUT truly owns keyboard focus before typing."""
    for _attempt in range(retries):
        bring_to_foreground(win, retries=2)
        if win.is_foreground:
            return True
        sleep(0.15)
    raise ProbeError("SUT window lost keyboard focus to another window; "
                     "refusing to type. fg_hwnd=%s expected=%s"
                     % (int(user32.GetForegroundWindow()), win.hwnd))


def _require_pyautogui():
    if pyautogui is None:
        raise ProbeError("pyautogui unavailable: %r" % _PYAUTOGUI_IMPORT_ERROR)


def click_screen(x, y, clicks=1, button="left", interval=0.08,
                 settle=0.18):
    _require_pyautogui()
    pyautogui.click(x=int(round(x)), y=int(round(y)), clicks=clicks,
                    interval=interval, button=button)
    sleep(settle)


def click_client(win, x, y, clicks=1, button="left", settle=0.2,
                 foreground=True):
    """Click a client-relative point, refusing to click through other apps."""
    if foreground:
        bring_to_foreground(win)
    sx, sy = win.client_to_screen(x, y)
    root = root_window_at(sx, sy)
    if not is_own_window(root, win):
        set_topmost(win, True)
        bring_to_foreground(win)
        sleep(0.15)
        root = root_window_at(sx, sy)
        if not is_own_window(root, win):
            raise ProbeError(
                "refusing to click through foreign window: target screen=(%d,"
                "%d) client=(%d,%d) hwnd=%s title=%r (SUT hwnd=%s pid=%s)"
                % (sx, sy, x, y, root, _window_text(root)[:80],
                   win.hwnd, win.pid))
    click_screen(sx, sy, clicks=clicks, button=button, settle=settle)
    return (sx, sy)


def double_click_client(win, x, y, settle=0.3):
    if pyautogui is None:
        raise ProbeError("pyautogui unavailable")
    bring_to_foreground(win)
    sx, sy = win.client_to_screen(x, y)
    pyautogui.doubleClick(x=sx, y=sy)
    sleep(settle)
    return (sx, sy)


def type_text(text, interval=0.02, settle=0.1):
    """Type ASCII text via keystrokes; non-ASCII via clipboard paste."""
    _require_pyautogui()
    if all(ord(ch) < 128 for ch in text):
        pyautogui.write(text, interval=interval)
    else:
        paste_text(text)
    sleep(settle)


def paste_text(text, settle=0.15):
    _require_pyautogui()
    if pyperclip is not None:
        pyperclip.copy(text)
        sleep(0.05)
        pyautogui.hotkey("ctrl", "v")
    else:  # pragma: no cover - clipboard module is part of the runtime
        raise ProbeError("pyperclip unavailable; cannot type non-ASCII text")
    sleep(settle)


def press(*keys, interval=0.05, settle=0.1):
    _require_pyautogui()
    for key in keys:
        pyautogui.press(key, interval=interval)
    sleep(settle)


def hotkey(*keys, settle=0.1):
    _require_pyautogui()
    pyautogui.hotkey(*keys)
    sleep(settle)


def drag(from_screen, to_screen, duration=0.55, button="left", hold=0.08,
         steps=None):
    """Press at ``from_screen``, move to ``to_screen``, release."""
    _require_pyautogui()
    fx, fy = int(round(from_screen[0])), int(round(from_screen[1]))
    tx, ty = int(round(to_screen[0])), int(round(to_screen[1]))
    pyautogui.moveTo(fx, fy, duration=0.12)
    sleep(hold)
    pyautogui.mouseDown(button=button)
    sleep(hold)
    if steps:
        for i in range(1, steps + 1):
            x = fx + (tx - fx) * i / steps
            y = fy + (ty - fy) * i / steps
            pyautogui.moveTo(int(round(x)), int(round(y)),
                             duration=duration / steps)
            sleep(0.01)
    else:
        pyautogui.moveTo(tx, ty, duration=duration)
    sleep(hold)
    pyautogui.mouseUp(button=button)
    sleep(0.25)


def drag_client(win, from_client, delta=(0, 0), to_client=None, **kwargs):
    """Drag inside the SUT window, refusing to drag through other apps."""
    bring_to_foreground(win)
    set_topmost(win, True)
    bring_to_foreground(win)
    fx, fy = from_client
    if to_client is None:
        to_client = (fx + delta[0], fy + delta[1])
    from_screen = win.client_to_screen(fx, fy)
    to_screen = win.client_to_screen(*to_client)
    if not is_own_window(root_window_at(*from_screen), win):
        raise ProbeError(
            "refusing to drag through foreign window at screen=%r "
            "(SUT hwnd=%s pid=%s)" % (from_screen, win.hwnd, win.pid))
    drag(from_screen, to_screen, **kwargs)
    return (from_screen, to_screen)


# --------------------------------------------------------------------------- #
# Screenshots
# --------------------------------------------------------------------------- #
def grab_screen():
    return ImageGrab.grab()


def save_screen(path):
    ensure_dir(os.path.dirname(path))
    img = grab_screen()
    img.save(path)
    return path


def _crop(img, box):
    left, top, right, bottom = box
    width, height = img.size
    left = max(0, min(int(left), width - 1))
    top = max(0, min(int(top), height - 1))
    right = max(left + 1, min(int(right), width))
    bottom = max(top + 1, min(int(bottom), height))
    return img.crop((left, top, right, bottom))


def assert_capturable(win):
    """Raise ProbeError if the SUT window is occluded by another application."""
    if not win.alive:
        raise ProbeError("SUT window is gone; cannot capture screenshot")
    width, height = win.client_size
    center = win.client_to_screen(width // 2, height // 2)
    root = root_window_at(*center)
    if not is_own_window(root, win):
        set_topmost(win, True)
        bring_to_foreground(win)
        sleep(0.15)
        root = root_window_at(*center)
        if not is_own_window(root, win):
            raise ProbeError(
                "refusing to capture occluded SUT window: hwnd under centre "
                "%s title=%r is not our window (SUT hwnd=%s pid=%s)"
                % (root, _window_text(root)[:80], win.hwnd, win.pid))
    return True


def grab_window(win, region=None, verify=True):
    """Screenshot of the window's client area.

    ``region`` is in client-relative coordinates: (x0, y0, x1, y1).
    ``verify`` refuses to capture if another application covers the window
    (which would produce misleading evidence).
    """
    if verify:
        assert_capturable(win)
    img = grab_screen()
    left, top, right, bottom = win.client_rect
    return _crop(img, (left, top, right, bottom))


def grab_window_region(win, region):
    img = grab_screen()
    left, top, right, bottom = win.client_rect
    x0, y0, x1, y1 = region
    return _crop(img, (left + x0, top + y0, left + x1, top + y1))


def save_window_shot(win, path, region=None):
    ensure_dir(os.path.dirname(path))
    img = grab_window(win, region=region)
    img.save(path)
    return path


def save_window_region_shot(win, path, region):
    ensure_dir(os.path.dirname(path))
    img = grab_window_region(win, region)
    img.save(path)
    return path


# --------------------------------------------------------------------------- #
# Pixel analysis
# --------------------------------------------------------------------------- #
def _as_array(img):
    return np.asarray(img.convert("RGB"), dtype=np.uint8)


def color_stats(img):
    """Fractions of white / black / light-purple pixels plus corner samples."""
    arr = _as_array(img)
    h, w, _ = arr.shape
    flat = arr.reshape(-1, 3).astype(np.int16)
    r, g, b = flat[:, 0], flat[:, 1], flat[:, 2]
    maxc = flat.max(axis=1)
    minc = flat.min(axis=1)
    spread = maxc - minc
    lum = (0.299 * r + 0.587 * g + 0.114 * b)
    total = float(flat.shape[0])

    near_white = (minc >= 244)
    pure_white = (minc == 255) & (maxc == 255)
    dark = lum < 110
    black_text = dark & (spread <= 28)
    purple = ((b > g + 6) & (r > g + 2) & (b >= r - 10) &
              (b >= 110) & (r >= 90) & (spread >= 20))
    light_purple = purple & (lum >= 140)
    redish = ((r > g + 45) & (r > b + 30) & (r >= 120) &
              (lum > 50) & (lum < 230))

    corners = []
    for cy, cx in ((0, 0), (0, w - 1), (h - 1, 0), (h - 1, w - 1),
                   (2, 2), (2, w - 3), (h - 3, 2), (h - 3, w - 3)):
        corners.append(tuple(int(v) for v in arr[cy, cx]))

    def frac(mask):
        return round(float(mask.sum()) / total, 6)

    return {
        "width": w,
        "height": h,
        "near_white_frac": frac(near_white),
        "pure_white_frac": frac(pure_white),
        "dark_frac": frac(dark),
        "black_text_px": int(black_text.sum()),
        "black_text_frac": frac(black_text),
        "purple_px": int(purple.sum()),
        "purple_frac": frac(purple),
        "light_purple_px": int(light_purple.sum()),
        "light_purple_frac": frac(light_purple),
        "light_purple_mean_hex": (_mean_hex(flat[light_purple])
                                  if light_purple.any() else None),
        "redish_px": int(redish.sum()),
        "redish_frac": frac(redish),
        "mean_hex": _mean_hex(flat),
        "corner_pixels": corners,
        "corners_white_frac": round(
            sum(1 for px in corners if min(px) >= 244) / float(len(corners)), 4),
    }


def _mean_hex(pixels):
    arr = np.asarray(pixels)
    if arr.size == 0:
        return None
    if arr.ndim == 1:
        mean = arr
    else:
        mean = arr.mean(axis=0)
    return "#%02X%02X%02X" % tuple(int(round(v)) for v in mean[:3])


def dominant_colors(img, top=8, quant=16):
    arr = _as_array(img)
    flat = arr.reshape(-1, 3)
    q = (flat // quant) * quant
    codes = (q[:, 0].astype(np.int64) << 16) | (q[:, 1].astype(np.int64) << 8) | q[:, 2].astype(np.int64)
    values, counts = np.unique(codes, return_counts=True)
    order = np.argsort(-counts)[:top]
    out = []
    total = float(len(codes))
    for idx in order:
        code = int(values[idx])
        rgb = ((code >> 16) & 0xFF, (code >> 8) & 0xFF, code & 0xFF)
        out.append({"hex": "#%02X%02X%02X" % rgb,
                    "count": int(counts[idx]),
                    "frac": round(float(counts[idx]) / total, 5)})
    return out


def text_pixels(img, box=None, lum_threshold=140):
    """Count dark-ish pixels (proxy for rendered text)."""
    if box is not None:
        img = _crop(img, box)
    arr = _as_array(img).reshape(-1, 3).astype(np.int16)
    lum = 0.299 * arr[:, 0] + 0.587 * arr[:, 1] + 0.114 * arr[:, 2]
    return int((lum < lum_threshold).sum())


def dark_mask(img):
    arr = _as_array(img).astype(np.int16)
    lum = (0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1]
           + 0.114 * arr[:, :, 2])
    return lum < 200


def bbox_of_mask(mask):
    rows = np.any(mask, axis=1)
    cols = np.any(mask, axis=0)
    if not rows.any() or not cols.any():
        return None
    r_idx = np.where(rows)[0]
    c_idx = np.where(cols)[0]
    return (int(c_idx[0]), int(r_idx[0]),
            int(c_idx[-1]) + 1, int(r_idx[-1]) + 1)


def _purple_mask(img):
    arr = _as_array(img).astype(np.int16)
    r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
    spread = arr.max(axis=2) - arr.min(axis=2)
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    return ((b > g + 6) & (r > g + 2) & (b >= r - 10) &
            (b >= 110) & (r >= 90) & (spread >= 20))


def find_purple_boxes(img, min_width=60, min_height=14, region=None):
    """Row-projection detection of large filled purple areas (e.g. buttons).

    Returns list of dicts {box, center, width, height, fill} sorted by y.
    """
    if region is not None:
        img = _crop(img, region)
    mask = _purple_mask(img)
    if not mask.any():
        return []
    h, w = mask.shape
    row_counts = mask.sum(axis=1)
    threshold = max(min_width * 0.5, 3)
    bands = []
    start = None
    for y in range(h):
        if row_counts[y] >= threshold:
            if start is None:
                start = y
        else:
            if start is not None:
                bands.append((start, y - 1))
                start = None
    if start is not None:
        bands.append((start, h - 1))

    boxes = []
    for y0, y1 in bands:
        band = mask[y0:y1 + 1, :]
        col_counts = band.sum(axis=0)
        xs = np.where(col_counts > 0)[0]
        if len(xs) == 0:
            continue
        x0, x1 = int(xs[0]), int(xs[-1]) + 1
        if (x1 - x0) < min_width or (y1 - y0 + 1) < min_height:
            continue
        boxes.append({
            "box": (x0, y0, x1, y1 + 1),
            "center": ((x0 + x1) // 2, (y0 + y1) // 2),
            "width": x1 - x0,
            "height": y1 - y0 + 1,
            "fill": round(float(band.sum()) / float((y1 - y0 + 1) * (x1 - x0)), 3),
        })
    boxes.sort(key=lambda d: d["box"][1])
    return boxes


def vertical_line_score(img, center_frac=0.5, span=8):
    """Score how strongly a vertical separator line exists near the centre."""
    arr = _as_array(img).astype(np.int16)
    h, w, _ = arr.shape
    cx = int(w * center_frac)
    x0, x1 = max(0, cx - span), min(w, cx + span)
    band = arr[:, x0:x1, :]
    lum = (0.299 * band[:, :, 0] + 0.587 * band[:, :, 1]
           + 0.114 * band[:, :, 2])
    col_scores = (lum < 240).mean(axis=0)
    if col_scores.size == 0:
        return {"score": 0.0, "x": cx, "col_scores": []}
    best = int(np.argmax(col_scores))
    return {
        "score": round(float(col_scores[best]), 4),
        "x": x0 + best,
        "col_scores": [round(float(v), 3) for v in col_scores],
    }


def best_vertical_line(img, x_range=(0.20, 0.80), y_range=(0.12, 0.76),
                       step=2):
    """Find the strongest vertical separator line anywhere in a wide band.

    The chat split is not necessarily at 50 % (observed contract: left pane
    ~37 %, right ~63 %), so a wide scan is required.
    """
    arr = _as_array(img).astype(np.int16)
    h, w, _ = arr.shape
    y0, y1 = int(h * y_range[0]), int(h * y_range[1])
    band = arr[y0:y1, :, :]
    lum = (0.299 * band[:, :, 0] + 0.587 * band[:, :, 1]
           + 0.114 * band[:, :, 2])
    col_scores = (lum < 245).mean(axis=0)
    xs = range(max(1, int(w * x_range[0])), min(w - 1, int(w * x_range[1])),
               max(1, int(step)))
    best_x, best_score = w // 2, 0.0
    for x in xs:
        score = float(col_scores[max(0, x - 1):x + 2].max())
        if score > best_score:
            best_score, best_x = score, x
    return {"x": int(best_x), "score": round(best_score, 4),
            "x_frac": round(best_x / float(w), 4)}


def horizontal_line_score(img, y_frac, span=6, x_range=(0.05, 0.95)):
    """Score how strongly a horizontal separator line exists near ``y_frac``."""
    arr = _as_array(img).astype(np.int16)
    h, w, _ = arr.shape
    cy = int(h * y_frac)
    y0, y1 = max(0, cy - span), min(h, cy + span)
    x0, x1 = int(w * x_range[0]), int(w * x_range[1])
    band = arr[y0:y1, x0:x1, :]
    lum = (0.299 * band[:, :, 0] + 0.587 * band[:, :, 1]
           + 0.114 * band[:, :, 2])
    row_scores = (lum < 240).mean(axis=1)
    if row_scores.size == 0:
        return {"score": 0.0, "y": cy, "row_scores": []}
    best = int(np.argmax(row_scores))
    return {
        "score": round(float(row_scores[best]), 4),
        "y": y0 + best,
        "row_scores": [round(float(v), 3) for v in row_scores],
    }


# --------------------------------------------------------------------------- #
# Layout heuristics (setup vs chat, element locations)
# --------------------------------------------------------------------------- #
def detect_layout(win):
    """Return heuristic layout metrics for the current screen of the app."""
    img = grab_window(win)
    w, h = img.size
    stats = color_stats(img)
    purple_boxes = find_purple_boxes(img)
    divider = best_vertical_line(img)
    layout = {
        "window": win.describe(),
        "image_size": [w, h],
        "colors": stats,
        "dominant_colors": dominant_colors(img),
        "purple_boxes": purple_boxes,
        "divider": divider,
    }

    # Setup screen: form content in the central column, no full-height divider,
    # and (when enabled) a large purple confirm button in the middle band.
    mid_boxes = [b for b in purple_boxes
                 if 0.30 * h <= b["center"][1] <= 0.90 * h
                 and b["width"] >= max(100, 0.12 * w)
                 and b["height"] >= 22]
    central_dark = text_pixels(img, box=(int(0.15 * w), int(0.15 * h),
                                         int(0.85 * w), int(0.95 * h)))
    # Chat screen: faint centre divider (observed at ~37 % of width, coverage
    # can be partial) plus a bottom input area.
    bottom_dark = text_pixels(img, box=(int(0.08 * w), int(0.78 * h),
                                        int(0.92 * w), h))
    has_divider = (divider["score"] >= 0.28
                   and 0.20 <= divider.get("x_frac", 0.5) <= 0.60)
    chat_score = (2 if has_divider else 0) + (1 if bottom_dark >= 40 else 0)
    setup_score = (0 if has_divider else 1) + (1 if central_dark >= 150 else 0)
    if mid_boxes:
        setup_score += 1
    if setup_score > chat_score:
        state = "setup"
    elif chat_score > setup_score:
        state = "chat"
    else:
        state = "unknown"
    layout.update({
        "state": state,
        "setup_score": setup_score,
        "chat_score": chat_score,
        "central_dark_px": central_dark,
        "has_divider": has_divider,
        "mid_purple_boxes": mid_boxes,
        "bottom_dark_px": bottom_dark,
    })

    # Element vertical ordering on the setup screen: key entry < dropdown <
    # confirm button.  We cannot OCR, so we use row-projection of dark pixels.
    layout["row_text_profile"] = _row_profile(img)
    layout["col_text_profile"] = _col_profile(img)
    return layout


def _row_profile(img, threshold=140):
    arr = _as_array(img).astype(np.int16)
    lum = (0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1]
           + 0.114 * arr[:, :, 2])
    rows = (lum < threshold).sum(axis=1)
    return rows


def _col_profile(img, threshold=140):
    arr = _as_array(img).astype(np.int16)
    lum = (0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1]
           + 0.114 * arr[:, :, 2])
    cols = (lum < threshold).sum(axis=0)
    return cols


def text_row_bands(profile, min_pixels=3, gap=3):
    """Group a row profile into bands of rows containing rendered text."""
    bands = []
    start = None
    empty = 0
    for y, value in enumerate(profile):
        if value >= min_pixels:
            if start is None:
                start = y
            empty = 0
        else:
            if start is not None:
                empty += 1
                if empty > gap:
                    bands.append((start, y - empty))
                    start = None
                    empty = 0
    if start is not None:
        bands.append((start, len(profile) - 1 - empty))
    return [(int(a), int(b)) for a, b in bands if b > a]


def titlebar_band(win, ratio=0.055, min_h=28, max_h=48):
    _w, h = win.client_size
    band_h = max(min_h, min(max_h, int(h * ratio)))
    return band_h


def _glyph_runs(band_lum, min_count, gap):
    """Column clusters of non-background pixels inside a titlebar band."""
    mask = band_lum < 215
    col_counts = mask.sum(axis=0)
    runs = []
    start = None
    empty = 0
    for x, count in enumerate(col_counts):
        if count >= min_count:
            if start is None:
                start = x
            empty = 0
        else:
            if start is not None:
                empty += 1
                if empty > gap:
                    runs.append((start, x - empty))
                    start = None
                    empty = 0
    if start is not None:
        runs.append((start, len(col_counts) - 1))
    return [(a, b) for a, b in runs if 3 <= b - a <= 90]


def find_top_right_buttons(win):
    """Detect the three custom titlebar buttons (min/max/close) as glyphs.

    Returns dict with ``centers`` (left to right: minimize, maximize, close),
    ``method`` ('glyphs', 'glyphs-inferred' or 'slot-fallback'),
    ``band_height`` and raw glyph clusters.
    """
    img = grab_window(win)
    w, h = img.size
    band_h = titlebar_band(win)
    offset = max(0, w - 300)
    band = _as_array(img.crop((offset, 0, w, band_h))).astype(np.int16)
    lum = (0.299 * band[:, :, 0] + 0.587 * band[:, :, 1]
           + 0.114 * band[:, :, 2])
    runs = _glyph_runs(lum, 2, 6)
    if len(runs) < 3:
        loose = _glyph_runs(lum, 1, 8)
        if len(loose) > len(runs):
            runs = loose
    runs = [(a, b) for a, b in runs if offset + b >= w - 280]
    xs = [offset + (a + b) // 2 for a, b in runs]
    raw = [(offset + a, offset + b) for a, b in runs]
    method = "slot-fallback"
    centers = []
    if len(xs) >= 3:
        chosen = xs[-3:]
        if (all(w - 290 <= x <= w - 4 for x in chosen)
                and all(10 <= chosen[i + 1] - chosen[i] <= 120
                        for i in range(2))):
            centers = [(x, band_h // 2) for x in chosen]
            method = "glyphs"
    elif len(xs) == 2:
        spacing = xs[1] - xs[0]
        inferred = xs[0] - spacing
        if 10 <= spacing <= 120 and inferred >= w - 290:
            centers = [(inferred, band_h // 2), (xs[0], band_h // 2),
                       (xs[1], band_h // 2)]
            method = "glyphs-inferred"
    if not centers:
        # last resort: three 44px slots hard against the right edge
        slot = 44
        centers = [(w - slot // 2 - slot * (2 - i), band_h // 2)
                   for i in range(3)]
    return {
        "centers": centers,
        "method": method,
        "band_height": band_h,
        "clusters": raw,
    }


def click_titlebar_button(win, index, fallback_slot=True):
    """Click button 0=minimize, 1=maximize, 2=close. Returns evidence dict."""
    info = find_top_right_buttons(win)
    method = info["method"]
    centers = info["centers"]
    if index >= len(centers):
        if not fallback_slot:
            raise ProbeError("cannot locate titlebar button %d" % index)
        slot = 44
        _w, _h = win.client_size
        centers = [( _w - slot // 2 - slot * (2 - i), info["band_height"] // 2)
                   for i in range(3)]
        method = "slot-fallback"
    x, y = centers[index]
    click_client(win, x, y, settle=0.35)
    time.sleep(0.25)
    return {"button_index": index, "clicked_client": (x, y), "method": method,
            "detected": info}


# --------------------------------------------------------------------------- #
# Temp delivery copy + test api-key.ini
# --------------------------------------------------------------------------- #
def delivery_status(delivery_dir=DELIVERY_DIR):
    """Inspect the delivery folder without touching it."""
    status = {
        "delivery_dir": delivery_dir,
        "exists": os.path.isdir(delivery_dir),
        "expected_files": list(DELIVERY_FILES),
        "present_files": [],
        "missing_files": list(DELIVERY_FILES),
        "extra_entries": [],
        "sizes": {},
        "hashes": {},
        "ready": False,
    }
    if not status["exists"]:
        return status
    entries = sorted(os.listdir(delivery_dir))
    status["present_files"] = entries
    status["missing_files"] = [f for f in DELIVERY_FILES
                               if f not in entries]
    status["extra_entries"] = [e for e in entries if e not in DELIVERY_FILES]
    for name in DELIVERY_FILES:
        path = os.path.join(delivery_dir, name)
        if os.path.isfile(path):
            status["sizes"][name] = os.path.getsize(path)
            try:
                status["hashes"][name] = sha256_file(path)
            except OSError as exc:
                status["hashes"][name] = "ERROR:%s" % exc
    status["ready"] = (not status["missing_files"]
                       and os.path.isfile(os.path.join(delivery_dir,
                                                       DELIVERY_EXE)))
    return status


def require_ready_delivery(delivery_dir=DELIVERY_DIR):
    status = delivery_status(delivery_dir)
    if not status["ready"]:
        raise BuildNotReady(
            "BUILD NOT READY: %s is missing or incomplete. present=%r "
            "missing=%r. Re-run once the Lead has packaged the EXE."
            % (delivery_dir, status["present_files"], status["missing_files"]))
    return status


def read_character_md(temp_or_delivery):
    path = os.path.join(temp_or_delivery, DELIVERY_CHARACTER)
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def write_test_ini(folder, base_url, api_key=DEFAULT_TEST_KEY,
                   model=DEFAULT_MODEL):
    """Write the per-copy api-key.ini the app reads on startup."""
    content = (
        "[deepseek]\n"
        "api_key = %s\n"
        "base_url = %s\n"
        "model = %s\n"
    ) % (api_key, base_url, model)
    path = os.path.join(folder, DELIVERY_INI)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)
    return path


def read_ini(folder):
    """Tolerant INI reader: returns {raw, sections, error}."""
    path = os.path.join(folder, DELIVERY_INI)
    result = {"path": path, "exists": os.path.isfile(path), "raw": "",
              "sections": {}, "error": None}
    if not result["exists"]:
        return result
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            result["raw"] = fh.read()
    except OSError as exc:
        result["error"] = "read failed: %s" % exc
        return result
    try:
        import configparser
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(result["raw"])
        result["sections"] = {
            section: dict(parser.items(section))
            for section in parser.sections()
        }
    except Exception as exc:
        result["error"] = "parse failed: %s" % exc
    return result


def ini_api_key(folder):
    info = read_ini(folder)
    section = info["sections"].get("deepseek", {})
    return (section.get("api_key") or "").strip()


@dataclass
class TempDelivery:
    root: str
    source_dir: str
    files: list = field(default_factory=list)
    keep: bool = False

    def path(self, name):
        return os.path.join(self.root, name)

    @property
    def exe(self):
        return self.path(DELIVERY_EXE)

    @property
    def ini(self):
        return self.path(DELIVERY_INI)

    @property
    def character_md(self):
        return self.path(DELIVERY_CHARACTER)

    def list_files(self):
        return sorted(os.listdir(self.root))

    def extra_entries(self):
        return [e for e in self.list_files() if e not in DELIVERY_FILES]

    def write_ini(self, base_url, api_key=DEFAULT_TEST_KEY,
                  model=DEFAULT_MODEL):
        return write_test_ini(self.root, base_url, api_key=api_key,
                              model=model)

    def read_ini(self):
        return read_ini(self.root)

    def api_key(self):
        return ini_api_key(self.root)

    def cleanup(self):
        if self.keep:
            return
        shutil.rmtree(self.root, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.cleanup()


def copy_delivery(delivery_dir=DELIVERY_DIR, keep=False, prefix="twilight-qa-",
                  require=True):
    """Copy the delivered 3-file folder into a fresh temp directory."""
    if require:
        require_ready_delivery(delivery_dir)
    else:
        if not os.path.isdir(delivery_dir):
            raise BuildNotReady("delivery folder missing: %s" % delivery_dir)
    root = tempfile.mkdtemp(prefix=prefix)
    copied = []
    for name in DELIVERY_FILES:
        source = os.path.join(delivery_dir, name)
        if os.path.isfile(source):
            shutil.copy2(source, os.path.join(root, name))
            copied.append(name)
    return TempDelivery(root=root, source_dir=delivery_dir, files=copied,
                        keep=keep)


# --------------------------------------------------------------------------- #
# App process lifecycle
# --------------------------------------------------------------------------- #
@dataclass
class AppProcess:
    proc: subprocess.Popen
    copy_dir: str
    exe_path: str
    stdout_path: Optional[str] = None
    stderr_path: Optional[str] = None
    started_at: float = field(default_factory=time.time)

    @property
    def pid(self):
        return self.proc.pid

    def poll(self):
        return self.proc.poll()

    def running(self):
        return self.proc.poll() is None

    def read_output(self):
        out = {"stdout": "", "stderr": ""}
        for key, path in (("stdout", self.stdout_path),
                          ("stderr", self.stderr_path)):
            if path and os.path.isfile(path):
                try:
                    with open(path, "r", encoding="utf-8",
                              errors="replace") as fh:
                        out[key] = fh.read()[-8000:]
                except OSError:
                    pass
        return out

    def terminate(self, timeout=6.0):
        terminate_process_tree(self.proc.pid, self.proc, timeout=timeout)

    def wait_exit(self, timeout=10.0):
        ok, _ = wait_until(lambda: self.proc.poll() is not None,
                           timeout=timeout, interval=0.1)
        return ok

    def process_tree(self):
        """pids of this process and descendants (PyInstaller onefile child)."""
        try:
            return sorted(process_descendants(self.proc.pid))
        except Exception:
            return [self.proc.pid]

    def process_details(self):
        try:
            return process_details(process_descendants(self.proc.pid))
        except Exception:
            return []

    def describe(self):
        output = self.read_output()
        return {
            "pid": self.proc.pid,
            "exe_path": self.exe_path,
            "copy_dir": self.copy_dir,
            "running": self.running(),
            "returncode": self.proc.poll(),
            "uptime_s": round(time.time() - self.started_at, 3),
            "process_tree": self.process_tree(),
            "process_details": self.process_details(),
            "stdout_tail": output["stdout"][-3000:],
            "stderr_tail": output["stderr"][-3000:],
        }


def launch_app(copy_dir, exe_name=DELIVERY_EXE, extra_env=None,
               artifact_prefix="app", capture_output=True):
    """Launch the delivered EXE from a temp copy, like a normal user."""
    exe_path = os.path.join(copy_dir, exe_name)
    if not os.path.isfile(exe_path):
        raise BuildNotReady("EXE missing in temp copy: %s" % exe_path)
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    if extra_env:
        env.update(extra_env)
    stdout_path = stderr_path = None
    stdout = stderr = subprocess.DEVNULL
    if capture_output:
        ensure_dir(ARTIFACTS_DIR)
        stdout_path = os.path.join(ARTIFACTS_DIR,
                                   "%s_stdout.log" % artifact_prefix)
        stderr_path = os.path.join(ARTIFACTS_DIR,
                                   "%s_stderr.log" % artifact_prefix)
        stdout = open(stdout_path, "wb")
        stderr = open(stderr_path, "wb")
    creationflags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        # The delivered EXE is windowed; this only prevents a stray console
        # for any child process. It does not affect window creation.
        creationflags |= subprocess.CREATE_NO_WINDOW
    try:
        proc = subprocess.Popen(
            [exe_path],
            cwd=copy_dir,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            creationflags=creationflags,
        )
    finally:
        # Popen already inherited the handles; close our copies.
        if capture_output:
            try:
                stdout.close()
            except Exception:
                pass
            try:
                stderr.close()
            except Exception:
                pass
    return AppProcess(proc=proc, copy_dir=copy_dir, exe_path=exe_path,
                      stdout_path=stdout_path, stderr_path=stderr_path)


def terminate_process_tree(pid, proc=None, timeout=6.0):
    """Kill the PyInstaller onefile process tree (parent + child)."""
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True, timeout=20)
    except Exception:
        pass
    if proc is not None:
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=3)
            except Exception:
                pass


def wait_process_exit(proc, timeout=10.0):
    return wait_until(lambda: proc.poll() is not None, timeout=timeout,
                      interval=0.1)[0]


# --------------------------------------------------------------------------- #
# High-level app driving helpers
# --------------------------------------------------------------------------- #
def launch_and_find(copy_dir, timeout=25.0, artifact_prefix="app",
                    title_contains=APP_TITLE_CORE, extra_env=None,
                    topmost=True):
    """Launch temp-copy EXE, wait for its main window, foreground it.

    The window is matched **only** against the launched process tree (recorded
    pid + descendants), never against a title match from another process.
    """
    before = snapshot_window_hwnds()
    app = launch_app(copy_dir, artifact_prefix=artifact_prefix,
                     extra_env=extra_env)
    try:
        win = find_app_window(title_contains=title_contains, pid=app.pid,
                              timeout=timeout, exclude_hwnds=before)
        if win.pid not in app.process_tree():
            raise ProbeError(
                "window pid=%s is not in the launched process tree %r"
                % (win.pid, app.process_tree()))
    except Exception:
        app.terminate()
        raise
    bring_to_foreground(win)
    if topmost:
        set_topmost(win, True)
    win.launched_pid = app.pid
    return app, win


def close_app_via_window(win, app, timeout=10.0):
    """Ask the window to close (WM_CLOSE) and wait for process exit."""
    if win is not None and win.alive:
        win.post_close()
    ok = app.wait_exit(timeout=timeout)
    if not ok:
        app.terminate()
    return {"closed": ok, "returncode": app.poll(),
            "app": app.describe()}


def app_has_quit(app, win=None, timeout=10.0):
    ok, _ = wait_until(
        lambda: app.proc.poll() is not None or
        (win is not None and not win.alive),
        timeout=timeout, interval=0.1)
    return ok


# --------------------------------------------------------------------------- #
# Self diagnostics when run directly
# --------------------------------------------------------------------------- #
def _main(argv):
    import argparse
    parser = argparse.ArgumentParser(description="Twilight Harness QA probe "
                                                 "diagnostics")
    parser.add_argument("--inspect", action="store_true",
                        help="print delivery status + matching windows")
    parser.add_argument("--shot", default=None,
                        help="save a full-desktop screenshot to this path")
    parser.add_argument("--windows", action="store_true",
                        help="list all visible top-level windows")
    args = parser.parse_args(argv)

    print("PROBE dpi_mode=%s screen=%s pyautogui=%s numpy=%s" % (
        DPI_MODE,
        pyautogui.size() if pyautogui is not None else "n/a",
        "ok" if pyautogui is not None else _PYAUTOGUI_IMPORT_ERROR,
        np.__version__,
    ))
    if args.inspect or not (args.shot or args.windows):
        status = delivery_status()
        print("DELIVERY", json.dumps(
            {k: v for k, v in status.items() if k != "hashes"},
            ensure_ascii=False, indent=2))
        print("HASHES", json.dumps(status["hashes"], indent=2))
    if args.windows:
        for info in enum_windows(visible_only=True):
            print("WINDOW pid=%-6s hwnd=%-10s class=%-24s title=%r" % (
                info["pid"], info["hwnd"], info["class_name"],
                info["title"]))
    if args.shot:
        save_screen(args.shot)
        print("SHOT", args.shot)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_main(sys.argv[1:]))
