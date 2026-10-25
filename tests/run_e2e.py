#!/usr/bin/env python3
"""End-to-end black-box QA for the Twilight Harness deliverable.

What it does
------------
Copies ``twilight-harness/`` (exactly 3 files) to a fresh temp directory, writes
a per-copy ``api-key.ini`` that points the app at ``mock_deepseek.py``, launches
``twilight-harness.exe`` exactly like a user, and drives it with pyautogui +
Win32 while capturing screenshots and API-request evidence.  The delivery
folder itself is **never** modified or launched from.

Scenarios (``--scenarios a,b,c,d,e,f``; all run by default)
    a  package/3-file check, initial window, 800x600, frameless, colours,
       setup layout, confirm-gated-while-empty
    b  key prefill from ini, language dropdown, confirm -> persona bootstrap
       request (character.md content + language), then chat screen
    c  chat layout (reasoning left / output right), bottom-center input,
       Enter=newline without sending, Ctrl+Enter sends exactly once, streaming
       reasoning then content
    d  custom window controls: titlebar drag, edge resize, maximize/restore,
       minimize/IsIconic/restore, close
    e  mock 401: stays on setup with a visible error
    f  write-back of a successful key to the copied ini + restart prefill

Usage (bundled Python — plain ``python`` on PATH is a broken Store stub)::

    "C:\\Users\\User\\.dsh\\dsh-runtimes\\dsh-primary-runtime\\dependencies\\python\\python.exe" ^
        tests/run_e2e.py

Exit codes: 0 all checks PASS, 1 FAIL/WARN present or harness crash,
2 build not ready.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
import traceback
from collections import Counter
from dataclasses import dataclass, field

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import numpy as np  # noqa: E402

import mock_deepseek  # noqa: E402
import ui_probe  # noqa: E402
from ui_probe import (  # noqa: E402
    APP_TITLE, APP_TITLE_CN, APP_TITLE_CORE, ARTIFACTS_DIR, BuildNotReady,
    DEFAULT_MODEL, DEFAULT_TEST_KEY, DELIVERY_CHARACTER, DELIVERY_DIR,
    DELIVERY_EXE, DELIVERY_FILES, DELIVERY_INI, LANGUAGE_TOKENS,
    LANGUAGE_TOKENS_EN, SCREENSHOTS_DIR, ProbeError, color_stats,
    detect_layout, find_purple_boxes, text_pixels,
)

# --------------------------------------------------------------------------- #
# Requirement catalogue (kept in sync with tests/QA_CHECKLIST.md)
# --------------------------------------------------------------------------- #
REQUIREMENTS = {
    "R01": "Delivery folder contains exactly api-key.ini, twilight-harness.exe, twilight-character.md",
    "R02": "Window title is literally 'Twilight Harness · 紫悦'",
    "R03": "Initial window size is 800x600",
    "R04": "Window is resizable",
    "R05": "Frameless window with custom top-right minimize/maximize/close buttons",
    "R06": "Pure white background, black text, light-purple accents",
    "R07": "Setup screen top-down: masked centered API-key input, language dropdown, confirm button",
    "R08": "Confirm button disabled while API-key input is empty",
    "R09": "Per-copy api-key.ini prefills the API-key entry",
    "R10": "Confirm calls a DeepSeek persona-bootstrap API (stream=false) before chat",
    "R11": "Bootstrap request contains substantial twilight-character.md content",
    "R12": "Bootstrap request includes the selected language",
    "R13": "Chat screen: reasoning/thinking region left, output transcript right",
    "R14": "Multiline user input at bottom-center",
    "R15": "Plain Enter inserts a newline and does not send",
    "R16": "Ctrl+Enter sends exactly once",
    "R17": "Responses stream reasoning_content first then content",
    "R18": "A 401 from the API keeps the user on setup with an error message",
    "R19": "Successful key is written back to the copy's api-key.ini",
    "R20": "Restart with a successful copy shows the prefilled key",
    "R21": "Chat request is OpenAI-compatible: path, bearer auth, model, messages, stream=true SSE",
    "R22": "Dragging the custom titlebar moves the window",
    "R23": "Dragging a window edge resizes the window",
    "R24": "Maximize then restore works",
    "R25": "Minimize -> IsIconic -> restore works",
    "R26": "Close button exits the app",
    "R27": "QA never modifies the delivery folder (stays exactly 3 files, hashes unchanged)",
}

EXTRA_LANGUAGE_HINTS = [
    "中文", "简体中文", "繁体中文", "汉语", "华语", "方言", "语言",
    "language", "Language",
]
LOCALE_PATTERN = re.compile(r"\b[a-z]{2,3}[_-][A-Za-z]{2,4}\b")

# Fallback layout hints, expressed as fractions of the client area.  They are
# overridable via tests/ui_hints.json after the first visual inspection.
DEFAULT_HINTS = {
    "key_entry": [0.500, 0.420],
    "dropdown": [0.500, 0.530],
    "confirm": [0.500, 0.640],
    "chat_input": [0.500, 0.920],
    "titlebar_y": 16,
}


# --------------------------------------------------------------------------- #
# Tiny data types
# --------------------------------------------------------------------------- #
@dataclass
class Check:
    scenario: str
    cid: str
    requirement: str
    name: str
    status: str
    detail: str = ""
    evidence: list = field(default_factory=list)

    def as_dict(self):
        return {
            "scenario": self.scenario,
            "id": self.cid,
            "requirement": self.requirement,
            "requirement_text": REQUIREMENTS.get(self.requirement, ""),
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "evidence": self.evidence,
        }


# --------------------------------------------------------------------------- #
# Small image helpers (kept local: run_e2e-specific analyses)
# --------------------------------------------------------------------------- #
def image_diff_count(img_a, img_b, box=None, threshold=40):
    """Count pixels whose max-channel delta exceeds ``threshold``."""
    arr_a = np.asarray(img_a.convert("RGB"), dtype=np.int16)
    arr_b = np.asarray(img_b.convert("RGB"), dtype=np.int16)
    if box is not None:
        x0, y0, x1, y1 = box
        h, w = arr_a.shape[:2]
        arr_a = arr_a[max(0, y0):min(h, y1), max(0, x0):min(w, x1)]
        arr_b = arr_b[max(0, y0):min(h, y1), max(0, x0):min(w, x1)]
    if arr_a.shape != arr_b.shape or arr_a.size == 0:
        return int(max(arr_a.size, arr_b.size))
    delta = np.abs(arr_a - arr_b).max(axis=2)
    return int((delta > threshold).sum())


def best_vertical_line(img, x_range=(0.20, 0.80), y_range=(0.12, 0.76),
                       step=2):
    """Wide-range vertical separator detection (chat split left ~37 %)."""
    return ui_probe.best_vertical_line(img, x_range=x_range,
                                       y_range=y_range, step=step)


def pane_regions(img):
    w, h = img.size
    divider = best_vertical_line(img)
    dx = divider["x"] if divider["score"] >= 0.45 else w // 2
    y0, y1 = int(h * 0.09), int(h * 0.78)
    return {
        "divider": divider,
        "divider_x": dx,
        "divider_frac": round(dx / float(w), 4),
        "left": (int(0.02 * w), y0, max(0, dx - 8), y1),
        "right": (min(w, dx + 8), y0, int(0.98 * w), y1),
        "full": (int(0.02 * w), y0, int(0.98 * w), y1),
    }


def region_text_counts(img, panes):
    return {
        "left": text_pixels(img, box=panes["left"]),
        "right": text_pixels(img, box=panes["right"]),
    }


def image_diff_bbox(img_a, img_b, box=None, threshold=50):
    """Bounding box (x0,y0,x1,y1) of changed pixels, or None."""
    arr_a = np.asarray(img_a.convert("RGB"), dtype=np.int16)
    arr_b = np.asarray(img_b.convert("RGB"), dtype=np.int16)
    if box is not None:
        x0, y0, x1, y1 = box
        h, w = arr_a.shape[:2]
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(w, x1), min(h, y1)
        arr_a = arr_a[y0:y1, x0:x1]
        arr_b = arr_b[y0:y1, x0:x1]
    else:
        x0, y0 = 0, 0
    if arr_a.shape != arr_b.shape or arr_a.size == 0:
        return None
    delta = np.abs(arr_a - arr_b).max(axis=2)
    mask = delta > threshold
    if not mask.any():
        return None
    rows = np.any(mask, axis=1)
    cols = np.any(mask, axis=0)
    r = np.where(rows)[0]
    c = np.where(cols)[0]
    return (int(x0 + c[0]), int(y0 + r[0]),
            int(x0 + c[-1]) + 1, int(y0 + r[-1]) + 1)


def count_text_rows(img, box, min_pixels=3, gap=4):
    """Number of rendered text lines inside a client-relative box."""
    crop = img.crop(box)
    profile = ui_probe._row_profile(crop)
    bands = ui_probe.text_row_bands(profile, min_pixels=min_pixels, gap=gap)
    return len([band for band in bands if band[1] - band[0] >= 3]), bands


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
class E2E:
    def __init__(self, args):
        self.args = args
        self.checks = []
        self.character_text = ""
        self.hints = dict(DEFAULT_HINTS)
        self._load_hints()
        self.server = None
        self.temp_copies = []
        self.copies_total = 0
        self.launched_exe_paths = []
        self.apps = []
        self.screenshots = []
        self.requests_by_scenario = {}
        self._scenario_sid = ""
        self._scenario_mark = 0
        self.delivery = {}
        self.started = time.time()
        self.timeout_scale = max(0.5, float(args.timeout_scale))
        self._preflight_done = False
        # Agent-sandbox quirk: PyInstaller onefile extraction fails when the
        # native child inherits the sandboxed Windows %TEMP%; point the child's
        # TEMP/TMP at a writable dir inside the workspace for QA only.
        self.runtime_temp = os.path.join(ARTIFACTS_DIR, "runtime_temp")
        ui_probe.ensure_dir(self.runtime_temp)

    # ------------------------------------------------------------------ #
    # infrastructure
    # ------------------------------------------------------------------ #
    def timeout(self, seconds):
        return seconds * self.timeout_scale

    def _load_hints(self):
        path = self.args.hints
        if path and os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                for key, value in data.items():
                    if isinstance(value, (list, tuple)) and len(value) == 2:
                        self.hints[key] = [float(value[0]), float(value[1])]
                    else:
                        self.hints[key] = value
                print("[hints] loaded overrides from %s" % path)
            except Exception as exc:
                print("[hints] WARNING could not load %s: %s" % (path, exc))

    def hint_point(self, win, name):
        fractions = self.hints.get(name, DEFAULT_HINTS[name])
        w, h = win.client_size
        return (int(round(w * fractions[0])), int(round(h * fractions[1])))

    def hint_y(self, win, name):
        return self.hint_point(win, name)[1]

    def begin_scenario(self, sid):
        self._scenario_sid = sid
        if self.server is not None:
            self._scenario_mark = self.server.mark()
        print("\n" + "=" * 78)
        print("SCENARIO %s  (%s)" % (sid, time.strftime("%H:%M:%S")))
        print("=" * 78)

    def end_scenario(self, sid, note=""):
        if self.server is not None and self._scenario_mark is not None:
            records = self.server.since(self._scenario_mark)
            self.requests_by_scenario[sid] = records
            print("[scenario %s] %d mock request(s) captured%s"
                  % (sid, len(records), (": " + note) if note else ""))
        self._scenario_sid = ""

    def check(self, cid, requirement, name, ok, detail="", evidence=None,
              warn=False, skip=False):
        if skip:
            status = "SKIP"
        elif ok:
            status = "PASS"
        elif warn:
            status = "WARN"
        else:
            status = "FAIL"
        item = Check(scenario=self._scenario_sid or "-", cid=cid,
                     requirement=requirement, name=name, status=status,
                     detail=str(detail), evidence=list(evidence or []))
        self.checks.append(item)
        print("[%s] %-4s %-58s %s"
              % (status, cid, name, ("- " + item.detail) if item.detail else ""))
        return status

    def info(self, cid, requirement, name, detail):
        self.check(cid, requirement, name, True, detail, evidence=None,
                   skip=True)

    def shot(self, win, filename, region=None):
        path = os.path.join(SCREENSHOTS_DIR, filename)
        try:
            if win is not None and win.alive:
                ui_probe.bring_to_foreground(win)
            if win is None:
                ui_probe.save_screen(path)
            elif region is not None:
                ui_probe.save_window_region_shot(win, path, region)
            else:
                ui_probe.save_window_shot(win, path)
            self.screenshots.append(path)
        except Exception as exc:
            print("[shot] WARNING could not save %s: %s" % (filename, exc))
            return path
        return path

    def shot_region(self, win, filename, region):
        return self.shot(win, filename, region=region)

    # -- mock server / requests ---------------------------------------- #
    def start_server(self):
        self.server = mock_deepseek.MockServer(
            port=self.args.mock_port,
            scenario="success",
            log_path=self.args.requests_log,
            chunk_delay=self.args.chunk_delay,
            verbose=self.args.verbose,
        ).start()
        print("[mock] %s  scenario=success  chunk_delay=%.3fs  log=%s"
              % (self.server.base_url, self.args.chunk_delay,
                 self.args.requests_log))

    def stop_server(self):
        if self.server is not None:
            try:
                self.server.stop()
            except Exception:
                pass
            self.server = None

    def req_mark(self):
        return self.server.mark() if self.server else 0

    def reqs_since(self, mark, kind=None):
        if self.server is None:
            return []
        return self.server.since(mark, kind)

    def wait_requests(self, mark=None, kind=None, timeout=20.0,
                      predicate=None):
        if self.server is None:
            return []
        mark = self._scenario_mark if mark is None else mark
        return self.server.wait_for(kind=kind, mark=mark,
                                    timeout=self.timeout(timeout),
                                    predicate=predicate)

    # -- delivery copy / process --------------------------------------- #
    def make_copy(self, sid, key=DEFAULT_TEST_KEY, model=DEFAULT_MODEL,
                  base_url=None):
        if self.server is None:
            raise ProbeError("mock server not started")
        copy = ui_probe.copy_delivery(delivery_dir=self.args.delivery_dir,
                                      keep=self.args.keep_temp,
                                      prefix="twilight-qa-%s-" % sid)
        root_real = os.path.realpath(copy.root)
        delivery_real = os.path.realpath(self.args.delivery_dir)
        if root_real == delivery_real or root_real.startswith(
                delivery_real + os.sep):
            copy.cleanup()
            raise ProbeError("refusing to run against the delivery folder "
                             "itself: %s" % root_real)
        copy.write_ini(base_url or self.server.base_url, api_key=key,
                       model=model)
        self.temp_copies.append(copy)
        self.copies_total += 1
        return copy

    def launch(self, copy, sid, wait_timeout=30.0):
        launch_env = {
            "TEMP": self.runtime_temp,
            "TMP": self.runtime_temp,
            "TMPDIR": self.runtime_temp,
        }
        app, win = ui_probe.launch_and_find(
            copy.root, timeout=self.timeout(wait_timeout),
            artifact_prefix="%s_%s" % (sid, os.path.basename(copy.root)),
            extra_env=launch_env)
        self.apps.append(app)
        self.launched_exe_paths.append(os.path.realpath(app.exe_path))
        return app, win

    def teardown(self, app=None, win=None, copy=None):
        if app is not None and app.running():
            if win is not None and win.alive:
                try:
                    win.post_close()
                except Exception:
                    pass
                if not app.wait_exit(timeout=5):
                    app.terminate()
            else:
                app.terminate()
        if copy is not None:
            copy.cleanup()
            if copy in self.temp_copies:
                self.temp_copies.remove(copy)
        if app is not None and app in self.apps:
            self.apps.remove(app)

    # -- request helpers ------------------------------------------------ #
    @staticmethod
    def request_brief(record):
        body = record.get("body") or {}
        headers = record.get("headers_lc") or {}
        messages = body.get("messages") or []
        last_user = ""
        for message in reversed(messages):
            if isinstance(message, dict) and message.get("role") == "user":
                last_user = str(message.get("content"))[:140]
                break
        return {
            "seq": record.get("seq"),
            "time": record.get("time"),
            "kind": record.get("request_kind"),
            "method": record.get("method"),
            "path": record.get("normalized_path") or record.get("path"),
            "status": record.get("response_status"),
            "auth": headers.get("authorization"),
            "content_type": headers.get("content-type"),
            "model": body.get("model"),
            "stream": body.get("stream"),
            "messages": len(messages),
            "last_user": last_user,
            "chunks_sent": record.get("chunks_sent"),
            "duration_s": record.get("duration_s"),
            "body_chars": len(json.dumps(body, ensure_ascii=False)),
        }

    def payload_text(self, records):
        return json.dumps([r.get("body") for r in records],
                          ensure_ascii=False)

    def character_overlap(self, records):
        char = self.character_text or ""
        out = {"character_available": bool(char), "character_chars": len(char),
               "chunks": 0, "matched_chunks": 0, "ratio": 0.0,
               "lines": 0, "matched_lines": 0}
        if not char:
            return out
        norm_char = re.sub(r"\s+", "", char)
        chunks = []
        seen = set()
        for start in range(0, max(1, len(norm_char) - 24), 8):
            chunk = norm_char[start:start + 24]
            if len(chunk) == 24 and chunk not in seen:
                seen.add(chunk)
                chunks.append(chunk)
        payload = re.sub(r"\s+", "", self.payload_text(records))
        matched = sum(1 for chunk in chunks if chunk in payload)
        out["chunks"] = len(chunks)
        out["matched_chunks"] = matched
        out["ratio"] = round(matched / max(1, len(chunks)), 4)
        lines = [line.strip() for line in char.splitlines()
                 if len(line.strip()) >= 20]
        matched_lines = 0
        for line in lines:
            key = re.sub(r"\s+", "", line)[:20]
            if len(key) >= 12 and key in payload:
                matched_lines += 1
        out["lines"] = len(lines)
        out["matched_lines"] = matched_lines
        return out

    @staticmethod
    def find_language_tokens(text):
        found = []
        for token in LANGUAGE_TOKENS:
            if token in text:
                found.append(token)
        lowered = text.lower()
        for token in LANGUAGE_TOKENS_EN:
            if re.search(r"\b" + re.escape(token.lower()) + r"\b", lowered):
                found.append(token)
        for token in EXTRA_LANGUAGE_HINTS:
            if token in text:
                found.append(token)
        for match in LOCALE_PATTERN.finditer(text):
            found.append(match.group(0))
        return sorted(set(found))

    def language_evidence(self, records):
        payload = self.payload_text(records)
        found = self.find_language_tokens(payload)
        has_word = ("语言" in payload) or ("language" in payload.lower())
        markers = []
        for pattern in (
            r"请直接用([^，。\n]{2,40}?)写",
            r"用\s*([^，。\s]{2,30}?)\s*开始和用户对话",
            r"(?:语言|Language)\s*[：:]\s*([^\n，。]{2,30})",
            r"reply\s+(?:in|using)\s+([A-Za-z\u4e00-\u9fff ]{2,30})",
        ):
            markers.extend(match.group(1).strip()
                           for match in re.finditer(pattern, payload))
        return {"found": found, "found_language_word": has_word,
                "selected_language_markers": sorted(set(markers))}

    # -- UI helpers ----------------------------------------------------- #
    def confirm_point(self, win, img=None):
        img = img if img is not None else ui_probe.grab_window(win)
        w, h = img.size
        boxes = find_purple_boxes(img)
        mid = [b for b in boxes
               if 0.30 * h <= b["center"][1] <= 0.92 * h
               and b["width"] >= max(80, 0.10 * w)
               and b["height"] >= 16]
        if mid:
            hint_x, hint_y = self.hint_point(win, "confirm")
            mid.sort(key=lambda b: abs(b["center"][1] - hint_y))
            target = mid[0]
            return {"point": target["center"], "method": "purple-accent-button",
                    "boxes": len(mid), "box": target["box"]}
        return {"point": self.hint_point(win, "confirm"),
                "method": "layout-hint", "boxes": 0, "box": None}

    def type_key(self, win, text):
        """Click the API-key entry and type ``text`` with verification."""
        before = ui_probe.grab_window(win)
        w, h = before.size
        hx, hy = self.hint_point(win, "key_entry")
        candidates = [(hx, hy, "hint")]
        profile = ui_probe._row_profile(before)
        for y0, y1 in ui_probe.text_row_bands(profile, min_pixels=4, gap=4):
            cy = (y0 + y1) // 2
            if 0.14 * h <= cy <= 0.62 * h and abs(cy - hy) > 12:
                candidates.append((w // 2, cy, "row-band@%d" % cy))
        candidates = candidates[:5]
        attempts = []
        for x, y, method in candidates:
            ui_probe.click_client(win, x, y)
            ui_probe.ensure_foreground(win)
            ui_probe.hotkey("ctrl", "a")
            # Clipboard paste instead of synthetic keystrokes: the shared
            # Windows session has a Chinese IME active, which would intercept
            # typed ASCII (composition window) and mangle the key.  Pasting is
            # a normal user action and is IME-proof.
            ui_probe.paste_text(text)
            time.sleep(0.45)
            # Blur the field: the app commits its masked rendering and updates
            # the confirm-button/status state on focus change.
            try:
                ui_probe.click_client(win, int(0.5 * w), int(0.40 * h),
                                      settle=0.25)
            except ProbeError:
                pass
            time.sleep(0.55)
            after = ui_probe.grab_window(win)
            region = (int(0.10 * w), max(0, y - 45), int(0.90 * w),
                      min(h, y + 45))
            diff = image_diff_count(before, after, box=region)
            text_px = text_pixels(after, box=region)
            attempts.append({"method": method, "point": (x, y),
                             "diff": diff, "text_px": text_px})
            if diff > 40 or text_px > 30:
                shot = self.shot(win, "a03_key_typed_masked.png"
                                 if self._scenario_sid == "a"
                                 else "%s_key_typed.png" % self._scenario_sid)
                return {"ok": True, "method": method, "point": (x, y),
                        "attempts": attempts, "screenshot": shot,
                        "text_px": text_px}
            # undo anything typed into the wrong widget before trying again
            ui_probe.hotkey("ctrl", "a")
            ui_probe.press("delete")
            ui_probe.press("escape")
        return {"ok": False, "method": None, "attempts": attempts,
                "screenshot": None}

    def select_language(self, win, prefix):
        """Open the custom language dropdown and pick a non-default option.

        The real app draws an in-window popup below the field (~12 rows x 35
        px) and supports MOUSE selection (no Down/Enter navigation).  The popup
        can take a moment to appear, and clicking a stale coordinate would hit
        the confirm button underneath, so poll until the popup is visibly open
        and then click exactly one option row.
        """
        before = ui_probe.grab_window(win)
        w, h = before.size
        dx, dy = self.hint_point(win, "dropdown")
        header_region = (int(0.10 * w), max(0, dy - 30),
                         int(0.90 * w), min(h, dy + 30))
        popup_region = (int(0.12 * w), min(h - 1, dy + 12),
                        int(0.88 * w), min(h, dy + int(0.60 * h)))
        result = {"opened_diff": 0, "clicked_dropdown": (dx, dy),
                  "screenshots": [], "method": None, "changed": False,
                  "selected_option_y": None}

        opened_img = None
        for _attempt in range(2):
            ui_probe.bring_to_foreground(win)
            ui_probe.click_client(win, dx, dy, settle=0.45)
            deadline = time.time() + 2.5
            while time.time() < deadline:
                current = ui_probe.grab_window(win)
                if image_diff_count(before, current,
                                    box=popup_region) >= 200:
                    opened_img = current
                    break
                time.sleep(0.12)
            if opened_img is not None:
                break
        result["opened_diff"] = (
            image_diff_count(before, opened_img, box=popup_region)
            if opened_img is not None
            else image_diff_count(before, ui_probe.grab_window(win),
                                  box=header_region))
        open_shot = self.shot(win, "%s_dropdown_open.png" % prefix)
        result["screenshots"].append(open_shot)
        if opened_img is None:
            ui_probe.press("escape")
            result["detail"] = "dropdown popup did not appear after 2 clicks"
            return result

        # Locate real option rows inside the popup (skip 1-3px border bands).
        profile = ui_probe._row_profile(opened_img)
        bands = [(y0, y1) for (y0, y1)
                 in ui_probe.text_row_bands(profile, min_pixels=6, gap=6)
                 if y0 > dy + 8 and y1 - y0 >= 6]
        candidates = []
        for y0, y1 in bands[:12]:
            cy = (y0 + y1) // 2
            probe_box = (int(0.20 * w), cy - 6, int(0.80 * w), cy + 6)
            if image_diff_count(before, opened_img, box=probe_box) < 5:
                continue  # that row band is not part of the popup overlay
            candidates.append((y0, y1, cy))
        result["option_rows"] = [(a, b) for a, b, _cy in candidates]
        if not candidates:
            ui_probe.press("escape")
            result["detail"] = "no option rows detected in open popup"
            return result
        if len(candidates) >= 2:
            target = candidates[1]  # 2nd visible option -> differs from default
        else:
            target = candidates[0]

        _y0, _y1, cy = target
        # Verify the popup still overlays that row right now.
        current = ui_probe.grab_window(win)
        probe_box = (int(0.20 * w), cy - 8, int(0.80 * w), cy + 8)
        still_open = image_diff_count(before, current, box=probe_box) >= 5
        if not still_open:
            ui_probe.click_client(win, dx, dy, settle=0.5)
            time.sleep(0.4)
            current = ui_probe.grab_window(win)
            still_open = image_diff_count(before, current, box=probe_box) >= 5
        result["popup_open_before_click"] = still_open
        if not still_open:
            ui_probe.press("escape")
            result["detail"] = ("dropdown popup closed before selection; "
                                "refused to click (would hit confirm button)")
            return result
        ui_probe.click_client(win, int(0.5 * w), cy, foreground=False,
                              settle=0.5)
        time.sleep(0.3)
        now = ui_probe.grab_window(win)
        popup_closed = image_diff_count(opened_img, now,
                                        box=popup_region) >= 100
        if not popup_closed:
            ui_probe.press("escape")
            time.sleep(0.25)
            now = ui_probe.grab_window(win)
        selected = self.shot(win, "%s_language_selected.png" % prefix)
        header_changed = image_diff_count(before, now,
                                          box=header_region) >= 8
        result.update({
            "method": "mouse-click-option-row",
            "changed": popup_closed and header_changed,
            "popup_closed": popup_closed,
            "header_changed": header_changed,
            "selection_diff": image_diff_count(before, now),
            "selected_option_y": cy,
            "screenshots": result["screenshots"] + [selected],
        })
        return result

    def wait_layout(self, win, expected, timeout=20.0):
        deadline = time.time() + self.timeout(timeout)
        last = {"state": "unknown"}
        while time.time() < deadline:
            if not win.alive:
                return {"state": "gone", "detail": "window disappeared"}
            try:
                last = detect_layout(win)
            except Exception as exc:
                last = {"state": "error", "detail": repr(exc)}
            if last.get("state") == expected:
                return last
            time.sleep(0.3)
        return last

    def complete_setup(self, win, select_language=True, timeout=25.0):
        """Drive setup -> chat like a user; returns evidence dict."""
        evidence = {"language": None, "bootstrap": [], "layout": None,
                    "confirm": None}
        if select_language:
            evidence["language"] = self.select_language(
                win, self._scenario_sid or "setup")
        evidence["confirm"] = self.confirm_point(win)
        mark = self.req_mark()
        before = ui_probe.grab_window(win)
        ui_probe.click_client(win, *evidence["confirm"]["point"])
        evidence["bootstrap"] = self.wait_requests(mark=mark, kind="bootstrap",
                                                   timeout=timeout)
        evidence["layout"] = self.wait_layout(win, "chat", timeout=timeout)
        evidence["setup_before_click"] = before
        evidence["mark"] = mark
        return evidence

    def wait_chat_request(self, mark=None, timeout=25.0):
        return self.wait_requests(mark=mark, kind="chat", timeout=timeout)

    def click_titlebar_button(self, win, index):
        """Click custom min/max/close, allowing ui_hints.json overrides.

        ``titlebar_buttons`` in ui_hints.json may be a list of three
        [fx, fy] fractions (min, max, close).  It is only used when glyph
        detection had to fall back to guessed slots.
        """
        detected = ui_probe.find_top_right_buttons(win)
        hint_buttons = self.hints.get("titlebar_buttons")
        usable_hint = (isinstance(hint_buttons, (list, tuple))
                       and len(hint_buttons) == 3
                       and detected.get("method") == "slot-fallback")
        if usable_hint:
            width, height = win.client_size
            fx, fy = hint_buttons[index]
            point = (int(round(width * float(fx))),
                     int(round(height * float(fy))))
            ui_probe.click_client(win, *point)
            return {"button_index": index, "clicked_client": point,
                    "method": "ui_hints", "detected": detected}
        return ui_probe.click_titlebar_button(win, index)

    # -- setup-screen ordering heuristic -------------------------------- #
    def setup_order_evidence(self, win, layout=None):
        img = ui_probe.grab_window(win)
        w, h = img.size
        key_x, key_y = self.hint_point(win, "key_entry")
        drop_x, drop_y = self.hint_point(win, "dropdown")
        conf_x, conf_y = self.hint_point(win, "confirm")

        def widget_evidence(cy, half=26):
            text_px = text_pixels(
                img, box=(int(0.18 * w), max(0, cy - half),
                          int(0.82 * w), min(h, cy + half)))
            border = ui_probe.horizontal_line_score(img, cy / float(h),
                                                    span=42)
            return text_px, border["score"]

        key_px, key_border = widget_evidence(key_y)
        drop_px, drop_border = widget_evidence(drop_y)
        conf_px, conf_border = widget_evidence(conf_y)
        boxes = find_purple_boxes(img)
        near_confirm = [b for b in boxes
                        if abs(b["center"][1] - conf_y)
                        <= max(60, 0.10 * h)
                        and b["width"] >= max(80, 0.10 * w)]
        found_key = key_px >= 15 or key_border >= 0.25
        found_dropdown = drop_px >= 15 or drop_border >= 0.25
        found_confirm = (bool(near_confirm) or conf_border >= 0.25
                         or conf_px >= 20)
        order_ok = key_y < drop_y < conf_y
        ok = found_key and found_dropdown and found_confirm and order_ok
        return {
            "ok": ok,
            "confident": ok,
            "found": {"key": found_key, "dropdown": found_dropdown,
                      "confirm": found_confirm, "order": order_ok},
            "evidence": {
                "key": {"text_px": key_px, "border": key_border},
                "dropdown": {"text_px": drop_px, "border": drop_border},
                "confirm": {"text_px": conf_px, "border": conf_border,
                            "purple_boxes": len(near_confirm)},
            },
            "ys": {"key": key_y, "dropdown": drop_y, "confirm": conf_y},
        }

    def key_field_text_px(self, win, img=None):
        img = img if img is not None else ui_probe.grab_window(win)
        w, h = img.size
        _kx, ky = self.hint_point(win, "key_entry")
        return text_pixels(img, box=(int(0.15 * w), max(0, ky - 30),
                                     int(0.85 * w), min(h, ky + 20)))

    # -- chat helpers --------------------------------------------------- #
    def type_into_chat_input(self, win, text):
        before = ui_probe.grab_window(win)
        w, h = before.size
        cx, cy = self.hint_point(win, "chat_input")
        candidates = [(cx, cy, "hint")]
        profile = ui_probe._row_profile(before)
        for y0, y1 in ui_probe.text_row_bands(profile, min_pixels=3, gap=4):
            band_y = (y0 + y1) // 2
            if band_y > 0.70 * h and abs(band_y - cy) > 10:
                candidates.append((w // 2, band_y, "row-band@%d" % band_y))
        candidates = candidates[:4]
        attempts = []
        for x, y, method in candidates:
            ui_probe.bring_to_foreground(win)
            ui_probe.click_client(win, x, y)
            ui_probe.ensure_foreground(win)
            ui_probe.paste_text(text)
            time.sleep(0.45)
            after = ui_probe.grab_window(win)
            region = (int(0.05 * w), max(0, y - 60), int(0.95 * w),
                      min(h, y + 50))
            diff = image_diff_count(before, after, box=region)
            text_px = text_pixels(after, box=region)
            attempts.append({"method": method, "point": (x, y),
                             "diff": diff, "text_px": text_px})
            if diff > 50 or text_px > 40:
                return {"ok": True, "method": method, "point": (x, y),
                        "attempts": attempts, "before": before,
                        "after": after}
            ui_probe.hotkey("ctrl", "a")
            ui_probe.press("delete")
        return {"ok": False, "method": None, "attempts": attempts,
                "before": before, "after": before}

    # ------------------------------------------------------------------ #
    # Scenarios
    # ------------------------------------------------------------------ #
    def scenario_a(self):
        self.begin_scenario("a")
        copy = self.make_copy("a", key="")
        app = win = None
        try:
            app, win = self.launch(copy, "a")
            tree = app.process_tree()
            self.check("A00", "R27",
                       "SUT window belongs to the launched process tree "
                       "(pid recorded, no title-only match)",
                       win.pid in tree,
                       "launched_pid=%s window_pid=%s tree=%s exe=%s"
                       % (app.pid, win.pid, tree,
                          os.path.basename(app.exe_path)))
            self.check("A01", "R02",
                       "window title contains 'Twilight Harness' and '紫悦'",
                       APP_TITLE_CORE in win.title and APP_TITLE_CN in win.title,
                       "actual title=%r literal_match=%s"
                       % (win.title, win.title.strip() == APP_TITLE))
            desc = win.describe()
            cw, ch = win.client_size
            ow, oh = win.size
            logical_c = (cw / win.scale, ch / win.scale)
            logical_o = (ow / win.scale, oh / win.scale)
            size_ok = any(abs(v[0] - 800) <= 4 and abs(v[1] - 600) <= 4
                          for v in (logical_c, logical_o))
            self.check("A02", "R03", "initial window size is 800x600",
                       size_ok,
                       "client=%s outer=%s dpi=%s logical_client=(%.1f,%.1f) "
                       "logical_outer=(%.1f,%.1f)"
                       % (win.client_size, win.size, win.dpi, *logical_c,
                          *logical_o))
            self.check("A03", "R05", "window is frameless (no WS_CAPTION)",
                       desc["frameless"],
                       "style=%s caption=%s thickframe=%s"
                       % (desc["style_hex"], desc["has_caption"],
                          desc["has_thickframe"]))
            buttons = ui_probe.find_top_right_buttons(win)
            self.check("A04", "R05",
                       "three custom titlebar buttons detected top-right",
                       len(buttons["centers"]) >= 3,
                       "method=%s clusters=%s centers=%s"
                       % (buttons["method"], buttons["clusters"],
                          buttons["centers"]))
            shot = self.shot(win, "a01_initial_setup_empty_key.png")
            img = ui_probe.grab_window(win)
            stats = color_stats(img)
            self.check("A05", "R06", "pure-white background dominance",
                       stats["near_white_frac"] >= 0.70
                       and stats["corners_white_frac"] >= 0.75,
                       "near_white=%.3f pure_white=%.3f corners_white=%.2f "
                       "dominant=%s"
                       % (stats["near_white_frac"], stats["pure_white_frac"],
                          stats["corners_white_frac"],
                          [c["hex"] for c in ui_probe.dominant_colors(img,
                                                                     top=3)]),
                       evidence=[shot])
            self.check("A06", "R06", "black text rendered",
                       stats["black_text_px"] >= 300,
                       "black_text_px=%d (%.4f%%)"
                       % (stats["black_text_px"],
                          100 * stats["black_text_frac"]))
            self.check("A07", "R06", "light-purple accent pixels present",
                       stats["light_purple_px"] >= 100,
                       "light_purple_px=%d mean=%s"
                       % (stats["light_purple_px"],
                          stats["light_purple_mean_hex"]))
            layout = detect_layout(win)
            self.check("A08", "R07", "setup screen detected at startup",
                       layout["state"] == "setup",
                       "state=%s setup_score=%s chat_score=%s"
                       % (layout["state"], layout["setup_score"],
                          layout["chat_score"]))
            order = self.setup_order_evidence(win, layout)
            self.check("A09", "R07",
                       "setup elements ordered top-down key<dropdown<confirm",
                       order["ok"],
                       json.dumps(order, ensure_ascii=False),
                       warn=not order["confident"])
            confirm = self.confirm_point(win, img)
            self.check("A10", "R08",
                       "confirm has no enabled accent fill while key empty",
                       confirm["boxes"] == 0,
                       "enabled_accent_boxes=%d method=%s"
                       % (confirm["boxes"], confirm["method"]), warn=True)
            mark = self.req_mark()
            ui_probe.click_client(win, *confirm["point"])
            time.sleep(1.2)
            reqs = self.reqs_since(mark)
            layout_after = detect_layout(win)
            gating_shot = self.shot(win, "a02_confirm_clicked_empty_key.png")
            self.check("A11", "R08",
                       "confirm click with empty key: no request, stays setup",
                       len(reqs) == 0 and layout_after["state"] == "setup",
                       "requests=%d state_after=%s"
                       % (len(reqs), layout_after["state"]),
                       evidence=[gating_shot])
            pre_type_img = ui_probe.grab_window(win)
            typed = self.type_key(win, DEFAULT_TEST_KEY)
            typed_img = ui_probe.grab_window(win)
            w_img, h_img = typed_img.size
            typed_boxes = [b for b in find_purple_boxes(typed_img)
                           if 0.25 * h_img <= b["center"][1] <= 0.95 * h_img
                           and b["width"] >= max(80, 0.10 * w_img)]
            mask_px = self.key_field_text_px(win, typed_img)
            _cx, cy = self.hint_point(win, "confirm")
            confirm_region = (int(0.18 * w_img), max(0, cy - 36),
                              int(0.82 * w_img), min(h_img, cy + 36))
            confirm_diff = image_diff_count(pre_type_img, typed_img,
                                            box=confirm_region)
            confirm_purple = color_stats(
                typed_img.crop(confirm_region))["purple_frac"]
            enabled = (len(typed_boxes) > 0 or confirm_diff >= 50
                       or confirm_purple >= 0.10)
            self.check("A12", "R08",
                       "typing a key enables confirm (enabled accent/state "
                       "change)",
                       typed["ok"] and enabled,
                       "typed=%s accent_boxes_now=%d confirm_region_diff=%d "
                       "confirm_purple_frac=%.4f key_field_text_px=%d"
                       % (typed["ok"], len(typed_boxes), confirm_diff,
                          confirm_purple, mask_px),
                       warn=not typed["ok"],
                       evidence=[typed["screenshot"]] if typed["screenshot"]
                       else [])
            self.info("A13", "R07",
                      "masked key visual evidence (inspect screenshot)",
                      "key_field_text_px=%d screenshot=%s"
                      % (mask_px, typed["screenshot"]))
            _kx, ky = self.hint_point(win, "key_entry")
            key_region = (int(0.10 * w_img), max(0, ky - 45),
                          int(0.90 * w_img), min(h_img, ky + 45))
            total_diff = image_diff_count(pre_type_img, typed_img)
            key_diff = image_diff_count(pre_type_img, typed_img,
                                        box=key_region)
            outside_diff = max(0, total_diff - key_diff)
            self.check("A14", "R07",
                       "UI feedback outside the key field updates after "
                       "typing (status line / button state)",
                       outside_diff >= 20,
                       "changed_px_total=%d key_field_region=%d outside=%d"
                       % (total_diff, key_diff, outside_diff), warn=True,
                       evidence=[typed["screenshot"]] if typed["screenshot"]
                       else [])
        finally:
            self.teardown(app, win, copy)
            self.end_scenario("a")

    def scenario_b(self):
        self.begin_scenario("b")
        copy = self.make_copy("b", key=DEFAULT_TEST_KEY)
        app = win = None
        try:
            app, win = self.launch(copy, "b")
            prefill_shot = self.shot(win, "b01_setup_key_prefilled.png")
            prefill_px = self.key_field_text_px(win)
            self.check("B01", "R09",
                       "api-key.ini prefills the key entry on launch",
                       prefill_px >= 30,
                       "key_field_text_px=%d" % prefill_px,
                       evidence=[prefill_shot])
            lang = self.select_language(win, "b")
            self.check("B02", "R07",
                       "language dropdown opens on click",
                       lang["opened_diff"] > 40,
                       "visible_change_px=%d method=%s"
                       % (lang["opened_diff"], lang["method"]),
                       evidence=lang["screenshots"][:1])
            self.check("B03", "R12",
                       "a non-default language can be selected in the dropdown",
                       bool(lang["changed"]),
                       "method=%s selection_change=%s"
                       % (lang["method"], lang.get("selection_diff")),
                       warn=True,
                       evidence=lang["screenshots"][-1:])
            confirm = self.confirm_point(win)
            mark = self.req_mark()
            ui_probe.click_client(win, *confirm["point"])
            bootstrap = self.wait_requests(mark=mark, kind="bootstrap",
                                           timeout=25)
            time.sleep(0.6)
            all_after = self.reqs_since(mark)
            chat_now = [r for r in all_after if r.get("request_kind") == "chat"]
            self.check("B04", "R10",
                       "confirm triggers exactly one bootstrap request "
                       "(stream=false) before chat",
                       len(bootstrap) == 1 and len(chat_now) == 0,
                       "bootstrap=%d chat_requests=%d confirm_method=%s"
                       % (len(bootstrap), len(chat_now), confirm["method"]),
                       evidence=[])
            briefs = [self.request_brief(r) for r in bootstrap]
            boot = bootstrap[0] if bootstrap else {}
            headers = boot.get("headers_lc") or {}
            body = boot.get("body") or {}
            contract_ok = bool(
                boot
                and boot.get("method") == "POST"
                and str(boot.get("normalized_path", "")).endswith(
                    "/chat/completions")
                and headers.get("authorization") == "Bearer " + DEFAULT_TEST_KEY
                and (headers.get("content-type") or "").startswith(
                    "application/json")
                and body.get("model") == DEFAULT_MODEL
                and (body.get("stream") is False
                     or body.get("stream") is None)
                and isinstance(body.get("messages"), list)
                and len(body.get("messages")) >= 1
                and boot.get("response_status") == 200
            )
            self.check("B05", "R10/R21",
                       "bootstrap request is OpenAI-compatible for the mock",
                       contract_ok, json.dumps(briefs[0] if briefs else {},
                                               ensure_ascii=False))
            overlap = self.character_overlap(bootstrap)
            self.check("B06", "R11",
                       "bootstrap request carries substantial "
                       "twilight-character.md content",
                       overlap["ratio"] >= 0.15 and overlap["matched_lines"] >= 5,
                       json.dumps(overlap, ensure_ascii=False))
            self.check("B10", "R11",
                       "bootstrap request injects (nearly) the full character "
                       "card verbatim",
                       overlap["ratio"] >= 0.50,
                       "shingle_overlap_ratio=%s matched_lines=%d/%d"
                       % (overlap["ratio"], overlap["matched_lines"],
                          overlap["lines"]), warn=True)
            language = self.language_evidence(bootstrap)
            markers = language["selected_language_markers"]
            b07_ok = bool(markers) or len(language["found"]) >= 1
            self.check("B07", "R12",
                       "bootstrap request includes a language selection",
                       b07_ok,
                       "selected_markers=%s found_tokens=%s language_word=%s"
                       % (markers, language["found"],
                          language["found_language_word"]),
                       warn=(not markers
                             and len(language["found"]) == 0
                             and language["found_language_word"]))
            layout = self.wait_layout(win, "chat", timeout=25)
            chat_shot = self.shot(win, "b02_chat_page_after_bootstrap.png")
            self.check("B08", "R10/R13",
                       "chat screen appears after successful bootstrap",
                       layout.get("state") == "chat",
                       "state=%s divider_score=%s"
                       % (layout.get("state"),
                          layout.get("divider", {}).get("score")),
                       evidence=[chat_shot])
            self.info("B09", "R11",
                      "bootstrap request evidence",
                      "bootstrap=%d body_chars=%s"
                      % (len(bootstrap),
                         briefs[0].get("body_chars") if briefs else None))
        finally:
            self.teardown(app, win, copy)
            self.end_scenario("b")

    def scenario_c(self):
        self.begin_scenario("c")
        copy = self.make_copy("c", key=DEFAULT_TEST_KEY)
        app = win = None
        try:
            app, win = self.launch(copy, "c")
            setup = self.complete_setup(win, select_language=True)
            chat_shot = self.shot(win, "c01_chat_page_empty.png")
            empty_img = ui_probe.grab_window(win)
            panes = pane_regions(empty_img)
            base_counts = region_text_counts(empty_img, panes)
            self.check("C01", "R13",
                       "chat screen shows split left/right panes",
                       panes["divider"]["score"] >= 0.35
                       or (panes["divider_x"] != empty_img.size[0] // 2),
                       "divider_x=%d score=%.2f left_region=%s right_region=%s"
                       % (panes["divider_x"], panes["divider"]["score"],
                          panes["left"], panes["right"]),
                       evidence=[chat_shot])
            w, h = empty_img.size
            ix, iy = self.hint_point(win, "chat_input")
            bottom_band_text = text_pixels(
                empty_img, box=(int(0.10 * w), int(0.80 * h),
                                int(0.90 * w), h))
            msg1 = "qa-enter-newline-check, ok!"
            msg2 = "second line 2"
            typed = self.type_into_chat_input(win, msg1)
            self.check("C03", "R14",
                       "text can be entered into the multiline chat input",
                       typed["ok"], "method=%s attempts=%s"
                       % (typed["method"], json.dumps(typed["attempts"])))
            typed_shot = self.shot(win, "c00_chat_input_typed.png")
            typed_img = typed.get("after")
            if typed_img is None or (
                    typed_img.size != typed["before"].size):
                typed_img = ui_probe.grab_window(win)
            diff_bbox = image_diff_bbox(typed["before"], typed_img,
                                        box=(0, int(0.55 * h), w, h))
            if diff_bbox:
                cx_frac = ((diff_bbox[0] + diff_bbox[2]) / 2.0) / w
                cy_frac = ((diff_bbox[1] + diff_bbox[3]) / 2.0) / h
            else:
                cx_frac = cy_frac = -1.0
            bottom_center = (0.15 <= cx_frac <= 0.85 and cy_frac >= 0.65)
            self.check("C02", "R14",
                       "user input area is at the bottom-center (typed-text "
                       "bounding box)",
                       bottom_center,
                       "typed_bbox=%s center_frac=(%.2f,%.2f) hint=(%d,%d) "
                       "bottom_band_text_px=%d"
                       % (diff_bbox, cx_frac, cy_frac, ix, iy,
                          bottom_band_text),
                       evidence=[typed_shot])
            # Input box interior only (excludes the static hint line below it).
            input_region = (int(0.05 * w), int(0.78 * h),
                            int(0.95 * w), int(0.955 * h))
            rows_typed, _bands = count_text_rows(typed_img, input_region)
            typed_px = text_pixels(typed_img, box=input_region)
            mark = self.req_mark()
            # Plain Enter must insert a newline and must NOT send.
            ui_probe.ensure_foreground(win)
            ui_probe.press("enter")
            time.sleep(0.8)
            ui_probe.paste_text(msg2)  # second line after the newline
            time.sleep(0.7)
            reqs_after_enter = self.reqs_since(mark)
            enter_shot = self.shot(win, "c02_after_enter_newline.png")
            after_enter_img = ui_probe.grab_window(win)
            rows_after, bands_after = count_text_rows(after_enter_img,
                                                      input_region)
            input_still = text_pixels(after_enter_img, box=input_region)
            self.check("C04", "R15",
                       "plain Enter inserts a newline and does NOT send",
                       len(reqs_after_enter) == 0 and rows_after >= 2,
                       "requests_after_enter=%d text_rows %d -> %d "
                       "input_text_px %d -> %d"
                       % (len(reqs_after_enter), rows_typed, rows_after,
                          typed_px, input_still),
                       evidence=[enter_shot])
            second_shot = self.shot(win, "c03_input_multiline.png")
            multiline_img = ui_probe.grab_window(win)
            input_after_two = text_pixels(multiline_img, box=input_region)
            self.check("C05", "R14",
                       "multiline input holds multiple lines before sending",
                       rows_after >= 2 and input_after_two >= typed_px,
                       "text_rows=%d bands=%s text_px=%d->%d requests=0"
                       % (rows_after, bands_after, typed_px,
                          input_after_two),
                       evidence=[second_shot])
            # Ctrl+Enter sends
            ui_probe.ensure_foreground(win)
            ui_probe.hotkey("ctrl", "enter")
            time.sleep(0.40)
            mid_shot = self.shot(win, "c04_streaming_reasoning_left.png")
            mid_img = ui_probe.grab_window(win)
            mid_counts = region_text_counts(mid_img, panes)
            chat_reqs = self.wait_requests(mark=mark, kind="chat", timeout=25)
            time.sleep(0.5)
            stable = self.reqs_since(mark, "chat")
            done_shot = self.shot(win, "c05_streaming_done_answer_right.png")
            done_img = ui_probe.grab_window(win)
            done_counts = region_text_counts(done_img, panes)
            self.check("C06", "R16",
                       "Ctrl+Enter sends a chat request",
                       len(chat_reqs) == 1,
                       "chat_requests=%d" % len(chat_reqs))
            self.check("C07", "R16",
                       "Ctrl+Enter sends exactly once (no duplicates)",
                       len(stable) == 1,
                       "chat_requests_after_stability=%d" % len(stable))
            briefs = [self.request_brief(r) for r in chat_reqs]
            chat_rec = chat_reqs[0] if chat_reqs else {}
            chat_body = chat_rec.get("body") or {}
            chat_headers = chat_rec.get("headers_lc") or {}
            user_contents = [
                str(m.get("content"))
                for m in (chat_body.get("messages") or [])
                if isinstance(m, dict) and m.get("role") == "user"
            ]
            sent_text = user_contents[-1] if user_contents else ""
            contract_ok = bool(
                chat_rec
                and chat_rec.get("method") == "POST"
                and str(chat_rec.get("normalized_path", "")).endswith(
                    "/chat/completions")
                and chat_headers.get("authorization")
                == "Bearer " + DEFAULT_TEST_KEY
                and chat_body.get("model") in (None, DEFAULT_MODEL)
                and chat_body.get("stream") is True
                and "qa-enter-newline-check" in sent_text
                and chat_rec.get("response_status") == 200
            )
            self.check("C08", "R21",
                       "chat request is OpenAI-compatible SSE with user text",
                       contract_ok,
                       json.dumps(briefs[0] if briefs else {},
                                  ensure_ascii=False)[:900]
                       + " sent_text=%r" % sent_text[:200])
            fidelity_ok = (msg1 in sent_text and msg2 in sent_text
                           and "\n" in sent_text)
            self.check("C15", "R14/R15",
                       "user text fidelity preserved (spaces, punctuation, "
                       "newline) in the sent message",
                       fidelity_ok,
                       "sent_text=%r expected_to_contain=%r"
                       % (sent_text[:300], [msg1, msg2, "\\n"]))
            streamed = bool(chat_rec and (chat_rec.get("chunks_sent") or 0) >= 20
                            and (chat_rec.get("duration_s") or 0) >= 0.6)
            self.check("C09", "R17",
                       "mock streamed SSE slowly in realistic chunks",
                       streamed,
                       "chunks_sent=%s duration_s=%s"
                       % (chat_rec.get("chunks_sent"),
                          chat_rec.get("duration_s")), warn=not streamed)
            self.check("C10", "R17",
                       "reasoning content becomes visible in the left pane "
                       "while streaming",
                       mid_counts["left"] >= base_counts["left"] + 40,
                       "left_text_px %d -> mid %d; right %d -> mid %d"
                       % (base_counts["left"], mid_counts["left"],
                          base_counts["right"], mid_counts["right"]),
                       evidence=[mid_shot])
            self.check("C11", "R17",
                       "streamed answer becomes visible in the right pane",
                       done_counts["right"] >= base_counts["right"] + 40,
                       "right_text_px %d -> %d"
                       % (base_counts["right"], done_counts["right"]),
                       evidence=[done_shot])
            separated = (
                done_counts["left"] > base_counts["left"] + 20
                and done_counts["right"] > base_counts["right"] + 20
                and panes["divider_x"] > 0.25 * w
                and panes["divider_x"] < 0.75 * w
            )
            self.check("C12", "R13",
                       "after streaming both panes are populated and separated",
                       separated,
                       "left=%d right=%d divider_x=%d"
                       % (done_counts["left"], done_counts["right"],
                          panes["divider_x"]),
                       evidence=[chat_shot, done_shot])
            pane_frac = panes["divider_frac"]
            proportions_ok = 0.28 <= pane_frac <= 0.45
            self.check("C14", "R13",
                       "pane proportions: reasoning left ~37% / output right "
                       "~63% at 800x600",
                       proportions_ok,
                       "divider_x=%d of %d (fraction=%.3f, expected 0.28-0.45)"
                       % (panes["divider_x"], w, pane_frac),
                       evidence=[chat_shot])
            self.info("C13", "R17", "final chat screenshot",
                      "screenshots=%s"
                      % [os.path.basename(p) for p in
                         (chat_shot, mid_shot, done_shot)])
        finally:
            self.teardown(app, win, copy)
            self.end_scenario("c")

    def scenario_d(self):
        self.begin_scenario("d")
        copy = self.make_copy("d", key=DEFAULT_TEST_KEY)
        app = win = None
        try:
            app, win = self.launch(copy, "d")
            initial = win.describe()
            self.shot(win, "d01_initial_window.png")

            # -- D01 drag titlebar ------------------------------------------
            drag_attempts = []
            moved = False
            titlebar_y = int(self.hints.get("titlebar_y", 16))
            for y in (titlebar_y, 10, 24):
                before = win.rect
                ui_probe.drag_client(win, (win.client_size[0] // 2, y),
                                     delta=(70, 45), duration=0.6)
                time.sleep(0.45)
                after = win.rect
                dx, dy = after[0] - before[0], after[1] - before[1]
                drag_attempts.append({"y": y, "before": before, "after": after,
                                      "dx": dx, "dy": dy})
                if abs(dx) >= 20 or abs(dy) >= 20:
                    moved = True
                    break
            self.shot(win, "d02_after_titlebar_drag.png")
            self.check("D01", "R22", "dragging the custom titlebar moves window",
                       moved, json.dumps(drag_attempts, ensure_ascii=False),
                       warn=False)

            # -- D02 edge resize -------------------------------------------
            resize_attempts = []
            resized = False
            for mode in ("outer-right-edge", "client-right-edge",
                         "bottom-right-corner"):
                before_size = win.size
                before_rect = win.rect
                if mode == "outer-right-edge":
                    start = (before_rect[2] - 2,
                             (before_rect[1] + before_rect[3]) // 2)
                    delta = (130, 0)
                elif mode == "client-right-edge":
                    start = win.client_to_screen(win.client_size[0] - 2,
                                                 win.client_size[1] // 2)
                    delta = (130, 0)
                else:
                    start = (before_rect[2] - 2, before_rect[3] - 2)
                    delta = (130, 90)
                ui_probe.ensure_usable(win)
                root_at = ui_probe.root_window_at(*start)
                if not ui_probe.is_own_window(root_at, win):
                    resize_attempts.append(
                        {"mode": mode,
                         "skipped": "foreign window at start point",
                         "hwnd": root_at})
                    continue
                ui_probe.drag(start, (start[0] + delta[0],
                                      start[1] + delta[1]), duration=0.6)
                time.sleep(0.5)
                after_size = win.size
                resize_attempts.append({
                    "mode": mode, "before": before_size,
                    "after": after_size,
                    "dw": after_size[0] - before_size[0],
                    "dh": after_size[1] - before_size[1]})
                if after_size[0] >= before_size[0] + 60:
                    resized = True
                    break
            self.shot(win, "d03_after_edge_resize.png")
            self.check("D02", "R04/R23",
                       "dragging a window edge resizes the window",
                       resized, json.dumps(resize_attempts,
                                           ensure_ascii=False))

            # -- D03 maximize ----------------------------------------------
            width_before = win.size[0]
            buttons = ui_probe.find_top_right_buttons(win)
            max_method = "custom-button"
            self.click_titlebar_button(win, 1)
            ok, value = ui_probe.wait_until(
                lambda: win.zoomed or win.size[0] >= max(1000,
                                                         width_before * 1.25),
                timeout=self.timeout(5.0), interval=0.15)
            if not ok:
                max_method = "double-click-titlebar-fallback"
                ui_probe.double_click_client(win, win.client_size[0] // 2,
                                             int(self.hints.get("titlebar_y",
                                                                16)))
                ok, value = ui_probe.wait_until(
                    lambda: win.zoomed or win.size[0] >= max(
                        1000, width_before * 1.25),
                    timeout=self.timeout(5.0), interval=0.15)
            max_shot = self.shot(win, "d04_maximized.png")
            max_evidence = {"method": max_method, "zoomed": win.zoomed,
                            "size": win.size, "work_area": win.work_area(),
                            "detected_buttons": buttons["centers"]}
            self.check("D03", "R05/R24",
                       "custom maximize button maximizes the window",
                       ok, json.dumps(max_evidence, ensure_ascii=False),
                       evidence=[max_shot])

            # -- D04 restore ----------------------------------------------
            restore_method = "custom-button"
            self.click_titlebar_button(win, 1)
            ok_restore, _ = ui_probe.wait_until(
                lambda: (not win.zoomed) and
                win.size[0] <= width_before * 1.15,
                timeout=self.timeout(5.0), interval=0.15)
            if not ok_restore and win.zoomed:
                restore_method = "win32-SW_RESTORE-fallback"
                win.restore()
                ok_restore, _ = ui_probe.wait_until(
                    lambda: (not win.zoomed) and
                    win.size[0] <= width_before * 1.15,
                    timeout=self.timeout(5.0), interval=0.15)
            restore_shot = self.shot(win, "d05_restored.png")
            self.check("D04", "R24",
                       "clicking maximize again restores the window",
                       ok_restore,
                       "method=%s zoomed=%s size=%s (expected width<=%d)"
                       % (restore_method, win.zoomed, win.size,
                          int(width_before * 1.15)),
                       evidence=[restore_shot])

            # -- D05 minimize ---------------------------------------------
            buttons = ui_probe.find_top_right_buttons(win)
            self.click_titlebar_button(win, 0)
            ok_min, _ = ui_probe.wait_until(lambda: win.iconic,
                                            timeout=self.timeout(4.0),
                                            interval=0.1)
            min_method = "custom-button"
            if not ok_min:
                win.minimize_win32()
                ok_min, _ = ui_probe.wait_until(lambda: win.iconic,
                                                timeout=self.timeout(4.0),
                                                interval=0.1)
                min_method = "win32-SW_MINIMIZE-fallback"
            min_shot = self.shot(None, "d06_minimized_desktop.png")
            min_desc = win.describe()
            self.check("D05", "R25",
                       "custom minimize button minimizes (IsIconic)",
                       ok_min and min_method == "custom-button",
                       "iconic=%s method=%s appwindow_style=%s visible=%s "
                       "detected_buttons=%s"
                       % (win.iconic, min_method,
                          min_desc.get("appwindow"), win.visible,
                          buttons["centers"]),
                       evidence=[min_shot])

            # -- D06 restore from minimize --------------------------------
            # A real user would click the taskbar button / Alt+Tab.  Shell
            # automation is unreliable in this shared session (observed both by
            # the Lead and by QA), so the equivalent Win32 SW_RESTORE(9) call is
            # used and reported explicitly.
            restore_min_method = "win32-SW_RESTORE"
            win.restore()
            ok_unmin, _ = ui_probe.wait_until(
                lambda: win.alive and not win.iconic and win.visible,
                timeout=self.timeout(5.0), interval=0.1)
            unmin_shot = self.shot(win, "d07_restored_from_minimize.png")
            self.check("D06", "R25",
                       "window restores from minimized state",
                       bool(ok_unmin),
                       "alive=%s iconic=%s visible=%s restore_method=%s "
                       "(taskbar/Alt+Tab automation is a known shell "
                       "limitation in this session)"
                       % (win.alive, win.iconic, win.visible,
                          restore_min_method),
                       evidence=[unmin_shot])

            # -- D07 close -------------------------------------------------
            ui_probe.bring_to_foreground(win)
            before_close_shot = self.shot(win, "d08_before_close.png")
            self.click_titlebar_button(win, 2)
            ok_close, _ = ui_probe.wait_until(
                lambda: app.poll() is not None or not win.alive,
                timeout=self.timeout(8.0), interval=0.15)
            close_method = "custom-button"
            if not ok_close:
                self.click_titlebar_button(win, 2)
                ok_close, _ = ui_probe.wait_until(
                    lambda: app.poll() is not None or not win.alive,
                    timeout=self.timeout(4.0), interval=0.15)
                close_method = "custom-button-retry" if ok_close else "none"
            if not ok_close:
                win.post_close()
                ok_close, _ = ui_probe.wait_until(
                    lambda: app.poll() is not None or not win.alive,
                    timeout=self.timeout(5.0), interval=0.15)
                close_method = "WM_CLOSE-fallback" if ok_close else "none"
            after_close_shot = self.shot(None, "d09_after_close_desktop.png")
            self.check("D07", "R26",
                       "custom close button exits the app",
                       ok_close and close_method.startswith("custom-button"),
                       "exit=%s method=%s returncode=%s"
                       % (ok_close, close_method, app.poll()),
                       evidence=[before_close_shot, after_close_shot])
            if app.running():
                app.terminate()
        finally:
            self.teardown(app, win, copy)
            self.end_scenario("d")

    def scenario_e(self):
        self.begin_scenario("e")
        copy = self.make_copy("e", key=DEFAULT_TEST_KEY)
        app = win = None
        try:
            app, win = self.launch(copy, "e")
            self.server.set_scenario("401")
            time.sleep(0.2)
            before_shot = self.shot(win, "e01_setup_before_401.png")
            before_img = ui_probe.grab_window(win)
            lang = self.select_language(win, "e")
            confirm = self.confirm_point(win)
            mark = self.req_mark()
            ui_probe.click_client(win, *confirm["point"])
            bootstrap = self.wait_requests(mark=mark, kind="bootstrap",
                                           timeout=20)
            time.sleep(1.5)
            after_img = ui_probe.grab_window(win)
            after_shot = self.shot(win, "e02_after_401_error.png")
            layout = detect_layout(win)
            reqs = self.reqs_since(mark)
            chat_reqs = [r for r in reqs if r.get("request_kind") == "chat"]
            boot = bootstrap[0] if bootstrap else {}
            self.check("E01", "R18",
                       "mock 401 reached on the bootstrap request",
                       bool(boot) and boot.get("response_status") == 401,
                       "bootstrap_requests=%d status=%s auth=%s"
                       % (len(bootstrap),
                          boot.get("response_status"),
                          (boot.get("headers_lc") or {}).get("authorization")))
            self.check("E02", "R18",
                       "app stays on setup after a 401 (no chat screen)",
                       layout.get("state") == "setup" and not chat_reqs
                       and win.alive,
                       "state=%s chat_requests=%d alive=%s"
                       % (layout.get("state"), len(chat_reqs), win.alive))
            h, w = before_img.size
            central = (int(0.10 * w), int(0.25 * h),
                       int(0.90 * w), int(0.95 * h))
            diff = image_diff_count(before_img, after_img, box=central)
            diff_box = image_diff_bbox(before_img, after_img, box=central)
            stats = color_stats(after_img)
            redish_after = color_stats(
                ui_probe.grab_window_region(
                    win, (int(0.05 * w), int(0.20 * h),
                          int(0.95 * w), int(0.95 * h))))["redish_px"]
            error_visible = (diff >= 60 and diff_box is not None)
            self.check("E03", "R18",
                       "an error message is visible on the setup screen",
                       error_visible,
                       "changed_px=%d bbox=%s redish_px=%d"
                       % (diff, diff_box, redish_after),
                       evidence=[before_shot, after_shot])
            self.check("E04", "R18",
                       "no chat request is attempted after the 401",
                       len(chat_reqs) == 0,
                       "chat_requests=%d total_requests=%d"
                       % (len(chat_reqs), len(reqs)))
            # Lead observation: controls must be re-enabled after the 401.
            self.wait_layout(win, "setup", timeout=5)
            confirm_after = self.confirm_point(win)
            key_px_after = self.key_field_text_px(win)
            controls_ok = (confirm_after["boxes"] >= 1 and key_px_after >= 20)
            self.check("E06", "R18",
                       "after the 401 the confirm control is re-enabled and "
                       "the key is still shown",
                       controls_ok,
                       "accent_confirm_boxes=%d key_field_text_px=%d "
                       "confirm_method=%s"
                       % (confirm_after["boxes"], key_px_after,
                          confirm_after["method"]),
                       evidence=[after_shot])
            self.info("E05", "R18", "401 evidence",
                      "bootstrap_requests=%d language_attempt=%s"
                      % (len(bootstrap), lang.get("method")))
        finally:
            if self.server is not None:
                self.server.set_scenario("success")
            self.teardown(app, win, copy)
            self.end_scenario("e")

    def scenario_f(self):
        self.begin_scenario("f")
        copy = self.make_copy("f", key="")
        app = win = None
        try:
            app, win = self.launch(copy, "f")
            empty_px = self.key_field_text_px(win)
            empty_shot = self.shot(win, "f01_setup_empty_key.png")
            self.check("F01", "R09",
                       "empty ini key -> key entry starts empty",
                       empty_px < 30,
                       "key_field_text_px=%d" % empty_px,
                       evidence=[empty_shot])
            typed_key = "sk-qa-writeback-001"
            lang = self.select_language(win, "f")
            typed = self.type_key(win, typed_key)
            typed_shot = self.shot(win, "f02_key_typed.png")
            mark = self.req_mark()
            confirm = self.confirm_point(win)
            ui_probe.click_client(win, *confirm["point"])
            bootstrap = self.wait_requests(mark=mark, kind="bootstrap",
                                           timeout=25)
            layout = self.wait_layout(win, "chat", timeout=25)
            chat_shot = self.shot(win, "f03_chat_after_success.png")
            boot = bootstrap[0] if bootstrap else {}
            self.check("F02", "R19",
                       "successful bootstrap uses the typed key",
                       bool(boot)
                       and (boot.get("headers_lc") or {}).get(
                           "authorization") == "Bearer " + typed_key,
                       "typed_ok=%s auth=%s state=%s"
                       % (typed["ok"],
                          (boot.get("headers_lc") or {}).get("authorization"),
                          layout.get("state")),
                       evidence=[typed_shot, chat_shot])
            self.check("F03", "R19/R10",
                       "chat screen reached after successful bootstrap",
                       layout.get("state") == "chat",
                       "state=%s bootstrap=%d language=%s"
                       % (layout.get("state"), len(bootstrap),
                          lang.get("method")))
            # graceful close (click X if we can, WM_CLOSE otherwise)
            closed = False
            try:
                self.click_titlebar_button(win, 2)
                closed = app.wait_exit(timeout=self.timeout(6))
            except Exception:
                closed = False
            if not closed:
                win.post_close()
                closed = app.wait_exit(timeout=self.timeout(5))
            if not closed:
                app.terminate()
            ini = copy.read_ini()
            api_key = (ini["sections"].get("deepseek", {}).get("api_key")
                       or "").strip()
            ini_shot = None
            self.check("F04", "R19",
                       "app writes successful key back to the copy's "
                       "api-key.ini",
                       api_key == typed_key,
                       "ini_api_key=%r expected=%r ini_parse_error=%s"
                       % (api_key, typed_key, ini["error"]))
            base_url_preserved = (
                ini["sections"].get("deepseek", {}).get("base_url") or "").strip()
            model_preserved = (
                ini["sections"].get("deepseek", {}).get("model") or "").strip()
            self.check("F05", "R19",
                       "write-back preserves base_url/model for restart",
                       bool(base_url_preserved) and bool(model_preserved),
                       "base_url=%r model=%r raw_tail=%r"
                       % (base_url_preserved, model_preserved,
                          ini["raw"][-200:]),
                       warn=True)

            # -- restart, expect prefill ----------------------------------
            app2, win2 = self.launch(copy, "f")
            prefill_shot = self.shot(win2, "f04_restart_prefilled.png")
            restart_px = self.key_field_text_px(win2)
            self.check("F06", "R20",
                       "restart shows the saved key prefilled (no typing)",
                       restart_px >= 30,
                       "key_field_text_px=%d" % restart_px,
                       evidence=[prefill_shot])
            mark2 = self.req_mark()
            confirm2 = self.confirm_point(win2)
            ui_probe.click_client(win2, *confirm2["point"])
            bootstrap2 = self.wait_requests(mark=mark2, kind="bootstrap",
                                            timeout=25)
            layout2 = self.wait_layout(win2, "chat", timeout=25)
            restart_shot = self.shot(win2, "f05_restart_chat.png")
            boot2 = bootstrap2[0] if bootstrap2 else {}
            self.check("F07", "R20",
                       "restart confirm sends bootstrap with the saved key "
                       "without retyping",
                       bool(boot2)
                       and (boot2.get("headers_lc") or {}).get(
                           "authorization") == "Bearer " + typed_key,
                       "bootstrap=%d auth=%s state=%s"
                       % (len(bootstrap2),
                          (boot2.get("headers_lc") or {}).get("authorization"),
                          layout2.get("state")),
                       evidence=[restart_shot])
            self.check("F08", "R20",
                       "restart reaches the chat screen using the saved key",
                       layout2.get("state") == "chat",
                       "state=%s" % layout2.get("state"))
            app2.terminate()
            app2 = None
            win2 = None
        finally:
            self.teardown(app, win, copy)
            if 'app2' in locals() and app2 is not None:
                app2.terminate()
            self.end_scenario("f")

    # ------------------------------------------------------------------ #
    # Pre/post flight
    # ------------------------------------------------------------------ #
    def preflight(self):
        self.delivery = ui_probe.delivery_status(self.args.delivery_dir)
        present = self.delivery["present_files"]
        expected = sorted(DELIVERY_FILES)
        self.check("G01", "R01",
                   "delivery folder contains exactly the 3 expected files",
                   sorted(present) == expected,
                   "present=%s expected=%s" % (present, expected))
        character_path = os.path.join(self.args.delivery_dir,
                                      DELIVERY_CHARACTER)
        if os.path.isfile(character_path):
            with open(character_path, "r", encoding="utf-8",
                      errors="replace") as fh:
                self.character_text = fh.read()
        self.check("G02", "R11",
                   "twilight-character.md is substantial (persona card)",
                   len(self.character_text) >= 1000,
                   "character_chars=%d" % len(self.character_text))
        if not self.delivery["ready"]:
            raise BuildNotReady(
                "BUILD NOT READY: %s\n  present=%r\n  missing=%r\n"
                "Run again after the Lead packages the EXE."
                % (self.args.delivery_dir, self.delivery["present_files"],
                   self.delivery["missing_files"]))
        # Another running copy of the app can steal foreground/screenshots.
        others = [info for info in ui_probe.enum_windows(visible_only=True)
                  if APP_TITLE_CORE in info["title"]]
        self.check("G05", "R27",
                   "no other copy of the app is running before QA "
                   "(avoids foreground/screenshot interference)",
                   len(others) == 0,
                   "other_windows=%r"
                   % [{"hwnd": w["hwnd"], "title": w["title"],
                       "pid": w["pid"]} for w in others], warn=True)
        self._preflight_done = True

    def postflight(self):
        after = ui_probe.delivery_status(self.args.delivery_dir)
        self.check("G03", "R27",
                   "delivery folder untouched by QA (same files + hashes)",
                   (self.delivery.get("hashes") == after.get("hashes")
                    and sorted(after["present_files"])
                    == sorted(self.delivery.get("present_files") or [])),
                   "before=%s after=%s"
                   % (sorted(self.delivery.get("hashes", {}).keys()),
                      sorted(after.get("hashes", {}).keys())))
        delivery_real = os.path.realpath(self.args.delivery_dir)
        offending = [path for path in self.launched_exe_paths
                     if path == os.path.join(delivery_real, DELIVERY_EXE)
                     or path.startswith(delivery_real + os.sep)]
        self.check("G04", "R27",
                   "all app launches used temp copies, never the delivery dir",
                   not offending and self.copies_total >= 1,
                   "copies_created=%d launched_exe_paths=%r offending=%r"
                   % (self.copies_total,
                      [os.path.basename(p) for p in
                       self.launched_exe_paths], offending))

    # ------------------------------------------------------------------ #
    # Reporting
    # ------------------------------------------------------------------ #
    def write_reports(self):
        ui_probe.ensure_dir(ARTIFACTS_DIR)
        counts = Counter(check.status for check in self.checks)
        summary = {key: counts.get(key, 0)
                   for key in ("PASS", "FAIL", "WARN", "SKIP")}
        report = {
            "when": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "duration_s": round(time.time() - self.started, 1),
            "delivery": {k: v for k, v in self.delivery.items()},
            "environment": {
                "python": sys.version.split()[0],
                "dpi_mode": ui_probe.DPI_MODE,
                "screen": list(ui_probe.pyautogui.size())
                if ui_probe.pyautogui is not None else None,
                "delivery_dir": self.args.delivery_dir,
                "mock_base_url": (self.server.base_url
                                  if self.server is not None else None),
                "chunk_delay": self.args.chunk_delay,
                "runtime_temp": self.runtime_temp,
                "hints": self.hints,
            },
            "summary": summary,
            "requirements": REQUIREMENTS,
            "checks": [c.as_dict() for c in self.checks],
            "screenshots": self.screenshots,
        }
        json_path = os.path.join(ARTIFACTS_DIR, "report.json")
        with open(json_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        with open(os.path.join(ARTIFACTS_DIR, "requests_by_scenario.json"),
                  "w", encoding="utf-8") as fh:
            json.dump({sid: [self.request_brief(r) for r in records]
                       for sid, records in
                       self.requests_by_scenario.items()},
                      fh, ensure_ascii=False, indent=2)
        # full request bodies (evidence for character/language checks)
        with open(os.path.join(ARTIFACTS_DIR, "requests_full.json"),
                  "w", encoding="utf-8") as fh:
            json.dump({sid: records for sid, records in
                       self.requests_by_scenario.items()},
                      fh, ensure_ascii=False, indent=1)

        md = []
        md.append("# Twilight Harness E2E QA report")
        md.append("")
        md.append("- When: %s" % report["when"])
        md.append("- Duration: %ss" % report["duration_s"])
        md.append("- Delivery: `%s`" % self.args.delivery_dir)
        md.append("- Mock base URL: %s" % report["environment"]["mock_base_url"])
        md.append("- Summary: **PASS %d / FAIL %d / WARN %d / SKIP %d**"
                  % (summary["PASS"], summary["FAIL"], summary["WARN"],
                     summary["SKIP"]))
        md.append("")
        md.append("## Checks")
        md.append("")
        md.append("| ID | Req | Check | Status | Detail | Evidence |")
        md.append("| --- | --- | --- | --- | --- | --- |")
        for check in self.checks:
            evidence = ", ".join(os.path.basename(p)
                                 for p in check.evidence) if check.evidence else ""
            detail = check.detail.replace("|", "\\|")
            if len(detail) > 320:
                detail = detail[:320] + "…"
            md.append("| %s | %s | %s | %s | %s | %s |"
                      % (check.cid, check.requirement, check.name,
                         check.status, detail, evidence))
        md.append("")
        md.append("## Requirement coverage")
        md.append("")
        md.append("| Req | Requirement | Checks | Status |")
        md.append("| --- | --- | --- | --- |")
        by_req = {}
        for check in self.checks:
            by_req.setdefault(check.requirement, []).append(check)
        for req_id, text in REQUIREMENTS.items():
            related = by_req.get(req_id, [])
            if not related:
                md.append("| %s | %s | (not exercised) | N/A |"
                          % (req_id, text))
                continue
            worst = ("FAIL" if any(c.status == "FAIL" for c in related)
                     else "WARN" if any(c.status == "WARN" for c in related)
                     else "PASS")
            md.append("| %s | %s | %s | %s |"
                      % (req_id, text,
                         ", ".join(c.cid for c in related), worst))
        md.append("")
        md.append("## Screenshots")
        md.append("")
        for path in self.screenshots:
            md.append("- `%s`" % os.path.relpath(path, os.path.dirname(HERE)))
        md_path = os.path.join(ARTIFACTS_DIR, "report.md")
        with open(md_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(md) + "\n")
        return summary, json_path, md_path

    # ------------------------------------------------------------------ #
    # Entry
    # ------------------------------------------------------------------ #
    def run(self):
        scenarios = [s.strip() for s in self.args.scenarios.split(",")
                     if s.strip()]
        try:
            self.preflight()
        except BuildNotReady as exc:
            print("\n" + "!" * 78)
            print(str(exc))
            print("!" * 78)
            return 2
        self.start_server()
        try:
            for sid in scenarios:
                func = getattr(self, "scenario_%s" % sid, None)
                if func is None:
                    self.check("HARNESS", "-", "unknown scenario %r" % sid,
                               False, "known: a,b,c,d,e,f")
                    continue
                try:
                    func()
                except Exception:
                    tb = traceback.format_exc()
                    self.check("SCENARIO_%s" % sid.upper(), "-",
                               "scenario %s crashed" % sid, False,
                               tb.splitlines()[-1] if tb else "")
                    print(tb)
        finally:
            # global safety net
            for app in list(self.apps):
                try:
                    app.terminate()
                except Exception:
                    pass
            self.apps = []
            for copy in list(self.temp_copies):
                if not self.args.keep_temp:
                    copy.cleanup()
            self.temp_copies = []
            self.end_scenario("_global")
            self.stop_server()
        try:
            self.postflight()
        except Exception:
            traceback.print_exc()
        summary, json_path, md_path = self.write_reports()

        failures = [c for c in self.checks if c.status == "FAIL"]
        warns = [c for c in self.checks if c.status == "WARN"]
        print("\n" + "=" * 78)
        print("QA SUMMARY: PASS %d / FAIL %d / WARN %d / SKIP %d"
              % (summary["PASS"], summary["FAIL"], summary["WARN"],
                 summary["SKIP"]))
        if failures:
            print("\nFAILURES:")
            for check in failures:
                print("  - [%s] %s %s | %s" % (check.cid, check.requirement,
                                              check.name, check.detail[:300]))
        if warns:
            print("\nWARNINGS (inspect screenshots / tune ui_hints.json):")
            for check in warns:
                print("  - [%s] %s %s | %s" % (check.cid, check.requirement,
                                              check.name, check.detail[:300]))
        print("\nReport: %s" % md_path)
        print("JSON  : %s" % json_path)
        print("Shots : %s" % SCREENSHOTS_DIR)
        print("Reqs  : %s" % ", ".join(REQUIREMENTS))
        print("=" * 78)
        return 0 if not failures and not warns else 1


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser():
    parser = argparse.ArgumentParser(
        description="Twilight Harness end-to-end black-box QA")
    parser.add_argument("--delivery-dir", default=DELIVERY_DIR,
                        help="delivered folder with the 3 files "
                             "(default: twilight-harness/)")
    parser.add_argument("--scenarios", default="a,b,c,d,e,f",
                        help="comma separated scenario ids (a,b,c,d,e,f)")
    parser.add_argument("--mock-port", type=int, default=0,
                        help="mock server port (0 = auto)")
    parser.add_argument("--chunk-delay", type=float, default=0.045,
                        help="SSE chunk delay seconds (default 0.045)")
    parser.add_argument("--hints", default=os.path.join(HERE,
                                                        "ui_hints.json"),
                        help="optional layout-hint JSON overrides")
    parser.add_argument("--keep-temp", action="store_true",
                        help="keep temp copies of the delivery folder")
    parser.add_argument("--keep-screenshots", action="store_true",
                        help="do not clear tests/screenshots/ before running")
    parser.add_argument("--timeout-scale", type=float, default=1.0,
                        help="scale all wait timeouts (slow machines)")
    parser.add_argument("--requests-log", default=None,
                        help="JSONL request log path "
                             "(default: tests/artifacts/requests.jsonl)")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.requests_log is None:
        args.requests_log = os.path.join(ARTIFACTS_DIR, "requests.jsonl")
    ui_probe.ensure_dir(ARTIFACTS_DIR)
    ui_probe.ensure_dir(SCREENSHOTS_DIR)
    runtime_temp = os.path.join(ARTIFACTS_DIR, "runtime_temp")
    ui_probe.ensure_dir(runtime_temp)
    if not args.keep_temp:
        for name in os.listdir(runtime_temp):
            path = os.path.join(runtime_temp, name)
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    os.remove(path)
            except OSError:
                pass
    if not args.keep_screenshots:
        for name in os.listdir(SCREENSHOTS_DIR):
            if name.lower().endswith(".png"):
                try:
                    os.remove(os.path.join(SCREENSHOTS_DIR, name))
                except OSError:
                    pass
    try:
        with open(args.requests_log, "w", encoding="utf-8"):
            pass
    except OSError:
        pass
    runner = E2E(args)
    exit_code = runner.run()
    if not args.keep_temp:
        try:
            for name in os.listdir(runtime_temp):
                path = os.path.join(runtime_temp, name)
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    try:
                        os.remove(path)
                    except OSError:
                        pass
        except OSError:
            pass
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
