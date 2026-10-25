#!/usr/bin/env python3
"""TEST FIXTURE ONLY -- not the product.

A tiny fake window that mimics enough of the Twilight Harness contract to
self-test ``ui_probe.py`` before the real EXE exists:

* 800x600 frameless white window, black text, light-purple accent button
* title ``Twilight Harness · 紫悦 (FAKE)``
* custom top-right minimize / maximize / close buttons
* draggable titlebar, right-edge resize
* every user action appended as a JSON line to the marker file given by argv[1]

It is launched by ``selftest_harness.py``; it is never used against the real
delivery folder and is not part of the product package.
"""

import json
import os
import sys
import tkinter as tk
from tkinter import ttk

ACCENT = "#B39DDB"
FAKE_TITLE = "Twilight Harness \u00b7 \u7d2b\u60a6 (FAKE)"
START_GEOMETRY = "800x600+320+180"


class FakeApp:
    def __init__(self, root, marker_path):
        self.root = root
        self.marker_path = marker_path
        self.restore_geometry = START_GEOMETRY
        self.maximized = False
        self._drag_origin = None
        self._resize_origin = None

        root.overrideredirect(True)
        root.title(FAKE_TITLE)
        root.geometry(START_GEOMETRY)
        root.attributes("-topmost", True)
        root.configure(bg="white")

        # ---- custom titlebar ----
        self.bar = tk.Frame(root, bg="white", height=36)
        self.bar.pack(side="top", fill="x")
        self.bar.pack_propagate(False)
        self.title_label = tk.Label(self.bar, text=FAKE_TITLE, bg="white",
                                    fg="black", font=("Segoe UI", 10))
        self.title_label.pack(side="left", padx=12)
        self.close_btn = self._chrome_button("\u2715", "close")
        self.max_btn = self._chrome_button("\u25a1", "max")
        self.min_btn = self._chrome_button("\u2014", "min")

        for widget in (self.bar, self.title_label):
            widget.bind("<ButtonPress-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)
            widget.bind("<ButtonRelease-1>", self._drag_end)
        # Root-level fallbacks: titlebar drag anywhere in the top bar and
        # right-edge resize; more robust than per-widget bindings.
        root.bind("<ButtonPress-1>", self._on_press, add="+")
        root.bind("<B1-Motion>", self._on_motion, add="+")
        root.bind("<ButtonRelease-1>", self._on_release, add="+")

        # ---- body ----
        body = tk.Frame(root, bg="white")
        body.pack(fill="both", expand=True)
        tk.Label(body, text="QA HARNESS SELF-TEST FIXTURE", bg="white",
                 fg="black", font=("Segoe UI", 14, "bold")).pack(pady=(28, 6))
        tk.Label(body, text="line one of black text", bg="white", fg="black",
                 font=("Segoe UI", 10)).pack()

        entry_row = tk.Frame(body, bg="white")
        entry_row.pack(pady=(22, 8))
        tk.Label(entry_row, text="API key:", bg="white", fg="black",
                 font=("Segoe UI", 10)).pack(side="left")
        self.key_var = tk.StringVar(value="sk-prefilled")
        self.key_entry = ttk.Entry(entry_row, textvariable=self.key_var,
                                   show="\u2022", width=32)
        self.key_entry.pack(side="left", padx=8)

        self.language = tk.StringVar(value="")
        self.dropdown = ttk.Combobox(body, textvariable=self.language,
                                     values=["\u666e\u901a\u8bdd",
                                             "\u7f8e\u5f0f\u82f1\u8bed",
                                             "\u65e5\u8bed"],
                                     state="readonly", width=22)
        self.dropdown.pack(pady=8)

        self.confirm = tk.Button(body, text="Confirm", bg=ACCENT, fg="black",
                                 activebackground=ACCENT, bd=0, width=22,
                                 command=self._confirm)
        self.confirm.pack(pady=14)
        self.status = tk.Label(body, text="", bg="white", fg="#666666",
                               font=("Segoe UI", 9))
        self.status.pack()

        # ---- right-edge resize strip (visual; events handled at root) ----
        self.edge = tk.Frame(root, bg="#EEEEEE", width=5)
        self.edge.place(relx=1.0, rely=0.0, relheight=1.0, x=-5, width=5)
        self.edge.configure(cursor="sb_h_double_arrow")

        self._record("launched", geometry=root.geometry())
        root.after(200, self._focus_key)

    # -- helpers ----------------------------------------------------------
    def _chrome_button(self, text, action):
        label = tk.Label(self.bar, text=text, bg="white", fg="#555555",
                         width=3, font=("Segoe UI", 11))
        label.pack(side="right", padx=1)
        label.bind("<Button-1>",
                   lambda _event, act=action: (self._do(act), "break")[1])
        return label

    def _focus_key(self):
        self.key_entry.focus_set()

    def _record(self, action, **extra):
        record = {"action": action}
        record.update(extra)
        try:
            with open(self.marker_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                fh.flush()
        except OSError:
            pass

    # -- window controls --------------------------------------------------
    def _do(self, action):
        self._record(action)
        if action == "close":
            self.root.destroy()
        elif action == "min":
            self.root.withdraw()  # fixture: hide (real app uses taskbar icon)
        elif action == "max":
            if not self.maximized:
                self.restore_geometry = self.root.geometry()
                max_w = self.root.winfo_screenwidth()
                max_h = self.root.winfo_screenheight() - 48
                self.root.geometry("%dx%d+0+0" % (max_w, max_h))
                self.maximized = True
            else:
                self.root.geometry(self.restore_geometry)
                self.maximized = False
            self._record("maximized_state",
                         maximized=self.maximized,
                         geometry=self.root.geometry())

    def _confirm(self):
        self._record("confirm", language=self.language.get(),
                     key=self.key_var.get())
        self.status.configure(text="confirmed (fixture)")

    # -- drag / resize ----------------------------------------------------
    def _drag_start(self, event):
        self._drag_origin = (event.x_root, event.y_root,
                             self.root.winfo_x(), self.root.winfo_y())
        self._record("drag_start")

    def _drag_move(self, event):
        if not self._drag_origin:
            return
        ox, oy, wx, wy = self._drag_origin
        self.root.geometry("+%d+%d" % (wx + event.x_root - ox,
                                       wy + event.y_root - oy))

    def _drag_end(self, _event):
        self._drag_origin = None
        self._record("drag_end", geometry=self.root.geometry())

    # -- root-level fallbacks (robust for the QA fixture) ----------------
    def _on_press(self, event):
        x = event.x_root - self.root.winfo_rootx()
        y = event.y_root - self.root.winfo_rooty()
        width = self.root.winfo_width()
        bar_h = self.bar.winfo_height()
        if y <= bar_h and x > width - 150:
            return  # titlebar buttons own this area
        if x >= width - 8:
            self._resize_start(event)
        elif y <= bar_h:
            self._drag_start(event)

    def _on_motion(self, event):
        if self._drag_origin:
            self._drag_move(event)
        elif self._resize_origin:
            self._resize_move(event)

    def _on_release(self, event):
        if self._drag_origin:
            self._drag_end(event)
        self._resize_origin = None

    def _resize_start(self, event):
        self._resize_origin = (event.x_root, self.root.winfo_width())
        self._record("resize_start")

    def _resize_move(self, event):
        if not self._resize_origin:
            return
        ox, width = self._resize_origin
        new_width = max(300, width + event.x_root - ox)
        height = self.root.winfo_height()
        self.root.geometry("%dx%d+%d+%d" % (new_width, height,
                                            self.root.winfo_x(),
                                            self.root.winfo_y()))
        self._record("resize_move", width=new_width)


def main(argv):
    if len(argv) < 1:
        print("usage: fake_window_app.py <marker.jsonl>")
        return 2
    marker_path = os.path.abspath(argv[0])
    root = tk.Tk()
    app = FakeApp(root, marker_path)
    try:
        root.mainloop()
    finally:
        try:
            app._record("exited")
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
