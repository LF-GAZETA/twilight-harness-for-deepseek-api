"""Twilight Harness — Twilight Sparkle chat GUI.

Visual language follows the user's supplied GUI design:
- pale lavender canvas with a strong black centre rule on the setup screen
- Uni Neue for Latin display/UI text and Source Han Sans for Chinese body text
- white rectangular form fields, lilac pill buttons, pony illustration
- chat screen with centred API status, split thinking/answer panes and avatar
  message blocks, plus a large rounded composer at the bottom.
"""

from __future__ import annotations

import ctypes
import os
import queue
import re
import threading
import time
import traceback
from pathlib import Path
import tkinter as tk
from tkinter import filedialog

import api_client
import derpi
import feature_flags
import local_tools
import progress_window
import persona
import theme
import ui_strings
import widgets
from design_assets import (
    AVATAR_OFFLINE_PNG_BASE64,
    AVATAR_ONLINE_PNG_BASE64,
    AVATAR_THINKING_PNG_BASE64,
    AVATAR_THINK_SIDE_PNG_BASE64,
    PONY_PNG_BASE64,
)

if os.name == "nt":
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
else:  # pragma: no cover - Windows-only deliverable
    _user32 = None

GWL_EXSTYLE = -20
WS_EX_APPWINDOW = 0x00040000
WS_EX_TOOLWINDOW = 0x00000080
GA_ROOT = 2
SW_MINIMIZE = 6
SPI_GETWORKAREA = 0x0030
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_FRAMECHANGED = 0x0020

if feature_flags.ATTACHMENTS_ENABLED:
    import attachments
    import local_actions
else:  # standard build: attachment modules are excluded
    attachments = None
    local_actions = None

UI_STRINGS = ui_strings.UI_STRINGS
_locale_for_label = ui_strings.locale_for_label
_content_locale_for_label = ui_strings.content_locale_for_label


def _work_area(root: tk.Misc) -> tuple[int, int, int, int]:
    fallback = (0, 0, root.winfo_screenwidth(), root.winfo_screenheight())
    if _user32 is None:
        return fallback
    try:
        rect = _RECT()
        if _user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
            width = int(rect.right - rect.left)
            height = int(rect.bottom - rect.top)
            if width > 320 and height > 240:
                return int(rect.left), int(rect.top), width, height
    except Exception:  # noqa: BLE001
        pass
    return fallback


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


def _hwnd(root: tk.Misc) -> int:
    if _user32 is None:
        return 0
    try:
        return int(_user32.GetAncestor(root.winfo_id(), GA_ROOT))
    except Exception:  # noqa: BLE001
        return 0


def _bring_to_front(root: tk.Misc) -> None:
    hwnd = _hwnd(root)
    if not hwnd or _user32 is None:
        return
    try:
        _user32.ShowWindow(hwnd, 9)
        _user32.SetForegroundWindow(hwnd)
    except Exception:  # noqa: BLE001
        pass


