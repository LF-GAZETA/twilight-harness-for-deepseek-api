# Twilight Harness — QA checklist & evidence map

Black-box QA for the final deliverable folder `twilight-harness/` containing exactly:

```text
twilight-harness/
├── api-key.ini
├── twilight-harness.exe
└── twilight-character.md
```

Owner: `qa-engineer`.  The suite **only writes under `tests/`** (plus OS temp dirs for
throw-away copies).  It never modifies or launches the delivery folder in place.

> **FINAL STATUS (2026-10-25): functional PASS** — all R01–R27 verified against the final build in
> completed per-scenario runs (a,b,c,d,e,f); 0 product defects.  The final *consolidated* rerun was
> blocked by an elevated third-party security overlay on the shared desktop (environment, not
> product).  Full report, evidence package and residual-risk notes: **`tests/FINAL_QA_REPORT.md`**
> and `tests/artifacts/final_evidence/MANIFEST.json`.

---

## 0. How to run

Always use the bundled Python — the plain `python` on PATH is a broken Windows Store stub:

```bat
"C:\Users\User\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe" tests\run_e2e.py
```

Common variants:

```bat
:: only some scenarios
... python.exe tests\run_e2e.py --scenarios a,b,c
:: slower machine (scale all wait timeouts), keep temp copies for debugging
... python.exe tests\run_e2e.py --timeout-scale 2 --keep-temp
:: harness self-check (mock server + pixel analysis + window automation fixture)
... python.exe tests\selftest_harness.py
:: harness self-check without touching the shared desktop
... python.exe tests\selftest_harness.py --skip-ui
```

Exit codes: `0` = all checks PASS · `1` = FAIL/WARN present (or harness crash) · `2` = build
not ready (`twilight-harness/` missing/incomplete).  The dry-run helper uses `3` when its
local PyInstaller fixture cannot be built.

Outputs:

| Path | Contents |
| --- | --- |
| `tests/artifacts/report.md` | human-readable pass/fail table + requirement coverage |
| `tests/artifacts/report.json` | machine-readable results, environment, delivery hashes |
| `tests/artifacts/requests.jsonl` | every mock-API request (JSON body + headers), appended live |
| `tests/artifacts/requests_by_scenario.json` | per-scenario request summaries |
| `tests/artifacts/requests_full.json` | per-scenario full request bodies (persona content evidence) |
| `tests/artifacts/<scenario>_*_stdout.log` / `_stderr.log` | captured app stdout/stderr per launch |
| `tests/screenshots/*.png` | descriptive screenshots listed in §5 |
| `tests/artifacts/selftest_report.json` | harness self-test results |

The run clears `tests/screenshots/*.png` and `tests/artifacts/requests.jsonl` first
(use `--keep-screenshots` to preserve the previous run).

---

## 1. Safety / isolation rules baked into the harness

| Rule | Implementation |
| --- | --- |
| Never touch the real delivery folder | `ui_probe.copy_delivery()` copies the 3 files to a fresh `%TEMP%\twilight-qa-*` dir; all ini editing / EXE launch / key write-back happens there. `E2E.make_copy()` hard-refuses a temp dir equal to or inside the delivery dir. |
| Delivery stays exactly 3 files and unchanged | preflight + postflight sha256 snapshot in `E2E.preflight()` / `postflight()` (checks `G01`, `G03`). |
| Interception only through the per-copy ini | each copy gets `[deepseek] api_key/base_url/model` with `base_url = http://127.0.0.1:<mock-port>`; no env vars, no private app hooks. |
| Never attach to someone else's window | `find_app_window(pid=launched_pid)` accepts windows only from the launched process tree (PyInstaller onefile child included, via Toolhelp32), plus a pre-launch hwnd exclusion snapshot. Hard ownership assertion after discovery (check `A00`). |
| Never click/type through another app | `click_client()`/`drag_client()` verify `WindowFromPoint` ownership and raise `ProbeError` instead of interacting; `grab_window()` refuses to capture an occluded window. Windows belonging to the SUT process (e.g. combobox popup) are allowed. |
| Shared-desktop interference | `G05` warns if another copy of `Twilight Harness · 紫悦` is already running; QA pins its own window with `HWND_TOPMOST` and re-foregrounds before every action/capture. |
| Cleanup | app killed by `taskkill /T` (PyInstaller tree) if still alive; temp copies removed unless `--keep-temp`; mock server stopped in `finally`. |
| PyInstaller-in-sandbox startup | QA launches the temp-copy EXE with child `TEMP`/`TMP`/`TMPDIR` pointed at `tests/artifacts/runtime_temp` (a workspace-writable dir). This is an agent-sandbox quirk only; a normal user double-click uses the default `%TEMP%`. |
| IME-proof text entry | The shared Windows session has a Chinese IME active, so synthetic keystrokes can get composed/mangled. QA enters key/message text with clipboard paste (`Ctrl+V`, a normal user action) and verifies the **sent request** contains the exact text; Enter/Ctrl+Enter are still real key presses. |

