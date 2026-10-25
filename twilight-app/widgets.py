"""Custom Tk widgets matching the supplied lavender Twilight Harness design."""

from __future__ import annotations

import ctypes
import os
import re
import tkinter as tk
from tkinter import font as tkfont
from typing import Callable, Sequence

import markdown_tables
import theme


# ---------------------------------------------------------------------------
# Small pill buttons and window controls
# ---------------------------------------------------------------------------
class RoundedButton(tk.Canvas):
    def __init__(
        self,
        master,
        text: str,
        command: Callable[[], None],
        *,
        width: int = 92,
        height: int = 35,
        radius: int = 17,
        fill: str = theme.BUTTON,
        hover_fill: str = theme.BUTTON_HOVER,
        pressed_fill: str = theme.BUTTON_PRESSED,
        disabled_fill: str = theme.BUTTON_DISABLED,
        fg: str = theme.BLACK,
        disabled_fg: str = theme.DISABLED_TEXT,
        border: str = "",
        font: tkfont.Font | None = None,
        bg: str = theme.BG,
    ) -> None:
        super().__init__(
            master,
            width=width,
            height=height,
            bg=bg,
            highlightthickness=0,
            bd=0,
            takefocus=0,
            cursor="hand2",
        )
        self._req_width = width
        self._req_height = height
        self._radius = radius
        self._fill = fill
        self._hover_fill = hover_fill
        self._pressed_fill = pressed_fill
        self._disabled_fill = disabled_fill
        self._fg = fg
        self._disabled_fg = disabled_fg
        self._border = border
        self._font = font or theme.ui(11)
        self._text = text
        self._command = command
        self._enabled = True
        self._hovered = False
        self._pressed = False
        self._bg = bg
        self.bind("<Configure>", lambda _event: self._draw())
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.after_idle(self._draw)

    def set_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self._enabled:
            return
        self._enabled = enabled
        self._hovered = False
        self._pressed = False
        self.configure(cursor="hand2" if enabled else "arrow")
        self._draw()

    def set_text(self, text: str) -> None:
        if text != self._text:
            self._text = text
            self._draw()

    def set_font(self, font: tkfont.Font) -> None:
        self._font = font
        self._draw()

    def _on_enter(self, _event=None):
        if self._enabled:
            self._hovered = True
            self._draw()

    def _on_leave(self, _event=None):
        self._hovered = False
        self._pressed = False
        self._draw()

    def _on_press(self, _event=None):
        if self._enabled:
            self._pressed = True
            self._draw()
        return "break"

    def _on_release(self, event=None):
        if not self._enabled:
            return "break"
        was = self._pressed
        self._pressed = False
        self._draw()
        if was and event is not None:
            if 0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height():
                try:
                    self._command()
                except Exception:  # noqa: BLE001
                    import traceback

                    traceback.print_exc()
        return "break"

    def _draw(self) -> None:
        self.delete("all")
        w = self.winfo_width() if self.winfo_width() > 1 else self._req_width
        h = self.winfo_height() if self.winfo_height() > 1 else self._req_height
        if self._enabled:
            fill = self._pressed_fill if self._pressed else (self._hover_fill if self._hovered else self._fill)
            fg = self._fg
            cursor = "hand2"
        else:
            fill = self._disabled_fill
            fg = self._disabled_fg
            cursor = "arrow"
        self.configure(cursor=cursor)
        theme.rounded_rect(
            self,
            1,
            1,
            w - 1,
            h - 1,
            self._radius,
            fill=fill,
            outline=self._border or fill,
            width=1 if self._border else 0,
        )
        self.create_text(w / 2, h / 2 + 1, text=self._text, fill=fg, font=self._font, anchor="center")


class WindowButton(tk.Canvas):
    """Monoline minimize / maximize / close button for the custom top bar."""

    def __init__(
        self,
        master,
        kind: str,
        command: Callable[[], None],
        *,
        bg: str = theme.BG,
    ) -> None:
        super().__init__(
            master,
            width=34,
            height=38,
            bg=bg,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
            takefocus=0,
        )
        self._kind = kind
        self._command = command
        self._hovered = False
        self._maximized = False
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<ButtonRelease-1>", self._on_release)
        self.after_idle(self._draw)

    def set_maximized(self, maximized: bool) -> None:
        self._maximized = bool(maximized)
        self._draw()

    def _set_hover(self, hovered: bool) -> None:
        self._hovered = hovered
        self._draw()

    def _on_release(self, _event=None):
        if self._hovered:
            try:
                self._command()
            except Exception:  # noqa: BLE001
                import traceback

                traceback.print_exc()
        return "break"

    def _draw(self) -> None:
        self.delete("all")
        w, h = 34, 38
        if self._hovered:
            theme.rounded_rect(self, 4, 4, w - 4, h - 4, 8, fill=theme.PANEL_HOVER, outline="")
        fg = theme.BLACK
        width = 1.5
        if self._kind == "minimize":
            self.create_line(10.5, h / 2 + 1, w - 10.5, h / 2 + 1, fill=fg, width=width)
        elif self._kind == "maximize":
            if self._maximized:
                self.create_rectangle(11.5, 14, 22.5, 25, outline=fg, width=width)
                self.create_line(14.5, 11, 25, 11, fill=fg, width=width)
                self.create_line(25, 11, 25, 22, fill=fg, width=width)
            else:
                self.create_rectangle(11.5, 11.5, 23.5, 24.5, outline=fg, width=width)
        else:
            self.create_line(11.5, 11.5, 23.5, 24.5, fill=fg, width=width)
            self.create_line(23.5, 11.5, 11.5, 24.5, fill=fg, width=width)


