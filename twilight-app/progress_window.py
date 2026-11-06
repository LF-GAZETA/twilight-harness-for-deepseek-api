"""Floating task progress board for local downloads and AI jobs.

Worker threads only put small event dictionaries into a queue; all Tk access is
done on the main thread.  The board starts in the lower-left area of the screen
at 460x380 and is a normal resizable/minimizable/maximizable Toplevel window.

Each row is laid out like:

    Downloading Derpibooru Picture...                 19 / 400
    [=====================================.............]
    derpibooru-3920122                    2.1 MiB / 8.4 MiB

The summary on the right shows the batch image count for downloads, the step
count for Stable Diffusion, or the percentage for other jobs.  The footer shows
the current file/step and byte progress (plus speed/ETA when measurable).
"""

from __future__ import annotations

import queue
import time
import tkinter as tk
from dataclasses import dataclass
from tkinter import ttk
from typing import Callable

import theme

Event = dict

_DEFAULT_LABELS = {
    "progress_downloading": "Downloading Derpibooru Picture...",
    "progress_sd15": "Running Stable Diffusion...",
    "progress_vhs": "Now VHS-ification...",
    "progress_steps": "{done} / {total} Steps",
    "progress_eta": "ETA {seconds}s",
    "progress_uvr": "Separating vocals...",
    "progress_whisper": "Transcribing...",
    "progress_sovits": "Synthesizing voice...",
}


def _format_bytes(value: int | float) -> str:
    value = max(0.0, float(value))
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.2f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.2f} GiB"


@dataclass
class _Row:
    frame: tk.Frame
    status: tk.Label
    summary: tk.Label
    bar: tk.Canvas
    name: tk.Label
    detail: tk.Label
    event: Event
    last_done: int = 0
    last_time: float = 0.0
    speed: float = 0.0
    remove_job: str | None = None


