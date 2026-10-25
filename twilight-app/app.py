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
import threading
import traceback
from pathlib import Path
import tkinter as tk
from tkinter import filedialog

import api_client
import derpi
import feature_flags
import local_tools
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

        self._brand_canvas = tk.Canvas(
            view,
            bg=theme.BG,
            highlightthickness=0,
            bd=0,
            width=200,
            height=40,
        )
        self._brand_canvas.place(x=22, rely=1.0, y=-24, anchor="sw")

        self._pony_label = tk.Label(view, image=self._pony_photo, bg=theme.BG, bd=0)
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
            self._pony_label.place_configure(x=width - 20, y=form_y + 20, anchor="se")
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
                        x, baseline, text=char, anchor="sw", fill=theme.BLACK, font=font
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
            self._set_form_message(self._s["missing_profile"], color=theme.ERROR)
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
            api_client.save_settings(self.home, self.settings)
        except OSError as exc:
            self._saved_key_warning = self._s["saved_warning"]
            api_client.LOGGER.warning("Could not save API key: %s", exc)
        self._enter_chat(reply)

    def _handle_prep_error(self, status: int | None, message: str) -> None:
        if self._closed or self._mode != "setup" or not self._preparing:
            return
        self._stop_setup_animation()
        self._set_setup_busy(False)
        self._set_form_message(self._s["error_prefix"], color=theme.ERROR)
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
        self._set_chat_status("ready")
        if self._saved_key_warning and self._right_text is not None:
            self._append_right_text("\n" + self._saved_key_warning + "\n", "sys_notice")
        if self._resizer is not None:
            self._resizer.lift()
        self.root.after(80, self._focus_composer)

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
                foreground=theme.BLACK,
                spacing1=1,
                spacing3=2,
                lmargin1=3,
                lmargin2=3,
            )
            widget.tag_configure(
                "muted",
                font=small,
                foreground=theme.MUTED,
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
            widget.tag_configure("md_bold", font=body_bold, foreground=theme.BLACK)
            widget.tag_configure("md_italic", font=body_italic, foreground=theme.BLACK)
            widget.tag_configure(
                "md_code",
                font=mono,
                background=theme.BUTTON,
                foreground=theme.BLACK,
            )
            widget.tag_configure(
                "md_codeblock",
                font=mono,
                background=theme.BUTTON,
                foreground=theme.BLACK,
                lmargin1=12,
                lmargin2=12,
                rmargin=6,
                spacing1=4,
                spacing3=4,
            )
            widget.tag_configure(
                "md_heading",
                font=body_bold,
                foreground=theme.ACCENT,
                spacing1=7,
                spacing3=3,
            )
            widget.tag_configure(
                "md_bullet",
                font=body,
                foreground=theme.ACCENT,
                lmargin1=2,
                lmargin2=15,
            )
            widget.tag_configure(
                "md_quote",
                font=body_italic,
                foreground=theme.MUTED,
                lmargin1=10,
                lmargin2=10,
                spacing1=2,
                spacing3=3,
            )
            widget.tag_configure(
                "md_table",
                font=table_font,
                foreground=theme.BLACK,
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
        for name in names:
            if name not in content:
                content = (content + " " + name).strip()
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
        try:
            attachment = attachments.extract_attachment(path)
        except attachments.AttachmentError as exc:
            self._append_right_text("\n" + self._attachment_error_text(exc, path.name) + "\n", "sys_error")
            return
        except Exception as exc:  # noqa: BLE001
            api_client.LOGGER.exception("Attachment extraction failed")
            self._append_right_text("\n" + self._attachment_error_text(
                attachments.AttachmentError("read", str(exc)), path.name
            ) + "\n", "sys_error")
            return
        self._pending_attachments.append(attachment)

        def remove_pending(_attachment=attachment) -> None:
            try:
                self._pending_attachments.remove(_attachment)
            except ValueError:
                pass

        self._composer.insert_attachment(attachment.name, on_remove=remove_pending)
        self._focus_composer()

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
            values = {"ready": s["ready"], "thinking": s["thinking"], "offline": s["offline"]}
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
                images = derpi.find_images(query, 4)
                ids = [str(item.get("id")) for item in images if item.get("id")]
                self._queue.put(("tool_progress", "derpi", ids))
                paths = derpi.download_selected(images, 4)
                if not paths:
                    raise derpi.DerpiError(
                        "no downloadable derpicdn.net images were returned for the selected image IDs"
                    )
            except Exception as exc:  # noqa: BLE001
                api_client.LOGGER.exception("Derpibooru download failed")
                self._queue.put(("local_error", str(exc)))
                return
            self._queue.put(("local_done", "derpi", [str(path) for path in paths]))

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
                paths = local_actions.run(request)
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
            "upscale": "local_upscale_done",
            "audio": "local_audio_done",
            "transcode": "local_transcode_done",
        }.get(kind, "local_derpi_done")
        message = s[key] + "\n" + "\n".join(paths)
        self._append_right_text("\n" + message + "\n", "sys_notice")
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
        if derpi.is_request(content):
            self._start_derpi_request(content)
            return
        if (
            feature_flags.ATTACHMENTS_ENABLED
            and local_actions is not None
            and self._pending_attachments is not None
        ):
            request = local_actions.parse_request(content, self._pending_attachments)
            if request is not None:
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
        if image_items and attachments is not None:
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
                    else:
                        raise
                if not cancel_event.is_set():
                    self._queue.put(("assistant_done", reply))
            except api_client.ApiError as exc:
                if not cancel_event.is_set():
                    self._queue.put(("assistant_error", exc.status, exc.user_message))
            except Exception as exc:  # noqa: BLE001
                api_client.LOGGER.exception("Chat streaming failed")
                if not cancel_event.is_set():
                    self._queue.put(("assistant_error", None, f"{type(exc).__name__}: {exc}"))

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

    def _handle_assistant_done(self, reply: api_client.ChatReply) -> None:
        if self._closed or self._mode != "chat" or not self._sending:
            return
        clean_content, tool_calls = local_tools.extract_calls(reply.content)
        if self._right_text is not None:
            at_end = self._near_bottom(self._right_text)
            self._set_normal(self._right_text)
            if self._stream_anchor is not None:
                try:
                    self._right_text.delete(self._stream_anchor, "end-1c")
                except tk.TclError:
                    pass
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
        self._history.append({"role": "assistant", "content": clean_content})
        self._trim_history()
        self._effective_model = reply.model
        self._current_answer_avatar = None
        if tool_calls:
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
                    )
                except Exception as exc:  # noqa: BLE001
                    api_client.LOGGER.exception("Local tool execution failed")
                    outcomes = [local_tools.ToolOutcome(tool="local", error=str(exc))]
                self._queue.put(("tools_done", outcomes))

            threading.Thread(target=run_tools, name="twilight-local-tools", daemon=True).start()
            return
        self._sending = False
        self._set_chat_status("ready")
        self._focus_composer()

    def _handle_tool_progress(self, tool: str, ids: list[str]) -> None:
        if self._closed or tool != "derpi" or not ids:
            return
        self._append_right_text(
            "\n" + self._s["local_ids_found"] + "\n" + ", ".join(ids) + "\n",
            "sys_notice",
        )

    def _handle_tools_done(self, outcomes) -> None:
        if self._closed:
            return
        s = self._s
        key_by_tool = {
            "derpi": "local_derpi_done",
            "upscale": "local_upscale_done",
            "audio": "local_audio_done",
            "transcode": "local_transcode_done",
        }
        for outcome in outcomes:
            if outcome.error:
                self._append_right_text(
                    "\n" + s["local_failed"] + outcome.error + "\n",
                    "sys_error",
                )
                continue
            key = key_by_tool.get(outcome.tool, "local_derpi_done")
            message = s[key] + "\n" + "\n".join(outcome.paths)
            self._append_right_text("\n" + message + "\n", "sys_notice")
        self._sending = False
        self._set_chat_status("ready")
        self._focus_composer()

    def _handle_chat_error(self, status: int | None, message: str) -> None:
        if self._closed or self._mode != "chat" or not self._sending:
            return
        api_client.LOGGER.error("Chat request failed: %s", message)
        if self._right_text is not None:
            self._set_normal(self._right_text)
            if self._answer_block_start is not None:
                try:
                    self._right_text.delete(self._answer_block_start, "end-1c")
                except tk.TclError:
                    pass
            text = self._short_setup_error(status)
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
        while len(self._history) > 30 or (total > 60_000 and len(self._history) > 8):
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
        events: list[tuple] = []
        while True:
            try:
                events.append(self._queue.get_nowait())
            except queue.Empty:
                break
        index = 0
        while index < len(events):
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
            elif kind == "assistant_error":
                _, status, message = events[index]
                self._handle_chat_error(status, message)
            elif kind == "vision_fallback":
                self._vision_fallback_notice = True
            elif kind == "tool_progress":
                _, progress_tool, ids = events[index]
                self._handle_tool_progress(progress_tool, ids)
            elif kind == "local_done":
                _, local_kind, paths = events[index]
                self._handle_local_done(local_kind, paths)
            elif kind == "local_error":
                _, local_message = events[index]
                self._handle_local_error(local_message)
            elif kind == "tools_done":
                _, outcomes = events[index]
                self._handle_tools_done(outcomes)
            index += 1
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