class TopBar(tk.Frame):
    """Frameless title bar / API status bar used across both screens."""

    def __init__(
        self,
        master,
        *,
        on_minimize: Callable[[], None],
        on_maximize: Callable[[], None],
        on_close: Callable[[], None],
        on_toggle_maximize: Callable[[], None] | None = None,
        is_maximized: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__(master, bg=theme.BG, height=38)
        self.pack_propagate(False)
        self._toplevel = self.winfo_toplevel()
        self._is_maximized = is_maximized or (lambda: False)
        self._on_toggle_maximize = on_toggle_maximize or on_maximize
        self._drag_origin: tuple[int, int, int, int] | None = None
        self._moved = False
        self._mode = "setup"

        # Full-height centre rule for the setup screen.
        self.center_line = tk.Frame(self, bg=theme.BLACK, width=4)
        self.center_line.place(relx=0.5, y=0, width=4, relheight=1.0, anchor="n")

        self.status_frame = tk.Frame(self, bg=theme.BG)
        self.status_prefix = tk.Label(
            self.status_frame,
            text="API状态: ",
            bg=theme.BG,
            fg=theme.BLACK,
            font=theme.ui(12),
            anchor="e",
        )
        self.status_prefix.pack(side="left")
        self.status_value = tk.Label(
            self.status_frame,
            text="就绪",
            bg=theme.BG,
            fg=theme.ACCENT,
            font=theme.ui(12),
            anchor="w",
        )
        self.status_value.pack(side="left")
        self.status_frame.place(x=20, rely=0.5, anchor="w")
        self.status_frame.place_forget()

        self.rule = tk.Frame(self, bg=theme.BLACK, height=2)
        self.rule.place(x=0, rely=1.0, y=-2, relwidth=1.0, height=2)
        self.rule.place_forget()

        controls = tk.Frame(self, bg=theme.BG)
        controls.pack(side="right", fill="y")
        self.close_button = WindowButton(controls, "close", on_close)
        self.close_button.pack(side="right", fill="y")
        self.max_button = WindowButton(controls, "maximize", on_maximize)
        self.max_button.pack(side="right", fill="y")
        self.min_button = WindowButton(controls, "minimize", on_minimize)
        self.min_button.pack(side="right", fill="y")

        for widget in (self, self.center_line, self.status_frame, self.status_prefix, self.status_value, self.rule):
            widget.bind("<ButtonPress-1>", self._drag_start, add="+")
            widget.bind("<B1-Motion>", self._drag_motion, add="+")
            widget.bind("<ButtonRelease-1>", self._drag_end, add="+")
            widget.bind("<Double-Button-1>", self._double_click, add="+")
            try:
                widget.configure(cursor="arrow")
            except tk.TclError:
                pass

    def set_maximized(self, maximized: bool) -> None:
        self.max_button.set_maximized(maximized)

    def show_setup(self) -> None:
        self._mode = "setup"
        self.status_frame.place_forget()
        self.rule.place_forget()
        self.center_line.place(relx=0.5, y=0, width=4, relheight=1.0, anchor="n")

    def show_chat(self) -> None:
        self._mode = "chat"
        self.center_line.place_forget()
        self.status_frame.place(x=20, rely=0.5, anchor="w")
        self.rule.place(x=0, rely=1.0, y=-2, relwidth=1.0, height=2)
        self.rule.lift()

    def set_status(self, prefix: str, value: str) -> None:
        try:
            self.status_prefix.configure(text=prefix)
            self.status_value.configure(text=value)
        except tk.TclError:
            pass

    def set_locale(self, locale: str) -> None:
        try:
            self.status_prefix.configure(font=theme.ui(12, locale=locale))
            self.status_value.configure(font=theme.ui(12, locale=locale))
        except tk.TclError:
            pass

    # -- window dragging ----------------------------------------------------
    def _drag_start(self, event):
        self._moved = False
        if self._is_maximized():
            self._drag_origin = None
            return
        self._drag_origin = (
            event.x_root,
            event.y_root,
            self._toplevel.winfo_x(),
            self._toplevel.winfo_y(),
        )

    def _drag_motion(self, event):
        if self._drag_origin is None:
            return
        sx, sy, wx, wy = self._drag_origin
        dx = event.x_root - sx
        dy = event.y_root - sy
        if abs(dx) > 2 or abs(dy) > 2:
            self._moved = True
        self._toplevel.geometry(f"+{wx + dx}+{wy + dy}")

    def _drag_end(self, _event=None):
        self._drag_origin = None

    def _double_click(self, _event=None):
        if not self._moved:
            try:
                self._on_toggle_maximize()
            except Exception:  # noqa: BLE001
                import traceback

                traceback.print_exc()
        return "break"


# ---------------------------------------------------------------------------
# Form controls
# ---------------------------------------------------------------------------
class RoundedEntry(tk.Canvas):
    """White, rounded-border API field with a Tk-managed placeholder."""

    def __init__(
        self,
        master,
        *,
        width: int = 360,
        height: int = 41,
        radius: int = 10,
        placeholder: str = "",
        on_change: Callable[[str], None] | None = None,
        on_submit: Callable[[], None] | None = None,
        font: tkfont.Font | None = None,
        bg: str = theme.BG,
    ) -> None:
        super().__init__(
            master,
            width=width,
            height=height,
            bg=bg,
            highlightthickness=0,
            bd=0,
            takefocus=0,
        )
        self._req_width = width
        self._req_height = height
        self._radius = radius
        self._placeholder = placeholder or ""
        self._placeholder_active = False
        self._suppress = False
        self._enabled = True
        self._focused = False
        self._error = False
        self._bg = bg
        self._on_change = on_change
        self._on_submit = on_submit
        self._font = font or theme.ui(11)
        self.var = tk.StringVar()
        self._entry = tk.Entry(
            self,
            textvariable=self.var,
            relief="flat",
            bd=0,
            highlightthickness=0,
            bg=theme.WHITE,
            fg=theme.BLACK,
            disabledbackground=theme.WHITE,
            disabledforeground=theme.DISABLED_TEXT,
            insertbackground=theme.BLACK,
            selectbackground=theme.BUTTON,
            selectforeground=theme.BLACK,
            font=self._font,
        )
        self._window = self.create_window(
            14,
            height / 2,
            anchor="w",
            window=self._entry,
            width=width - 28,
            height=height - 10,
        )
        self._entry.bind("<FocusIn>", self._focus_in)
        self._entry.bind("<FocusOut>", self._focus_out)
        self._entry.bind("<Return>", self._submit)
        self._entry.bind("<KP_Enter>", self._submit)
        self.var.trace_add("write", self._value_changed)
        self.bind("<Button-1>", self._focus_entry)
        self.bind("<Configure>", self._on_configure)
        self.after_idle(self._initial_placeholder)

    # -- public API ---------------------------------------------------------
    def get(self) -> str:
        if self._placeholder_active:
            return ""
        return self.var.get().strip()

    def set(self, value: str) -> None:
        value = value or ""
        self._suppress = True
        if value:
            self._placeholder_active = False
            self.var.set(value)
            self._entry.configure(fg=theme.BLACK)
        else:
            self.var.set("")
            self._placeholder_active = False
            self._entry.configure(fg=theme.BLACK)
            if not self._focused:
                self._apply_placeholder()
        self._suppress = False
        self._draw()
        self._notify_change()

    def focus_entry(self) -> None:
        try:
            self._entry.focus_set()
            self._entry.icursor("end")
        except tk.TclError:
            pass

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        try:
            self._entry.configure(state="normal" if enabled else "disabled")
        except tk.TclError:
            pass
        self._draw()

    def set_placeholder(self, text: str) -> None:
        self._placeholder = text or ""
        if self._placeholder_active:
            self._suppress = True
            self.var.set(self._placeholder)
            self._suppress = False
            self._draw()

    def set_locale(self, locale: str, placeholder: str | None = None) -> None:
        self._font = theme.ui(12, locale=locale)
        try:
            self._entry.configure(font=self._font)
        except tk.TclError:
            pass
        if placeholder is not None:
            self.set_placeholder(placeholder)

    def set_error(self, error: bool) -> None:
        self._error = bool(error)
        try:
            self._entry.configure(bg=theme.WHITE)
        except tk.TclError:
            pass
        self._draw()

    # -- placeholder / events ----------------------------------------------
    def _initial_placeholder(self) -> None:
        if not self._placeholder_active and not self.var.get():
            self._apply_placeholder()
            self._entry.configure(fg=theme.PLACEHOLDER)
        self._draw()

    def _apply_placeholder(self) -> None:
        if not self._placeholder or self.var.get():
            return
        self._suppress = True
        self._placeholder_active = True
        self.var.set(self._placeholder)
        self._suppress = False
        self._entry.configure(fg=theme.PLACEHOLDER)

    def _remove_placeholder(self) -> None:
        if not self._placeholder_active:
            return
        self._suppress = True
        self.var.set("")
        self._suppress = False
        self._placeholder_active = False
        self._entry.configure(fg=theme.BLACK)

    def _focus_in(self, _event=None):
        self._focused = True
        self._remove_placeholder()
        self._draw()

    def _focus_out(self, _event=None):
        self._focused = False
        if not self.var.get().strip():
            self._apply_placeholder()
            self._entry.configure(fg=theme.PLACEHOLDER)
        self._draw()
        self._notify_change()

    def _focus_entry(self, _event=None):
        if self._enabled:
            self._entry.focus_set()

    def _submit(self, _event=None):
        if self._on_submit is not None and self._enabled:
            self._on_submit()
        return "break"

    def _value_changed(self, *_args):
        if self._suppress:
            return
        if self._placeholder_active:
            return
        self._draw()
        self._notify_change()

    def _notify_change(self) -> None:
        if self._on_change is not None:
            try:
                self._on_change(self.get())
            except Exception:  # noqa: BLE001
                import traceback

                traceback.print_exc()

    def _on_configure(self, event):
        self._req_width = max(1, event.width)
        self._req_height = max(1, event.height)
        self.coords(self._window, 14, event.height / 2)
        self.itemconfigure(
            self._window,
            width=max(20, event.width - 28),
            height=max(20, event.height - 10),
        )
        self._draw()

    def _draw(self):
        self.delete("chrome")
        w = self.winfo_width() if self.winfo_width() > 1 else self._req_width
        h = self.winfo_height() if self.winfo_height() > 1 else self._req_height
        if self._error:
            border = theme.ERROR
            width = 2
        elif self._focused and self._enabled:
            border = theme.ACCENT
            width = 2
        elif not self._enabled:
            border = theme.PLACEHOLDER
            width = 1
        else:
            border = theme.BUTTON
            width = 2
        theme.rounded_rect(
            self,
            2,
            2,
            w - 2,
            h - 2,
            self._radius,
            fill=theme.WHITE,
            outline=border,
            width=width,
            tags="chrome",
        )
        self.tag_lower("chrome")
        self._entry.configure(cursor="xterm" if self._enabled else "arrow")




def _round_native_toplevel(window: tk.Toplevel, radius: int = 10) -> None:
    """Clip a popup Toplevel to the same rounded language as the app window."""
    if os.name != "nt":
        return
    try:
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        user32.GetAncestor.restype = ctypes.c_void_p
        user32.SetWindowRgn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool]
        user32.SetWindowRgn.restype = ctypes.c_int
        gdi32.CreateRoundRectRgn.argtypes = [ctypes.c_int] * 6
        gdi32.CreateRoundRectRgn.restype = ctypes.c_void_p
        hwnd = user32.GetAncestor(window.winfo_id(), 2)
        if not hwnd:
            return
        width = max(1, window.winfo_width())
        height = max(1, window.winfo_height())
        hrgn = gdi32.CreateRoundRectRgn(0, 0, width + 1, height + 1, radius * 2, radius * 2)
        if not hrgn:
            return
        if not user32.SetWindowRgn(hwnd, hrgn, True):
            gdi32.DeleteObject(hrgn)
    except Exception:  # noqa: BLE001
        pass

