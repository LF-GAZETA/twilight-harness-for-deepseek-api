#!/usr/bin/env python3
"""TEST FIXTURE ONLY -- a stand-in app used to dry-run tests/run_e2e.py.

It is NOT the product and must never be shipped.  It implements just enough of
the agreed Twilight Harness contract for the QA suite to exercise its own
automation end-to-end before the Lead's real EXE exists:

* 800x600 frameless white window, black text, light-purple accents,
  title exactly ``Twilight Harness · 紫悦``, custom top-right min/max/close,
  draggable titlebar, right-edge resize.
* reads ``api-key.ini`` next to the EXE, prefills the masked key entry,
  language dropdown, confirm button disabled while key is empty.
* confirm -> ``POST {base}/chat/completions`` stream=false persona bootstrap
  whose system prompt contains the whole ``twilight-character.md`` + selected
  language; on success switches to the chat screen and writes the key back to
  the copied ini; on 401/HTTP error stays on setup and shows a red error.
* chat: reasoning Text left, output Text right, multiline input bottom-centre;
  Enter inserts a newline, Ctrl+Enter streams the reply (reasoning_content to
  the left pane, content to the right pane) via a worker thread.

Build/run through ``tests/dryrun_with_fake_app.py`` which packages it with
PyInstaller into a throw-away 3-file "delivery" folder under tests/artifacts/.
"""

import configparser
import ctypes
import json
import os
import queue
import sys
import threading
import time
import urllib.error
import urllib.request

import tkinter as tk
from tkinter import ttk

ACCENT = "#B39DDB"
ACCENT_DISABLED = "#EDE7F6"
DIVIDER = "#E6E6E6"
ERROR_RED = "#C62828"
TITLE = "Twilight Harness \u00b7 \u7d2b\u60a6"   # Twilight Harness · 紫悦
TITLEBAR_H = 36
WIN_W, WIN_H = 800, 600

LANGUAGES = [
    "上海话", "广东粤语", "东北话", "四川话", "新疆话", "普通话", "北京官话",
    "港澳粤语", "台湾话", "新加坡及马来西亚地区华语",
    "美式英语", "英式英语",
    "法国方言", "加拿大魁北克方言",
    "德国方言", "奥地利方言",
    "意大利语", "西班牙语", "葡萄牙方言", "巴西方言",
    "日语", "韩语", "阿拉伯语", "希伯来语",
]


def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


# --------------------------------------------------------------------------- #
# ini helpers
# --------------------------------------------------------------------------- #
def ini_path():
    return os.path.join(app_dir(), "api-key.ini")


def read_ini():
    values = {"api_key": "", "base_url": "https://api.deepseek.com",
              "model": "deepseek-reasoner"}
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(ini_path(), encoding="utf-8")
        if parser.has_section("deepseek"):
            for key in values:
                value = parser.get("deepseek", key, fallback=None)
                if value is not None:
                    values[key] = value.strip()
    except Exception:
        pass
    return values


def write_ini(api_key, base_url, model):
    parser = configparser.ConfigParser(interpolation=None)
    parser["deepseek"] = {
        "api_key": api_key,
        "base_url": base_url,
        "model": model,
    }
    try:
        with open(ini_path(), "w", encoding="utf-8") as fh:
            parser.write(fh)
    except OSError:
        pass


def character_text():
    path = os.path.join(app_dir(), "twilight-character.md")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


# --------------------------------------------------------------------------- #
# Minimal OpenAI-compatible client (stdlib only, works inside PyInstaller)
# --------------------------------------------------------------------------- #
def chat_endpoint(base_url):
    base = (base_url or "").strip().rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/chat/completions"


