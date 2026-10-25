#!/usr/bin/env python3
"""Self-test for the Twilight Harness QA harness itself.

Runs WITHOUT the real delivered EXE.  It proves the QA tooling is sound:

1. every test module compiles;
2. ``mock_deepseek.py`` speaks the OpenAI-compatible contract (non-stream,
   SSE stream with reasoning-then-content, path variants, 401, request log,
   request headers, streaming pacing) and its standalone CLI starts;
3. pixel-analysis helpers classify synthetic white/black/purple images;
4. ``ui_probe.py`` can launch a window, find it by title, measure rect/style,
   screenshot it, click widgets, drag it, resize it, maximize/minimize it and
   close it -- validated against the fixture ``_fake_window_app.py``.

Usage (always with the bundled Python; plain ``python`` on PATH is broken)::

    <bundled-python> tests/selftest_harness.py            # everything
    <bundled-python> tests/selftest_harness.py --skip-ui  # headless-ish parts

Exit code 0 = harness healthy; 1 = harness defect.
"""

from __future__ import annotations

import argparse
import json
import os
import py_compile
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import mock_deepseek  # noqa: E402
import ui_probe  # noqa: E402

SELFTEST_DIR = os.path.join(HERE, "artifacts", "selftest")
RESULTS = []


def record(check_id, name, ok, detail="", evidence=None, warn=False):
    status = "WARN" if warn and ok else ("PASS" if ok else "FAIL")
    RESULTS.append({
        "id": check_id,
        "name": name,
        "status": status,
        "detail": detail,
        "evidence": evidence or [],
    })
    print("[%s] %-42s %s" % (status, check_id + " " + name,
                             ("- " + str(detail)) if detail else ""))
    return ok


def check_compile():
    modules = ["mock_deepseek.py", "ui_probe.py", "run_e2e.py",
               "_fake_window_app.py", "_fake_contract_app.py",
               "dryrun_with_fake_app.py", "selftest_harness.py"]
    ok = True
    for name in modules:
        path = os.path.join(HERE, name)
        if not os.path.isfile(path):
            if name == "run_e2e.py":
                record("H01", "compile %s" % name, False,
                       "file missing (harness incomplete)")
                ok = False
                continue
            record("H01", "compile %s" % name, False, "file missing")
            ok = False
            continue
        try:
            py_compile.compile(path, doraise=True,
                               cfile=os.path.join(SELFTEST_DIR,
                                                  name + "c"))
        except py_compile.PyCompileError as exc:
            record("H01", "compile %s" % name, False, str(exc))
            ok = False
    if ok:
        record("H01", "compile all test modules", True,
               "mock_deepseek, ui_probe, run_e2e, fixture, selftest")
    return ok


def _post_json(url, payload, headers=None, timeout=20):
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer sk-test-qa"})
    if headers:
        request.add_header(*headers) if isinstance(headers, tuple) else None
    return urllib.request.urlopen(request, timeout=timeout)


def _parse_sse(text):
    events = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload == "[DONE]":
                events.append({"done": True})
            else:
                try:
                    events.append(json.loads(payload))
                except ValueError:
                    events.append({"unparsable": payload})
    return events