**Coordination rule:** the E2E run must happen when no other copy of the app is being
driven by other teammates/agents (the shared desktop is otherwise a collision risk).
The Lead announces `BUILD READY`; QA then runs against a temp copy.

---

## 2. Test assets

| File | Purpose |
| --- | --- |
| `tests/mock_deepseek.py` | stdlib threaded OpenAI-compatible mock `/chat/completions`: `stream=false` persona bootstrap JSON, `stream=true` SSE (`reasoning_content` then `content`, ~45 ms/chunk, `[DONE]`), scenarios `success`/`401`/`500`, JSONL request log; also runs standalone (`--port --scenario --log --chunk-delay`). |
| `tests/ui_probe.py` | delivery status/hashes, temp copy + per-copy ini, EXE launch + strict-PID window discovery, Win32 rect/style/DPI/isIconic/isZoomed, foreground/topmost/occlusion guards, pyautogui click/type/drag helpers, window-region screenshots, white/black/purple pixel analysis, divider/pane detection, custom titlebar-button glyph detection. |
| `tests/run_e2e.py` | the actual suite: scenarios a–f, requirement catalogue R01–R27, evidence capture, `report.md`/`report.json`. |
| `tests/selftest_harness.py` | proves the harness itself is sound (mock contract, pixel analysis, window automation against a fixture) without the product EXE. |
| `tests/_fake_window_app.py` | fixture window for `selftest_harness.py` (test-only, not the product). |
| `tests/_fake_contract_app.py` + `tests/dryrun_with_fake_app.py` | test-only fake "contract app" packaged by PyInstaller into `tests/artifacts/fake_delivery/` to dry-run the whole suite before the real EXE exists. Never shipped, never reported as the product. |
| `tests/ui_hints.json` (optional) | layout-hint overrides (fractions of client area) for `key_entry`, `dropdown`, `confirm`, `chat_input`, `titlebar_y`, and optionally `titlebar_buttons` (three `[fx, fy]` pairs min/max/close), tuned from screenshots if needed. |

---

## 3. Requirement → automated check matrix

Requirement IDs are shared by the code (`REQUIREMENTS` in `run_e2e.py`) and the report.

