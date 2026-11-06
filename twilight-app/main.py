"""Frozen/source entry point for Twilight Harness."""

from __future__ import annotations

import ctypes
import logging
import os
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path


APP_TITLE = "Twilight Harness · 紫悦"
MUTEX_NAME = "Local\\TwilightHarness.Sparkle"
ERROR_ALREADY_EXISTS = 183

# Module-level so the mutex handle lives for the whole process; the OS releases
# it when the process exits.
_SINGLE_INSTANCE_MUTEX: int | None = None


def _acquire_single_instance() -> bool:
    """Create the per-session single-instance mutex.

    Returns False only when another instance already owns the mutex.  Any Win32
    failure is treated as success so a broken ctypes environment never blocks
    startup.  ``TWILIGHT_ALLOW_MULTI=1`` skips the check for automated QA.
    """
    global _SINGLE_INSTANCE_MUTEX
    if os.name != "nt" or os.environ.get("TWILIGHT_ALLOW_MULTI") == "1":
        return True
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_mutex = kernel32.CreateMutexW
        create_mutex.restype = ctypes.c_void_p
        create_mutex.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
        handle = create_mutex(None, False, MUTEX_NAME)
        already_exists = ctypes.get_last_error() == ERROR_ALREADY_EXISTS
    except Exception:  # noqa: BLE001
        return True
    if not handle:
        return True
    _SINGLE_INSTANCE_MUTEX = handle
    return not already_exists


def _focus_existing_instance() -> bool:
    """Restore and foreground the main window of the running instance."""
    if os.name != "nt":
        return False
    try:
        user32 = ctypes.WinDLL("user32")
        find_window = user32.FindWindowW
        find_window.restype = ctypes.c_void_p
        find_window.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p)
        hwnd = find_window(None, APP_TITLE)
        if not hwnd:
            return False
        user32.ShowWindow(hwnd, 5)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:  # noqa: BLE001
        return False


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


def _retain_crash_log() -> Path | None:
    """Copy LOG_FILE outside temp/ so a fatal-startup log survives cleanup."""
    try:
        logging.shutdown()
    except Exception:  # noqa: BLE001
        pass
    try:
        import paths

        target_dir = paths.persistent_dir()
        target_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        retained = target_dir / f"twilight-harness-crash-{stamp}.log"
        shutil.copyfile(LOG_FILE, retained)
        return retained
    except Exception:  # noqa: BLE001
        return None


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


def _ai_selfcheck_report_path(argv: list[str]) -> Path:
    for index, arg in enumerate(argv):
        if arg == "--ai-report" and index + 1 < len(argv):
            return Path(argv[index + 1]).expanduser().resolve()
        if arg.startswith("--ai-report="):
            return Path(arg.split("=", 1)[1]).expanduser().resolve()
    return (_app_base_dir() / "ai-selfcheck.json").resolve()