def check_mock_server():
    ok_all = True
    log_path = os.path.join(SELFTEST_DIR, "mock_requests.jsonl")
    if os.path.isfile(log_path):
        os.remove(log_path)

    server = mock_deepseek.MockServer(port=0, scenario="success",
                                      log_path=log_path,
                                      chunk_delay=0.02)
    server.start()
    base = server.base_url
    try:
        # -- health endpoint ------------------------------------------------
        with urllib.request.urlopen(base + "/health", timeout=5) as resp:
            health = json.loads(resp.read().decode("utf-8"))
        record("H02", "mock GET /health", health.get("ok") is True,
               "scenario=%s" % health.get("scenario"))

        # -- persona bootstrap (stream=false) -------------------------------
        mark = server.mark()
        with _post_json(base + "/chat/completions", {
                "model": "deepseek-reasoner",
                "messages": [{"role": "user", "content": "bootstrap"}],
                "stream": False}) as resp:
            bootstrap = json.loads(resp.read().decode("utf-8"))
        message = bootstrap["choices"][0]["message"]
        ok = bool(bootstrap.get("object") == "chat.completion"
                  and message.get("content")
                  and message.get("reasoning_content"))
        record("H03", "mock bootstrap JSON (stream=false)", ok,
               "content=%d chars, reasoning=%d chars"
               % (len(message.get("content") or ""),
                  len(message.get("reasoning_content") or "")))
        ok_all &= ok

        # -- SSE chat (stream=true) -----------------------------------------
        mark = server.mark()
        started = time.time()
        request = urllib.request.Request(
            base + "/v1/chat/completions",
            data=json.dumps({
                "model": "deepseek-reasoner",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            }).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer sk-test-qa"})
        with urllib.request.urlopen(request, timeout=30) as resp:
            content_type = resp.headers.get("Content-Type", "")
            raw = resp.read()
        elapsed = time.time() - started
        events = _parse_sse(raw.decode("utf-8"))
        deltas = [e for e in events if "choices" in e]
        reasoning_positions = []
        content_positions = []
        for index, event in enumerate(deltas):
            delta = event["choices"][0].get("delta", {})
            if delta.get("reasoning_content"):
                reasoning_positions.append(index)
            if delta.get("content"):
                content_positions.append(index)
        has_done = any(e.get("done") for e in events)
        ok = (content_type.startswith("text/event-stream")
              and reasoning_positions and content_positions
              and max(reasoning_positions) < min(content_positions)
              and has_done
              and elapsed >= 0.3)
        record("H04", "mock SSE reasoning->content + [DONE]", ok,
               "events=%d reasoning=%d content=%d done=%s elapsed=%.2fs"
               % (len(events), len(reasoning_positions),
                  len(content_positions), has_done, elapsed),
               warn=(not ok and elapsed < 0.3))
        ok_all &= ok

        # -- path variants ---------------------------------------------------
        path_ok = True
        for path in ("/chat/completions/", "/some/base/chat/completions",
                     "/v1/chat/completions"):
            try:
                with _post_json(base + path, {
                        "model": "deepseek-reasoner",
                        "messages": [{"role": "user", "content": "x"}],
                        "stream": False}) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                path_ok &= bool(payload.get("choices"))
            except Exception as exc:
                path_ok = False
                record("H05", "mock path variant %s" % path, False, str(exc))
        record("H05", "mock accepts path variants", path_ok,
               "/chat/completions/, /some/base/..., /v1/...")
        ok_all &= path_ok

        # -- request log ------------------------------------------------------
        # The SSE record is appended right after the stream closes (outside the
        # state lock, after the in-memory append) so poll both surfaces.
        def _log_line_count():
            try:
                with open(log_path, "rb") as fh:
                    return sum(1 for _ in fh)
            except OSError:
                return 0

        deadline = time.time() + 5
        while time.time() < deadline:
            if len(server.records) >= 5 and _log_line_count() >= 5:
                break
            time.sleep(0.05)
        records = server.records
        log_lines = []
        if os.path.isfile(log_path):
            with open(log_path, "r", encoding="utf-8") as fh:
                log_lines = [json.loads(line) for line in fh if line.strip()]
        last = records[-1] if records else {}
        auth = (last.get("headers_lc", {}).get("authorization")
                or last.get("headers", {}).get("Authorization"))
        ok = (len(log_lines) >= 5 and auth == "Bearer sk-test-qa"
              and last.get("body", {}).get("model") == "deepseek-reasoner")
        record("H06", "mock JSONL request log + headers", ok,
               "log_lines=%d auth=%r" % (len(log_lines), auth))
        ok_all &= ok

        # -- 401 scenario -----------------------------------------------------
        server.set_scenario("401")
        status = None
        try:
            with _post_json(base + "/chat/completions", {
                    "model": "deepseek-reasoner",
                    "messages": [{"role": "user", "content": "x"}],
                    "stream": False}) as resp:
                status = resp.status
        except urllib.error.HTTPError as exc:
            status = exc.code
            body = json.loads(exc.read().decode("utf-8"))
        ok = status == 401 and body.get("error", {}).get("code") == "invalid_api_key"
        record("H07", "mock 401 scenario", ok, "status=%r" % status)
        ok_all &= ok
        server.set_scenario("success")
    finally:
        server.stop()

    # -- standalone CLI -------------------------------------------------------
    cli_log = os.path.join(SELFTEST_DIR, "cli_requests.jsonl")
    cli_stdout = os.path.join(SELFTEST_DIR, "cli_stdout.log")
    ok = True
    detail = ""
    try:
        with open(cli_stdout, "wb") as out:
            proc = subprocess.Popen(
                [sys.executable, os.path.join(HERE, "mock_deepseek.py"),
                 "--port", "0", "--duration", "2",
                 "--log", cli_log, "--chunk-delay", "0.01"],
                stdout=out, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            proc.wait(timeout=30)
        with open(cli_stdout, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        ok = "MOCK_READY http://127.0.0.1:" in text and "MOCK_STOPPED" in text
        detail = [line for line in text.splitlines()
                  if line.startswith("MOCK_")][:2]
    except Exception as exc:
        ok = False
        detail = repr(exc)
    record("H08", "mock standalone CLI starts/stops", ok, detail)
    ok_all &= ok
    return ok_all


def check_pixel_analysis():
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (240, 120), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.rectangle((8, 8, 60, 20), fill=(0, 0, 0))            # black text-ish
    draw.rectangle((60, 50, 200, 90), fill=(179, 157, 219))   # light purple
    stats = ui_probe.color_stats(img)
    boxes = ui_probe.find_purple_boxes(img)
    ok = (stats["near_white_frac"] > 0.5
          and stats["black_text_px"] > 0
          and stats["light_purple_px"] > 0
          and len(boxes) >= 1
          and boxes[0]["width"] >= 100)
    record("H09", "pixel analysis (white/black/light-purple)", ok,
           "white=%.2f black_px=%d purple_px=%d boxes=%d"
           % (stats["near_white_frac"], stats["black_text_px"],
              stats["light_purple_px"], len(boxes)))
    return ok


def _wait_marker(marker_path, action, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        records = _read_marker(marker_path)
        if any(r.get("action") == action for r in records):
            return True, records
        time.sleep(0.1)
    return False, _read_marker(marker_path)


def _read_marker(path):
    records = []
    if not os.path.isfile(path):
        return records
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except ValueError:
                        pass
    except OSError:
        pass
    return records


def check_ui_probe():
    ok_all = True
    marker = os.path.join(SELFTEST_DIR, "fake_marker.jsonl")
    if os.path.isfile(marker):
        os.remove(marker)
    exe = os.path.join(HERE, "_fake_window_app.py")
    before_windows = ui_probe.snapshot_window_hwnds()
    proc = subprocess.Popen(
        [sys.executable, exe, marker],
        cwd=HERE,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    win = None
    passed = True
    try:
        win = ui_probe.find_app_window(title_contains=ui_probe.APP_TITLE_CORE,
                                       pid=proc.pid, timeout=15,
                                       exclude_hwnds=before_windows)
        ui_probe.bring_to_foreground(win)
        record("U01", "find window by title", win is not None,
               "title=%r class=%r pid=%s"
               % (win.title, win.class_name, win.pid))
        passed &= win is not None
        if win is None:
            return False

        # geometry / style
        desc = win.describe()
        size_ok = win.client_size == (800, 600) or win.size == (800, 600)
        record("U02", "window size 800x600", size_ok,
               "client=%s outer=%s" % (win.client_size, win.size))
        passed &= size_ok
        frame_ok = desc["frameless"]
        record("U03", "frameless style", frame_ok,
               "style=%s caption=%s" % (desc["style_hex"],
                                        desc["has_caption"]))
        passed &= frame_ok

        # screenshot + colour analysis
        shot = os.path.join(SELFTEST_DIR, "u04_fake_window.png")
        ui_probe.save_window_shot(win, shot)
        stats = ui_probe.color_stats(ui_probe.grab_window(win))
        ok = (os.path.isfile(shot) and stats["near_white_frac"] > 0.5
              and stats["black_text_px"] > 50 and stats["light_purple_px"] > 50)
        record("U04", "screenshot + colour analysis", ok,
               "white=%.2f black_px=%d purple_px=%d"
               % (stats["near_white_frac"], stats["black_text_px"],
                  stats["light_purple_px"]), evidence=[shot])
        passed &= ok

        # click the purple confirm button
        img = ui_probe.grab_window(win)
        boxes = [b for b in ui_probe.find_purple_boxes(img)
                 if b["center"][1] > 0.3 * img.size[1]]
        if boxes:
            target = boxes[0]
            ui_probe.click_client(win, *target["center"])
            ok, _records = _wait_marker(marker, "confirm", timeout=4)
        else:
            ok = False
        record("U05", "click purple button via pixel detection", ok,
               "boxes=%d" % len(boxes))
        passed &= ok

        # titlebar button detection + maximize/restore
        tb = ui_probe.find_top_right_buttons(win)
        record("U06", "detect custom titlebar buttons", True,
               "method=%s clusters=%s" % (tb["method"], len(tb["clusters"])))
        ui_probe.click_titlebar_button(win, 1)
        ok, _ = _wait_marker(marker, "max", timeout=4)
        time.sleep(0.5)
        grew = win.size[0] > 1200
        record("U07", "maximize click grows window", ok and grew,
               "size_after=%s" % (win.size,))
        passed &= ok and grew
        ui_probe.click_titlebar_button(win, 1)
        time.sleep(0.8)
        restored = abs(win.size[0] - 800) <= 40
        record("U08", "maximize click restores window", restored,
               "size_after=%s" % (win.size,))
        passed &= restored

        # drag titlebar
        before = win.rect
        ui_probe.drag_client(win, (400, 17), delta=(60, 40))
        time.sleep(0.4)
        after = win.rect
        moved = (abs(after[0] - before[0]) >= 20
                 or abs(after[1] - before[1]) >= 20)
        ok, _ = _wait_marker(marker, "drag_end", timeout=2)
        record("U09", "drag custom titlebar moves window", moved,
               "before=%s after=%s" % (before, after))
        passed &= moved

        # edge resize
        before_size = win.size
        ui_probe.drag_client(win, (win.client_size[0] - 2,
                                   win.client_size[1] // 2),
                             delta=(110, 0), duration=0.5)
        time.sleep(0.5)
        after_size = win.size
        resized = after_size[0] >= before_size[0] + 60
        record("U10", "drag right edge resizes window", resized,
               "before=%s after=%s" % (before_size, after_size))
        passed &= resized

        # Win32 minimize / restore
        win.minimize_win32()
        time.sleep(0.6)
        iconic = win.iconic
        ui_probe.save_screen(os.path.join(SELFTEST_DIR,
                                          "u11_minimized_desktop.png"))
        win.restore()
        time.sleep(0.6)
        restored_visible = win.alive and not win.iconic and win.visible
        record("U11", "Win32 minimize -> IsIconic -> restore", iconic and restored_visible,
               "iconic=%s restored=%s" % (iconic, restored_visible),
               warn=(not iconic))
        passed &= iconic

        # close via detected close button
        ui_probe.bring_to_foreground(win)
        ui_probe.click_titlebar_button(win, 2)
        try:
            proc.wait(timeout=6)
            closed = proc.poll() is not None
        except subprocess.TimeoutExpired:
            closed = False
        ok, _ = _wait_marker(marker, "close", timeout=1)
        record("U12", "close button exits window/process", closed and ok,
               "process_exit=%s marker_close=%s" % (closed, ok))
        passed &= closed
    except Exception:
        traceback.print_exc()
        record("U00", "ui_probe self-test crashed", False,
               traceback.format_exc(limit=2))
        passed = False
    finally:
        if proc.poll() is None:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
        if win is not None and win.alive:
            try:
                win.post_close()
            except Exception:
                pass
    return passed


def check_delivery_readiness():
    status = ui_probe.delivery_status()
    print("\n[INFO] delivery status: ready=%s present=%r missing=%r extra=%r"
          % (status["ready"], status["present_files"],
             status["missing_files"], status["extra_entries"]))
    print("[INFO] delivery hashes: %s" % json.dumps(status["hashes"],
                                                    indent=2))
    if not status["ready"]:
        print("[INFO] AI BuildNotReady -> run_e2e.py will exit 2 until the "
              "Lead packages the EXE.")
    return True


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="Self-test the QA harness")
    parser.add_argument("--skip-ui", action="store_true",
                        help="skip the window-automation checks")
    parser.add_argument("--skip-mock", action="store_true",
                        help="skip mock-server checks")
    args = parser.parse_args(argv)
    ui_probe.ensure_dir(SELFTEST_DIR)

    print("=" * 78)
    print("Twilight Harness QA harness self-test")
    print("python:", sys.version.split()[0])
    print("pyautogui:", "ok" if ui_probe.pyautogui is not None
          else ui_probe._PYAUTOGUI_IMPORT_ERROR)
    print("numpy:", ui_probe.np.__version__, "| dpi mode:", ui_probe.DPI_MODE)
    print("=" * 78)

    check_compile()
    if not args.skip_mock:
        try:
            check_mock_server()
        except Exception:
            traceback.print_exc()
            record("H00", "mock-server self-test crashed", False,
                   traceback.format_exc(limit=2))
    try:
        check_pixel_analysis()
    except Exception:
        traceback.print_exc()
        record("H00", "pixel self-test crashed", False,
               traceback.format_exc(limit=2))
    if not args.skip_ui:
        try:
            check_ui_probe()
        except Exception:
            traceback.print_exc()
            record("U00", "ui-probe self-test crashed", False,
                   traceback.format_exc(limit=2))
    check_delivery_readiness()

    failures = [r for r in RESULTS if r["status"] == "FAIL"]
    warns = [r for r in RESULTS if r["status"] == "WARN"]
    report_path = os.path.join(SELFTEST_DIR, "selftest_report.json")
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump({"when": time.strftime("%Y-%m-%dT%H:%M:%S"),
                   "results": RESULTS, "report": report_path},
                  fh, ensure_ascii=False, indent=2)
    print("-" * 78)
    print("SELFTEST SUMMARY: %d PASS / %d WARN / %d FAIL (report: %s)"
          % (len(RESULTS) - len(failures) - len(warns), len(warns),
             len(failures), report_path))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