class LilacSelect(tk.Canvas):
    """White bordered language selector with a lilac chevron."""

    def __init__(
        self,
        master,
        values: Sequence[str],
        *,
        width: int = 360,
        height: int = 41,
        default: str | None = None,
        on_change: Callable[[str], None] | None = None,
        font: tkfont.Font | None = None,
        bg: str = theme.BG,
    ) -> None:
        super().__init__(
            master,
            width=width,
            height=height,
            bg=bg,
            highlightthickness=0,
            bd=0,
            takefocus=0,
            cursor="hand2",
        )
        self._req_width = width
        self._req_height = height
        self._values = list(values)
        self._value = default if default in self._values else (self._values[0] if self._values else "")
        self._on_change = on_change
        self._font = font or theme.ui(11)
        self._enabled = True
        self._open = False
        self._hovered = False
        self._bg = bg
        self._popup: tk.Toplevel | None = None
        self._root_bind_id: str | None = None
        self.bind("<Button-1>", self._on_click)
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<Configure>", lambda _e: self._draw())
        self.after_idle(self._draw)

    def get(self) -> str:
        return self._value

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        if not self._enabled:
            self._close_popup()
        self.configure(cursor="hand2" if self._enabled else "arrow")
        self._draw()

    def set_locale(self, locale: str) -> None:
        self._font = theme.ui(12, locale=locale)
        self._draw()

    def _set_hover(self, hovered: bool) -> None:
        self._hovered = hovered
        self._draw()

    def _on_click(self, _event=None):
        if not self._enabled:
            return "break"
        if self._popup is not None:
            self._close_popup()
        else:
            self._open_popup()
        return "break"

    def _open_popup(self) -> None:
        if self._popup is not None or not self._values:
            return
        self._open = True
        self._draw()
        popup = tk.Toplevel(self)
        popup.withdraw()
        popup.overrideredirect(True)
        popup.configure(bg=theme.BLACK)
        try:
            popup.attributes("-topmost", True)
        except tk.TclError:
            pass
        inner = tk.Frame(popup, bg=theme.WHITE)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        row_height = 31
        for value in self._values:
            selected = value == self._value
            row = tk.Frame(inner, bg=theme.BUTTON if selected else theme.WHITE, cursor="hand2")
            row.pack(fill="x")
            row.pack_propagate(False)
            row.configure(height=row_height)
            label = tk.Label(
                row,
                text=value,
                bg=row.cget("bg"),
                fg=theme.BLACK,
                font=self._font,
                anchor="w",
                padx=14,
                cursor="hand2",
            )
            label.pack(side="left", fill="both", expand=True)
            for widget in (row, label):
                widget.bind("<Enter>", lambda _e, r=row, lb=label, v=value: self._hover_row(r, lb, v, True))
                widget.bind("<Leave>", lambda _e, r=row, lb=label, v=value: self._hover_row(r, lb, v, False))
                widget.bind("<Button-1>", lambda _e, v=value: self._choose(v))
        self._popup = popup
        root = self.winfo_toplevel()
        try:
            self._root_bind_id = root.bind("<Button-1>", self._on_root_click, add="+")
        except tk.TclError:
            self._root_bind_id = None
        popup.bind("<Escape>", lambda _e: self._close_popup())
        popup.update_idletasks()
        width = max(self.winfo_width(), 240)
        height = row_height * len(self._values) + 2
        x = self.winfo_rootx()
        y_below = self.winfo_rooty() + self.winfo_height() + 2
        screen_h = self.winfo_screenheight()
        if y_below + height > screen_h - 12:
            y = max(8, self.winfo_rooty() - height - 2)
        else:
            y = y_below
        popup.geometry(f"{width}x{height}+{x}+{y}")
        popup.deiconify()
        popup.update_idletasks()
        _round_native_toplevel(popup, 10)
        popup.lift()
        popup.focus_force()

    def _hover_row(self, row, label, value, hovered: bool) -> None:
        if self._popup is None:
            return
        selected = value == self._value
        color = theme.BUTTON if selected else (theme.BG if hovered else theme.WHITE)
        for widget in (row, label):
            try:
                widget.configure(bg=color)
            except tk.TclError:
                pass

    def _choose(self, value: str) -> None:
        self._value = value
        self._close_popup()
        self._draw()
        if self._on_change is not None:
            try:
                self._on_change(value)
            except Exception:  # noqa: BLE001
                import traceback

                traceback.print_exc()

    def _on_root_click(self, event):
        if self._popup is None:
            return
        widget = event.widget
        try:
            if widget is self or str(widget).startswith(str(self) + "."):
                return
        except Exception:  # noqa: BLE001
            pass
        self._close_popup()

    def _close_popup(self) -> None:
        popup = self._popup
        self._popup = None
        self._open = False
        root = self.winfo_toplevel()
        if self._root_bind_id is not None:
            try:
                root.unbind("<Button-1>", self._root_bind_id)
            except tk.TclError:
                pass
            self._root_bind_id = None
        if popup is not None:
            try:
                popup.destroy()
            except tk.TclError:
                pass
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        w = self.winfo_width() if self.winfo_width() > 1 else self._req_width
        h = self.winfo_height() if self.winfo_height() > 1 else self._req_height
        if self._open:
            fill = theme.BG
            border = theme.ACCENT
            border_width = 2
        elif self._hovered and self._enabled:
            fill = "#FCF7FF"
            border = theme.BUTTON
            border_width = 2
        else:
            fill = theme.WHITE
            border = theme.BUTTON if self._enabled else theme.PLACEHOLDER
            border_width = 2 if self._enabled else 1
        theme.rounded_rect(self, 2, 2, w - 2, h - 2, 10, fill=fill, outline=border, width=border_width)
        self.create_text(
            14,
            h / 2,
            text=self._value,
            fill=theme.BLACK if self._enabled else theme.DISABLED_TEXT,
            font=self._font,
            anchor="w",
        )
        arrow = "#D9A9E8" if self._enabled else theme.PLACEHOLDER
        cx = w - 19
        cy = h / 2 - 1
        self.create_line(cx - 7, cy - 3, cx, cy + 4, fill=arrow, width=3, capstyle="round")
        self.create_line(cx, cy + 4, cx + 7, cy - 3, fill=arrow, width=3, capstyle="round")