| ID | User requirement | Automated check(s) | Primary evidence |
| --- | --- | --- | --- |
| R01 | Delivery folder is exactly `api-key.ini`, `twilight-harness.exe`, `twilight-character.md` | `G01` | `report.json` delivery listing |
| R02 | Window title literally `Twilight Harness · 紫悦` | `A01` (contains both parts; exact-literal flag recorded) | `report.json`, `a01_initial_setup_empty_key.png` |
| R03 | Initial window 800×600 | `A02` (client **and** outer logical size via per-window DPI; ±4 px) | `report.json`, `a01_*.png` |
| R04 | Window resizable | `D02` (right-edge drag grows width), style flags in `A03` | `d03_after_edge_resize.png` |
| R05 | Frameless with custom top-right minimize/maximize/close | `A03` (no `WS_CAPTION`), `A04` (3 glyph clusters detected), `D03`/`D05`/`D07` buttons behave | `a01_*.png`, `d04_maximized.png`, `d06_minimized_desktop.png` |
| R06 | Pure white background, black text, light-purple accents | `A05` near-white fraction + corner whites; `A06` neutral dark text pixels; `A07` light-purple pixels + mean hex | `a01_*.png`, `report.json` colour stats |
| R07 | Setup screen top-down: masked centered key input, language dropdown, confirm button | `A08` setup screen detected, `A09` ordering key<dropdown<confirm (text/border/accent evidence), `A12` typing enables confirm, `A13` masked-key screenshot, `A14` UI feedback outside the field updates after entry | `a01_*.png`, `a03_key_typed_masked.png`, `b01_setup_key_prefilled.png` |
| R08 | Confirm disabled while key empty | `A10` no enabled accent fill while empty, `A11` click with empty key → 0 API requests, stays setup; `A12` typing enables confirm | `a02_confirm_clicked_empty_key.png`, `a03_key_typed_masked.png`, `requests.jsonl` |
| R09 | Per-copy `api-key.ini` prefills the key | `B01` masked text visible at launch; `F01` empty ini → empty entry; `F06` restart prefilled | `b01_setup_key_prefilled.png`, `f01_setup_empty_key.png`, `f04_restart_prefilled.png` |
| R10 | Confirm performs DeepSeek persona-bootstrap call before chat | `B04` exactly one `stream=false` bootstrap request and 0 chat requests before it; `B08` chat screen after bootstrap; `F03` chat reached | `requests_full.json`, `b02_chat_page_after_bootstrap.png` |
| R11 | Bootstrap request contains substantial `twilight-character.md` content | `G02` character file substantial, `B06` character-shingle overlap ≥15 % and ≥5 matching lines, `B10` near-full-card injection (≥50 % shingles) | `requests_full.json` (bootstrap body), `report.json` |
| R12 | Bootstrap request includes the selected language | `B02` dropdown opens, `B03` mouse-selecting a non-default option changes the field, `B07` selected-language marker extracted from the request (e.g. `繁體中文（臺灣用語）`) | `b_dropdown_open.png`, `b_language_selected.png`, `requests_full.json` |
| R13 | Chat screen: reasoning/thinking left, output right | `C01` split panes detected, `C12` both panes populated & separated, `C14` pane proportions left ≈37 % / right ≈63 % at 800×600; `B08` chat layout | `c01_chat_page_empty.png`, `c05_streaming_done_answer_right.png` |
| R14 | Multiline user input at bottom-center | `C02` typed-text bbox bottom-center, `C03` text accepted, `C05` multiple rendered lines before send, `C15` spaces/punctuation/newline preserved in the sent message | `c00_chat_input_typed.png`, `c03_input_multiline.png` |
| R15 | Plain Enter inserts newline, does not send | `C04` 0 chat requests after Enter and a second rendered text line appears, `C15` the sent message actually contains the `\n` | `c02_after_enter_newline.png`, `requests_full.json` |
| R16 | Ctrl+Enter sends exactly once | `C06` one chat request, `C07` still exactly one after stability wait | `c04_streaming_reasoning_left.png`, `requests.jsonl` |
| R17 | Streaming: `reasoning_content` first then `content` | `C09` SSE chunks ≥20 and stream spans ≥0.6 s (mock-side pacing), `C10` left pane fills during stream, `C11` right pane fills at end, `C12` both panes | `c04_streaming_reasoning_left.png`, `c05_streaming_done_answer_right.png`, `requests.jsonl` |
| R18 | HTTP 401 keeps user on setup with visible error | `E01` 401 recorded for bootstrap request, `E02` still setup + no chat request + app alive, `E03` error text/colour appears, `E04` no chat attempted, `E06` confirm re-enabled and key still shown | `e01_setup_before_401.png`, `e02_after_401_error.png`, `requests_by_scenario.json` |
| R19 | Successful key is written back to the copy's `api-key.ini` | `F04` ini `api_key` equals typed key after success, `F05` base_url/model preserved (needed for restart) | `requests.jsonl` (f), ini dump in `report.json` |
| R20 | Restart with successful copy shows prefilled key | `F06` masked text present without typing, `F07` bootstrap on restart uses saved Bearer key, `F08` chat reached | `f04_restart_prefilled.png`, `f05_restart_chat.png` |
| R21 | OpenAI-compatible chat request (path, Bearer, model/messages/stream SSE) | `B05` bootstrap contract, `C08` chat request contract (POST `*/chat/completions`, `Authorization: Bearer`, JSON, model, `stream:true`, user text) | `requests_full.json` |
| R22 | Dragging the custom titlebar moves the window | `D01` rect delta after drag (tries several titlebar heights) | `d02_after_titlebar_drag.png` |
| R23 | Dragging an edge resizes | `D02` (outer right edge, client right edge, bottom-right corner fallbacks) | `d03_after_edge_resize.png` |
| R24 | Maximize → restore | `D03` size grows to ≥1.25× (or `IsZoomed`), `D04` returns to pre-max width | `d04_maximized.png`, `d05_restored.png` |
| R25 | Minimize → `IsIconic` → restore | `D05` `IsIconic=True` after custom min button (taskbar `WS_EX_APPWINDOW` style recorded); `D06` restores via `SW_RESTORE` (documented taskbar/Alt+Tab automation limitation in this shared session) | `d06_minimized_desktop.png`, `d07_restored_from_minimize.png` |
| R26 | Close button exits the app | `D07` process exits after clicking custom close (fails if only WM_CLOSE fallback works) | `d08_before_close.png`, `d09_after_close_desktop.png` |
| R27 | QA never modifies the delivery folder; window ownership strict | `G03` hashes/listing unchanged, `G04` all launches from temp copies, `A00` window belongs to launched process tree, `G05` shared-desktop warning | `report.json` delivery hashes + process details |