class TwilightApp:
    WIDTH = 800
    HEIGHT = 600
    MIN_WIDTH = 640
    MIN_HEIGHT = 480
    TOPBAR_HEIGHT = 38
    FORM_HEIGHT = 169

    def __init__(self, root: tk.Tk, home: Path) -> None:
        self.root = root
        self.home = Path(home).resolve()
        self.settings = api_client.load_settings(self.home)
        try:
            api_client.ensure_character_file(self.home)
        except api_client.CharacterFileError:
            api_client.LOGGER.exception("Could not extract embedded character card")
        self._closed = False
        self._mode = "setup"
        self._preparing = False
        self._sending = False
        self._maximized = False
        self._normal_geometry: str | None = None
        self._ui_locale = "zh-Hans"
        self._language_label = "简体中文"
        self._content_locale = "en"
        self._profile = ""
        self._character_path: Path | None = None
        self._effective_model = self.settings.model
        self._history: list[dict[str, str]] = []
        self._turn_count = 0
        self._stream_anchor: str | None = None
        self._think_stream_anchor: str | None = None
        self._answer_block_start: str | None = None
        self._vision_fallback_notice = False
        self._vision_supported: bool | None = None
        self._attach_busy = False
        self._chat_streaming = False
        self._stop_hint = None
        self._vhs_dialog = None
        self._current_reasoning = ""
        self._current_content = ""
        self._reasoning_present = False
        self._cancel_event = threading.Event()
        self._queue: queue.Queue = queue.Queue()
        self._queue_job: str | None = None
        self._setup_anim_job: str | None = None
        self._setup_anim_step = 0
        self._saved_key_warning = ""
        self._icon_image = None
        self._pony_photo = None
        self._avatar_offline_photo = None
        self._avatar_online_photo = None
        self._avatar_thinking_photo = None
        self._think_side_photo = None
        self._chat_state = "ready"
        self._right_avatar_names: list[str] = []
        self._avatar_seq = 0
        self._current_answer_avatar: str | None = None
        self._window_region_key: tuple[int, int, int] | None = None
        self._brand_canvas = None
        self._pony_label = None
        self._setup_view = None
        self._setup_form = None
        self._form_message = None
        self._key_entry = None
        self._lang_select = None
        self._continue_button = None
        self._exit_button = None
        self._chat_view = None
        self._chat_hint = None
        self._composer = None
        self._think_text = None
        self._right_text = None
        self._attach_hint = None
        self._pending_attachments: list = []
        self._last_sent_attachments: list = []
        self._topbar = None
        self._resizer = None

        theme.init_fonts(root)
        self._load_images()
        self._configure_window()
        self._build_shell()
        self._progress_window = progress_window.ProgressWindow(
            self.root, lambda key: self._s.get(key, key)
        )
        self._show_setup()
        self._queue_job = self.root.after(40, self._drain_queue)
        self.root.report_callback_exception = self._report_callback_exception
        self.root.bind("<Alt-F4>", lambda _event: self.close())
        self.root.after(80, self._on_first_paint)

    # ------------------------------------------------------------------
    # Window furniture
    # ------------------------------------------------------------------
    def _load_images(self) -> None:
        self._pony_photo = tk.PhotoImage(data=PONY_PNG_BASE64)
        try:
            panel = Path(__file__).resolve().parent / "assets" / "pony_panel.png"
            self._pony_panel_photo = tk.PhotoImage(file=str(panel)) if panel.is_file() else None
        except Exception:
            self._pony_panel_photo = None
        self._avatar_offline_photo = tk.PhotoImage(data=AVATAR_OFFLINE_PNG_BASE64)
        self._avatar_online_photo = tk.PhotoImage(data=AVATAR_ONLINE_PNG_BASE64)
        self._avatar_thinking_photo = tk.PhotoImage(data=AVATAR_THINKING_PNG_BASE64)
        self._think_side_photo = tk.PhotoImage(data=AVATAR_THINK_SIDE_PNG_BASE64)

    def _configure_window(self) -> None:
        root = self.root
        root.title(UI_STRINGS["zh-Hans"]["app_title"])
        root.configure(bg=theme.BG)
        root.overrideredirect(True)
        root.resizable(True, True)
        root.minsize(self.MIN_WIDTH, self.MIN_HEIGHT)
        root.geometry(f"{self.WIDTH}x{self.HEIGHT}")
        root.update_idletasks()
        left, top, work_w, work_h = _work_area(root)
        x = left + max(0, (work_w - self.WIDTH) // 2)
        y = top + max(0, (work_h - self.HEIGHT) // 2)
        root.geometry(f"{self.WIDTH}x{self.HEIGHT}+{x}+{y}")
        root.update_idletasks()
        self._apply_appwindow_style()
        self._apply_icon()

    def _apply_icon(self) -> None:
        try:
            from app_icon import ICON_PNG_BASE64

            image = tk.PhotoImage(data=ICON_PNG_BASE64)
            self._icon_image = image
            self.root.iconphoto(True, image)
        except Exception:  # noqa: BLE001
            pass
        try:
            if os.name == "nt":
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                    "TwilightHarness.TwilightSparkle.2"
                )
        except Exception:  # noqa: BLE001
            pass

    def _apply_appwindow_style(self) -> None:
        if _user32 is None:
            return
        hwnd = _hwnd(self.root)
        if not hwnd:
            return
        try:
            ex = int(_user32.GetWindowLongW(hwnd, GWL_EXSTYLE))
            ex = (ex | WS_EX_APPWINDOW) & ~WS_EX_TOOLWINDOW
            _user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex)
            _user32.SetWindowPos(
                hwnd,
                0,
                0,
                0,
                0,
                0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED,
            )
        except Exception:  # noqa: BLE001
            pass

    def _build_shell(self) -> None:
        self._topbar = widgets.TopBar(
            self.root,
            on_minimize=self.minimize,
            on_maximize=self.toggle_maximize,
            on_close=self.close,
            on_toggle_maximize=self.toggle_maximize,
            is_maximized=lambda: self._maximized,
        )
        self._topbar.pack(side="top", fill="x")
        self.body = tk.Frame(self.root, bg=theme.BG)
        self.body.pack(side="top", fill="both", expand=True)
        self._resizer = widgets.WindowResizer(
            self.root,
            min_width=self.MIN_WIDTH,
            min_height=self.MIN_HEIGHT,
            is_maximized=lambda: self._maximized,
        )
        self.root.bind("<Configure>", self._on_root_configure, add="+")
        self.root.after(80, self._apply_rounded_window)

    def _on_root_configure(self, event) -> None:
        if event.widget is self.root:
            self._apply_rounded_window()

    def _apply_rounded_window(self) -> None:
        """Clip the borderless window to a rounded rectangle (Windows)."""
        if os.name != "nt" or self._closed:
            return
        hwnd = _hwnd(self.root)
        if not hwnd:
            return
        try:
            width = max(1, self.root.winfo_width())
            height = max(1, self.root.winfo_height())
        except tk.TclError:
            return
        radius = 0 if self._maximized else 18
        key = (width, height, radius)
        if key == self._window_region_key:
            return
        try:
            user32 = ctypes.windll.user32
            gdi32 = ctypes.windll.gdi32
            user32.SetWindowRgn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool]
            user32.SetWindowRgn.restype = ctypes.c_int
            gdi32.CreateRoundRectRgn.argtypes = [ctypes.c_int] * 6
            gdi32.CreateRoundRectRgn.restype = ctypes.c_void_p
            hrgn = None
            if radius > 0:
                hrgn = gdi32.CreateRoundRectRgn(
                    0, 0, width + 1, height + 1, radius * 2, radius * 2
                )
                if not hrgn:
                    return
            applied = user32.SetWindowRgn(hwnd, hrgn, True)
            if not applied:
                if hrgn:
                    gdi32.DeleteObject(hrgn)
                return
            self._window_region_key = key
        except Exception:  # noqa: BLE001
            pass

    def _on_first_paint(self) -> None:
        if self._closed:
            return
        try:
            self.root.lift()
            self.root.focus_force()
            self.root.attributes("-topmost", True)
            self.root.after(220, self._drop_topmost)
        except tk.TclError:
            pass
        _bring_to_front(self.root)

    def _drop_topmost(self) -> None:
        if self._closed:
            return
        try:
            self.root.attributes("-topmost", False)
        except tk.TclError:
            pass

    def minimize(self) -> None:
        if self._closed or _user32 is None:
            return
        hwnd = _hwnd(self.root)
        if hwnd:
            try:
                _user32.ShowWindow(hwnd, SW_MINIMIZE)
            except Exception:  # noqa: BLE001
                pass

    def toggle_maximize(self) -> None:
        if self._closed:
            return
        if self._maximized:
            if self._normal_geometry:
                self.root.geometry(self._normal_geometry)
            self._maximized = False
            if self._topbar is not None:
                self._topbar.set_maximized(False)
        else:
            self._normal_geometry = self.root.geometry()
            left, top, width, height = _work_area(self.root)
            self._maximized = True
            if self._topbar is not None:
                self._topbar.set_maximized(True)
            self.root.geometry(f"{width}x{height}+{left}+{top}")
        self.root.update_idletasks()
        self._apply_rounded_window()
        if self._resizer is not None:
            self._resizer.lift()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._cancel_event.set()
        except Exception:  # noqa: BLE001
            pass
        for attr in ("_queue_job", "_setup_anim_job"):
            job = getattr(self, attr, None)
            if job:
                try:
                    self.root.after_cancel(job)
                except tk.TclError:
                    pass
                setattr(self, attr, None)
        try:
            self._progress_window.shutdown()
        except Exception:  # noqa: BLE001
            pass
        api_client.cleanup_character(self.home)
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    # ------------------------------------------------------------------
    # Screens
    # ------------------------------------------------------------------
    def _clear_body(self) -> None:
        try:
            for child in self.body.winfo_children():
                child.destroy()
        except tk.TclError:
            pass
        self._setup_view = None
        self._setup_form = None
        self._form_message = None
        self._key_entry = None
        self._lang_select = None
        self._continue_button = None
        self._exit_button = None
        self._brand_canvas = None
        self._pony_label = None
        self._chat_view = None
        self._composer = None
        self._think_text = None
        self._right_text = None
        self._chat_hint = None
        self._attach_hint = None
        self._stop_hint = None

    @property
    def _s(self) -> dict[str, str]:
        return UI_STRINGS[self._ui_locale]

    # -- setup ----------------------------------------------------------
    def _show_setup(self) -> None:
        if self._closed:
            return
        self._mode = "setup"
        self._clear_body()
        if self._topbar is not None:
            self._topbar.show_setup()
            self._topbar.set_locale(self._ui_locale)
        self.root.title(self._s["app_title"])

        view = tk.Frame(self.body, bg=theme.BG)
        view.pack(fill="both", expand=True)
        self._setup_view = view

        line = tk.Frame(view, bg=theme.BLACK, width=4)
        line.place(relx=0.5, y=0, width=4, relheight=1.0, anchor="n")
        self._setup_line = line
        self._tk_version_label = tk.Label(
            view,
            text="rc-261031-tkinter",
            bg=theme.BG,
            fg=theme.MUTED,
            font=("Consolas", 9),
            bd=0,
        )
        self._tk_version_label.place(relx=1.0, x=-22, y=12, anchor="ne")

        self._brand_canvas = tk.Canvas(
            view,
            bg=theme.BG,
            highlightthickness=0,
            bd=0,
            width=200,
            height=40,
        )
        self._brand_canvas.place(x=22, rely=1.0, y=-24, anchor="sw")

        self._pony_label = tk.Label(
            view,
            image=getattr(self, "_pony_panel_photo", None) or self._pony_photo,
            bg="#3d114f",
            bd=0,
        )
        self._pony_label.place(x=0, y=0)

        form = tk.Frame(view, bg=theme.BG)
        self._setup_form = form
        self._form_message = tk.Label(
            form,
            text="",
            bg=theme.BG,
            fg=theme.ERROR,
            font=theme.ui(9, locale=self._ui_locale),
            anchor="w",
            justify="left",
            bd=0,
            highlightthickness=0,
        )
        self._key_entry = widgets.RoundedEntry(
            form,
            width=360,
            height=41,
            placeholder=self._s["api_placeholder"],
            mask=True,
            on_change=self._on_key_change,
            on_submit=self._try_continue,
            font=theme.ui(12, locale=self._ui_locale),
        )
        self._lang_select = widgets.LilacSelect(
            form,
            persona.LANGUAGE_LABELS,
            width=360,
            height=41,
            default=self._language_label,
            on_change=self._on_language_change,
            font=theme.ui(12, locale=self._ui_locale),
        )
        self._continue_button = widgets.RoundedButton(
            form,
            self._s["continue"],
            self._try_continue,
            width=92,
            height=35,
            font=theme.ui(12, locale=self._ui_locale),
        )
        self._exit_button = widgets.RoundedButton(
            form,
            self._s["exit"],
            self.close,
            width=92,
            height=35,
            font=theme.ui(12, locale=self._ui_locale),
        )
        self._form_message.place(x=0, y=0, width=360, height=18)
        self._key_entry.place(x=0, y=20, width=360, height=41)
        self._lang_select.place(x=0, y=75, width=360, height=41)
        self._continue_button.place(x=0, y=134, width=92, height=35)
        self._exit_button.place(x=268, y=134, width=92, height=35)
        self._setup_form.place(x=420, y=0, width=360, height=self.FORM_HEIGHT)
        self._form_message.place_configure(width=360)
        view.bind("<Configure>", self._layout_setup)
        view.after_idle(self._layout_setup)

        if self.settings.api_key:
            self._key_entry.set(self.settings.api_key)
        self._apply_locale(self._ui_locale, update_placeholder=True)
        self._clear_form_message()
        self._update_continue_state()
        if self._resizer is not None:
            self._resizer.lift()

    def _layout_setup(self, _event=None) -> None:
        if self._closed or self._setup_view is None or self._setup_form is None:
            return
        try:
            width = self._setup_view.winfo_width()
            height = self._setup_view.winfo_height()
        except tk.TclError:
            return
        if width <= 1 or height <= 1:
            return
        form_width = max(280, min(360, width // 2 - 40))
        form_x = width - 20 - form_width
        form_y = max(0, height - 17 - self.FORM_HEIGHT)
        self._setup_form.place(x=form_x, y=form_y, width=form_width, height=self.FORM_HEIGHT)
        try:
            self._form_message.place_configure(x=0, y=0, width=form_width, height=18)
            self._key_entry.place_configure(x=0, y=20, width=form_width, height=41)
            self._lang_select.place_configure(x=0, y=75, width=form_width, height=41)
            self._continue_button.place_configure(x=0, y=134, width=92, height=35)
            self._exit_button.place_configure(x=max(0, form_width - 92), y=134, width=92, height=35)
            self._pony_label.place_configure(x=0, y=0, anchor="nw")
        except tk.TclError:
            return
        self._draw_brand_title(width)

    def _draw_brand_title(self, view_width: int) -> None:
        """Draw the tracked Uni Neue wordmark used in the supplied design."""
        if self._closed or self._brand_canvas is None:
            return
        if view_width >= 760:
            font_size, tracking = 28, 1
        elif view_width >= 690:
            font_size, tracking = 22, 1
        else:
            font_size, tracking = 18, 0
        font = theme.display(font_size)
        title = "Twilight Harness"
        widths = [font.measure(char) for char in title]
        total = sum(widths) + tracking * max(0, len(title) - 1)
        line_height = font.metrics("linespace") + 10
        try:
            self._brand_canvas.configure(width=total + 3, height=line_height)
            self._brand_canvas.delete("all")
            x = 1.0
            baseline = line_height - 6
            for char, char_width in zip(title, widths):
                if char != " ":
                    self._brand_canvas.create_text(
                        x, baseline, text=char, anchor="sw", fill=theme.SETUP_TITLE_TEXT, font=font
                    )
                x += char_width + tracking
            self._brand_canvas.place_configure(x=22, rely=1.0, y=-24, anchor="sw")
        except tk.TclError:
            pass

    def _clear_form_message(self) -> None:
        if self._form_message is not None:
            try:
                self._form_message.configure(text="", fg=theme.MUTED)
            except tk.TclError:
                pass
        if self._key_entry is not None:
            self._key_entry.set_error(False)

    def _set_form_message(self, text: str, *, color: str) -> None:
        if self._form_message is None:
            return
        try:
            self._form_message.configure(text=text, fg=color)
        except tk.TclError:
            pass

    def _update_continue_state(self) -> None:
        if self._continue_button is None:
            return
        key = self._key_entry.get() if self._key_entry is not None else ""
        enabled = bool(key) and not self._preparing
        self._continue_button.set_enabled(enabled)

    def _on_key_change(self, value: str) -> None:
        if self._preparing or self._mode != "setup":
            return
        if self._form_message is not None:
            try:
                current = self._form_message.cget("text")
            except tk.TclError:
                current = ""
            if current and not self._s["preparing"].startswith(current):
                self._clear_form_message()
        if self._key_entry is not None:
            self._key_entry.set_error(False)
        self._update_continue_state()
        if value:
            if self._form_message is not None and not self._form_message.cget("text"):
                self._set_form_message("", color=theme.MUTED)

    def _on_language_change(self, value: str) -> None:
        if self._preparing:
            return
        self._language_label = value
        self._ui_locale = _locale_for_label(value)
        self._content_locale = _content_locale_for_label(value)
        self._apply_locale(self._ui_locale, update_placeholder=True)
        self._clear_form_message()
        self._update_continue_state()

    def _apply_locale(self, locale: str, *, update_placeholder: bool = False) -> None:
        s = self._s
        try:
            self.root.title(s["app_title"])
        except tk.TclError:
            pass
        if self._key_entry is not None:
            self._key_entry.set_locale(locale, placeholder=s["api_placeholder"] if update_placeholder else None)
        if self._lang_select is not None:
            self._lang_select.set_locale(locale)
        if self._continue_button is not None:
            self._continue_button.set_text(s["continue"])
            self._continue_button.set_font(theme.ui(12, locale=locale))
        if self._exit_button is not None:
            self._exit_button.set_text(s["exit"])
            self._exit_button.set_font(theme.ui(12, locale=locale))
        if self._form_message is not None:
            try:
                self._form_message.configure(font=theme.ui(9, locale=locale))
            except tk.TclError:
                pass
        if self._topbar is not None:
            self._topbar.set_locale(locale)
        if self._chat_hint is not None:
            try:
                self._chat_hint.configure(text=s["send_hint"], font=theme.ui(9, locale=locale))
            except tk.TclError:
                pass
        if self._attach_hint is not None:
            try:
                self._attach_hint.configure(text=s["attach_hint"], font=theme.ui(9, locale=locale))
            except tk.TclError:
                pass
        if self._stop_hint is not None:
            try:
                self._stop_hint.configure(text=s["stop_generating"], font=theme.ui(9, locale=locale))
            except tk.TclError:
                pass
        if self._composer is not None:
            self._composer.set_locale(self._content_locale, s["composer_placeholder"])

    # ------------------------------------------------------------------
    # Setup -> persona bootstrap
    # ------------------------------------------------------------------
    def _try_continue(self) -> None:
        if self._closed or self._mode != "setup" or self._preparing:
            return
        if self._key_entry is None or self._lang_select is None:
            return
        key = self._key_entry.get().strip()
        if not key:
            self._set_form_message(self._s["error_no_key"], color=theme.ERROR)
            if self._key_entry is not None:
                self._key_entry.set_error(True)
                self._key_entry.focus_entry()
            return
        try:
            profile, path = api_client.read_character(self.home)
        except api_client.CharacterFileError as exc:
            api_client.LOGGER.error("Character file error: %s", exc)
            detail = str(exc).strip()
            message = detail if detail else self._s["missing_profile"]
            if len(message) > 240:
                message = message[:240] + "…"
            self._set_form_message(message, color=theme.ERROR)
            return
        language = self._lang_select.get() or "简体中文"
        self._profile = profile
        self._character_path = path
        self._language_label = language
        self._ui_locale = _locale_for_label(language)
        self._apply_locale(self._ui_locale, update_placeholder=False)
        self._set_setup_busy(True)
        self._start_setup_animation()
        messages = persona.build_bootstrap_messages(profile, language)
        base_url = self.settings.base_url
        model = self.settings.model

        def work() -> None:
            try:
                client = api_client.DeepSeekClient(key, base_url, model)
                reply = client.bootstrap(messages)
                self._queue.put(("prep_ok", reply, key, language))
            except api_client.ApiError as exc:
                self._queue.put(("prep_error", exc.status, exc.user_message))
            except Exception as exc:  # noqa: BLE001
                api_client.LOGGER.exception("Persona bootstrap failed")
                self._queue.put(("prep_error", None, f"{type(exc).__name__}: {exc}"))

        threading.Thread(target=work, name="twilight-bootstrap", daemon=True).start()

    def _set_setup_busy(self, busy: bool) -> None:
        self._preparing = busy
        if self._key_entry is not None:
            self._key_entry.set_enabled(not busy)
        if self._lang_select is not None:
            self._lang_select.set_enabled(not busy)
        if self._continue_button is not None:
            self._continue_button.set_enabled((not busy) and bool(self._key_entry.get() if self._key_entry else ""))
        if self._exit_button is not None:
            self._exit_button.set_enabled(True)
        self._update_continue_state()

    def _start_setup_animation(self) -> None:
        self._stop_setup_animation()
        self._setup_anim_step = 0
        base = self._s["preparing"]

        def tick() -> None:
            if self._closed or not self._preparing:
                return
            dots = "." * (self._setup_anim_step % 4)
            self._set_form_message(base + dots, color=theme.ACCENT)
            self._setup_anim_step += 1
            self._setup_anim_job = self.root.after(360, tick)

        tick()

    def _stop_setup_animation(self) -> None:
        if self._setup_anim_job is not None:
            try:
                self.root.after_cancel(self._setup_anim_job)
            except tk.TclError:
                pass
            self._setup_anim_job = None

    def _short_setup_error(self, status: int | None) -> str:
        s = self._s
        if status:
            return f"{s['error_prefix']} ({status})"
        return f"{s['error_prefix']} · {s['generic_error']}"

    def _handle_prep_ok(self, reply: api_client.BootstrapReply, key: str, language: str) -> None:
        if self._closed or self._mode != "setup" or not self._preparing:
            return
        self._stop_setup_animation()
        self._preparing = False
        self.settings.api_key = key
        self._language_label = language
        self._ui_locale = _locale_for_label(language)
        self._saved_key_warning = ""
        try:
            if getattr(self.settings, "api_key_from_env", False):
                # Environment-provided secrets must not be written to disk.
                api_client.LOGGER.info(
                    "API key came from the environment; not persisting it."
                )
            else:
                api_client.save_settings(self.home, self.settings)
        except OSError as exc:
            self._saved_key_warning = self._s["saved_warning"]
            api_client.LOGGER.warning("Could not save API key: %s", exc)
        self._startup_reply = reply
        self._preparing = True
        self._set_form_message(self._s.get("ai_starting_sd", "正在启动 Stable Diffusion..."), color=theme.ACCENT)
        self._announce_ai_activation(reply)

    def _setup_error_text(self, status: int | None, message: str) -> str:
        s = self._s
        lower = (message or "").lower()
        if status in (401, 403) or any(
            token in lower
            for token in ("401", "invalid api", "invalid_api_key", "unauthorized", "无效", "invalid key")
        ):
            return s["api_error_invalid"]
        if status is None or any(
            token in lower
            for token in ("connect", "connection", "timed out", "timeout", "winerror", "网络", "无法连接")
        ):
            return s["api_error_connection"]
        detail = (message or "").strip()
        if detail.startswith("API 返回错误："):
            detail = detail[len("API 返回错误："):].strip()
        if len(detail) > 220:
            detail = detail[:220] + "…"
        if detail and status:
            return f"{s['error_prefix']} (HTTP {status}) · {detail}"
        if detail:
            return f"{s['error_prefix']} · {detail}"
        return f"{s['error_prefix']} (HTTP {status})"

    def _handle_prep_error(self, status: int | None, message: str) -> None:
        if self._closed or self._mode != "setup" or not self._preparing:
            return
        self._stop_setup_animation()
        self._set_setup_busy(False)
        self._set_form_message(self._setup_error_text(status, message), color=theme.ERROR)
        if self._key_entry is not None:
            self._key_entry.set_error(True)
            self._key_entry.focus_entry()
        api_client.LOGGER.error("Bootstrap failed (status=%s): %s", status, message)

    # ------------------------------------------------------------------
    # Chat screen
    # ------------------------------------------------------------------
    def _enter_chat(self, reply: api_client.BootstrapReply) -> None:
        self._mode = "chat"
        self._preparing = False
        self._sending = False
        self._effective_model = reply.model
        self._history = [{"role": "assistant", "content": reply.content}]
        self._turn_count = 0
        self._content_locale = _content_locale_for_label(self._language_label)
        self._chat_state = "ready"
        self._pending_attachments = []
        if self._topbar is not None:
            self._topbar.show_chat()
            self._topbar.set_locale(self._ui_locale)
        self._build_chat_view()
        self.root.title(self._s["app_title"])
        self._add_opening(reply)
        self._preparing = False
        self._set_chat_status("ready")
        if self._saved_key_warning and self._right_text is not None:
            self._append_right_text("\n" + self._saved_key_warning + "\n", "sys_notice")
        if self._resizer is not None:
            self._resizer.lift()
        self.root.after(80, self._focus_composer)

    def _runtime_tts_url(self) -> str:
        try:
            import ai_audio
            import json as _json
            runtime_path = ai_audio.ai_home() / "tts-runtime.json"
            if runtime_path.is_file():
                runtime = _json.loads(runtime_path.read_text(encoding="utf-8-sig"))
                for key in ("zh", "en"):
                    entry = runtime.get(key, {}) if isinstance(runtime, dict) else {}
                    url = str(entry.get("api_url", "") or "").strip()
                    if url:
                        return os.environ.get("TWILIGHT_GPT_SOVITS_URL", url).strip()
        except Exception:
            pass
        return os.environ.get("TWILIGHT_GPT_SOVITS_URL", "").strip()

    def _announce_ai_activation(self, reply=None) -> None:
        """Load GPT-SoVITS and SD1.5, then enter the chat screen."""
        self._voice_ready = False
        self._voice = None
        self._sd_ready = False
        self._startup_total = 2
        self._startup_done = 0
        self._startup_reply = reply if reply is not None else getattr(self, "_startup_reply", None)

        def finish_one() -> None:
            self._startup_done += 1
            if self._startup_done >= self._startup_total and self._startup_reply is not None:
                self.root.after(250, self._finish_ai_startup)

        def report(kind: str, label: str, fraction: float, state: str = "running") -> None:
            fraction = max(0.0, min(1.0, fraction))
            if self._mode == "setup" and self._form_message is not None:
                if state == "error":
                    name = "Stable Diffusion" if kind == "sd15" else "GPT-SoVITS"
                    self._set_form_message(f"错误：无法启动{name}", color=theme.ERROR)
                else:
                    self._set_form_message(f"{label} {int(fraction * 100)}%", color=theme.ACCENT)
            if self._mode != "setup":
                self._report_progress({
                    "id": f"startup-{kind}",
                    "kind": f"startup_{kind}",
                    "label": label,
                    "name": "",
                    "fraction": fraction,
                    "state": state,
                    "compact": True,
                })

        sovits_label = "正在启动 GPT-SoVITS..."
        sd_label = "正在启动 Stable Diffusion..."
        # Create the two rows immediately (SD on top, GPT below) so they are
        # visible from the first frame even while both threads are starting.
        report("sd15", sd_label, 0.05)
        report("sovits", sovits_label, 0.05)

        def load_voice() -> None:
            label = sovits_label
            api_client.LOGGER.info(label)
            report("sovits", label, 0.05)
            try:
                import ai_audio

                http_url = self._runtime_tts_url()
                if http_url:
                    self._voice = None
                    self._voice_ready = True
                    report("sovits", label, 1.0, "done")
                    api_client.LOGGER.info("GPT-SoVITS 使用 HTTP TTS，跳过本地 ONNX：%s", http_url)
                    return

                voice = ai_audio.SoVitsVoice()
                if not voice.available():
                    detail = voice.missing_report()
                    http_url = ""
                    try:
                        import json as _json
                        runtime_path = ai_audio.ai_home() / "tts-runtime.json"
                        if runtime_path.is_file():
                            runtime = _json.loads(runtime_path.read_text(encoding="utf-8-sig"))
                            for key in ("zh", "en"):
                                entry = runtime.get(key, {}) if isinstance(runtime, dict) else {}
                                http_url = str(entry.get("api_url", "") or "").strip()
                                if http_url:
                                    break
                    except Exception:
                        http_url = ""
                    http_url = os.environ.get("TWILIGHT_GPT_SOVITS_URL", http_url).strip()
                    if http_url:
                        self._voice = voice
                        self._voice_ready = True
                        report("sovits", label, 1.0, "done")
                        api_client.LOGGER.info("GPT-SoVITS 本地 ONNX 缺失，使用 HTTP TTS：%s；%s", http_url, detail)
                        return
                    api_client.LOGGER.warning("GPT-SoVITS unavailable: %s", detail)
                    report("sovits", label, 0.0, "error")
                    return
                report("sovits", label, 0.25)
                voice.load()
                report("sovits", label, 0.99)
                self._voice = voice
                self._voice_ready = True
                report("sovits", label, 1.0, "done")
                api_client.LOGGER.info("GPT-SoVITS 启动完成")
            except Exception as exc:  # noqa: BLE001
                api_client.LOGGER.exception("GPT-SoVITS activation failed")
                report("sovits", label, 0.0, "error")
            finally:
                finish_one()

        def load_sd() -> None:
            label = sd_label
            api_client.LOGGER.info(label)
            report("sd15", label, 0.05)
            try:
                import ai_sd15

                engine = ai_sd15.SD15Engine()
                if not engine.available():
                    detail = engine.missing_report()
                    api_client.LOGGER.warning("SD1.5 unavailable: %s", detail)
                    report("sd15", label, 0.0, "error")
                    return
                report("sd15", label, 0.25)
                engine.load()
                report("sd15", label, 0.99)
                self._sd_ready = True
                report("sd15", label, 1.0, "done")
                api_client.LOGGER.info("Stable Diffusion 启动完成")
            except Exception as exc:  # noqa: BLE001
                api_client.LOGGER.exception("SD1.5 activation failed")
                report("sd15", label, 0.0, "error")
            finally:
                finish_one()

        threading.Thread(target=load_sd, name="twilight-activate-sd15", daemon=True).start()
        threading.Thread(target=load_voice, name="twilight-activate-sovits", daemon=True).start()
        # Startup rows are transient: hide the board after both loads have had
        # ample time, even if a worker's final event is lost.
        self.root.after(15000, self._progress_window.hide_startup)

    def _finish_ai_startup(self) -> None:
        if self._closed:
            return
        reply = getattr(self, "_startup_reply", None)
        self._startup_reply = None
        if reply is not None:
            self._enter_chat(reply)

    def _build_chat_view(self) -> None:
        self._clear_body()
        self._chat_state = "ready"
        self._right_avatar_names = []
        self._current_answer_avatar = None
        view = tk.Frame(self.body, bg=theme.BG)
        view.pack(fill="both", expand=True)
        self._chat_view = view

        grid = tk.Frame(view, bg=theme.BG)
        grid.pack(fill="both", expand=True)
        grid.grid_rowconfigure(0, weight=1)
        grid.grid_rowconfigure(1, weight=0)
        grid.grid_rowconfigure(2, weight=0)
        grid.grid_columnconfigure(0, weight=1)

        content = tk.Frame(grid, bg=theme.BG)
        content.grid(row=0, column=0, sticky="nsew")
        content.grid_rowconfigure(0, weight=1)
        content.grid_columnconfigure(0, weight=1, minsize=120)
        content.grid_columnconfigure(1, weight=0)
        content.grid_columnconfigure(2, weight=1, minsize=120)

        left = tk.Frame(content, bg=theme.BG, padx=12, pady=8)
        left.grid(row=0, column=0, sticky="nsew")
        left.grid_rowconfigure(0, weight=1)
        left.grid_columnconfigure(0, weight=1)
        left.grid_columnconfigure(1, weight=0)

        self._think_text = tk.Text(
            left,
            wrap="word",
            bd=0,
            highlightthickness=0,
            relief="flat",
            bg=theme.BG,
            fg=theme.BLACK,
            insertbackground=theme.BLACK,
            selectbackground=theme.BUTTON,
            selectforeground=theme.BLACK,
            font=theme.body(11, locale=self._content_locale),
            padx=0,
            pady=0,
            spacing1=1,
            spacing2=3,
            spacing3=1,
            cursor="arrow",
            width=1,
            height=1,
        )
        think_scroll = widgets.SlimScrollbar(left, self._think_text.yview, width=8, bg=theme.BG)
        think_scroll.grid(row=0, column=1, sticky="ns", padx=(4, 0))
        self._think_text.grid(row=0, column=0, sticky="nsew")
        self._think_text.configure(yscrollcommand=think_scroll.set, state="disabled")
        self._bind_wheel(self._think_text)

        divider = tk.Frame(content, bg=theme.BLACK, width=2)
        divider.grid(row=0, column=1, sticky="ns")

        right = tk.Frame(content, bg=theme.BG, padx=12, pady=8)
        right.grid(row=0, column=2, sticky="nsew")
        right.grid_rowconfigure(0, weight=1)
        right.grid_columnconfigure(0, weight=1)
        right.grid_columnconfigure(1, weight=0)

        self._right_text = tk.Text(
            right,
            wrap="word",
            bd=0,
            highlightthickness=0,
            relief="flat",
            bg=theme.BG,
            fg=theme.BLACK,
            insertbackground=theme.BLACK,
            selectbackground=theme.BUTTON,
            selectforeground=theme.BLACK,
            font=theme.body(11, locale=self._content_locale),
            padx=0,
            pady=0,
            spacing1=1,
            spacing2=3,
            spacing3=1,
            cursor="arrow",
            width=1,
            height=1,
        )
        right_scroll = widgets.SlimScrollbar(right, self._right_text.yview, width=8, bg=theme.BG)
        right_scroll.grid(row=0, column=1, sticky="ns", padx=(4, 0))
        self._right_text.grid(row=0, column=0, sticky="nsew")
        self._right_text.configure(yscrollcommand=right_scroll.set, state="disabled")
        self._bind_wheel(self._right_text)
        self._configure_chat_tags()

        content_rule = tk.Frame(grid, bg=theme.BLACK, height=2)
        content_rule.grid(row=1, column=0, sticky="ew")

        composer_area = tk.Frame(grid, bg=theme.BG, height=169)
        composer_area.grid(row=2, column=0, sticky="nsew")
        composer_area.pack_propagate(False)
        self._composer = widgets.Composer(
            composer_area,
            on_send=self._send_chat,
            on_attach=self._add_attachment if feature_flags.ATTACHMENTS_ENABLED else None,
            height=122,
            font=theme.body(11, locale=self._content_locale),
            placeholder=self._s["composer_placeholder"],
        )
        self._composer.place(relx=0.5, y=17, anchor="n", relwidth=0.82, height=122)
        self._chat_hint = tk.Label(
            composer_area,
            text=self._s["send_hint"],
            bg=theme.BG,
            fg=theme.BLACK,
            font=theme.ui(9, locale=self._ui_locale),
        )
        if feature_flags.ATTACHMENTS_ENABLED:
            self._chat_hint.place(relx=0.35, y=148, anchor="n")
            self._attach_hint = tk.Label(
                composer_area,
                text=self._s["attach_hint"],
                bg=theme.BG,
                fg=theme.BLACK,
                font=theme.ui(9, locale=self._ui_locale),
            )
            self._attach_hint.place(relx=0.68, y=148, anchor="n")
        else:
            self._chat_hint.place(relx=0.5, y=148, anchor="n")
            self._attach_hint = None
        self._stop_hint = tk.Label(
            composer_area,
            text=self._s["stop_generating"],
            bg=theme.BG,
            fg=theme.ACCENT,
            font=theme.ui(9, locale=self._ui_locale),
            cursor="hand2",
        )
        self._stop_hint.bind("<Button-1>", lambda _event: self._cancel_generation())

    def _configure_chat_tags(self) -> None:
        body = theme.body(11, locale=self._content_locale)
        body_bold = theme.body(11, weight="bold", locale=self._content_locale)
        body_italic = theme.body(11, slant="italic", locale=self._content_locale)
        small = theme.ui(8, locale=self._ui_locale)
        mono = theme.font(9, family=theme.MONO_FAMILY)
        table_font = theme.body(9, locale=self._content_locale)
        for widget in (self._think_text, self._right_text):
            if widget is None:
                continue
            is_think = widget is self._think_text
            text_color = theme.THINKING_TEXT if is_think else theme.BLACK
            muted_color = theme.THINKING_TEXT if is_think else theme.MUTED
            highlight_color = theme.THINKING_TEXT if is_think else theme.ACCENT
            widget.tag_configure("block_gap", font=theme.font(4, family=theme.body_family(self._content_locale)))
            widget.tag_configure(
                "body",
                font=body,
                foreground=theme.BLACK,
                spacing1=1,
                spacing3=2,
                lmargin1=3,
                lmargin2=3,
            )
            widget.tag_configure(
                "user_text",
                font=body,
                foreground=theme.ACCENT,
                justify="right",
                lmargin1=32,
                lmargin2=32,
                rmargin=4,
                spacing1=10,
                spacing3=6,
            )
            widget.tag_configure(
                "user_attachment",
                font=body,
                foreground=theme.ACCENT,
                underline=True,
            )
            widget.tag_configure(
                "think_body",
                font=body,
                foreground=theme.THINKING_TEXT,
                spacing1=1,
                spacing3=2,
                lmargin1=3,
                lmargin2=3,
            )
            widget.tag_configure(
                "muted",
                font=small,
                foreground=muted_color,
                spacing1=2,
                spacing3=2,
                lmargin1=3,
                lmargin2=3,
            )
            widget.tag_configure(
                "sys_notice",
                font=small,
                foreground=theme.ACCENT,
                justify="center",
                spacing1=8,
                spacing3=6,
            )
            widget.tag_configure(
                "sys_error",
                font=small,
                foreground=theme.ERROR,
                justify="center",
                spacing1=8,
                spacing3=6,
            )
            widget.tag_configure("md_bold", font=body_bold, foreground=text_color)
            widget.tag_configure("md_italic", font=body_italic, foreground=text_color)
            widget.tag_configure(
                "md_code",
                font=mono,
                background=theme.BUTTON,
                foreground=text_color,
            )
            widget.tag_configure(
                "md_codeblock",
                font=mono,
                background=theme.BUTTON,
                foreground=text_color,
                lmargin1=12,
                lmargin2=12,
                rmargin=6,
                spacing1=4,
                spacing3=4,
            )
            widget.tag_configure(
                "md_heading",
                font=body_bold,
                foreground=highlight_color,
                spacing1=7,
                spacing3=3,
            )
            widget.tag_configure(
                "md_bullet",
                font=body,
                foreground=highlight_color,
                lmargin1=2,
                lmargin2=15,
            )
            widget.tag_configure(
                "md_quote",
                font=body_italic,
                foreground=muted_color,
                lmargin1=10,
                lmargin2=10,
                spacing1=2,
                spacing3=3,
            )
            widget.tag_configure(
                "md_table",
                font=table_font,
                foreground=text_color,
                background=theme.WHITE,
            )

    def _bind_wheel(self, widget: tk.Text) -> None:
        def on_wheel(event):
            try:
                widget.yview_scroll(-1 if event.delta > 0 else 1, "units")
            except tk.TclError:
                return "break"
            return "break"

        widget.bind("<MouseWheel>", on_wheel)
        widget.bind("<Button-4>", lambda _e: widget.yview_scroll(-1, "units"))
        widget.bind("<Button-5>", lambda _e: widget.yview_scroll(1, "units"))

    # -- initial chat content -------------------------------------------
    def _add_opening(self, reply: api_client.BootstrapReply) -> None:
        if self._right_text is None or self._think_text is None:
            return
        # Left: real bootstrap reasoning if the model supplied it.
        self._append_avatar(self._think_text, self._think_side_photo, leading_gap=False)
        reasoning = reply.reasoning.strip()
        if reasoning:
            self._append_think_markdown(reasoning)
        else:
            self._append_think(self._s["intro_think"], "muted")
        # Right: the in-character opening.
        self._append_avatar(
            self._right_text,
            self._dialogue_avatar_photo(),
            leading_gap=False,
            track_dialogue=True,
        )
        self._append_markdown(reply.content.strip())
        self._scroll_to_end(self._think_text)
        self._scroll_to_end(self._right_text)

    # -- text helpers ---------------------------------------------------
    @staticmethod
    def _near_bottom(widget: tk.Text | None) -> bool:
        if widget is None:
            return True
        try:
            return widget.yview()[1] >= 0.995
        except tk.TclError:
            return True

    def _set_normal(self, widget: tk.Text) -> None:
        try:
            widget.configure(state="normal")
        except tk.TclError:
            pass

    def _set_disabled(self, widget: tk.Text) -> None:
        try:
            widget.configure(state="disabled")
        except tk.TclError:
            pass

    def _scroll_to_end(self, widget: tk.Text | None) -> None:
        if widget is None:
            return
        try:
            widget.see("end")
        except tk.TclError:
            pass

    def _dialogue_avatar_photo(self):
        if self._chat_state == "thinking":
            return self._avatar_thinking_photo
        if self._chat_state == "offline":
            return self._avatar_offline_photo
        return self._avatar_online_photo

    def _next_avatar_name(self) -> str:
        self._avatar_seq += 1
        return f"chat_avatar_{self._avatar_seq}"

    def _append_avatar(
        self,
        widget: tk.Text,
        image,
        *,
        leading_gap: bool,
        track_dialogue: bool = False,
    ) -> str:
        name = self._next_avatar_name()
        at_end = self._near_bottom(widget)
        self._set_normal(widget)
        try:
            if leading_gap and widget.index("end-1c") != "1.0":
                widget.insert("end-1c", "\n", "block_gap")
            widget.image_create(
                "end-1c",
                image=image,
                name=name,
                align="baseline",
                padx=0,
                pady=2,
            )
            widget.insert("end-1c", "\n", "body")
        except tk.TclError:
            return name
        finally:
            self._set_disabled(widget)
        if track_dialogue:
            self._right_avatar_names.append(name)
        if at_end:
            self._scroll_to_end(widget)
        return name

    def _update_dialogue_avatars(self) -> None:
        if self._right_text is None or not self._right_avatar_names:
            return
        photo = self._dialogue_avatar_photo()
        widget = self._right_text
        try:
            was_disabled = widget.cget("state") == "disabled"
        except tk.TclError:
            return
        if was_disabled:
            self._set_normal(widget)
        for name in list(self._right_avatar_names):
            try:
                widget.image_configure(name, image=photo)
            except tk.TclError:
                pass
        if was_disabled:
            self._set_disabled(widget)

    def _append_think_markdown(self, content: str) -> None:
        if self._think_text is None or not content.strip():
            return
        at_end = self._near_bottom(self._think_text)
        self._set_normal(self._think_text)
        widgets.insert_markdown(
            self._think_text,
            content.strip(),
            base_tag="think_body",
            bold_tag="md_bold",
            italic_tag="md_italic",
            code_tag="md_code",
            codeblock_tag="md_codeblock",
            heading_tag="md_heading",
            bullet_tag="md_bullet",
            quote_tag="md_quote",
        )
        self._think_text.insert("end-1c", "\n", "think_body")
        self._set_disabled(self._think_text)
        if at_end:
            self._scroll_to_end(self._think_text)

    def _append_think(self, content: str, tag: str) -> None:
        if self._think_text is None or not content:
            return
        at_end = self._near_bottom(self._think_text)
        self._set_normal(self._think_text)
        try:
            self._think_text.insert("end-1c", content, tag)
        except tk.TclError:
            pass
        self._set_disabled(self._think_text)
        if at_end:
            self._scroll_to_end(self._think_text)

    def _append_right_text(self, content: str, tag: str) -> None:
        if self._right_text is None or not content:
            return
        # Tk Text interprets backslash escapes while inserting; forward
        # slashes keep Windows export paths intact instead of turning 	/

        # into tabs and carriage returns.
        content = content.replace("\\", "/")
        at_end = self._near_bottom(self._right_text)
        self._set_normal(self._right_text)
        try:
            self._right_text.insert("end-1c", content, tag)
        except tk.TclError:
            pass
        self._set_disabled(self._right_text)
        if at_end:
            self._scroll_to_end(self._right_text)

    def _append_markdown(self, content: str) -> None:
        if self._right_text is None:
            return
        at_end = self._near_bottom(self._right_text)
        self._set_normal(self._right_text)
        widgets.insert_markdown(
            self._right_text,
            content.strip(),
            base_tag="body",
            bold_tag="md_bold",
            italic_tag="md_italic",
            code_tag="md_code",
            codeblock_tag="md_codeblock",
            heading_tag="md_heading",
            bullet_tag="md_bullet",
            quote_tag="md_quote",
        )
        self._right_text.insert("end-1c", "\n", "body")
        self._set_disabled(self._right_text)
        if at_end:
            self._scroll_to_end(self._right_text)

    def _append_user_turn(self, content: str, attachment_names: list[str] | None = None) -> None:
        if self._right_text is None:
            return
        names = [name for name in (attachment_names or []) if name]
        content = content.rstrip()
        for name in names:
            content = content.replace(f"{name} ×", "").replace(name, "")
        content = content.strip()
        if names:
            content = (content + chr(10) + chr(10) + "附件：" + " · ".join(names)).strip()
        self._set_normal(self._right_text)
        if self._right_text.index("end-1c") != "1.0":
            self._right_text.insert("end-1c", "\n", "block_gap")
        start = self._right_text.index("end-1c")
        self._right_text.insert("end-1c", content.rstrip() + "\n", "user_text")
        end = self._right_text.index("end-1c")
        for name in names:
            pos = start
            while True:
                found = self._right_text.search(name, pos, stopindex=end)
                if not found:
                    break
                after = f"{found}+{len(name)}c"
                self._right_text.tag_add("user_attachment", found, after)
                pos = after
        self._set_disabled(self._right_text)
        self._scroll_to_end(self._right_text)

    def _attachment_error_text(self, exc: attachments.AttachmentError, filename: str = "") -> str:
        s = self._s
        mapping = {
            "unsupported": s["attach_error_unsupported"],
            "empty": s["attach_error_empty"],
            "too_large": s["attach_error_too_large"],
            "read": s["attach_error_read"],
            "ocr": s["attach_error_ocr"],
            "legacy": s["attach_error_legacy"],
            "video": s["attach_error_video"],
        }
        message = mapping.get(exc.code, s["attach_error_read"])
        if filename:
            message = f"{message}: {filename}"
        return message

    def _add_attachment(self) -> None:
        if not feature_flags.ATTACHMENTS_ENABLED or attachments is None:
            return
        if self._closed or self._mode != "chat" or self._sending or self._composer is None:
            return
        if len(self._pending_attachments) >= attachments.MAX_FILES:
            self._append_right_text("\n" + self._s["attach_limit"] + "\n", "sys_error")
            return
        s = self._s
        chosen = filedialog.askopenfilename(
            parent=self.root,
            title=s["attach_open"],
            filetypes=[
                ("PDF", "*.pdf"),
                ("Word", "*.docx"),
                ("Word 97-2003", "*.doc"),
                ("PowerPoint", "*.pptx"),
                ("PowerPoint 97-2003", "*.ppt"),
                ("Excel", "*.xlsx"),
                ("Excel 97-2003", "*.xls"),
                ("Text", "*.txt"),
                ("Markdown", "*.md"),
                ("CSV", "*.csv"),
                ("JSON", "*.json"),
                ("Python", "*.py"),
                ("PNG", "*.png"),
                ("JPEG", "*.jpg"),
                ("BMP", "*.bmp"),
                ("WebP", "*.webp"),
                ("MP4", "*.mp4"),
                ("MOV", "*.mov"),
                ("MKV", "*.mkv"),
                ("AVI", "*.avi"),
                ("WebM", "*.webm"),
                ("WMV", "*.wmv"),
                (s["all_files"], "*.*"),
            ],
        )
        if not chosen:
            return
        path = Path(chosen)
        if any(item.path == path for item in self._pending_attachments):
            self._append_right_text("\n" + s["attach_duplicate"] + "\n", "sys_error")
            return
        if self._attach_busy:
            return
        self._set_attach_busy(True)
        self._append_right_text("\n" + s["attachment_processing"] + path.name + "\n", "sys_notice")

        def work() -> None:
            try:
                attachment = attachments.extract_attachment(path)
            except attachments.AttachmentError as exc:
                self._queue.put(("attachment_error", self._attachment_error_text(exc, path.name)))
                return
            except Exception as exc:  # noqa: BLE001
                api_client.LOGGER.exception("Attachment extraction failed")
                self._queue.put((
                    "attachment_error",
                    self._attachment_error_text(
                        attachments.AttachmentError("read", str(exc)), path.name
                    ),
                ))
                return
            self._queue.put(("attachment_ready", attachment))

        threading.Thread(target=work, name="twilight-attachment", daemon=True).start()

    def _set_attach_busy(self, busy: bool) -> None:
        self._attach_busy = bool(busy)
        if self._chat_hint is not None:
            try:
                self._chat_hint.configure(
                    text=self._s["attachment_processing"] if busy else self._s["send_hint"]
                )
            except tk.TclError:
                pass

    def _handle_attachment_ready(self, attachment) -> None:
        self._set_attach_busy(False)
        if self._closed or self._mode != "chat":
            return
        if any(item.path == attachment.path for item in self._pending_attachments):
            return
        self._pending_attachments.append(attachment)

        def remove_pending(_attachment=attachment) -> None:
            try:
                self._pending_attachments.remove(_attachment)
            except ValueError:
                pass

        if self._composer is not None:
            clean_name = attachment.name
            for marker in (" 时长：", " 时长:", " 分辨率：", " 分辨率:", " 视频帧"):
                if marker in clean_name:
                    clean_name = clean_name.split(marker, 1)[0].strip()
                    break
            self._composer.insert_attachment(clean_name, on_remove=remove_pending)
        self._focus_composer()

    def _handle_attachment_error(self, message: str) -> None:
        self._set_attach_busy(False)
        if self._closed:
            return
        self._append_right_text("\n" + message + "\n", "sys_error")

    def _send_attachment_context(self) -> tuple[str, list[str]]:
        if not feature_flags.ATTACHMENTS_ENABLED or attachments is None:
            return "", []
        items = list(self._pending_attachments)
        names = [item.name for item in items]
        if not items:
            return "", names
        return attachments.build_context(items), names

    def _begin_assistant_block(self) -> None:
        if self._right_text is None:
            return
        self._answer_block_start = self._right_text.index("end-1c")
        self._current_answer_avatar = self._append_avatar(
            self._right_text,
            self._dialogue_avatar_photo(),
            leading_gap=True,
            track_dialogue=True,
        )
        self._stream_anchor = self._right_text.index("end-1c")
        self._scroll_to_end(self._right_text)

    def _begin_thinking_block(self) -> None:
        if self._think_text is None:
            return
        self._append_avatar(self._think_text, self._think_side_photo, leading_gap=True)
        self._think_stream_anchor = self._think_text.index("end-1c")
        self._reasoning_present = False

    # -- chat status ----------------------------------------------------
    def _set_chat_status(self, state: str) -> None:
        self._chat_state = state
        if self._topbar is not None:
            s = self._s
            values = {
                "ready": s["ready"],
                "thinking": s["thinking"],
                "offline": s["offline"],
                "synthesizing": s.get("synthesizing", "语音生成中…"),
                "streaming": s.get("streaming", "输出中…"),
            }
            self._topbar.set_status(s["status_prefix"], values.get(state, s["ready"]))
        self._update_dialogue_avatars()

    # ------------------------------------------------------------------
    # Sending / streaming
    # ------------------------------------------------------------------
    def _start_derpi_request(self, content: str) -> None:
        s = self._s
        display = content
        for item in self._pending_attachments:
            display = display.replace(f"{item.name} ×", item.name)
        display = display.strip()
        query = derpi.extract_query(content)
        self._sending = True
        self._composer.clear()
        self._pending_attachments.clear()
        self._set_chat_status("thinking")
        self._append_user_turn(display)
        self._append_right_text("\n" + s["local_working"] + "\n", "sys_notice")

        def work() -> None:
            try:
                images = derpi.find_images(query, 10)
                ids = [str(item.get("id")) for item in images if item.get("id")]
                self._queue.put(("tool_progress", "derpi", ids))
                paths = derpi.download_selected(images, 10, progress=self._report_progress)
                if not paths:
                    raise derpi.DerpiError(
                        "no downloadable derpicdn.net images were returned for the selected image IDs"
                    )
            except Exception as exc:  # noqa: BLE001
                api_client.LOGGER.exception("Derpibooru download failed")
                self._queue.put(("local_error", str(exc)))
                return
            self._queue.put(("local_done", "derpi", [str(path) for path in paths[:10]]))

        threading.Thread(target=work, name="twilight-derpi", daemon=True).start()

    def _start_local_request(self, request, content: str) -> None:
        s = self._s
        if request.error == "image":
            self._append_right_text("\n" + s["local_need_image"] + "\n", "sys_error")
            return
        if request.error == "video":
            self._append_right_text("\n" + s["local_need_video"] + "\n", "sys_error")
            return
        display = content
        for item in self._pending_attachments:
            display = display.replace(f"{item.name} ×", item.name)
        display = display.strip()
        self._sending = True
        self._composer.clear()
        self._pending_attachments.clear()
        self._set_chat_status("thinking")
        self._append_user_turn(display)
        self._append_right_text("\n" + s["local_working"] + "\n", "sys_notice")

        def work() -> None:
            try:
                paths = local_actions.run(request, progress=self._report_progress)
            except Exception as exc:  # noqa: BLE001
                api_client.LOGGER.exception("Local action failed")
                self._queue.put(("local_error", str(exc)))
                return
            self._queue.put(("local_done", request.kind, [str(path) for path in paths]))

        threading.Thread(target=work, name="twilight-local-action", daemon=True).start()

    def _handle_local_done(self, kind: str, paths: list[str]) -> None:
        if self._closed:
            return
        s = self._s
        key = {
            "derpi": "local_derpi_done",
            "image": "local_image_done",
            "image": "local_image_done",
            "upscale": "local_upscale_done",
            "video_upscale": "local_video_upscale_done",
            "audio": "local_audio_done",
            "transcode": "local_transcode_done",
            "vhs": "local_vhs_done",
        }.get(kind, "local_derpi_done")
        message = s[key] + "\n" + self._format_result_paths(paths)
        if paths:
            try:
                _result_dir = Path(paths[0]).parent
                message += chr(10) + "导出目录：" + _result_dir.as_posix() + chr(10) + "文件：" + chr(10).join(str(Path(p)).replace("\\", "/") for p in paths[:10])
            except Exception:
                pass
        previews = []
        try:
            import attachments as _attachment_mod
            from pathlib import Path as _Path
            for _path in paths[:10]:
                _suffix = _Path(_path).suffix.lower()
                if _suffix in {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".wmv", ".flv", ".ts"}:
                    _thumb = _attachment_mod.video_thumbnail(_path)
                    if _thumb:
                        previews.append(_thumb)
                elif _suffix in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}:
                    previews.append(_path)
        except Exception:
            previews = []
        self._append_right_text("\n" + message + "\n", "sys_notice")
        if previews:
            self._append_image_previews(previews)
        self._sending = False
        self._set_chat_status("ready")
        self._focus_composer()

    def _handle_local_error(self, message: str) -> None:
        if self._closed:
            return
        self._append_right_text(
            "\n" + self._s["local_failed"] + message + "\n",
            "sys_error",
        )
        self._sending = False
        self._set_chat_status("ready")
        self._focus_composer()

    # ------------------------------------------------------------------
    # Streaming cancellation / vision capability / VHS mode chooser
    # ------------------------------------------------------------------
    def _show_stop_control(self) -> None:
        if self._stop_hint is None:
            return
        try:
            if self._chat_hint is not None:
                self._chat_hint.place_forget()
            if self._attach_hint is not None:
                self._attach_hint.place_forget()
            self._stop_hint.place(relx=0.5, y=146, anchor="n")
        except tk.TclError:
            pass

    def _hide_stop_control(self) -> None:
        if self._stop_hint is not None:
            try:
                self._stop_hint.place_forget()
            except tk.TclError:
                pass
        try:
            if self._chat_hint is not None:
                if feature_flags.ATTACHMENTS_ENABLED:
                    self._chat_hint.place(relx=0.35, y=148, anchor="n")
                    if self._attach_hint is not None:
                        self._attach_hint.place(relx=0.68, y=148, anchor="n")
                else:
                    self._chat_hint.place(relx=0.5, y=148, anchor="n")
        except tk.TclError:
            pass

    def _cancel_generation(self) -> None:
        if not self._sending or not self._chat_streaming:
            return
        try:
            self._cancel_event.set()
        except Exception:  # noqa: BLE001
            pass

    def _handle_assistant_cancelled(self, reply) -> None:
        if self._closed or self._mode != "chat" or not self._sending:
            return
        self._chat_streaming = False
        self._hide_stop_control()
        if reply is not None and (reply.content or reply.reasoning):
            self._handle_assistant_done(reply)
        else:
            self._sending = False
            self._set_chat_status("ready")
            self._focus_composer()
        self._append_right_text("\n" + self._s["generation_stopped"] + "\n", "sys_notice")

    def _handle_vision_supported(self, supported: bool) -> None:
        self._vision_supported = bool(supported)

    def _close_vhs_dialog(self) -> None:
        dialog = self._vhs_dialog
        self._vhs_dialog = None
        if dialog is not None:
            try:
                dialog.grab_release()
            except tk.TclError:
                pass
            try:
                dialog.destroy()
            except tk.TclError:
                pass

    def _show_vhs_mode_dialog(self, request, content: str) -> None:
        if self._closed or self._mode != "chat":
            return
        if self._vhs_dialog is not None:
            try:
                self._vhs_dialog.lift()
            except tk.TclError:
                pass
            return
        s = self._s
        dialog = tk.Toplevel(self.root)
        self._vhs_dialog = dialog
        dialog.title(s["vhs_title"])
        dialog.configure(bg=theme.BG)
        dialog.resizable(False, False)
        try:
            dialog.transient(self.root)
        except tk.TclError:
            pass
        width, height = 360, 330
        try:
            rx, ry = self.root.winfo_rootx(), self.root.winfo_rooty()
            rw, rh = self.root.winfo_width(), self.root.winfo_height()
            dialog.geometry(
                f"{width}x{height}+{max(0, rx + (rw - width) // 2)}+{max(0, ry + (rh - height) // 2)}"
            )
        except tk.TclError:
            pass
        tk.Label(
            dialog,
            text=s["vhs_title"],
            bg=theme.BG,
            fg=theme.SETUP_TITLE_TEXT,
            font=theme.ui(13, locale=self._ui_locale),
            anchor="w",
        ).pack(fill="x", padx=18, pady=(16, 2))
        tk.Label(
            dialog,
            text=s["vhs_choose"],
            bg=theme.BG,
            fg=theme.INK,
            font=theme.ui(10, locale=self._ui_locale),
            anchor="w",
            justify="left",
            wraplength=320,
        ).pack(fill="x", padx=18, pady=(0, 4))
        tk.Label(
            dialog,
            text=s["vhs_hint"],
            bg=theme.BG,
            fg=theme.MUTED,
            font=theme.ui(9, locale=self._ui_locale),
            anchor="w",
            justify="left",
            wraplength=320,
        ).pack(fill="x", padx=18, pady=(0, 8))

        def choose(mode: str) -> None:
            self._close_vhs_dialog()
            request.params["mode"] = mode
            self._start_local_request(request, content)

        for mode in ("240p", "288p", "360p"):
            widgets.RoundedButton(
                dialog,
                f"VHS · {mode}",
                (lambda m=mode: choose(m)),
                width=300,
                height=36,
                font=theme.ui(11, locale=self._ui_locale),
            ).pack(padx=18, pady=4)
        widgets.RoundedButton(
            dialog,
            s["vhs_cancel"],
            self._close_vhs_dialog,
            width=120,
            height=30,
            font=theme.ui(10, locale=self._ui_locale),
        ).pack(pady=(6, 12))
        dialog.protocol("WM_DELETE_WINDOW", self._close_vhs_dialog)
        try:
            dialog.grab_set()
        except tk.TclError:
            pass

    def _show_aging_choice_dialog(self) -> None:
        if self._closed or self._mode != "chat" or not self._pending_attachments:
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("做旧方式")
        dialog.configure(bg=theme.BG)
        dialog.resizable(False, False)
        try:
            dialog.transient(self.root)
        except tk.TclError:
            pass
        mode = tk.IntVar(value=0)
        value = tk.IntVar(value=5)
        tk.Label(dialog, text="选择做旧方式", bg=theme.BG, fg=theme.SETUP_TITLE_TEXT,
                 font=theme.ui(12, locale=self._ui_locale)).pack(anchor="w", padx=18, pady=(14, 4))
        tk.Radiobutton(dialog, text="VHS化", variable=mode, value=0, bg=theme.BG,
                       fg=theme.INK, selectcolor=theme.BG, font=theme.ui(10, locale=self._ui_locale)).pack(anchor="w", padx=20)
        tk.Radiobutton(dialog, text="压片", variable=mode, value=1, bg=theme.BG,
                       fg=theme.INK, selectcolor=theme.BG, font=theme.ui(10, locale=self._ui_locale)).pack(anchor="w", padx=20)
        tk.Scale(dialog, from_=0, to=100, orient="horizontal", variable=value,
                 length=280, bg=theme.BG, fg=theme.INK, highlightthickness=0,
                 font=theme.ui(9, locale=self._ui_locale)).pack(fill="x", padx=18, pady=8)
        def apply_choice() -> None:
            amount = max(0.0, min(1.0, value.get() / 100.0))
            chosen = mode.get()
            selected = self._pending_attachments[0]
            dialog.destroy()
            self._sending = True
            self._composer.clear()
            self._pending_attachments.clear()
            self._last_sent_attachments = [selected]
            self._set_chat_status("thinking")
            self._append_user_turn(f"做旧 {int(amount * 100)}%")
            self._append_right_text(chr(10) + "正在处理…" + chr(10), "sys_notice")
            def work() -> None:
                try:
                    import effects_bridge
                    item = selected
                    mode_name = "vhs" if chosen == 0 else "squeeze"
                    variable = "q" if chosen == 0 else "crush"
                    result = effects_bridge.run(mode_name, variable, amount, item.path,
                                                progress=self._report_progress)
                    self._queue.put(("local_done", "vhs" if chosen == 0 else "transcode", [str(result)]))
                except Exception as exc:  # noqa: BLE001
                    self._queue.put(("local_error", str(exc)))
            import threading
            threading.Thread(target=work, name="twilight-aging", daemon=True).start()
        widgets.RoundedButton(dialog, "应用", apply_choice, width=120, height=32,
                              font=theme.ui(10, locale=self._ui_locale)).pack(pady=(4, 14))
        try:
            dialog.grab_set()
        except tk.TclError:
            pass

    # ------------------------------------------------------------------
    # Local AI features: 文生图 / 语音适配 / 兼容层 / 训练内嵌提示
    # ------------------------------------------------------------------
    AI_COMMANDS = ("/ai", "/sd", "/voice", "/embed")
    _SIZE_RE = re.compile(r"(?:\bsize\s*[=:：]?\s*)?\b(\d{3,4})\s*[x×]\s*(\d{3,4})\b", re.IGNORECASE)

    def _ai_command(self, content: str) -> str | None:
        raw = (content or "").strip()
        lower = raw.lower()
        for cmd in self.AI_COMMANDS:
            if lower == cmd or lower.startswith(cmd + " ") or lower.startswith(cmd + "\n"):
                return cmd
        return None

    def _ai_home(self) -> str:
        try:
            import ai_resources

            value = getattr(ai_resources, "AI_HOME", None)
            if value is None and hasattr(ai_resources, "ai_home"):
                value = ai_resources.ai_home()
            return str(value) if value is not None else ""
        except Exception:  # noqa: BLE001
            return ""

    def _start_ai_request(self, content: str) -> None:
        command = self._ai_command(content)
        if command is None or self._closed or self._sending:
            return
        s = self._s
        display = content.strip()
        argument = display[len(command):].strip() if display.upper().startswith(command.upper()) else ""
        self._sending = True
        if self._composer is not None:
            self._composer.clear()
        self._set_chat_status("thinking")
        self._append_user_turn(display)
        self._append_right_text("\n" + s["local_working"] + "\n", "sys_notice")

        def work() -> None:
            try:
                text = self._run_ai_command(command, argument)
            except Exception as exc:  # noqa: BLE001
                api_client.LOGGER.exception("AI command failed")
                message = getattr(exc, "user_message", None) or f"{type(exc).__name__}: {exc}"
                self._queue.put(("ai_error", str(message)))
                return
            self._queue.put(("ai_result", command, text))

        threading.Thread(target=work, name="twilight-ai", daemon=True).start()

    @staticmethod
    def _availability_line(label: str, checker) -> str:
        try:
            problem = checker()
            if problem:
                return f"✗ {label}：{problem}"
            return f"✓ {label}：可用"
        except Exception as exc:  # noqa: BLE001
            return f"✗ {label}：{exc}"

    def _run_ai_command(self, command: str, argument: str) -> str:
        if not feature_flags.ATTACHMENTS_ENABLED:
            raise RuntimeError("标准版不包含本地 AI 功能，请使用附件版。")
        import ai_resources

        ai_resources.ensure_layout()

        if command == "/ai":
            lines = ["AI 兼容层与资源状态（优先级 CUDA→ROCm→ZLUDA→DirectML→Vulkan→CPU）："]
            try:
                import ai_compat

                lines.extend(ai_compat.backend_report_lines())
            except Exception as exc:  # noqa: BLE001
                lines.append(f"后端探测失败：{exc}")
            try:
                lines.append(f"资源目录：{self._ai_home()}")
            except Exception:  # noqa: BLE001
                pass
            for label, fn_name in (
                ("SD1.5 基座", "require_sd15"),
                ("UVR MDX-Net", "require_uvr"),
                ("Whisper.cpp", "require_whisper"),
                ("GPT-SoVITS", "require_gpt_sovits"),
            ):
                fn = getattr(ai_resources, fn_name, None)
                if fn is None:
                    continue
                try:
                    fn()
                    lines.append(f"✓ {label}：模型文件齐全")
                except ai_resources.MissingModels as exc:
                    lines.append(f"✗ {label}：{exc.user_message}")
            for name in ("lora_status", "reference_audio_status"):
                fn = getattr(ai_resources, name, None)
                if fn is not None:
                    try:
                        lines.append(str(fn()))
                    except Exception as exc:  # noqa: BLE001
                        lines.append(f"{name} 检查失败：{exc}")
            try:
                status = local_actions.esrgan_int8_status() if local_actions is not None else {}
                if status:
                    mode = "INT8" if status.get("int8_active") else "FP16"
                    lines.append(f"Real-ESRGAN：{mode}（目录 {status.get('dir', '?')}）")
            except Exception as exc:  # noqa: BLE001
                lines.append(f"Real-ESRGAN 状态检查失败：{exc}")
            return "\n".join(lines)

        if command == "/sd":
            if not argument:
                return "用法：/sd <自然语言画面描述> [可选尺寸，如 960x720]"
            import ai_prompt
            import ai_sd15

            size = None
            match = self._SIZE_RE.search(argument)
            prompt_text = argument
            if match is not None:
                try:
                    width, height = int(match.group(1)), int(match.group(2))
                    size = (width, height)
                    prompt_text = (argument[:match.start()] + " " + argument[match.end():]).strip()
                except ValueError:
                    size = None
            prompt = ai_prompt.deepseek_to_prompt(prompt_text, settings=self.settings)
            api_client.LOGGER.info(
                "SD prompt prepared (source=%s): positive=%r negative=%r",
                prompt.source,
                prompt.positive,
                prompt.negative,
            )
            engine = ai_sd15.SD15Engine()
            if not engine.available():
                raise RuntimeError(engine.missing_report() or "SD1.5 模型缺失")

            def sd_progress(step: int, total: int) -> None:
                total = max(1, int(total))
                step = max(0, min(total, int(step)))
                self._report_progress({
                    "id": "sd15",
                    "kind": "sd15",
                    "name": "SD1.5",
                    "fraction": step / total,
                    "done": step,
                    "total": total,
                    "unit": "steps",
                    "state": "done" if step >= total else "running",
                })

            try:
                path = engine.generate(
                    prompt.positive, prompt.negative, size=size, progress=sd_progress
                )
            except Exception:
                self._report_progress({
                    "id": "sd15",
                    "kind": "sd15",
                    "name": "SD1.5",
                    "fraction": 0.0,
                    "done": 0,
                    "total": 25,
                    "unit": "steps",
                    "state": "error",
                })
                raise
            return (
                "文生图完成："
                + str(path)
                + "\n正向提示词："
                + prompt.positive
                + "\n负向提示词："
                + prompt.negative
            )

        if command == "/voice":
            import ai_audio

            lines = ["语音适配状态："]
            for label, cls_name in (
                ("UVR MDX-Net 人声分离", "VocalSeparator"),
                ("Whisper.cpp Q5_1 语音识别", "Transcriber"),
                ("GPT-SoVITS v2 音色克隆", "SoVitsVoice"),
            ):
                cls = getattr(ai_audio, cls_name, None)
                if cls is None:
                    lines.append(f"✗ {label}：模块未加载")
                    continue
                try:
                    instance = cls()
                except Exception as exc:  # noqa: BLE001
                    lines.append(f"✗ {label}：{exc}")
                    continue
                try:
                    problem = instance.missing_report()
                except Exception as exc:  # noqa: BLE001
                    problem = str(exc)
                if problem:
                    lines.append(f"✗ {label}：{problem}")
                else:
                    lines.append(f"✓ {label}：可用")
                status = getattr(instance, "status", None)
                if callable(status):
                    try:
                        lines.extend(f"  {line}" for line in status())
                    except Exception as exc:  # noqa: BLE001
                        lines.append(f"  状态读取失败：{exc}")
            try:
                lines.append(str(ai_resources.reference_audio_status()))
            except Exception as exc:  # noqa: BLE001
                lines.append(f"参考音频检查失败：{exc}")
            return "\n".join(lines)

        # /embed: 训练与内嵌由源码环境的 tools/ai_embed.py 执行（训练脚本不进入单 EXE）
        problems = ai_resources.check_training_data()
        lines = ["训练与内嵌预检："]
        lines.extend(problems if problems else ["✓ 训练用图与训练音频均已就绪。"])
        for name in ("require_sd15", "require_uvr", "require_whisper", "require_gpt_sovits"):
            fn = getattr(ai_resources, name, None)
            if fn is None:
                continue
            try:
                fn()
            except ai_resources.MissingModels as exc:
                lines.append("✗ " + exc.user_message)
        lines.append(
            "训练与单 EXE 内嵌需在源码环境执行（训练脚本不进入最终 EXE）："
            "\n  python tools/ai_embed.py preflight"
            "\n  python tools/ai_embed.py train --execute"
            "\n  python tools/ai_embed.py quantize --execute"
            "\n  python tools/ai_embed.py embed --execute"
        )
        home = self._ai_home()
        if home:
            lines.append(f"资源目录：{home}")
        return "\n".join(lines)

    def _handle_ai_result(self, command: str, text: str) -> None:
        if self._closed or self._mode != "chat":
            return
        self._append_right_text("\n" + (text or "") + "\n", "sys_notice")
        self._sending = False
        self._set_chat_status("ready")
        self._focus_composer()

    def _handle_ai_error(self, message: str) -> None:
        if self._closed or self._mode != "chat":
            return
        self._append_right_text(
            "\n" + self._s["local_failed"] + message + "\n",
            "sys_error",
        )
        self._sending = False
        self._set_chat_status("ready")
        self._focus_composer()

    def _send_chat(self) -> None:
        if self._closed or self._mode != "chat" or self._sending or self._composer is None:
            return
        if self._pending_attachments:
            visible_text = self._composer.get_value()
            self._pending_attachments = [
                item for item in self._pending_attachments if item.name in visible_text
            ]
        content = self._composer.get_value().strip()
        if not content and not self._pending_attachments:
            return
        if feature_flags.ATTACHMENTS_ENABLED and self._ai_command(content) is not None:
            self._start_ai_request(content)
            return
        if derpi.is_request(content):
            self._start_derpi_request(content)
            return
        if (self._pending_attachments
                and ("做旧" in content or "压片" in content or "压个片" in content)
                and not content.strip().startswith(("/vhs", "/压片", "/compress"))):
            self._show_aging_choice_dialog()
            return
        if (
            feature_flags.ATTACHMENTS_ENABLED
            and local_actions is not None
            and self._pending_attachments is not None
        ):
            request = local_actions.parse_request(content, self._pending_attachments)
            if request is not None:
                if request.kind == "vhs" and not request.params.get("mode"):
                    self._show_vhs_mode_dialog(request, content)
                    return
                self._start_local_request(request, content)
                return
        if not self._profile:
            self._append_right_text("\n" + self._s["missing_profile"] + "\n", "sys_error")
            return
        attachment_context, attachment_names = self._send_attachment_context()
        for name in attachment_names:
            content = content.replace(f"{name} ×", name)
        content = content.strip()
        image_items = [
            item
            for item in self._pending_attachments
            if getattr(item, "image_uri", "") or getattr(item, "image_uris", ())
        ]
        if not content:
            content = " ".join(attachment_names)
        api_content = content
        if attachment_context:
            api_content = (
                attachment_context
                + "\n\n[用户消息 / USER MESSAGE]\n"
                + content
            )
        self._sending = True
        self._last_sent_attachments = list(self._pending_attachments)
        self._current_reasoning = ""
        self._current_content = ""
        self._vision_fallback_notice = False
        self._turn_count += 1
        self._composer.clear()
        self._pending_attachments.clear()
        self._set_chat_status("thinking")
        self._append_user_turn(content, attachment_names)
        self._begin_assistant_block()
        self._begin_thinking_block()
        self._history.append({"role": "user", "content": api_content})
        self._trim_history()
        system_prompt = persona.build_system_prompt(self._profile, self._language_label)
        system_prompt += "\n\n" + local_tools.system_prompt(feature_flags.ATTACHMENTS_ENABLED)
        text_messages = [{"role": "system", "content": system_prompt}] + list(self._history)
        messages = text_messages
        if image_items and attachments is not None and self._vision_supported is not False:
            vision_messages = list(text_messages)
            vision_messages[-1] = {
                "role": "user",
                "content": attachments.build_vision_content(api_content, image_items),
            }
            messages = vision_messages
        api_key = self.settings.api_key
        base_url = self.settings.base_url
        model = self._effective_model
        self._cancel_event = threading.Event()
        cancel_event = self._cancel_event

        def on_reasoning(chunk: str) -> None:
            self._queue.put(("assistant_reasoning", chunk))

        def on_content(chunk: str) -> None:
            self._queue.put(("assistant_content", chunk))

        def work() -> None:
            had_output = False

            def emit_reasoning(chunk: str) -> None:
                nonlocal had_output
                had_output = True
                on_reasoning(chunk)

            def emit_content(chunk: str) -> None:
                nonlocal had_output
                had_output = True
                on_content(chunk)

            try:
                client = api_client.DeepSeekClient(api_key, base_url, model)
                try:
                    reply = client.stream_chat(
                        messages,
                        on_reasoning=emit_reasoning,
                        on_content=emit_content,
                        cancel_event=cancel_event,
                    )
                except api_client.ApiError as exc:
                    if (
                        image_items
                        and not had_output
                        and exc.status in (400, 404, 415, 422)
                    ):
                        self._queue.put(("vision_fallback",))
                        reply = client.stream_chat(
                            text_messages,
                            on_reasoning=emit_reasoning,
                            on_content=emit_content,
                            cancel_event=cancel_event,
                        )
                        self._queue.put(("vision_supported", False))
                    else:
                        raise
                if cancel_event.is_set():
                    self._queue.put(("assistant_cancelled", reply))
                else:
                    if image_items:
                        self._queue.put(("vision_supported", True))
                    self._queue.put(("assistant_done", reply))
            except api_client.ApiError as exc:
                if cancel_event.is_set():
                    self._queue.put(("assistant_cancelled", None))
                else:
                    self._queue.put(("assistant_error", exc.status, exc.user_message))
            except Exception as exc:  # noqa: BLE001
                api_client.LOGGER.exception("Chat streaming failed")
                if cancel_event.is_set():
                    self._queue.put(("assistant_cancelled", None))
                else:
                    self._queue.put(("assistant_error", None, f"{type(exc).__name__}: {exc}"))

        self._chat_streaming = True
        self._show_stop_control()
        threading.Thread(target=work, name="twilight-chat", daemon=True).start()

    def _handle_stream_chunk(self, kind: str, chunk: str) -> None:
        if self._closed or self._mode != "chat" or not chunk:
            return
        if kind == "assistant_reasoning":
            self._current_reasoning += chunk
            self._reasoning_present = True
            self._append_think(chunk, "think_body")
        else:
            self._current_content += chunk
            if self._voice_ready and self._voice is not None:
                return
            self._set_chat_status("streaming")
            if self._right_text is None or self._stream_anchor is None:
                return
            at_end = self._near_bottom(self._right_text)
            self._set_normal(self._right_text)
            try:
                self._right_text.insert("end-1c", chunk, "body")
            except tk.TclError:
                pass
            self._set_disabled(self._right_text)
            if at_end:
                self._scroll_to_end(self._right_text)

    def _forced_image_call(self) -> dict | None:
        """Route explicit image-generation requests to SD1.5, not Derpibooru."""
        user_text = ""
        for item in reversed(getattr(self, "_history", [])):
            if item.get("role") == "user":
                user_text = str(item.get("content", "") or "")
                break
        if not user_text:
            return None
        lower = user_text.lower()
        if any(token in lower for token in ("搜索", "搜图", "找图", "下载图", "search", "find image", "download image")):
            return None
        import re as _re
        generate = bool(_re.search(
            r"(生成|画|绘制|做|来|整|出).{0,6}(图|图片|插画|立绘|头像|壁纸|场景)"
            r"|(画|绘制|生成|做)(一张|一幅|一个|张|幅|个)"
            r"|(draw|generate|create|make).{0,16}(image|picture|illustration|art|wallpaper)",
            user_text,
            _re.IGNORECASE,
        ))
        if not generate:
            return None
        prompt = user_text.strip()
        prompt = _re.sub(r"^(请|麻烦|帮我|给我|我想要|我想|能否|可以)+", "", prompt).strip()
        prompt = _re.sub(r"(生成|绘制|画|做|来|整|出)(一张|一幅|一个|张|幅|个)?", " ", prompt)
        prompt = _re.sub(r"(图片|插画|立绘|头像|壁纸|场景|image|picture|illustration)", " ", prompt, flags=_re.IGNORECASE)
        prompt = _re.sub(r"\s+", " ", prompt).strip(" ，,。.：:") or user_text.strip()
        return {
            "tool": "image",
            "prompt": prompt[:500],
            "negative": "lowres, bad anatomy, extra limbs, text, watermark, photo, realistic",
            "size": "960x720",
        }

    def _handle_assistant_done(self, reply: api_client.ChatReply) -> None:
        if self._closed or self._mode != "chat" or not self._sending:
            return
        self._chat_streaming = False
        self._hide_stop_control()
        clean_content, tool_calls = local_tools.extract_calls(reply.content)
        forced_image = self._forced_image_call()
        if forced_image is not None:
            tool_calls = [call for call in tool_calls if str(call.get("tool", "")).lower() == "image"] or [forced_image]
        defer_for_tts = bool(self._voice_ready and self._voice is not None
                             and clean_content.strip() and not tool_calls)
        if self._right_text is not None:
            at_end = self._near_bottom(self._right_text)
            self._set_normal(self._right_text)
            if self._stream_anchor is not None:
                try:
                    self._right_text.delete(self._stream_anchor, "end-1c")
                except tk.TclError:
                    pass
            if not defer_for_tts and not tool_calls:
                widgets.insert_markdown(
                    self._right_text,
                    clean_content.strip(),
                    base_tag="body",
                    bold_tag="md_bold",
                    italic_tag="md_italic",
                    code_tag="md_code",
                    codeblock_tag="md_codeblock",
                    heading_tag="md_heading",
                    bullet_tag="md_bullet",
                    quote_tag="md_quote",
                )
                self._right_text.insert("end-1c", "\n", "body")
            self._set_disabled(self._right_text)
            self._stream_anchor = None
            self._answer_block_start = None
            if at_end:
                self._scroll_to_end(self._right_text)
        if self._think_text is not None:
            if self._current_reasoning.strip():
                at_end = self._near_bottom(self._think_text)
                self._set_normal(self._think_text)
                if self._think_stream_anchor is not None:
                    try:
                        self._think_text.delete(self._think_stream_anchor, "end-1c")
                    except tk.TclError:
                        pass
                widgets.insert_markdown(
                    self._think_text,
                    self._current_reasoning.strip(),
                    base_tag="think_body",
                    bold_tag="md_bold",
                    italic_tag="md_italic",
                    code_tag="md_code",
                    codeblock_tag="md_codeblock",
                    heading_tag="md_heading",
                    bullet_tag="md_bullet",
                    quote_tag="md_quote",
                )
                self._think_text.insert("end-1c", "\n", "think_body")
                self._set_disabled(self._think_text)
                self._think_stream_anchor = None
                if at_end:
                    self._scroll_to_end(self._think_text)
            else:
                self._append_think(self._s["no_reasoning"], "muted")
                self._think_stream_anchor = None
        if self._vision_fallback_notice:
            self._append_think(self._s["vision_fallback"] + "\n", "muted")
            self._vision_fallback_notice = False
        if clean_content.strip():
            self._history.append({"role": "assistant", "content": clean_content})
        self._trim_history()
        self._effective_model = reply.model
        self._current_answer_avatar = None
        if tool_calls:
            self._tool_answer_content = clean_content.strip() if clean_content.strip() else ""
            self._tool_audio_path = None
            self._tool_audio_ready = False
            self._tool_tools_done = False
            self._tool_outcomes = None
            self._tool_flow_done = False
            if self._tool_answer_content:
                self._set_chat_status("synthesizing")
                self._speak_reply(
                    self._tool_answer_content,
                    on_ready=self._on_tool_audio_ready,
                    defer_play=True,
                )
            else:
                self._set_chat_status("thinking")
            self._append_right_text("\n" + self._s["local_working"] + "\n", "sys_notice")
            attachments_for_tools = list(self._last_sent_attachments)

            def run_tools() -> None:
                try:
                    outcomes = local_tools.execute_calls(
                        tool_calls,
                        attachments_for_tools,
                        feature_flags.ATTACHMENTS_ENABLED,
                        on_progress=lambda tool, ids: self._queue.put(
                            ("tool_progress", tool, ids)
                        ),
                        task_progress=self._report_progress,
                    )
                except Exception as exc:  # noqa: BLE001
                    api_client.LOGGER.exception("Local tool execution failed")
                    outcomes = [local_tools.ToolOutcome(tool="local", error=str(exc))]
                self._queue.put(("tools_done", outcomes))

            threading.Thread(target=run_tools, name="twilight-local-tools", daemon=True).start()
            return
        if defer_for_tts:
            self._set_chat_status("synthesizing")
            self._speak_reply(
                clean_content,
                on_ready=lambda _path: self._queue.put(("tts_ready", clean_content)),
            )
            return
        self._sending = False
        self._set_chat_status("ready")
        self._speak_reply(clean_content)
        self._focus_composer()

    @staticmethod
    def _speech_text(content: str) -> str:
        import re
        text = re.sub(r"```.*?```", " ", content or "", flags=re.S)
        text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
        text = re.sub(r"\[[^\]]*\]\([^)]*\)", " ", text)
        text = re.sub(r"\[\[.*?\]\]", " ", text, flags=re.S)
        text = re.sub(r"(?m)^\s*(?:导出目录|文件|附件|图片|图像)\s*[:：].*$", " ", text)
        text = re.sub(r"(?m)^\s*/.*$", " ", text)
        text = re.sub(r"(?:^|\s)/[A-Za-z_]+(?:\s+[0-9.]+)?", " ", text)
        text = re.sub(r"`([^`]*)`", r"\1", text)
        text = re.sub(r"(?i)(?:[A-Z]:[\/]|/)[^\s，。；、)）]+", " ", text)
        text = re.sub(r"(?i)\bqwen_image\b|\bLOCAL_TOOL\b|\bqwen_[0-9-]+\.png\b", " ", text)
        text = re.sub(r"(?i)Qwen-Image[-0-9. Turbo]*\s*生成完成", "图片生成完成", text)
        text = re.sub(r"[（(][^）)]*[）)]", " ", text)
        text = re.sub(r"\*[^*]+\*", " ", text)
        text = re.sub(r"[—–]+", "——", text)
        text = re.sub(r"…+|\.{3,}|[~～]+", "，", text)
        text = re.sub(r"[#>*_~\-]+", " ", text)
        text = re.sub(r"[「」『』“”‘’\"']", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:1200]

    @staticmethod
    def _trim_silence_wav(path, pad_seconds: float = 0.05) -> None:
        import array
        import wave
        from pathlib import Path as _Path
        target = _Path(path)
        try:
            with wave.open(str(target), "rb") as source:
                params = source.getparams()
                if params.sampwidth != 2:
                    return
                frames = source.readframes(source.getnframes())
            samples = array.array("h")
            samples.frombytes(frames)
            if not samples:
                return
            peak = max(abs(value) for value in samples)
            threshold = max(120, int(peak * 0.02))
            first = 0
            while first < len(samples) and abs(samples[first]) < threshold:
                first += 1
            last = len(samples) - 1
            while last > first and abs(samples[last]) < threshold:
                last -= 1
            if first >= last:
                return
            pad = max(0, int(params.framerate * pad_seconds))
            first = max(0, first - pad)
            last = min(len(samples) - 1, last + pad)
            trimmed = samples[first:last + 1]
            with wave.open(str(target), "wb") as output:
                output.setparams(params)
                output.writeframes(trimmed.tobytes())
        except Exception:
            pass

    @staticmethod
    def _pause_seconds(chunk: str) -> float:
        stripped = chunk.rstrip()
        if not stripped:
            return 0.12
        last = stripped[-1]
        if last in ".!?":
            return 0.32
        if last == ";":
            return 0.20
        if last in "—–":
            return 0.24
        if last == ",":
            return 0.14
        return 0.12

    @staticmethod
    def _compress_silence_wav(path, max_seconds: float = 0.14) -> None:
        import array
        import wave
        from pathlib import Path as _Path
        target = _Path(path)
        try:
            with wave.open(str(target), "rb") as source:
                params = source.getparams()
                if params.sampwidth != 2:
                    return
                frames = source.readframes(source.getnframes())
            samples = array.array("h")
            samples.frombytes(frames)
            if not samples:
                return
            peak = max(abs(value) for value in samples)
            threshold = max(120, int(peak * 0.02))
            max_silence = max(1, int(params.framerate * max_seconds))
            output = array.array("h")
            run = 0
            for value in samples:
                if abs(value) < threshold:
                    run += 1
                    if run <= max_silence:
                        output.append(value)
                else:
                    run = 0
                    output.append(value)
            if len(output) != len(samples):
                with wave.open(str(target), "wb") as handle:
                    handle.setparams(params)
                    handle.writeframes(output.tobytes())
        except Exception:
            pass

    @staticmethod
    def _ensure_pcm16_wav(path) -> None:
        """Convert IEEE-float WAV (Rust output) to PCM16 so winsound can play it."""
        import array
        import struct
        import wave
        from pathlib import Path as _Path
        target = _Path(path)
        data = target.read_bytes()
        if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
            return
        position = 12
        fmt = None
        audio = None
        while position + 8 <= len(data):
            chunk = data[position:position + 4]
            size = struct.unpack_from("<I", data, position + 4)[0]
            body = data[position + 8:position + 8 + size]
            if chunk == b"fmt ":
                fmt = struct.unpack_from("<HHIIHH", body, 0)
                if fmt[0] == 0xFFFE and len(body) >= 40:
                    fmt = (struct.unpack_from("<H", body, 24)[0],) + fmt[1:]
            elif chunk == b"data":
                audio = body
            position += 8 + size + (size & 1)
        if fmt is None or audio is None or fmt[0] != 3 or fmt[5] != 32:
            return
        samples = array.array("f")
        samples.frombytes(audio[:len(audio) - (len(audio) % 4)])
        pcm = array.array("h", (max(-32768, min(32767, int(value * 32767))) for value in samples))
        temporary = target.with_suffix(".pcm16.wav")
        with wave.open(str(temporary), "wb") as handle:
            handle.setnchannels(fmt[1])
            handle.setsampwidth(2)
            handle.setframerate(fmt[2])
            handle.writeframes(pcm.tobytes())
        temporary.replace(target)

    @staticmethod
    def _split_tts_sentences(text: str, limit: int = 80, language: str = "zh") -> list[str]:
        import re
        if language == "en":
            protected = text
            placeholders = {}
            for index, abbr in enumerate(("Mr.", "Mrs.", "Ms.", "Dr.", "Prof.", "St.", "vs.", "etc.", "e.g.", "i.e.")):
                key = "__ABBR%d__" % index
                placeholders[key] = abbr
                protected = protected.replace(abbr, key)
            parts = [part.strip() for part in re.split(r"(?<=[.!?;])\s+", protected) if part.strip()]
            restored: list[str] = []
            for part in parts:
                for key, abbr in placeholders.items():
                    part = part.replace(key, abbr)
                restored.append(part)
            merged: list[str] = []
            index = 0
            while index < len(restored):
                part = restored[index]
                if len(part) < 35 and index + 1 < len(restored):
                    part = part + " " + restored[index + 1]
                    index += 1
                merged.append(part)
                index += 1
            final: list[str] = []
            for part in merged:
                if len(part) <= 220:
                    final.append(part)
                    continue
                words = part.split()
                current = ""
                for word in words:
                    candidate = f"{current} {word}".strip()
                    if current and len(candidate) > 220:
                        final.append(current)
                        current = word
                    else:
                        current = candidate
                if current:
                    final.append(current)
            return final or [text]
        connectors = ("却", "但", "而", "所以", "因此", "不过", "可是", "然后", "并且",
                      "同时", "还是", "或者", "因为", "如果", "虽然", "只是", "就是",
                      "也", "又", "还", "便", "就", "才")
        sentences = [part.strip() for part in re.split(r"(?<=[。！？!?；;—])", text) if part.strip()]
        sentences = [part for part in sentences if part.strip("—– ")]
        chunks = []
        for sentence in sentences:
            if len(sentence) <= limit:
                chunks.append(sentence)
                continue
            clauses = [part for part in re.split(r"(?<=[，,、])", sentence) if part.strip()]
            current = ""
            for clause in clauses:
                starts_new = clause.startswith(connectors)
                if current and len(current) + len(clause) > limit and not starts_new:
                    chunks.append(current)
                    current = clause
                else:
                    current += clause
            if current:
                chunks.append(current)
        merged = []
        for chunk in chunks:
            short = len(chunk) < 24
            starts_new = chunk.startswith(connectors)
            if (merged and (short or starts_new)
                    and not merged[-1].rstrip().endswith(("—", "–"))
                    and len(merged[-1]) + len(chunk) <= limit + 12):
                merged[-1] += chunk
            else:
                merged.append(chunk)
        final = []
        for chunk in merged:
            if len(chunk) <= limit:
                final.append(chunk)
                continue
            current = ""
            for part in re.split(r"(?<=[，,、。！？!?；;])", chunk):
                if not part:
                    continue
                current += part
                if len(current) >= limit:
                    final.append(current)
                    current = ""
            if current:
                final.append(current)
        return final or [text]

    @staticmethod
    def _http_tts_once(url: str, sentence: str, ref, prompt_text: str, language: str,
                       target, *, repetition_penalty: float = 1.3,
                       temperature: float = 0.75, top_k: int = 45,
                       top_p: float = 0.95, speed_factor: float = 0.95,
                       fragment_interval: float = 0.12) -> None:
        import json as _json
        import urllib.request
        from pathlib import Path as _Path
        payload = {
            "text": sentence,
            "text_lang": language,
            "ref_audio_path": ref.as_posix() if ref else "",
            "prompt_text": prompt_text,
            "prompt_lang": language,
            "top_k": top_k,
            "top_p": top_p,
            "temperature": temperature,
            "repetition_penalty": repetition_penalty,
            "speed_factor": speed_factor,
            "fragment_interval": fragment_interval,
            "text_split_method": "cut0",
            "batch_size": 1,
            "media_type": "wav",
            "streaming_mode": False,
        }
        request = urllib.request.Request(
            url.rstrip("/") + "/tts",
            data=_json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=300) as response:
            body = response.read()
        if not body.startswith(b"RIFF"):
            raise RuntimeError(body[:300].decode("utf-8", errors="replace"))
        _Path(target).write_bytes(body)

    @staticmethod
    def _shift_pitch(path, semitones: float) -> None:
        if abs(semitones) < 0.05:
            return
        try:
            import subprocess
            import wave
            from pathlib import Path as _Path
            import attachments
            executable = attachments.ffmpeg_executable()
            if not executable:
                return
            target = _Path(path)
            with wave.open(str(target), "rb") as source:
                rate = source.getframerate()
            ratio = 2.0 ** (semitones / 12.0)
            temporary = target.with_suffix(".pitch.wav")
            subprocess.run([
                executable, "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(target),
                "-af", f"asetrate={int(rate * ratio)},aresample={rate},atempo={1.0 / ratio:.6f}",
                "-c:a", "pcm_s16le", str(temporary),
            ], check=True)
            if temporary.is_file() and temporary.stat().st_size > 44:
                temporary.replace(target)
        except Exception:
            pass

    @staticmethod
    def _synthesize_http(url: str, text: str, reference, target, language: str) -> None:
        import wave
        from pathlib import Path as _Path
        ref = _Path(reference) if reference else None
        prompt_text = ""
        if ref is not None:
            prompt_file = ref.with_suffix(".txt")
            if prompt_file.is_file():
                prompt_text = prompt_file.read_text(encoding="utf-8").strip()
        sentences = TwilightApp._split_tts_sentences(text, language=language)
        api_client.LOGGER.info("GPT-SoVITS 合成开始：%d 句", len(sentences))
        try:
            print(f"[tts] GPT-SoVITS 合成开始：{len(sentences)} 句", flush=True)
        except Exception:
            pass
        target_path = _Path(target)
        parts: list[_Path] = []
        for index, sentence in enumerate(sentences):
            percent = int(index * 100 / max(1, len(sentences)))
            api_client.LOGGER.info("GPT-SoVITS 生成中：第 %d/%d 句（%d%%）", index + 1, len(sentences), percent)
            try:
                print(f"[tts] GPT-SoVITS 生成中：{index + 1}/{len(sentences)}（{percent}%）", flush=True)
            except Exception:
                pass
            part = target_path.with_name(f".{target_path.stem}.part{index}.wav")
            last = sentence.rstrip()[-1] if sentence.rstrip() else ""
            if last == "?":
                options = {"temperature": 0.92, "top_k": 60, "speed_factor": 0.97,
                           "fragment_interval": 0.16}
                pitch = 1.0
            elif last == "!":
                options = {"temperature": 0.88, "top_k": 55, "speed_factor": 1.03,
                           "fragment_interval": 0.10}
                pitch = 0.5
            else:
                options = {}
                pitch = 0.0
            spoken_sentence = sentence.rstrip("—– ").strip() or sentence
            TwilightApp._http_tts_once(url, spoken_sentence, ref, prompt_text, language, part, **options)
            TwilightApp._ensure_pcm16_wav(part)
            if pitch:
                TwilightApp._shift_pitch(part, pitch)
            TwilightApp._trim_silence_wav(part)
            TwilightApp._compress_silence_wav(part, 0.14)
            # A short sentence should not produce a long repeated tail.
            try:
                with wave.open(str(part), "rb") as probe:
                    duration = probe.getnframes() / max(1, probe.getframerate())
                allowed = max(5.0, len(sentence) * 0.85)
                if duration > allowed * 1.8:
                    TwilightApp._http_tts_once(url, sentence, ref, prompt_text, language, part,
                                       repetition_penalty=1.65, temperature=0.65)
                    TwilightApp._ensure_pcm16_wav(part)
                    with wave.open(str(part), "rb") as probe:
                        duration = probe.getnframes() / max(1, probe.getframerate())
                    if duration > allowed * 2.0:
                        TwilightApp._trim_wav(part, allowed)
            except Exception:
                pass
            parts.append(part)
        if not parts:
            raise RuntimeError("TTS 没有返回任何语音片段")
        with wave.open(str(parts[0]), "rb") as first:
            params = first.getparams()
        with wave.open(str(target_path), "wb") as merged:
            merged.setparams(params)
            for index, part in enumerate(parts):
                with wave.open(str(part), "rb") as source:
                    merged.writeframes(source.readframes(source.getnframes()))
                if index < len(parts) - 1:
                    pause = TwilightApp._pause_seconds(sentences[index])
                    silence = max(0, int(params.framerate * pause))
                    merged.writeframes(b"\x00\x00" * silence)
                try:
                    part.unlink()
                except OSError:
                    pass

    @staticmethod
    def _normalize_wav(path) -> None:
        import array
        import wave
        from pathlib import Path as _Path
        target = _Path(path)
        try:
            with wave.open(str(target), "rb") as source:
                params = source.getparams()
                if params.sampwidth != 2:
                    return
                frames = source.readframes(source.getnframes())
            samples = array.array("h")
            samples.frombytes(frames)
            peak = max((abs(value) for value in samples), default=0)
            desired = int(32767 * 0.89)
            if peak <= 0 or peak >= desired:
                return
            gain = desired / peak
            for index, value in enumerate(samples):
                samples[index] = max(-32768, min(32767, int(value * gain)))
            with wave.open(str(target), "wb") as output:
                output.setparams(params)
                output.writeframes(samples.tobytes())
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _trim_wav(path, max_seconds: float = 25.0) -> None:
        import wave
        from pathlib import Path as _Path
        target = _Path(path)
        try:
            with wave.open(str(target), "rb") as source:
                params = source.getparams()
                max_frames = int(max_seconds * params.framerate)
                if source.getnframes() <= max_frames:
                    return
                frames = source.readframes(max_frames)
            with wave.open(str(target), "wb") as output:
                output.setparams(params)
                output.writeframes(frames)
        except Exception:  # noqa: BLE001
            pass

    def _stream_answer(self, content: str) -> None:
        """Reveal the answer after TTS, line by line, then release the composer."""
        import re as _re
        content = _re.sub(r"\[\[GALLERY:[^\]]+\]\]", "", content)
        content = content.replace("\\", "/")
        self._set_chat_status("streaming")
        if self._right_text is None:
            self._sending = False
            self._set_chat_status("ready")
            self._focus_composer()
            return
        lines = content.strip().splitlines() or [""]
        index = 0

        def step() -> None:
            nonlocal index
            if self._closed:
                return
            if index >= len(lines):
                self._right_text.insert("end-1c", "\n", "body")
                self._set_disabled(self._right_text)
                self._sending = False
                self._set_chat_status("ready")
                self._focus_composer()
                return
            line = lines[index]
            index += 1
            at_end = self._near_bottom(self._right_text)
            self._set_normal(self._right_text)
            widgets.insert_markdown(
                self._right_text,
                line,
                base_tag="body",
                bold_tag="md_bold",
                italic_tag="md_italic",
                code_tag="md_code",
                codeblock_tag="md_codeblock",
                heading_tag="md_heading",
                bullet_tag="md_bullet",
                quote_tag="md_quote",
            )
            self._right_text.insert("end-1c", "\n", "body")
            self._set_disabled(self._right_text)
            if at_end:
                self._scroll_to_end(self._right_text)
            self.root.after(70, step)

        step()

    def _handle_tts_ready(self, content: str) -> None:
        if self._closed:
            return
        self._stream_anchor = None
        self._answer_block_start = None
        self._stream_answer(content)

    @staticmethod
    def _split_tts_text(text: str, limit: int = 24) -> list[str]:
        import re
        chunks = [part for part in re.split(r"(?<=[，。！？；,.!?;])", text) if part.strip()]
        merged: list[str] = []
        for chunk in chunks:
            chunk = chunk.strip()
            if not chunk:
                continue
            if merged and len(merged[-1]) + len(chunk) <= limit:
                merged[-1] += chunk
            else:
                merged.append(chunk)
        result: list[str] = []
        for chunk in merged:
            if len(chunk) <= limit:
                result.append(chunk)
                continue
            for index in range(0, len(chunk), limit):
                result.append(chunk[index:index + limit])
        import re as _re
        return [part for part in result if _re.search(r"[\w一-鿿]", part)]

    def _speak_reply(self, content: str, on_ready=None, defer_play: bool = False) -> None:
        """Synthesise the reply with the local GPT-SoVITS voice and play it."""
        api_client.LOGGER.info("回复TTS检查：ready=%s voice=%s closed=%s", self._voice_ready, self._voice is not None, self._closed)
        if self._closed:
            return
        import ai_audio
        text = self._speech_text(content)
        if not text:
            if on_ready is not None:
                on_ready(None)
            return
        chinese = any("\u4e00" <= char <= "\u9fff" for char in text)
        language = "CN" if chinese else "EN"
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = ai_audio.ai_home() / "outputs" / "tts" / f"{language}-{stamp}.wav"
        target.parent.mkdir(parents=True, exist_ok=True)
        references = ai_audio._find_reference_audio() or []
        wanted = ("zh", "cn") if chinese else ("en",)
        user_refs = [path for path in references
                     if path.name.lower().startswith("twi-")
                     and any(token in path.name.lower() for token in wanted)]
        preferred_refs = []
        for token in ("clean-norm", "demucs-norm", "ref13-denoise", "ref13-soft", "ref13-norm", "ref5-norm", "80s-ref"):
            preferred_refs = [path for path in user_refs if token in path.name.lower()]
            if preferred_refs:
                break
        reference = next(
            iter(preferred_refs or user_refs),
            next(
                (path for path in references if any(token in path.name.lower() for token in wanted)),
                references[0] if references else None,
            ),
        )
        runtime = {}
        try:
            runtime_path = ai_audio.ai_home() / "tts-runtime.json"
            if runtime_path.is_file():
                import json as _json
                runtime = _json.loads(runtime_path.read_text(encoding="utf-8-sig"))
        except Exception:
            runtime = {}
        runtime_key = "zh" if chinese else "en"
        runtime_entry = runtime.get(runtime_key, {}) if isinstance(runtime, dict) else {}
        configured_reference = str(runtime_entry.get("reference", "") or "").strip()
        if configured_reference:
            candidate = Path(configured_reference).expanduser()
            if candidate.is_file():
                reference = candidate
        configured_url = str(runtime_entry.get("api_url", "") or "").strip()
        server_url = os.environ.get(
            "TWILIGHT_GPT_SOVITS_URL" if chinese else "TWILIGHT_GPT_SOVITS_URL_EN",
            configured_url,
        ).strip()
        if not self._voice_ready or self._voice is None:
            if not server_url:
                api_client.LOGGER.warning("TTS 未就绪且未配置 HTTP API：ready=%s voice=%s", self._voice_ready, self._voice is not None)
                if on_ready is not None:
                    on_ready(None)
                return

        progress_id = f"tts-{stamp}"

        def report_tts(fraction: float, state: str = "running") -> None:
            self._report_progress({
                "id": progress_id,
                "kind": "sovits",
                "label": "GPT-SoVITS 正在生成语音…",
                "name": Path(reference).name if reference else "",
                "fraction": max(0.0, min(1.0, fraction)),
                "state": state,
            })

        report_tts(0.02)
        import threading as _threading

        def run() -> None:
            ready = target
            stop = _threading.Event()

            def ticker() -> None:
                started = time.time()
                while not stop.wait(0.75):
                    report_tts(min(0.9, 0.02 + (time.time() - started) / 30.0 * 0.88))

            _threading.Thread(target=ticker, name="twilight-tts-progress", daemon=True).start()
            try:
                server = server_url
                if server:
                    self._synthesize_http(server, text, reference, target, "zh" if chinese else "en")
                else:
                    self._voice.synthesize(text, reference_audio=reference, out_path=target)
                stop.set()
                report_tts(0.95)
                self._ensure_pcm16_wav(target)
                self._trim_wav(target, 120.0)
                self._normalize_wav(target)
                api_client.LOGGER.info("回复语音已保存：%s", target)
                if not defer_play:
                    import winsound
                    winsound.PlaySound(str(target), winsound.SND_FILENAME | winsound.SND_ASYNC)
            except Exception:  # noqa: BLE001
                stop.set()
                report_tts(1.0, "error")
                ready = None
                api_client.LOGGER.exception("回复语音合成失败")
            else:
                report_tts(1.0, "done")
            finally:
                stop.set()
                if on_ready is not None:
                    self.root.after(0, lambda: on_ready(ready))

        threading.Thread(target=run, name="twilight-reply-tts", daemon=True).start()

    def _report_progress(self, event: dict) -> None:
        try:
            self._progress_window.report(event)
        except Exception:  # noqa: BLE001
            pass

    def _format_result_paths(self, paths: list[str]) -> str:
        if len(paths) <= 60:
            return "\n".join(paths)
        shown = "\n".join(paths[:40])
        try:
            folder = str(Path(paths[0]).parent)
        except Exception:  # noqa: BLE001
            folder = ""
        return shown + f"\n… (+{len(paths) - 40} files)" + (f"\n{folder}" if folder else "")

    def _handle_tool_progress(self, tool: str, ids: list[str]) -> None:
        if self._closed or tool != "derpi" or not ids:
            return
        shown = ", ".join(ids[:24])
        if len(ids) > 24:
            shown += f" … (+{len(ids) - 24})"
        self._append_right_text(
            "\n" + self._s["local_ids_found"] + "\n" + shown + "\n",
            "sys_notice",
        )

    def _image_max_width(self) -> int:
        try:
            width = max(1, self._right_text.winfo_width())
        except Exception:
            width = 0
        return 280 if width < 100 else max(220, min(320, int(width * 0.42)))

    def _append_image_previews(self, paths) -> None:
        if self._right_text is None:
            return
        try:
            from PIL import Image, ImageTk
            from pathlib import Path as _Path
        except Exception:
            return
        image_paths = [p for p in paths if _Path(p).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}]
        if not image_paths:
            return
        if not hasattr(self, "_chat_image_entries"):
            self._chat_image_entries = []
        if not hasattr(self, "_chat_image_refs"):
            self._chat_image_refs = []
        if not getattr(self, "_image_resize_bound", False):
            self._right_text.bind("<Configure>", self._on_chat_image_resize)
            self._image_resize_bound = True
        max_width = self._image_max_width()
        self._set_normal(self._right_text)
        try:
            self._right_text.tag_configure("image_center", justify="center", spacing1=6, spacing3=6)
        except Exception:
            pass
        for raw in image_paths[:4]:
            try:
                image = Image.open(raw).convert("RGB")
                image.thumbnail((max_width, max_width), Image.Resampling.LANCZOS)
                photo = ImageTk.PhotoImage(image)
                position = self._right_text.image_create("end-1c", image=photo, padx=8, pady=6)
                tag = f"imgprev_{len(self._chat_image_entries)}"
                self._right_text.tag_add(tag, position, position)
                self._right_text.tag_add("image_center", position, position)
                self._right_text.insert("end-1c", "\n", "body")
                self._chat_image_refs.append(photo)
                self._chat_image_entries.append({"path": str(raw), "tag": tag})
                self._right_text.tag_bind(tag, "<Button-1>",
                                          lambda _event, _path=str(raw): self._open_preview(_path))
            except Exception:
                continue
        self._set_disabled(self._right_text)
        self._scroll_to_end(self._right_text)

    def _open_preview(self, path: str) -> None:
        import os
        from pathlib import Path as _Path
        target = _Path(path)
        if target.name.endswith(".thumb.jpg"):
            stem = target.with_suffix("").with_suffix("")
            for ext in (".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".wmv", ".flv", ".ts"):
                candidate = stem.with_suffix(ext)
                if candidate.is_file():
                    target = candidate
                    break
        try:
            os.startfile(str(target))
        except OSError:
            pass

    def _on_chat_image_resize(self, _event=None) -> None:
        job = getattr(self, "_image_resize_job", None)
        if job is not None:
            try:
                self.root.after_cancel(job)
            except Exception:
                pass
        self._image_resize_job = self.root.after(220, self._refresh_image_previews)

    def _refresh_image_previews(self) -> None:
        self._image_resize_job = None
        if self._right_text is None or not getattr(self, "_chat_image_entries", None):
            return
        try:
            from PIL import Image, ImageTk
        except Exception:
            return
        max_width = self._image_max_width()
        self._set_normal(self._right_text)
        for entry in reversed(self._chat_image_entries):
            try:
                ranges = self._right_text.tag_ranges(entry["tag"])
                if not ranges:
                    continue
                start = str(ranges[0])
                self._right_text.delete(start, str(ranges[-1]))
                image = Image.open(entry["path"]).convert("RGB")
                image.thumbnail((max_width, max_width), Image.Resampling.LANCZOS)
                photo = ImageTk.PhotoImage(image)
                position = self._right_text.image_create(start, image=photo, padx=8, pady=6)
                self._right_text.tag_add(entry["tag"], position, position)
                self._right_text.tag_add("image_center", position, position)
                self._chat_image_refs.append(photo)
            except Exception:
                continue
        self._set_disabled(self._right_text)

    def _tool_audio_ready(self, path) -> None:
        self._tool_audio_path = path if path else None
        if getattr(self, "_tools_finished", False):
            self._play_tool_audio()

    def _play_tool_audio(self) -> None:
        path = getattr(self, "_tool_audio_path", None)
        if not path or getattr(self, "_tool_audio_played", False):
            return
        try:
            import winsound
            winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC)
            self._tool_audio_played = True
        except Exception:
            api_client.LOGGER.exception("工具结果语音播放失败")

    def _on_tool_audio_ready(self, path) -> None:
        self._tool_audio_path = path if path else None
        self._tool_audio_ready = True
        self._maybe_finish_tool_flow()

    def _maybe_finish_tool_flow(self) -> None:
        if self._closed or getattr(self, "_tool_flow_done", False):
            return
        if not getattr(self, "_tool_tools_done", False):
            return
        if getattr(self, "_tool_answer_content", "") and not getattr(self, "_tool_audio_ready", False):
            return
        self._tool_flow_done = True
        self._render_tool_outcomes(getattr(self, "_tool_outcomes", []) or [])
        self._play_tool_audio()
        content = getattr(self, "_tool_answer_content", "")
        self._tool_answer_content = ""
        if content:
            self._stream_answer(content)
        else:
            self._sending = False
            self._set_chat_status("ready")
            self._focus_composer()

    def _render_tool_outcomes(self, outcomes) -> None:
        s = self._s
        key_by_tool = {
            "derpi": "local_derpi_done",
            "upscale": "local_upscale_done",
            "video_upscale": "local_video_upscale_done",
            "audio": "local_audio_done",
            "transcode": "local_transcode_done",
            "vhs": "local_vhs_done",
        }
        for outcome in outcomes:
            api_client.LOGGER.info("工具调用完成：%s paths=%d error=%s", outcome.tool, len(outcome.paths), outcome.error or "-")
            try:
                self._report_progress({
                    "id": f"tool-{outcome.tool}",
                    "kind": "tool",
                    "label": f"工具完成：{outcome.tool}",
                    "fraction": 1.0,
                    "state": "error" if outcome.error else "done",
                })
            except Exception:
                pass
            if outcome.error:
                self._append_right_text("\n" + s["local_failed"] + outcome.error + "\n", "sys_error")
                continue
            if outcome.tool == "image":
                heading = s.get("local_image_done", "Generated: ")
            else:
                key = key_by_tool.get(outcome.tool, "local_derpi_done")
                heading = s.get(key, s["local_derpi_done"])
            previews = list(getattr(outcome, "previews", []) or [])
            if not previews:
                previews = list(outcome.paths or [])
            if previews:
                self._append_image_previews(previews)
            if not getattr(self, "_tool_answer_content", ""):
                message = heading + chr(10) + self._format_result_paths(outcome.paths)
                if outcome.paths:
                    try:
                        message += chr(10) + "导出目录：" + Path(outcome.paths[0]).parent.as_posix()
                    except Exception:
                        pass
                self._append_right_text(chr(10) + message + chr(10), "sys_notice")

    def _handle_tools_done(self, outcomes) -> None:
        if self._closed:
            return
        self._tool_outcomes = outcomes
        self._tool_tools_done = True
        if getattr(self, "_tool_answer_content", ""):
            self._set_chat_status("synthesizing")
        self._maybe_finish_tool_flow()

    def _handle_chat_error(self, status: int | None, message: str) -> None:
        if self._closed or self._mode != "chat" or not self._sending:
            return
        self._chat_streaming = False
        self._hide_stop_control()
        api_client.LOGGER.error("Chat request failed: %s", message)
        if self._right_text is not None:
            self._set_normal(self._right_text)
            if self._answer_block_start is not None:
                try:
                    self._right_text.delete(self._answer_block_start, "end-1c")
                except tk.TclError:
                    pass
            text = self._setup_error_text(status, message)
            self._right_text.insert("end-1c", "\n", "block_gap")
            self._right_text.insert("end-1c", text + "\n", "sys_error")
            self._set_disabled(self._right_text)
            self._scroll_to_end(self._right_text)
        self._stream_anchor = None
        self._think_stream_anchor = None
        self._answer_block_start = None
        if self._current_answer_avatar is not None:
            try:
                self._right_avatar_names.remove(self._current_answer_avatar)
            except ValueError:
                pass
        self._current_answer_avatar = None
        if self._history and self._history[-1].get("role") == "user":
            self._history.pop()
        if self._think_text is not None:
            self._append_think(self._s["network_error"] + "\n", "muted")
        if self._vision_fallback_notice:
            if self._think_text is not None:
                self._append_think(self._s["vision_fallback"] + "\n", "muted")
            self._vision_fallback_notice = False
        self._sending = False
        self._set_chat_status("offline")
        self._focus_composer()

    def _trim_history(self) -> None:
        total = sum(len(item.get("content", "")) for item in self._history)
        # Keep the most recent turn even if it is very large (e.g. one big
        # attachment context block), but never retain several huge turns.
        while self._history and (
            len(self._history) > 30 or (total > 60_000 and len(self._history) > 1)
        ):
            removed = self._history.pop(0)
            total -= len(removed.get("content", ""))

    def _focus_composer(self) -> None:
        if self._closed or self._mode != "chat" or self._composer is None:
            return
        try:
            self._composer.focus_input()
        except tk.TclError:
            pass

    # ------------------------------------------------------------------
    # Queue polling
    # ------------------------------------------------------------------
    def _drain_queue(self) -> None:
        if self._closed:
            return
        try:
            events: list[tuple] = []
            while True:
                try:
                    events.append(self._queue.get_nowait())
                except queue.Empty:
                    break
            index = 0
            while index < len(events):
                try:
                    kind = events[index][0]
                    if kind in ("assistant_reasoning", "assistant_content"):
                        buffer: list[str] = []
                        while index < len(events) and events[index][0] == kind:
                            buffer.append(events[index][1])
                            index += 1
                        self._handle_stream_chunk(kind, "".join(buffer))
                        continue
                    if kind == "prep_ok":
                        _, reply, key, language = events[index]
                        self._handle_prep_ok(reply, key, language)
                    elif kind == "prep_error":
                        _, status, message = events[index]
                        self._handle_prep_error(status, message)
                    elif kind == "assistant_done":
                        _, reply = events[index]
                        self._handle_assistant_done(reply)
                    elif kind == "tool_final_ready":
                        _, tool_content, tool_audio = events[index]
                        if tool_audio:
                            try:
                                import winsound
                                winsound.PlaySound(str(tool_audio), winsound.SND_FILENAME | winsound.SND_ASYNC)
                            except Exception:
                                api_client.LOGGER.exception("工具结果语音播放失败")
                        self._stream_answer(tool_content)
                    elif kind == "tts_ready":
                        _, tts_content = events[index]
                        self._handle_tts_ready(tts_content)
                    elif kind == "assistant_cancelled":
                        _, reply = events[index]
                        self._handle_assistant_cancelled(reply)
                    elif kind == "assistant_error":
                        _, status, message = events[index]
                        self._handle_chat_error(status, message)
                    elif kind == "vision_fallback":
                        self._vision_fallback_notice = True
                    elif kind == "vision_supported":
                        _, supported = events[index]
                        self._handle_vision_supported(supported)
                    elif kind == "attachment_ready":
                        _, attachment = events[index]
                        self._handle_attachment_ready(attachment)
                    elif kind == "attachment_error":
                        _, message = events[index]
                        self._handle_attachment_error(message)
                    elif kind == "tool_progress":
                        _, progress_tool, ids = events[index]
                        self._handle_tool_progress(progress_tool, ids)
                    elif kind == "local_done":
                        _, local_kind, paths = events[index]
                        self._handle_local_done(local_kind, paths)
                    elif kind == "local_error":
                        _, local_message = events[index]
                        self._handle_local_error(local_message)
                    elif kind == "ai_result":
                        _, ai_command, ai_text = events[index]
                        self._handle_ai_result(ai_command, ai_text)
                    elif kind == "ai_error":
                        _, ai_message = events[index]
                        self._handle_ai_error(ai_message)
                    elif kind == "tools_done":
                        _, outcomes = events[index]
                        self._handle_tools_done(outcomes)
                except Exception:  # noqa: BLE001
                    api_client.LOGGER.exception("Queue event handler failed")
                index += 1
        finally:
            if not self._closed:
                try:
                    self._queue_job = self.root.after(35, self._drain_queue)
                except tk.TclError:
                    pass

    def _report_callback_exception(self, exc, value, tb) -> None:
        api_client.LOGGER.error(
            "Tk callback exception:\n%s",
            "".join(traceback.format_exception(exc, value, tb)),
        )


def run(root: tk.Tk, home: Path | None = None) -> TwilightApp:
    app = TwilightApp(root, home or api_client.default_home())

    def on_delete() -> None:
        app.close()

    root.protocol("WM_DELETE_WINDOW", on_delete)
    return app