def _ai_selfcheck(argv: list[str]) -> int:
    """Headless CPU-only self-check used by tools/ai_embed.py.

    Runs one SD1.5 text-to-image inference and one GPT-SoVITS inference on the
    embedded model pack (or reports exactly what is missing), writes a JSON
    report and returns 0 only when every required step succeeded.  This runs
    before mutex/Tk/logging setup so it can be executed on a CPU-only machine.
    """
    import json

    report_path = _ai_selfcheck_report_path(argv)
    report: dict = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "frozen": bool(getattr(sys, "frozen", False)),
        "executable": sys.executable,
        "backend_requested": "cpu",
        "ok": False,
        "backends": [],
        "sd15": {},
        "voice": {},
        "uvr": {},
        "whisper": {},
        "errors": [],
    }

    def record_error(message: str) -> None:
        report["errors"].append(message)

    try:
        import ai_compat
        import ai_resources

        ai_resources.ensure_layout()
        backends = ai_compat.probe_backends()
        report["backends"] = [
            {"name": item.name, "available": item.available, "reason": item.reason}
            for item in backends
        ]
        selected, notes = ai_compat.select_backend(prefer="cpu", strict_cpu_offload=True)
        report["backend"] = selected.name
        report["backend_notes"] = notes
    except Exception as exc:  # noqa: BLE001
        record_error(f"兼容层初始化失败：{type(exc).__name__}: {exc}")
        report["backend"] = "cpu"

    prompt_positive = (
        "Twilight Sparkle reading an old book in a cozy library, warm light, "
        "detailed illustration, CPU self-check image"
    )
    prompt_negative = "low quality, blurry, extra fingers, Wrong Anatomy"

    try:
        import ai_sd15

        engine = ai_sd15.SD15Engine()
        # ``missing_report()`` returns backend notes even on success; availability
        # is the authoritative check (it verifies models and a usable backend).
        if not engine.available():
            detail = engine.missing_report()
            report["sd15"] = {"ok": False, "error": detail}
            record_error("SD1.5 自检失败：" + str(detail))
        else:
            size_env = os.environ.get("TWILIGHT_AI_SELFCHECK_SIZE", "").strip().lower()
            size = None
            if size_env and "x" in size_env:
                width, height = size_env.split("x", 1)
                size = (int(width), int(height))
            started = time.monotonic()
            image_path = engine.generate(
                prompt_positive, prompt_negative, size=size, backend="cpu"
            )
            elapsed = time.monotonic() - started
            ok = image_path.is_file() and image_path.stat().st_size > 0
            report["sd15"] = {
                "ok": ok,
                "path": str(image_path),
                "elapsed_s": round(elapsed, 3),
                "size": list(size) if size else "default(960x720)",
                "backend": str(getattr(engine._backend, "name", engine._backend)),
            }
            if not ok:
                record_error("SD1.5 自检：输出图片不存在或为空")
    except Exception as exc:  # noqa: BLE001
        report["sd15"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        record_error(f"SD1.5 自检异常：{type(exc).__name__}: {exc}")

    try:
        import ai_audio

        voice = ai_audio.SoVitsVoice()
        missing = voice.missing_report()
        if missing:
            report["voice"] = {"ok": False, "error": str(missing)}
            record_error("GPT-SoVITS 自检失败：" + str(missing))
        else:
            started = time.monotonic()
            audio_path = voice.synthesize("你好，这是 Twilight Harness 的 CPU 自检语音。")
            elapsed = time.monotonic() - started
            ok = audio_path.is_file() and audio_path.stat().st_size > 0
            report["voice"] = {
                "ok": ok,
                "path": str(audio_path),
                "elapsed_s": round(elapsed, 3),
            }
            if not ok:
                record_error("GPT-SoVITS 自检：输出音频不存在或为空")
    except Exception as exc:  # noqa: BLE001
        report["voice"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        record_error(f"GPT-SoVITS 自检异常：{type(exc).__name__}: {exc}")

    for label, class_name in (("uvr", "VocalSeparator"), ("whisper", "Transcriber")):
        try:
            import ai_audio

            cls = getattr(ai_audio, class_name, None)
            if cls is None:
                report[label] = {"ok": False, "error": "模块未加载"}
                continue
            instance = cls()
            problem = instance.missing_report()
            report[label] = {"ok": not problem, "error": problem or ""}
        except Exception as exc:  # noqa: BLE001
            report[label] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    report["ok"] = not report["errors"]
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        print(f"AI self-check: could not write report {report_path}: {exc}", file=sys.stderr)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


def main() -> int:
    if "--ai-selfcheck" in sys.argv[1:]:
        return _ai_selfcheck(sys.argv[1:])
    if not _acquire_single_instance():
        _focus_existing_instance()
        return 0
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
        if _is_debug_build():
            try:
                import debug_console

                root.bind("<<DebugQuit>>", lambda _event: root.destroy())
                debug_console.start(application, root)
            except Exception:  # noqa: BLE001 - console must never block startup
                logging.getLogger("twilight_harness").exception(
                    "Debug console failed to start"
                )
        root.mainloop()
        del application
        return 0
    except Exception:  # noqa: BLE001
        logging.exception("Fatal startup error")
        details = traceback.format_exc()
        retained_log = _retain_crash_log()
        log_hint = retained_log if retained_log is not None else LOG_FILE
        try:
            if root is None:
                root = tk.Tk()
            messagebox.showerror(
                APP_TITLE,
                "程序启动时遇到问题。\n\n"
                + details[-1600:]
                + f"\n\n诊断日志：{log_hint}",
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
