"""Floating task progress board for local downloads and Real-ESRGAN jobs.

Worker threads only put small event dictionaries into a queue; all Tk access is
done on the main thread.  The board starts in the lower-left area of the screen
at 400x350 and is a normal resizable/minimizable/maximizable Toplevel window.
"""

from __future__ import annotations

import queue
import tkinter as tk
from tkinter import ttk
from typing import Callable

import theme

Event = dict


def _format_bytes(value: int | float) -> str:
    value = max(0.0, float(value))
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.2f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.2f} GiB"


class ProgressWindow:
    def __init__(self, root: tk.Misc, translate: Callable[[str], str]) -> None:
        self.root = root
        self.translate = translate
        self.events: queue.Queue[Event] = queue.Queue()
        self.window: tk.Toplevel | None = None
        self.body: tk.Frame | None = None
        self.canvas: tk.Canvas | None = None
        self.inner: tk.Frame | None = None
        self.rows: dict[str, tuple[tk.Frame, tk.Label, tk.Canvas, tk.Label, Event]] = {}
        self.closed = False
        self._poll_job: str | None = root.after(120, self._poll)

    # -- thread-safe API -------------------------------------------------
    def report(self, event: Event) -> None:
        if not self.closed:
            self.events.put(dict(event))

    def shutdown(self) -> None:
        self.closed = True
        try:
            if self._poll_job is not None:
                self.root.after_cancel(self._poll_job)
        except tk.TclError:
            pass
        self._poll_job = None
        if self.window is not None:
            try:
                self.window.destroy()
            except tk.TclError:
                pass
            self.window = None

    # -- main-thread UI --------------------------------------------------
    def _ensure_window(self) -> None:
        if self.window is not None:
            return
        window = tk.Toplevel(self.root)
        window.title(self.translate("progress_title"))
        window.configure(bg=theme.BG)
        screen_w = window.winfo_screenwidth()
        screen_h = window.winfo_screenheight()
        width, height = 400, 350
        x = 24
        y = max(40, screen_h - height - 80)
        window.geometry(f"{width}x{height}+{x}+{y}")
        window.minsize(300, 180)
        window.resizable(True, True)
        window.protocol("WM_DELETE_WINDOW", window.withdraw)
        self.window = window

        outer = tk.Frame(window, bg=theme.BG)
        outer.pack(fill="both", expand=True, padx=10, pady=8)
        canvas = tk.Canvas(outer, bg=theme.BG, highlightthickness=0)
        scroll = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        body = tk.Frame(canvas, bg=theme.BG)
        window_id = canvas.create_window((0, 0), window=body, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        body.bind(
            "<Configure>",
            lambda _e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.bind(
            "<Configure>",
            lambda e: canvas.itemconfigure(window_id, width=e.width),
        )
        self.body = body
        self.canvas = canvas
        self.inner = body
        window.update_idletasks()

    def _poll(self) -> None:
        if self.closed:
            return
        try:
            while True:
                event = self.events.get_nowait()
                self._apply(event)
        except queue.Empty:
            pass
        try:
            self._poll_job = self.root.after(120, self._poll)
        except tk.TclError:
            self._poll_job = None

    def _label_for(self, event: Event) -> str:
        kind = str(event.get("kind") or "download")
        name = str(event.get("name") or "")
        if kind == "realesrgan":
            prefix = self.translate("progress_realesrgan")
        elif kind == "ffmpeg":
            prefix = self.translate("progress_ffmpeg")
        else:
            prefix = self.translate("progress_download")
        return prefix + name

    def _apply(self, event: Event) -> None:
        self._ensure_window()
        assert self.window is not None and self.inner is not None
        self.window.title(self.translate("progress_title"))
        item_id = str(event.get("id") or f"{event.get('kind')}-{event.get('name')}")
        if item_id not in self.rows and len(self.rows) >= 80:
            # Very large Derpibooru requests can contain thousands of images.
            # Keep the board responsive by retaining the most recent 80 rows.
            remove_key = next(iter(self.rows))
            for key, (row, _label, _bar, _detail, data) in self.rows.items():
                if data.get("state") in {"done", "error"}:
                    remove_key = key
                    break
            row, *_ = self.rows.pop(remove_key)
            try:
                row.destroy()
            except tk.TclError:
                pass
        if item_id not in self.rows:
            row = tk.Frame(self.inner, bg=theme.BG)
            row.pack(fill="x", padx=2, pady=6)
            label = tk.Label(
                row,
                text=self._label_for(event),
                anchor="w",
                bg=theme.BG,
                fg=theme.INK,
                font=theme.ui(9),
            )
            label.pack(fill="x")
            bar = tk.Canvas(
                row,
                height=16,
                bg=theme.BG,
                highlightthickness=0,
                bd=0,
            )
            bar.pack(fill="x", pady=(3, 1))
            detail = tk.Label(
                row,
                text="",
                anchor="e",
                bg=theme.BG,
                fg=theme.MUTED,
                font=theme.ui(8),
            )
            detail.pack(fill="x")
            self.rows[item_id] = (row, label, bar, detail, dict(event))
            bar.bind("<Configure>", lambda _e, canvas=bar: self._redraw(canvas))
        row, label, bar, detail, _data = self.rows[item_id]
        self.rows[item_id] = (row, label, bar, detail, dict(event))
        label.configure(text=self._label_for(event))
        fraction = max(0.0, min(1.0, float(event.get("fraction") or 0.0)))
        total = int(event.get("total") or 0)
        done = int(event.get("done") or 0)
        if total > 0:
            detail.configure(text=f"{_format_bytes(done)} / {_format_bytes(total)}")
        else:
            detail.configure(text=f"{int(fraction * 100)}%")
        state = str(event.get("state") or "running")
        bar._twilight_fraction = fraction  # type: ignore[attr-defined]
        bar._twilight_state = state  # type: ignore[attr-defined]
        self._redraw(bar)
        self.window.deiconify()
        self.window.lift()

    def _redraw(self, bar: tk.Canvas) -> None:
        try:
            width = max(1, bar.winfo_width())
            height = max(1, bar.winfo_height())
        except tk.TclError:
            return
        bar.delete("all")
        bar.create_rectangle(
            0, 0, width - 1, height - 1,
            outline=theme.INK, fill=theme.WHITE, width=2,
        )
        fraction = float(getattr(bar, "_twilight_fraction", 0.0))
        state = str(getattr(bar, "_twilight_state", "running"))
        fill = theme.ERROR if state == "error" else theme.ACCENT
        if fraction > 0:
            bar.create_rectangle(
                2, 2, max(2, int((width - 4) * fraction) + 2), height - 2,
                outline="", fill=fill,
            )