class ProgressWindow:
    def __init__(self, root: tk.Misc, translate: Callable[[str], str]) -> None:
        self.root = root
        self.translate = translate
        self.events: queue.Queue[Event] = queue.Queue()
        self.window: tk.Toplevel | None = None
        self.body: tk.Frame | None = None
        self.canvas: tk.Canvas | None = None
        self.inner: tk.Frame | None = None
        self.rows: dict[str, _Row] = {}
        self._download_states: dict[str, Event] = {}
        self._download_active: str | None = None
        self._download_total: int = 0
        self.closed = False
        self._startup_kinds: set[str] = set()
        self._startup_finished = False
        self._startup_close_job: str | None = None
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
        for row in self.rows.values():
            if row.remove_job is not None:
                try:
                    self.root.after_cancel(row.remove_job)
                except tk.TclError:
                    pass
        self.rows.clear()
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
        screen_h = window.winfo_screenheight()
        width, height = 460, 380
        x = 24
        y = max(40, screen_h - height - 80)
        window.geometry(f"{width}x{height}+{x}+{y}")
        window.minsize(340, 200)
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
        scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        def sync_scroll(_event=None) -> None:
            try:
                needed = body.winfo_reqheight() > canvas.winfo_height()
            except tk.TclError:
                return
            if needed and not scroll.winfo_manager():
                scroll.pack(side="right", fill="y")
            elif not needed and scroll.winfo_manager():
                scroll.pack_forget()

        def on_body(_event=None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))
            sync_scroll()

        body.bind("<Configure>", on_body)
        canvas.bind(
            "<Configure>",
            lambda e: (canvas.itemconfigure(window_id, width=e.width), sync_scroll()),
        )
        self.body = body
        self.canvas = canvas
        self.inner = body
        window.update_idletasks()
        sync_scroll()

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

    def _t(self, key: str, fallback: str = "") -> str:
        text = self.translate(key)
        if text == key:
            return _DEFAULT_LABELS.get(key, fallback or key)
        return text

    def _status_for(self, event: Event) -> str:
        kind = str(event.get("kind") or "download")
        if kind in {"startup_sovits", "startup_sd15"}:
            return str(event.get("label") or "")
        if kind == "download":
            return self._t("progress_downloading")
        if kind == "sd15":
            return self._t("progress_sd15")
        if kind == "ntscqt":
            return self._t("progress_vhs")
        if kind == "uvr":
            return self._t("progress_uvr")
        if kind == "whisper":
            return self._t("progress_whisper")
        if kind == "sovits":
            return self._t("progress_sovits")
        if kind == "tool":
            return self._t("progress_tool", "Running tool")
        if kind == "image":
            return self._t("progress_image", "Generating image")
        if kind == "realesrgan":
            return self._t("progress_realesrgan", "Real-ESRGAN: ").rstrip(":： ")
        if kind == "ffmpeg":
            return self._t("progress_ffmpeg", "ffmpeg: ").rstrip(":： ")
        if kind == "pedalboard":
            return self._t("progress_pedalboard", "Pedalboard: ").rstrip(":： ")
        return kind

    def _summary_for(self, event: Event) -> str:
        if event.get("compact"):
            return ""
        fraction = max(0.0, min(1.0, float(event.get("fraction") or 0.0)))
        done = int(event.get("done") or 0)
        total = int(event.get("total") or 0)
        batch_total = int(event.get("batch_total") or 0)
        if batch_total > 0:
            batch_done = max(0, min(batch_total, int(event.get("batch_done") or 0)))
            return f"{batch_done} / {batch_total}"
        # Only the download batch count belongs in the header; step counts and
        # percentages are shown below the bar, matching the compact card layout.
        return ""

    def _detail_for(self, row: _Row, event: Event) -> str:
        fraction = max(0.0, min(1.0, float(event.get("fraction") or 0.0)))
        if event.get("compact"):
            return "启动失败" if str(event.get("state") or "") == "error" else f"{int(fraction * 100)}%"
        done = int(event.get("done") or 0)
        total = int(event.get("total") or 0)
        unit = str(event.get("unit") or "")
        state = str(event.get("state") or "running")
        parts: list[str] = []
        if str(event.get("kind") or "") in {"sovits", "image"}:
            return f"{int(fraction * 100)}%"
        if unit == "steps" and total > 0:
            parts.append(
                self._t("progress_steps", "{done} / {total} Steps").format(
                    done=max(0, min(total, done)), total=total
                )
            )
        elif total > 0:
            parts.append(f"{_format_bytes(done)} / {_format_bytes(total)}")
            if state == "running":
                speed = row.speed
                if speed > 0:
                    parts.append(f"{_format_bytes(speed)}/s")
                    if total > done > 0:
                        eta = (total - done) / speed
                        parts.append(
                            self._t("progress_eta", "ETA {seconds}s").format(
                                seconds=max(1, int(eta))
                            )
                        )
        else:
            parts.append(f"{int(fraction * 100)}%")
        if state == "error":
            parts.append("!")
        return " · ".join(parts)

    def _update_speed(self, row: _Row, done: int) -> None:
        now = time.monotonic()
        if row.last_time > 0 and done >= row.last_done:
            elapsed = now - row.last_time
            delta = done - row.last_done
            if elapsed >= 0.15 and delta > 0:
                sample = delta / elapsed
                row.speed = sample if row.speed <= 0 else row.speed * 0.65 + sample * 0.35
        row.last_done = done
        row.last_time = now

    def _aggregate_download(self, raw: Event) -> tuple[str, Event]:
        """Collapse per-photo events into one batch row.

        The batch row shows the currently active file's byte progress and the
        overall completed/total count.  Other workers may finish in the
        background; their progress is not shown as separate rows.
        """
        file_id = str(raw.get("id") or raw.get("name") or "download")
        self._download_states[file_id] = raw
        batch_total = int(raw.get("batch_total") or 0)
        batch_done = int(raw.get("batch_done") or 0)
        if batch_total > 0 and (
            self._download_total != batch_total
            or (batch_done == 0 and self._download_active is None)
        ):
            self._download_states = {file_id: raw}
            self._download_active = file_id
            self._download_total = batch_total
        elif batch_total > 0:
            self._download_total = batch_total

        active = self._download_active
        active_state = ""
        if active is not None:
            active_state = str(self._download_states.get(active, {}).get("state") or "")
        switched = False
        if active is None or active_state in {"done", "error"}:
            for candidate, state in self._download_states.items():
                if str(state.get("state") or "") not in {"done", "error"}:
                    self._download_active = candidate
                    switched = active is not None
                    active = candidate
                    break
            else:
                self._download_active = None
                active = None

        if batch_total > 0 and batch_done >= batch_total:
            display = dict(raw)
            display["state"] = "error" if str(raw.get("state") or "") == "error" else "done"
            self._download_active = None
            return "download-batch", display
        if active is not None:
            display = dict(self._download_states.get(active, raw))
            if switched:
                display["fraction"] = 0.0
                display["done"] = 0
            display["batch_done"] = batch_done
            display["batch_total"] = batch_total
            return "download-batch", display
        return "download-batch", dict(raw)

    def _schedule_remove(self, item_id: str, row: _Row) -> None:
        if row.remove_job is not None:
            try:
                self.root.after_cancel(row.remove_job)
            except tk.TclError:
                pass
        row.remove_job = self.root.after(5000, lambda: self._remove_row(item_id, row))

    def _remove_row(self, item_id: str, row: _Row) -> None:
        row.remove_job = None
        if self.rows.get(item_id) is not row:
            return
        self.rows.pop(item_id, None)
        try:
            row.frame.destroy()
        except tk.TclError:
            pass
        if item_id == "download-batch":
            self._download_states.clear()
            self._download_active = None
            self._download_total = 0
        if not self.rows and self.window is not None:
            try:
                self.window.withdraw()
            except tk.TclError:
                pass

    def _apply(self, event: Event) -> None:
        kind = str(event.get("kind") or "")
        if kind.startswith("startup_") and self._startup_finished:
            return
        self._ensure_window()
        assert self.window is not None and self.inner is not None
        self.window.title(self.translate("progress_title"))
        raw_event = dict(event)
        if str(raw_event.get("kind") or "download") == "download":
            item_id, event = self._aggregate_download(raw_event)
        else:
            item_id = str(raw_event.get("id") or f"{raw_event.get('kind')}-{raw_event.get('name')}")
        is_new_row = item_id not in self.rows
        if is_new_row and len(self.rows) >= 80:
            # Very large Derpibooru requests can contain thousands of images.
            # Keep the board responsive by retaining the most recent 80 rows.
            remove_key = next(iter(self.rows))
            for key, row in self.rows.items():
                if row.event.get("state") in {"done", "error"}:
                    remove_key = key
                    break
            old = self.rows.pop(remove_key)
            try:
                old.frame.destroy()
            except tk.TclError:
                pass
        compact = bool(event.get("compact"))
        if is_new_row:
            frame = tk.Frame(self.inner, bg=theme.BG)
            frame.pack(fill="x", padx=4, pady=7)
            head = tk.Frame(frame, bg=theme.BG)
            head.pack(fill="x")
            status = tk.Label(
                head,
                text="",
                anchor="w",
                bg=theme.BG,
                fg=theme.INK,
                font=theme.ui(9),
            )
            summary = tk.Label(
                head,
                text="",
                anchor="e",
                bg=theme.BG,
                fg=theme.INK,
                font=theme.ui(9),
            )
            status.pack(side="left", fill="x", expand=True)
            summary.pack(side="right")
            bar = tk.Canvas(
                frame,
                height=20,
                bg=theme.BG,
                highlightthickness=0,
                bd=0,
            )
            bar.pack(fill="x", pady=(3, 2))
            foot = tk.Frame(frame, bg=theme.BG)
            foot.pack(fill="x")
            name = tk.Label(
                foot,
                text="",
                anchor="w",
                bg=theme.BG,
                fg=theme.MUTED,
                font=theme.ui(8),
            )
            detail = tk.Label(
                foot,
                text="",
                anchor="e",
                bg=theme.BG,
                fg=theme.MUTED,
                font=theme.ui(8),
            )
            if not compact:
                name.pack(side="left", fill="x", expand=True)
            detail.pack(side="right")
            row = _Row(frame, status, summary, bar, name, detail, dict(event))
            self.rows[item_id] = row
            bar.bind("<Configure>", lambda _e, canvas=bar: self._redraw(canvas))
        else:
            row = self.rows[item_id]
            row.event = dict(event)

        fraction = max(0.0, min(1.0, float(event.get("fraction") or 0.0)))
        done = int(event.get("done") or 0)
        total = int(event.get("total") or 0)
        state = str(event.get("state") or "running")
        if str(event.get("unit") or "") != "steps":
            self._update_speed(row, done)
        else:
            row.speed = 0.0
        row.status.configure(text=self._status_for(event))
        row.summary.configure(text=self._summary_for(event))
        row.name.configure(text=str(event.get("name") or ""))
        row.detail.configure(text=self._detail_for(row, event))
        row.bar._twilight_fraction = fraction  # type: ignore[attr-defined]
        row.bar._twilight_state = state  # type: ignore[attr-defined]
        self._redraw(row.bar)
        kind = str(event.get("kind") or "")
        if kind.startswith("startup_"):
            self._startup_kinds.add(kind)
            if self._startup_close_job is not None:
                try:
                    self.root.after_cancel(self._startup_close_job)
                except tk.TclError:
                    pass
            self._startup_close_job = self.root.after(5000, self._close_startup)
        if state in {"done", "error"}:
            self._schedule_remove(item_id, row)
        elif row.remove_job is not None:
            try:
                self.root.after_cancel(row.remove_job)
            except tk.TclError:
                pass
            row.remove_job = None
        if is_new_row:
            # Surface the board once, when a new task first appears.  Later
            # progress updates must never undo a user minimize or withdraw.
            self.window.deiconify()
            self.window.lift()

    def _close_startup(self) -> None:
        self._startup_close_job = None
        self._startup_finished = True
        if self.closed or self.window is None:
            return
        if len(self._startup_kinds) < 2:
            return
        # Do not hide a board that also has an active non-startup task.
        for key, row in list(self.rows.items()):
            kind = str(row.event.get("kind") or "")
            if not kind.startswith("startup_") and row.event.get("state") not in {"done", "error"}:
                return
        for key in [key for key, row in self.rows.items() if str(row.event.get("kind") or "").startswith("startup_")]:
            row = self.rows.pop(key, None)
            if row is not None:
                if row.remove_job is not None:
                    try:
                        self.root.after_cancel(row.remove_job)
                    except tk.TclError:
                        pass
                try:
                    row.frame.destroy()
                except tk.TclError:
                    pass
        try:
            self.window.withdraw()
        except tk.TclError:
            pass

    def hide_startup(self) -> None:
        """Force-hide the startup board after a bounded delay."""
        try:
            with open(r"H:/Github/_ai_install/hide_startup.log", "a", encoding="utf-8") as log:
                log.write("hide_startup called\n")
        except OSError:
            pass
        self._startup_finished = True
        if self.closed or self.window is None:
            return
        if self._startup_close_job is not None:
            try:
                self.root.after_cancel(self._startup_close_job)
            except tk.TclError:
                pass
            self._startup_close_job = None
        for key in [key for key, row in self.rows.items()
                    if str(row.event.get("kind") or "").startswith("startup_")]:
            row = self.rows.pop(key, None)
            if row is None:
                continue
            if row.remove_job is not None:
                try:
                    self.root.after_cancel(row.remove_job)
                except tk.TclError:
                    pass
            try:
                row.frame.destroy()
            except tk.TclError:
                pass
        try:
            self.window.withdraw()
        except tk.TclError:
            pass

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