# ---------------------------------------------------------------------------
# Scrollbar
# ---------------------------------------------------------------------------
class SlimScrollbar(tk.Canvas):
    def __init__(self, master, command: Callable[..., None], *, width: int = 8, bg: str = theme.BG) -> None:
        super().__init__(master, width=width, height=100, bg=bg, highlightthickness=0, bd=0)
        self._command = command
        self._width = width
        self._first = 0.0
        self._last = 1.0
        self._thumb_top = 0.0
        self._thumb_bottom = 0.0
        self._drag_offset = 0.0
        self._hover = False
        self._pressed = False
        self.bind("<Configure>", lambda _e: self._draw())
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonRelease-1>", self._release)

    def set(self, first, last) -> None:
        try:
            self._first = max(0.0, min(1.0, float(first)))
            self._last = max(0.0, min(1.0, float(last)))
        except (TypeError, ValueError):
            self._first, self._last = 0.0, 1.0
        self._draw()

    def _enter(self, _event=None):
        self._hover = True
        self._draw()

    def _leave(self, _event=None):
        self._hover = False
        self._pressed = False
        self._draw()

    def _press(self, event):
        h = self.winfo_height()
        if h <= 1 or self._last - self._first >= 0.999:
            return
        if self._thumb_top <= event.y <= self._thumb_bottom:
            self._drag_offset = event.y - self._thumb_top
            self._pressed = True
            return
        page = self._last - self._first
        fraction = event.y / h - page / 2
        self._jump(fraction)
        self._drag_offset = page * h / 2
        self._pressed = True

    def _drag(self, event):
        if not self._pressed:
            return
        h = self.winfo_height()
        page = max(0.001, self._last - self._first)
        top = event.y - self._drag_offset
        fraction = max(0.0, min(1.0 - page, top / max(1, h)))
        self._jump(fraction)

    def _release(self, _event=None):
        self._pressed = False
        self._draw()

    def _jump(self, fraction: float) -> None:
        try:
            self._command("moveto", max(0.0, min(1.0, fraction)))
        except tk.TclError:
            return

    def _draw(self) -> None:
        self.delete("thumb")
        h = self.winfo_height()
        if h <= 1 or self._last - self._first >= 0.999:
            self._thumb_top = self._thumb_bottom = 0.0
            return
        page = max(0.04, self._last - self._first)
        top = self._first * h
        bottom = top + page * h
        min_height = 28
        if bottom - top < min_height:
            center = (top + bottom) / 2
            top = max(0, center - min_height / 2)
            bottom = min(h, top + min_height)
            top = max(0, bottom - min_height)
        self._thumb_top, self._thumb_bottom = top, bottom
        fill = theme.SCROLL_HOVER if (self._hover or self._pressed) else theme.SCROLL
        theme.rounded_rect(
            self,
            self._width - 7,
            top + 1,
            self._width - 1,
            bottom - 1,
            3,
            fill=fill,
            outline="",
            tags="thumb",
        )