def post_json(base_url, api_key, payload, timeout=60):
    request = urllib.request.Request(
        chat_endpoint(base_url),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + api_key,
            "Accept": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def stream_chat(base_url, api_key, payload, on_event, timeout=120):
    request = urllib.request.Request(
        chat_endpoint(base_url),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + api_key,
            "Accept": "text/event-stream",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        for raw_line in response:
            line = raw_line.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                event = json.loads(data)
            except ValueError:
                continue
            choices = event.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            if delta.get("reasoning_content"):
                on_event("reasoning", delta["reasoning_content"])
            if delta.get("content"):
                on_event("content", delta["content"])


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #
class HarnessApp:
    def __init__(self, root):
        self.root = root
        self.config = read_ini()
        self.character = character_text()
        self.events = queue.Queue()
        self.pending = False
        self.language = tk.StringVar(value="")
        self.key_var = tk.StringVar(value=self.config.get("api_key", ""))
        self.messages = []
        self._drag_origin = None
        self._resize_origin = None
        self._maximized = False
        self._restore_geometry = "800x600+880+420"

        root.overrideredirect(True)
        root.title(TITLE)
        root.geometry("800x600+%d+%d" % ((root.winfo_screenwidth() - WIN_W) // 2,
                                         (root.winfo_screenheight() - WIN_H) // 2))
        root.configure(bg="white")

        self.titlebar = tk.Frame(root, bg="white", height=TITLEBAR_H,
                                 bd=0, highlightthickness=0)
        self.titlebar.place(x=0, y=0, width=WIN_W, height=TITLEBAR_H)
        self.body = tk.Frame(root, bg="white", bd=0, highlightthickness=0)
        self.body.place(x=0, y=TITLEBAR_H, width=WIN_W, height=WIN_H - TITLEBAR_H)
        self._build_setup()
        self._build_chat()
        self._build_titlebar_children()

        root.bind("<ButtonPress-1>", self._on_press, add="+")
        root.bind("<B1-Motion>", self._on_motion, add="+")
        root.bind("<ButtonRelease-1>", self._on_release, add="+")
        root.bind("<Control-Return>", lambda _event: self.send_message())
        self.key_var.trace_add("write", lambda *_: self._sync_confirm())

        self._sync_confirm()
        root.after(60, self._make_appwindow)
        root.after(50, self._poll_events)

    # -- window chrome ---------------------------------------------------- #
    def _build_titlebar_children(self):
        tk.Label(self.titlebar, text=TITLE, bg="white", fg="black",
                 font=("Segoe UI", 10)).place(x=14, rely=0.5, anchor="w")
        self.close_btn = self._chrome("\u2715", self.close)
        self.max_btn = self._chrome("\u25a1", self.toggle_maximize)
        self.min_btn = self._chrome("\u2014", self.minimize)

    def _chrome(self, glyph, command):
        label = tk.Label(self.titlebar, text=glyph, bg="white", fg="#444444",
                         width=3, font=("Segoe UI", 12))
        label.place(relx=1.0, x=-46 * (1 if command == self.close else
                                       2 if command == self.toggle_maximize
                                       else 3), rely=0.5, anchor="center")
        label.bind("<Button-1>", lambda _event: (command(), "break")[1])
        return label

    def _make_appwindow(self):
        try:
            user32 = ctypes.windll.user32
            hwnd = user32.GetAncestor(self.root.winfo_id(), 2)
            GWL_EXSTYLE = -20
            WS_EX_APPWINDOW = 0x00040000
            ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_APPWINDOW)
        except Exception:
            pass

    def _hwnd(self):
        try:
            return ctypes.windll.user32.GetAncestor(self.root.winfo_id(), 2)
        except Exception:
            return self.root.winfo_id()

    def minimize(self):
        try:
            ctypes.windll.user32.ShowWindow(self._hwnd(), 6)  # SW_MINIMIZE
        except Exception:
            self.root.withdraw()

    def toggle_maximize(self):
        if not self._maximized:
            self._restore_geometry = self.root.geometry()
            width = self.root.winfo_screenwidth()
            height = self.root.winfo_screenheight() - 48
            self.root.geometry("%dx%d+0+0" % (width, height))
            self._maximized = True
        else:
            self.root.geometry(self._restore_geometry)
            self._maximized = False

    def close(self):
        self.root.destroy()

    # -- root-level drag / resize ---------------------------------------- #
    def _on_press(self, event):
        x = event.x_root - self.root.winfo_rootx()
        y = event.y_root - self.root.winfo_rooty()
        if y <= TITLEBAR_H and x > WIN_W - 150:
            return
        if x >= self.root.winfo_width() - 8:
            self._resize_origin = (event.x_root, self.root.winfo_width())
        elif y <= TITLEBAR_H:
            self._drag_origin = (event.x_root, event.y_root,
                                 self.root.winfo_x(), self.root.winfo_y())

    def _on_motion(self, event):
        if self._drag_origin:
            ox, oy, wx, wy = self._drag_origin
            self.root.geometry("+%d+%d" % (wx + event.x_root - ox,
                                           wy + event.y_root - oy))
        elif self._resize_origin:
            ox, width = self._resize_origin
            new_width = max(400, width + event.x_root - ox)
            self.root.geometry("%dx%d+%d+%d" % (
                new_width, self.root.winfo_height(),
                self.root.winfo_x(), self.root.winfo_y()))

    def _on_release(self, _event):
        self._drag_origin = None
        self._resize_origin = None

    # -- setup screen ----------------------------------------------------- #
    def _build_setup(self):
        self.setup = tk.Frame(self.body, bg="white")
        self.setup.place(x=0, y=0, width=WIN_W, height=WIN_H - TITLEBAR_H)

        tk.Label(self.setup, text="Twilight Harness \u00b7 \u7d2b\u60a6",
                 bg="white", fg=ACCENT, font=("Segoe UI", 18, "bold")).place(
            relx=0.5, y=84, anchor="center")
        tk.Label(self.setup, text="\u8bf7\u5148\u914d\u7f6e DeepSeek API \u5bc6\u94a5",
                 bg="white", fg="#555555", font=("Segoe UI", 10)).place(
            relx=0.5, y=124, anchor="center")

        tk.Label(self.setup, text="API \u5bc6\u94a5\uff08API Key\uff09",
                 bg="white", fg="black", font=("Segoe UI", 10)).place(
            relx=0.5, y=182, anchor="center")
        self.key_entry = ttk.Entry(self.setup, textvariable=self.key_var,
                                   show="\u2022", width=44,
                                   font=("Segoe UI", 10))
        self.key_entry.place(relx=0.5, y=214, anchor="center", width=380)

        tk.Label(self.setup, text="\u5bf9\u8bdd\u8bed\u8a00 / Language",
                 bg="white", fg="black", font=("Segoe UI", 10)).place(
            relx=0.5, y=250, anchor="center")
        self.dropdown = ttk.Combobox(self.setup, textvariable=self.language,
                                     values=LANGUAGES, state="readonly",
                                     width=40, font=("Segoe UI", 10))
        self.dropdown.place(relx=0.5, y=279, anchor="center", width=380)

        self.confirm_btn = tk.Button(
            self.setup, text="\u786e\u8ba4\u5e76\u5f00\u59cb / Confirm",
            bg=ACCENT_DISABLED, fg="black", activebackground=ACCENT,
            bd=0, width=24, font=("Segoe UI", 10),
            command=self.on_confirm)
        self.confirm_btn.place(relx=0.5, y=344, anchor="center",
                               width=240, height=38)

        self.error_label = tk.Label(self.setup, text="", bg="white",
                                    fg=ERROR_RED, font=("Segoe UI", 10),
                                    wraplength=620, justify="center")
        self.error_label.place(relx=0.5, y=392, anchor="center")
        self.status_label = tk.Label(
            self.setup, text="", bg="white", fg="#777777",
            font=("Segoe UI", 9))
        self.status_label.place(relx=0.5, y=428, anchor="center")

    def _sync_confirm(self):
        has_key = bool(self.key_var.get().strip())
        if has_key:
            self.confirm_btn.configure(state="normal", bg=ACCENT,
                                       activebackground=ACCENT)
        else:
            self.confirm_btn.configure(state="disabled", bg=ACCENT_DISABLED,
                                       activebackground=ACCENT_DISABLED)

    # -- chat screen ------------------------------------------------------ #
    def _build_chat(self):
        self.chat = tk.Frame(self.body, bg="white")
        self.reasoning_label = tk.Label(
            self.chat, text="\u601d\u8003\u8fc7\u7a0b / Reasoning",
            bg="white", fg="#555555", font=("Segoe UI", 10, "bold"))
        self.reasoning_label.place(x=14, y=10)
        self.reasoning_text = tk.Text(
            self.chat, bg="white", fg="#333333", bd=0,
            highlightthickness=0, wrap="word", font=("Segoe UI", 10),
            state="disabled")
        self.reasoning_text.place(x=10, y=32, width=382, height=378)
        self.divider = tk.Frame(self.chat, bg=DIVIDER)
        self.divider.place(x=397, y=0, width=4, height=430)
        self.output_label = tk.Label(
            self.chat, text="\u56de\u590d / Output", bg="white", fg="#555555",
            font=("Segoe UI", 10, "bold"))
        self.output_label.place(x=414, y=10)
        self.output_text = tk.Text(
            self.chat, bg="white", fg="black", bd=0, highlightthickness=0,
            wrap="word", font=("Segoe UI", 10), state="disabled")
        self.output_text.place(x=410, y=32, width=380, height=378)
        self.input_hint = tk.Label(
            self.chat,
            text="Enter \u6362\u884c \u00b7 Ctrl+Enter \u53d1\u9001",
            bg="white", fg="#777777", font=("Segoe UI", 9))
        self.input_hint.place(relx=0.5, y=438, anchor="n")
        self.input_text = tk.Text(
            self.chat, bg="white", fg="black", bd=1, relief="solid",
            highlightthickness=0, wrap="word", font=("Segoe UI", 10),
            height=4)
        self.input_text.place(relx=0.5, y=458, anchor="n", width=580,
                              height=88)

    def show_chat(self):
        self.setup.place_forget()
        self.chat.place(x=0, y=0, width=WIN_W, height=WIN_H - TITLEBAR_H)
        self.input_text.focus_set()

    # -- bootstrap -------------------------------------------------------- #
    def on_confirm(self):
        if self.pending:
            return
        key = self.key_var.get().strip()
        if not key:
            return
        language = self.language.get().strip() or "美式英语"
        self.pending = True
        self.error_label.configure(text="")
        self.status_label.configure(text="\u6b63\u5728\u9a8c\u8bc1 API \u5bc6\u94a5\u5e76\u521d\u59cb\u5316\u89d2\u8272\u2026")
        self.confirm_btn.configure(state="disabled")
        system = (
            self.character
            + "\n\n---\n\u4ee5\u4e0a\u662f\u89d2\u8272\u8bbe\u5b9a\u3002"
              "\u8bf7\u5f00\u59cb\u626e\u6f14\uff1b\u672c\u6b21\u5bf9\u8bdd\u8bed\u8a00\uff1a"
            + language + "\u3002"
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": "\uff08\u89d2\u8272\u521d\u59cb\u5316\uff09"
                                        "\u8bf7\u7528\u8bbe\u5b9a\u7684\u8bed\u8a00"
                                        "\u6253\u62db\u547c\u3002"},
        ]
        thread = threading.Thread(
            target=self._bootstrap_worker,
            args=(key, self.config["base_url"], self.config["model"],
                  messages),
            daemon=True)
        thread.start()

    def _bootstrap_worker(self, key, base_url, model, messages):
        try:
            payload = post_json(base_url, key, {
                "model": model, "messages": messages, "stream": False})
            choice = (payload.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            self.events.put(("bootstrap_ok", {
                "key": key,
                "content": message.get("content") or "",
                "reasoning": message.get("reasoning_content") or "",
            }))
        except urllib.error.HTTPError as exc:
            self.events.put(("bootstrap_http_error",
                             {"status": exc.code,
                              "body": exc.read().decode("utf-8", "replace")}))
        except Exception as exc:  # noqa: BLE001
            self.events.put(("bootstrap_error", repr(exc)))

    # -- chat send -------------------------------------------------------- #
    def send_message(self):
        text = self.input_text.get("1.0", "end").rstrip("\n")
        if not text.strip() or self.pending:
            return
        self.pending = True
        self.input_text.delete("1.0", "end")
        self.messages.append({"role": "user", "content": text})
        self._append(self.reasoning_text, "")
        self._append(self.output_text, "")
        self.status_label.configure(text="")
        key = self.key_var.get().strip()
        history = list(self.messages)
        thread = threading.Thread(
            target=self._chat_worker,
            args=(key, self.config["base_url"], self.config["model"],
                  history),
            daemon=True)
        thread.start()

    def _chat_worker(self, key, base_url, model, messages):
        def on_event(kind, chunk):
            self.events.put((kind, chunk))
        try:
            stream_chat(base_url, key, {
                "model": model, "messages": messages, "stream": True},
                on_event)
            self.events.put(("chat_done", None))
        except urllib.error.HTTPError as exc:
            self.events.put(("chat_error", "HTTP %s" % exc.code))
        except Exception as exc:  # noqa: BLE001
            self.events.put(("chat_error", repr(exc)))

    # -- event pump ------------------------------------------------------- #
    def _poll_events(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                self._handle_event(kind, payload)
        except queue.Empty:
            pass
        self.root.after(40, self._poll_events)

    def _handle_event(self, kind, payload):
        if kind == "bootstrap_ok":
            self.pending = False
            write_ini(payload["key"], self.config["base_url"],
                      self.config["model"])
            self.show_chat()
            if payload["reasoning"]:
                self._append(self.reasoning_text, payload["reasoning"] + "\n")
            if payload["content"]:
                self._append(self.output_text, payload["content"] + "\n\n")
                self.messages.append({"role": "assistant",
                                      "content": payload["content"]})
        elif kind == "bootstrap_http_error":
            self.pending = False
            self._sync_confirm()
            status = payload.get("status")
            self.error_label.configure(
                text="API \u9519\u8bef\uff08HTTP %s\uff09\uff1a"
                     "\u5bc6\u94a5\u65e0\u6548\u6216\u670d\u52a1\u4e0d\u53ef\u7528\uff0c"
                     "\u8bf7\u68c0\u67e5\u540e\u91cd\u8bd5\u3002" % status)
            self.status_label.configure(text="")
        elif kind == "bootstrap_error":
            self.pending = False
            self._sync_confirm()
            self.error_label.configure(text="\u8fde\u63a5\u9519\u8bef\uff1a"
                                            + str(payload)[:200])
        elif kind == "reasoning":
            self._append(self.reasoning_text, payload)
        elif kind == "content":
            self._append(self.output_text, payload)
        elif kind == "chat_done":
            self.pending = False
            self.messages.append({"role": "assistant",
                                  "content": self.output_text.get("1.0",
                                                                  "end").strip()})
        elif kind == "chat_error":
            self.pending = False
            self._append(self.output_text, "\n[\u9519\u8bef] " + str(payload))

    @staticmethod
    def _append(widget, text):
        widget.configure(state="normal")
        widget.insert("end", text)
        widget.see("end")
        widget.configure(state="disabled")


def main():
    root = tk.Tk()
    HarnessApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
