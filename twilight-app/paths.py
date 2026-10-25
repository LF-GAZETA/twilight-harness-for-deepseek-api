"""Filesystem layout for Twilight Harness.

The delivered app is intentionally location-independent: everything except the
executable lives in sibling folders (`fonts/` and `temp/`).  The PyInstaller
runtime itself also extracts into `temp/` through --runtime-tmpdir.
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path


def resource_base() -> Path:
    """Folder containing the exe and its sibling resources."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def fonts_dir() -> Path:
    base = resource_base()
    for candidate in (base / "fonts", base / "assets" / "fonts"):
        if candidate.is_dir():
            return candidate
    return base / "fonts"


def data_home() -> Path:
    """Per-run temp data folder (temp/api-key.ini, character card, logs)."""
    override = os.environ.get("TWILIGHT_HOME")
    if override:
        home = Path(override).expanduser().resolve()
    else:
        home = resource_base() / "temp"
    try:
        home.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return home


def persistent_dir() -> Path:
    """Persistent per-user data, used to survive deletion of temp/."""
    local = os.environ.get("LOCALAPPDATA")
    if local:
        base = Path(local) / "TwilightHarness"
    else:
        base = Path.home() / "AppData" / "Local" / "TwilightHarness"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return base


def cleanup_runtime_fonts() -> None:
    if not getattr(sys, "frozen", False):
        return
    try:
        shutil.rmtree(resource_base() / "fonts", ignore_errors=True)
    except OSError:
        pass


def _known_sidecar_dir(target: Path, kind: str) -> bool:
    """Return True only when the folder obviously belongs to WPS magic cube.

    WPS creates data/ksysdoct and log/<process>.kmagiccube64.dll.log beside
    the process that launched COM.  We remove only these known patterns so a
    user folder named data/log is not destroyed blindly.
    """
    try:
        if target.is_symlink() or not target.is_dir():
            return False
        entries = list(target.iterdir())
    except OSError:
        return False
    if not entries:
        return True
    if kind == "data":
        return all(entry.is_dir() and entry.name == "ksysdoct" for entry in entries)
    return all(
        entry.is_file() and "kmagiccube" in entry.name.lower() and entry.name.lower().endswith(".log")
        for entry in entries
    )


def cleanup_sidecar_artifacts(base_dir: Path | None = None) -> None:
    """Remove WPS-generated data/ and log/ folders beside the executable.

    The WPS COM compatibility layer for legacy .doc/.ppt/.xls files makes WPS
    write these folders next to the process executable.  Cleanup is attempted
    both at startup (stale folders) and on normal close.
    """
    if base_dir is None:
        if not getattr(sys, "frozen", False):
            return
        base_dir = resource_base()
    base = Path(base_dir)
    for name, kind in (("data", "data"), ("log", "log")):
        target = base / name
        if _known_sidecar_dir(target, kind):
            for _ in range(5):
                try:
                    shutil.rmtree(target)
                    break
                except OSError:
                    time.sleep(0.15)
            continue
        # Unknown folder: remove only the known WPS artifacts, then an empty dir.
        try:
            if kind == "data":
                shutil.rmtree(target / "ksysdoct", ignore_errors=True)
            else:
                for item in target.glob("*kmagiccube*.log"):
                    try:
                        item.unlink()
                    except OSError:
                        pass
            target.rmdir()
        except OSError:
            pass


def materialize_fonts() -> Path:
    """Copy the embedded fonts/ payload next to the exe on first use."""
    target = resource_base() / "fonts"
    if not getattr(sys, "frozen", False):
        # Source checkout already has assets/fonts; no extraction needed.
        return fonts_dir()
    bundle = Path(getattr(sys, "_MEIPASS", resource_base()))
    source = bundle / "fonts"
    if not source.is_dir():
        return target
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        return target
    for item in source.iterdir():
        if not item.is_file():
            continue
        dest = target / item.name
        try:
            if dest.is_file() and dest.stat().st_size == item.stat().st_size:
                continue
            shutil.copyfile(item, dest)
        except OSError:
            continue
    return target