# ---------------------------------------------------------------------------
# Large composer at the bottom of the chat screen
# ---------------------------------------------------------------------------
class Composer(tk.Canvas):
    def __init__(
        self,
        master,
        *,
        on_send: Callable[[], None],
        on_attach: Callable[[], None] | None = None,
        height: int = 122,
        font: tkfont.Font | None = None,
        placeholder: str = "说点什么吧……",
        bg: str = theme.BG,
    ) -> None:
        super().__init__(master, height=height, bg=bg, highlightthickness=0, bd=0)
        self._base_height = height
        self._on_send = on_send
        self._on_attach = on_attach
        self._font = font or theme.body(10)
        self._attach_seq = 0
        self._attachment_tokens: dict[str, tuple[str, str, object]] = {}
        self._placeholder_text = placeholder
        self._placeholder_active = True
        self._focused = False
        self._bg = bg
        self.text = tk.Text(
            self,
            wrap="word",
            bd=0,
            highlightthickness=0,
            relief="flat",
            bg=theme.WHITE,
            fg=theme.BLACK,
            insertbackground=theme.BLACK,
            selectbackground=theme.BUTTON,
            selectforeground=theme.BLACK,
            font=self._font,
            padx=0,
            pady=0,
            spacing1=2,
            spacing2=3,
            spacing3=0,
            undo=True,
            maxundo=200,
        )
        self._window = self.create_window(17, 13, anchor="nw", window=self.text)
        self._placeholder_tag = "composer_placeholder"
        self.text.tag_configure(
            self._placeholder_tag,
            foreground=theme.PLACEHOLDER,
            font=self._font,
        )
        self._attachment_tag = "composer_attachment"
        self.text.tag_configure(
            self._attachment_tag,
            foreground=theme.ACCENT,
            underline=True,
            font=self._font,
        )
        self.text.insert("1.0", placeholder, self._placeholder_tag)
        self.text.bind("<FocusIn>", self._focus_in)
        self.text.bind("<FocusOut>", self._focus_out)
        self.text.bind("<KeyPress>", self._key_press, add="+")
        self.text.bind("<Control-Return>", self._ctrl_enter)
        self.text.bind("<Control-KP_Enter>", self._ctrl_enter)
        self.text.bind("<Alt-Return>", self._alt_enter)
        self.text.bind("<Alt-KP_Enter>", self._alt_enter)
        self.bind("<Configure>", self._on_configure)
        self.after_idle(self._draw)

    def get_value(self) -> str:
        if self._placeholder_active:
            return ""
        return self.text.get("1.0", "end-1c").strip()

    def clear(self) -> None:
        self.text.delete("1.0", "end")
        self._attachment_tokens.clear()
        self._show_placeholder()
        try:
            self.text.edit_reset()
        except tk.TclError:
            pass

    def set_locale(self, locale: str, placeholder: str | None = None) -> None:
        self._font = theme.body(11, locale=locale)
        try:
            self.text.configure(font=self._font)
            self.text.tag_configure(self._placeholder_tag, font=self._font)
            self.text.tag_configure(self._attachment_tag, font=self._font)
        except tk.TclError:
            pass
        if placeholder is not None:
            self._placeholder_text = placeholder
            if self._placeholder_active:
                self._show_placeholder()

    def focus_input(self) -> None:
        try:
            self.text.focus_set()
            self.text.mark_set("insert", "end-1c")
        except tk.TclError:
            pass

    def _focus_in(self, _event=None):
        self._focused = True
        self._draw()

    def _focus_out(self, _event=None):
        self._focused = False
        self._draw()

    def _key_press(self, event):
        if self._placeholder_active:
            if event.keysym in (
                "Shift_L",
                "Shift_R",
                "Control_L",
                "Control_R",
                "Alt_L",
                "Alt_R",
                "Caps_Lock",
                "Num_Lock",
                "Tab",
                "Escape",
            ):
                return None
            self._clear_placeholder()
        return None

    def insert_attachment(self, name: str, on_remove=None) -> None:
        """Append a removable, underlined attachment reference."""
        if self._placeholder_active:
            self._clear_placeholder()
        try:
            self._attach_seq += 1
            token_tag = f"composer_attach_token_{self._attach_seq}"
            name_tag = f"composer_attach_name_{self._attach_seq}"
            remove_tag = f"composer_attach_remove_{self._attach_seq}"
            current = self.text.get("1.0", "end-1c")
            prefix = "" if not current or current.endswith((" ", "\n")) else " "
            start = self.text.index("insert")
            insertion = prefix + name + " × "
            self.text.insert("insert", insertion)
            name_start = self.text.index(f"{start}+{len(prefix)}c")
            name_end = self.text.index(f"{name_start}+{len(name)}c")
            remove_start = self.text.index(f"{name_end}+1c")
            remove_end = self.text.index(f"{remove_start}+1c")
            end = self.text.index("insert")
            self.text.tag_configure(name_tag, foreground=theme.ACCENT, underline=True, font=self._font)
            self.text.tag_configure(remove_tag, foreground=theme.ACCENT, font=self._font)
            self.text.tag_add(token_tag, start, end)
            self.text.tag_add(name_tag, name_start, name_end)
            self.text.tag_add(remove_tag, remove_start, remove_end)
            self.text.tag_bind(
                remove_tag,
                "<Button-1>",
                lambda _event, tag=token_tag: self._remove_attachment_token(tag),
            )
            self._attachment_tokens[token_tag] = (name_tag, remove_tag, on_remove)
            self.text.mark_set("insert", "end-1c")
            self.text.focus_set()
        except tk.TclError:
            pass

    def _remove_attachment_token(self, token_tag: str) -> str:
        entry = self._attachment_tokens.pop(token_tag, None)
        if entry is None:
            return "break"
        name_tag, remove_tag, callback = entry
        try:
            ranges = self.text.tag_ranges(token_tag)
            if ranges:
                self.text.delete(ranges[0], ranges[1])
            self.text.tag_delete(token_tag, name_tag, remove_tag)
            self.text.focus_set()
        except tk.TclError:
            pass
        if callback is not None:
            try:
                callback()
            except Exception:  # noqa: BLE001
                pass
        return "break"

    def _ctrl_enter(self, _event=None):
        if self._placeholder_active:
            return "break"
        self._on_send()
        return "break"

    def _alt_enter(self, _event=None):
        if self._on_attach is not None:
            self._on_attach()
        return "break"

    def _clear_placeholder(self) -> None:
        if not self._placeholder_active:
            return
        self.text.delete("1.0", "end")
        self._placeholder_active = False
        self.text.configure(fg=theme.BLACK)

    def _show_placeholder(self) -> None:
        self._placeholder_active = True
        self.text.configure(fg=theme.BLACK)
        self.text.insert("1.0", self._placeholder_text, self._placeholder_tag)
        self.text.mark_set("insert", "1.0")

    def _on_configure(self, event):
        width = max(40, event.width - 34)
        height = max(24, event.height - 30)
        self.coords(self._window, 17, 13)
        self.itemconfigure(self._window, width=width, height=height)
        self._draw()

    def _draw(self):
        self.delete("chrome")
        w = max(10, self.winfo_width())
        h = max(10, self.winfo_height())
        theme.rounded_rect(
            self,
            2,
            2,
            w - 2,
            h - 2,
            16,
            fill=theme.WHITE,
            outline=theme.BLACK,
            width=2,
            tags="chrome",
        )
        self.tag_lower("chrome")