### Extra / robustness checks
- `A00` process-tree ownership (never attach to another copy).
- `A14` UI feedback besides the key field updates after entry (status line / button state).
- `B10` near-full persona card is injected into the bootstrap request (not just a summary).
- `C14` pane proportions (left ≈37 %, right ≈63 % at 800×600).
- `C15` user text fidelity: spaces, punctuation and newline survive into the sent message.
- `E06` controls re-enabled after the 401 error.
- `G05` warns when another `Twilight Harness · 紫悦` window is already running; the run
  still refuses to interact with it.
- Harness self-test `H01–H09` (mock server contract, standalone CLI, pixel analysis) and
  `U01–U12` (window discovery/rect/style/screenshot/click/drag/resize/max/min/close against
  the fixture) — see `tests/artifacts/selftest/selftest_report.json`.

---

## 4. Scenario outlines (what a real user does)

| Scenario | Steps (all via mouse/keyboard, like a user) |
| --- | --- |
| **a — package & first launch** | copy 3 files to temp → write ini with **empty** key → launch EXE → wait window → record title/size/style/DPI → screenshot → colour analysis → verify setup layout → click confirm while empty (must do nothing) → enter key (masked after blur) → confirm must become enabled. |
| **b — setup bootstrap** | copy with ini key `sk-test-qa` → launch → verify masked prefill → open language dropdown (screenshot) → mouse-click a different option (screenshot) → click confirm → assert exactly one `stream=false` bootstrap request containing the full persona card + selected language → chat screen appears. |
| **c — chat & input semantics** | complete setup → screenshot empty chat → enter first line, press **Enter** (newline inserted, no request), add second line → assert two rendered lines, no request → press **Ctrl+Enter** → exactly one `stream=true` request whose user message preserves spaces/punctuation/newline → mid-stream screenshot (reasoning left) → final screenshot (answer right); pixel-detect both panes and the 37/63 split. |
| **d — window controls** | drag titlebar (rect moves) → drag right edge (width grows) → click maximize (grows to work area) → click again (restores) → click minimize (`IsIconic`) → restore → click close (process exits).  Every click is guarded by `WindowFromPoint` ownership. |
| **e — 401 error** | set mock scenario `401` → launch with prefilled key → select language → confirm → request returns 401 → assert **still setup**, error visible (red/diff evidence), no chat request, controls re-enabled with key still shown. |
| **f — write-back & restart** | copy with empty ini key → launch → paste key `sk-qa-writeback-001` → select language → confirm success → graceful close → assert ini now contains the key (base_url/model preserved) → relaunch same copy → key is prefilled without typing → confirm → bootstrap uses saved Bearer key → chat. |

---

## 5. Screenshot evidence index

