"""Frozen/source entry point for Twilight Harness."""

from __future__ import annotations

import ctypes
import logging
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path


APP_TITLE = "Twilight Harness · 紫悦"


def _app_base_dir() -> Path:
    """Directory that contains the delivered files (or the source tree)."""
    env_home = os.environ.get("TWILIGHT_HOME")
    if env_home:
        return Path(env_home).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _runtime_temp_dir() -> Path:
    """Local temp folder beside the exe, used for logs and PyInstaller runtime."""
    candidate = _app_base_dir() / "temp"
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        return candidate
    except OSError:
        fallback = Path(tempfile.gettempdir()) / "twilight-harness-temp"
        try:
            fallback.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return fallback


TEMP_DIR = _runtime_temp_dir()
LOG_FILE = TEMP_DIR / "twilight-harness.log"


def _enable_dpi_awareness() -> None:
    if os.name != "nt":
        return
    try:
        # PER_MONITOR_AWARE_V2 keeps the custom-drawn UI crisp on scaled displays.
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:  # noqa: BLE001
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:  # noqa: BLE001
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass


def _set_app_id() -> None:
    if os.name != "nt":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "TwilightHarness.TwilightSparkle.2"
        )
    except Exception:  # noqa: BLE001
        pass


def _is_debug_build() -> bool:
    """The debug executable enables its console by its file name too."""
    if os.environ.get("TWILIGHT_DEBUG"):
        return True
    if getattr(sys, "_MEIPASS", None) and "debug" in Path(sys.executable).stem.lower():
        return True
    return False


def _set_console_title() -> None:
    if os.name != "nt" or not _is_debug_build():
        return
    try:
        ctypes.windll.kernel32.SetConsoleTitleW("Twilight Harness Debug Backend")
    except Exception:  # noqa: BLE001
        pass


def _configure_logging() -> None:
    debug = _is_debug_build()
    level = logging.DEBUG if debug else logging.INFO
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    try:
        file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        root.addHandler(file_handler)
    except OSError:
        pass
    if debug and sys.stderr is not None:
        stream = logging.StreamHandler()
        stream.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(stream)
    logging.getLogger("twilight_harness").info(
        "Starting Twilight Harness; frozen=%s executable=%s", getattr(sys, "frozen", False), sys.executable
    )


def _cleanup_temp() -> None:
    try:
        logging.shutdown()
    except Exception:  # noqa: BLE001
        pass
    try:
        shutil.rmtree(TEMP_DIR, ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass
    try:
        import paths

        paths.cleanup_runtime_fonts()
        paths.cleanup_sidecar_artifacts()
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    _enable_dpi_awareness()
    _set_app_id()
    _configure_logging()
    _set_console_title()
    try:
        import paths

        paths.cleanup_sidecar_artifacts()
        paths.materialize_fonts()
        import font_manager

        font_manager.ensure_fonts()
    except Exception:  # noqa: BLE001
        logging.getLogger("twilight_harness").exception("Bundled font setup failed")
    try:
        import tkinter as tk
        from tkinter import messagebox
    except Exception:  # noqa: BLE001
        logging.exception("Tkinter could not be imported")
        _cleanup_temp()
        return 1
    root = None
    try:
        root = tk.Tk()
        root.withdraw()
        import app as twilight_app

        application = twilight_app.run(root)
        root.deiconify()
        root.mainloop()
        del application
        return 0
    except Exception:  # noqa: BLE001
        logging.exception("Fatal startup error")
        details = traceback.format_exc()
        try:
            if root is None:
                root = tk.Tk()
            messagebox.showerror(
                APP_TITLE,
                "程序启动时遇到问题。\n\n"
                + details[-1600:]
                + f"\n\n诊断日志：{LOG_FILE}",
            )
        except Exception:  # noqa: BLE001
            pass
        return 1
    finally:
        try:
            if root is not None:
                root.destroy()
        except Exception:  # noqa: BLE001
            pass
        _cleanup_temp()


if __name__ == "__main__":
    raise SystemExit(main())