# ---------------------------------------------------------------------------
# Frameless resize grips
# ---------------------------------------------------------------------------
class WindowResizer:
    def __init__(
        self,
        root: tk.Tk,
        *,
        min_width: int = 640,
        min_height: int = 480,
        is_maximized: Callable[[], bool] | None = None,
    ) -> None:
        self.root = root
        self.min_width = min_width
        self.min_height = min_height
        self._is_maximized = is_maximized or (lambda: False)
        self._grips: list[tk.Frame] = []
        self._start: tuple[int, int, int, int, int, int] | None = None
        self._direction = ""
        specs = (
            ("n", dict(x=10, y=0, relwidth=1, width=-20, height=5), "sb_v_double_arrow"),
            ("s", dict(x=10, rely=1, y=-5, relwidth=1, width=-20, height=5), "sb_v_double_arrow"),
            ("w", dict(x=0, y=10, width=5, relheight=1, height=-20), "sb_h_double_arrow"),
            ("e", dict(relx=1, x=-5, y=10, width=5, relheight=1, height=-20), "sb_h_double_arrow"),
            ("nw", dict(x=0, y=0, width=9, height=9), "size_nw_se"),
            ("ne", dict(relx=1, x=-9, y=0, width=9, height=9), "size_ne_sw"),
            ("sw", dict(x=0, rely=1, y=-9, width=9, height=9), "size_ne_sw"),
            ("se", dict(relx=1, x=-9, rely=1, y=-9, width=9, height=9), "size_nw_se"),
        )
        for direction, placement, cursor in specs:
            grip = tk.Frame(root, bg=theme.BG, cursor=cursor, bd=0, highlightthickness=0)
            grip.place(**placement)
            grip.bind("<ButtonPress-1>", lambda event, d=direction: self._press(event, d))
            grip.bind("<B1-Motion>", self._motion)
            grip.bind("<ButtonRelease-1>", self._release)
            grip.lift()
            self._grips.append(grip)

    def lift(self) -> None:
        for grip in self._grips:
            try:
                grip.lift()
            except tk.TclError:
                pass

    def _press(self, event, direction: str):
        if self._is_maximized():
            self._start = None
            return "break"
        self._direction = direction
        self._start = (
            event.x_root,
            event.y_root,
            self.root.winfo_x(),
            self.root.winfo_y(),
            self.root.winfo_width(),
            self.root.winfo_height(),
        )
        return "break"

    def _motion(self, event):
        if self._start is None:
            return "break"
        sx, sy, wx, wy, ww, wh = self._start
        dx = event.x_root - sx
        dy = event.y_root - sy
        x, y, w, h = wx, wy, ww, wh
        if "e" in self._direction:
            w = max(self.min_width, ww + dx)
        if "s" in self._direction:
            h = max(self.min_height, wh + dy)
        if "w" in self._direction:
            w = max(self.min_width, ww - dx)
            x = wx + ww - w
        if "n" in self._direction:
            h = max(self.min_height, wh - dy)
            y = wy + wh - h
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        return "break"

    def _release(self, _event=None):
        self._start = None
        return "break"