| File | Scenario | Shows |
| --- | --- | --- |
| `a01_initial_setup_empty_key.png` | a | initial 800×600 frameless setup screen, white/black/accent colours |
| `a02_confirm_clicked_empty_key.png` | a | confirm click with empty key did nothing (still setup) |
| `a03_key_typed_masked.png` | a | key rendered masked (bullets) after blur, confirm enabled |
| `b01_setup_key_prefilled.png` | b | key prefilled from `api-key.ini` (masked) |
| `b_dropdown_open.png`, `b_language_selected.png` | b | language dropdown open / non-default option selected (mouse) |
| `b02_chat_page_after_bootstrap.png` | b | chat screen after persona bootstrap |
| `c01_chat_page_empty.png` | c | reasoning-left (37 %) / output-right (63 %) layout + bottom-center input |
| `c00_chat_input_typed.png` | c | first line entered in the bottom-center input |
| `c02_after_enter_newline.png` | c | Enter inserted a newline; second line added; no send |
| `c03_input_multiline.png` | c | multiline input before Ctrl+Enter |
| `c04_streaming_reasoning_left.png` | c | mid-stream reasoning text appearing in left pane |
| `c05_streaming_done_answer_right.png` | c | completed answer in right pane |
| `d01_initial_window.png` … `d09_after_close_desktop.png` | d | drag, resize, maximized, restored, minimized desktop, restored, before/after close |
| `e01_setup_before_401.png`, `e02_after_401_error.png` | e | error state on setup after HTTP 401 (controls re-enabled) |
| `f01_setup_empty_key.png`, `f02_key_typed.png`, `f03_chat_after_success.png` | f | write-back flow |
| `f04_restart_prefilled.png`, `f05_restart_chat.png` | f | restarted app with saved key prefilled and chat reached |

Screenshots are cropped to the SUT **client area** from the screen only after the harness
verifies the SUT window is foreground and not occluded (so another app can never leak into
evidence).

---

## 6. Mock API contract exercised

`tests/mock_deepseek.py` (threaded stdlib `http.server`):

- `POST */chat/completions` accepted (`/chat/completions`, `/v1/chat/completions`, any base
  prefix, trailing slashes) — so a Lead base_url that already ends in `/v1` or
  `/chat/completions` also works.
- `Authorization: Bearer <key>`, JSON body with `model`, `messages`, `stream`.
- `stream=false` → JSON `choices[0].message.content` **and** `reasoning_content`
  (bootstrap greeting + reasoning).
- `stream=true` → real SSE: role chunk, 14 `reasoning_content` chunks, 14 `content`
  chunks, finish chunk, `data: [DONE]`; chunked transfer, one chunk flushed every ~45 ms
  (configurable `--chunk-delay`) so streaming is observable.
- Scenarios: `success`, `401` (invalid-key error JSON + `WWW-Authenticate`), `500`.
- Every request appended to `tests/artifacts/requests.jsonl` as
  `{seq, time, method, path, headers, headers_lc, body, request_kind, scenario,
  response_status, response_kind, chunks_sent, duration_s}`.

---

## 7. Pass/fail policy

- **PASS** requires the concrete assertion to hold *and* evidence to be captured.
- **FAIL** always includes the measured values and the reproduction scenario (screenshots +
  request log sequence numbers in `report.md`).
- **WARN** = heuristic could not be confirmed visually/automatically (e.g. dropdown
  selection verification inconclusive, error text only detected via pixel diff).  WARNs are
  reviewed manually against the screenshots; if a WARN hides a genuine defect it is
  converted to FAIL and reported to the Lead with repro steps.
- Any request/response assertions are made against the recorded mock traffic (real
  network payload evidence), never against app source code.
- Zero FAIL and zero WARN required for sign-off.  Visual claims (masked input, light-purple
  accents, layout) are additionally verified by a human-style inspection of the captured
  PNGs during the QA run.

## 8. Known limitations / environment notes

- No OCR is available, so text presence/position is validated via pixel analysis plus the
  screenshot evidence itself; exact wording is validated where it must be (request bodies
  contain the typed text / language / persona content).
- **Host IME:** the shared Windows session has a Chinese IME active.  Synthetic keystrokes
  for ASCII text are intercepted by the IME composer (this is why earlier interim reports
  showed hyphen/space loss).  QA therefore pastes key/message text via the clipboard — a
  normal user action — and verifies the exact text in the outgoing request (`C15`).  The app
  itself preserves spaces, punctuation and newlines (proven by `C15`).  This is a QA
  environment workaround, not an app defect.
- **Taskbar restore:** clicking the taskbar to restore a minimized frameless window did not
  work under pyautogui for the Lead either (shell/automation limitation).  QA verifies
  `IsIconic=True` and restores with the equivalent `SW_RESTORE(9)`; this is reported
  explicitly in `D05`/`D06` details.
- Layout hints (`tests/ui_hints.json`) may be tuned from the first run's screenshots if the
  final visual layout differs from the agreed contract; hint changes are QA-side only and
  do not alter the app.
- The suite requires a real interactive Windows desktop session (screenshots + synthetic
  input).  Do not run it concurrently with other agents/teammates driving the same desktop.