# ---------------------------------------------------------------------------
# Pragmatic Markdown insertion for the answer pane
# ---------------------------------------------------------------------------
_INLINE_RE = re.compile(
    r"(\*\*[^*\n]+\*\*|__[^_\n]+__|`[^`\n]+`|\*[^*\n]+\*|_[^_\n]+_|\[[^\]\n]+\]\([^)\n]+\))"
)


def insert_markdown(
    widget: tk.Text,
    content: str,
    *,
    base_tag: str = "assistant_body",
    bold_tag: str = "md_bold",
    italic_tag: str = "md_italic",
    code_tag: str = "md_code",
    codeblock_tag: str = "md_codeblock",
    heading_tag: str = "md_heading",
    bullet_tag: str = "md_bullet",
    quote_tag: str = "md_quote",
    table_tag: str = "md_table",
) -> None:
    lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    in_code = False
    code_lines: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("```"):
            if in_code:
                _flush_code(widget, code_lines, codeblock_tag, base_tag)
                code_lines = []
                in_code = False
            else:
                in_code = True
                code_lines = []
            index += 1
            continue
        if in_code:
            code_lines.append(line)
            index += 1
            continue
        if not line.strip():
            widget.insert("end-1c", "\n", base_tag)
            index += 1
            continue
        table_end = markdown_tables.try_insert_table(
            widget, lines, index, base_tag, table_tag
        )
        if table_end is not None:
            index = table_end
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading:
            widget.insert("end-1c", heading.group(2).strip() + "\n", heading_tag)
            index += 1
            continue
        quote = re.match(r"^>\s?(.*)$", line)
        if quote:
            widget.insert("end-1c", "  " + quote.group(1).strip() + "\n", quote_tag)
            index += 1
            continue
        bullet = re.match(r"^(\s*)[-*+]\s+(.*)$", line)
        if bullet:
            widget.insert("end-1c", "·  ", bullet_tag)
            _insert_inline(widget, bullet.group(2), base_tag, bold_tag, italic_tag, code_tag)
            widget.insert("end-1c", "\n", base_tag)
            index += 1
            continue
        numbered = re.match(r"^\s*(\d+[.)])\s+(.*)$", line)
        if numbered:
            widget.insert("end-1c", f"  {numbered.group(1)}  ", bullet_tag)
            _insert_inline(widget, numbered.group(2), base_tag, bold_tag, italic_tag, code_tag)
            widget.insert("end-1c", "\n", base_tag)
            index += 1
            continue
        _insert_inline(widget, line, base_tag, bold_tag, italic_tag, code_tag)
        widget.insert("end-1c", "\n", base_tag)
        index += 1
    if in_code:
        _flush_code(widget, code_lines, codeblock_tag, base_tag)
def _flush_code(widget: tk.Text, lines: list[str], codeblock_tag: str, base_tag: str) -> None:
    if not lines:
        return
    widget.insert("end-1c", "\n", base_tag)
    widget.insert("end-1c", "\n".join(lines) + "\n", codeblock_tag)
    widget.insert("end-1c", "\n", base_tag)


def _insert_inline(
    widget: tk.Text,
    text: str,
    base_tag: str,
    bold_tag: str,
    italic_tag: str,
    code_tag: str,
) -> None:
    for part in _INLINE_RE.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            widget.insert("end-1c", part[2:-2], (base_tag, bold_tag))
        elif part.startswith("__") and part.endswith("__") and len(part) > 4:
            widget.insert("end-1c", part[2:-2], (base_tag, bold_tag))
        elif part.startswith("`") and part.endswith("`") and len(part) > 2:
            widget.insert("end-1c", part[1:-1], (base_tag, code_tag))
        elif part.startswith("*") and part.endswith("*") and len(part) > 2:
            widget.insert("end-1c", part[1:-1], (base_tag, italic_tag))
        elif part.startswith("_") and part.endswith("_") and len(part) > 2:
            widget.insert("end-1c", part[1:-1], (base_tag, italic_tag))
        elif part.startswith("[") and "](" in part and part.endswith(")"):
            label = part[1 : part.index("]")]
            widget.insert("end-1c", label, (base_tag, italic_tag))
        else:
            widget.insert("end-1c", part, base_tag)
    return None
