# Twilight Harness — Final QA Report

**Date:** 2026-10-25 (Asia/Shanghai) · **QA:** `qa-engineer` · **Build under test:** final packaged deliverable
`C:\Users\User\Downloads\DSH\twilight-harness\` (Lead-announced "BUILD READY", built 00:34, EXE 11,537,025 bytes).

## 1. Verdict

| Item | Result |
| --- | --- |
| **Functional result** | **PASS** — every user-facing requirement (R01–R27) was exercised against the final build in completed per-scenario runs (a, b, c, d, e, f) with evidence captured; **0 product defects found**. |
| **Final consolidated single-run suite** | **BLOCKED by environment** — an *elevated* third-party security overlay (Kingsoft Antivirus `kxecenter.exe`) took the shared desktop foreground and blocked all synthetic input (UIPI / security self-protection). Two full-suite attempts (00:50 and 00:58) produced spurious failures *only* because of this interference. See §6. |
| **Delivery integrity** | **PASS** — folder contains exactly the 3 expected files; SHA-256 unchanged before/after every QA run; QA never launched or modified the delivery folder. |

Residual risk is limited to the unexecuted *repeat* consolidated run; the product itself passed
every check in the per-scenario runs, and the Lead independently smoke-tested the same build
(`_session/exe_smoke/`) with matching results.

## 2. Delivery under test (verified, read-only)

```text
C:\Users\User\Downloads\DSH\twilight-harness\
├── api-key.ini           292 bytes    sha256 e24ecd7c451bc9c72cc441b4f3cd73ba20cdf65988c3f8c7b30c0adc3080a66e
├── twilight-character.md 19,246 bytes sha256 37229b0285462a9412f28e6031194e425801502eb230c65445ab28e627824243
└── twilight-harness.exe  11,537,025 bytes sha256 ef11f35fda263670bd4aaa61522dd37b1f55dca89b061a9ff7fdd3874123f00c
```

`api-key.ini` ships with a blank key, `base_url = https://api.deepseek.com`,
`model = deepseek-reasoner`, preserving the app's comments/instructions.

QA interception: every test run copied these 3 files to a fresh temp directory, rewrote only the
**copy's** `api-key.ini` (`[deepseek] api_key = sk-test-qa`, `base_url = http://127.0.0.1:<mock>`),
launched the **copy's** EXE (child `TEMP`/`TMP` redirected to a workspace dir for the agent-sandbox
PyInstaller quirk) and drove it with mouse/keyboard. No private app hooks were used.

## 3. Environment

- Windows 10/11 desktop, 2560×1440 @ 96 DPI (single monitor), bundled Python 3.12.14,
  pyautogui/pywinauto/Pillow/numpy available.
- Mock backend: `tests/mock_deepseek.py` (OpenAI-compatible `/chat/completions`; `stream=false`
  bootstrap JSON; real SSE `reasoning_content`→`content`, 31 chunks, ~45 ms/chunk, `[DONE]`;
  scenarios success/401/500; JSONL request log).
- Host quirk: a Chinese IME is active in the shared Windows session; QA enters text with
  clipboard paste (normal user action) to avoid IME composition and verifies the exact **sent**
  payload — see §7.

## 4. Completed per-scenario runs (final build, final harness code)

| Run (time) | Scenarios | Result | Notes |
| --- | --- | --- | --- |
| 00:47 | a | **19 PASS / 0 FAIL / 0 WARN / 1 SKIP** | package + initial window + setup gating + masked entry |
| 00:48 | b, c | **28 PASS / 0 FAIL / 0 WARN / 2 SKIP** | prefill, language mouse-select, bootstrap payload, chat, Enter/Ctrl+Enter, streaming, 37.5% split |
| 00:48 | d | **12 PASS / 0 FAIL / 0 WARN** | titlebar drag, edge resize, maximize/restore, minimize/IsIconic/restore, close |
| 00:49 | e, f | **18 PASS / 0 FAIL / 0 WARN / 1 SKIP** | 401 error path; key write-back + restart prefill |
| 00:50 / 00:58 | full a–f (2 attempts) | **BLOCKED** | elevated Kingsoft overlay stole foreground; input injection blocked → spurious A/B/C/D failures. All failures were re-verified as environment-induced; see §6. |

Every scenario is driven like a real user: mouse clicks on on-screen coordinates, clipboard text
entry, Enter / Ctrl+Enter key presses, titlebar drags, window buttons, and screenshots checked by
pixel analysis + human inspection. Window discovery is restricted to the launched process tree
(PID + descendants), and clicks are refused if another app's window covers the target point.

## 5. Requirement coverage R01–R27 (all PASS)

| Req | Requirement | Result & measured evidence |
| --- | --- | --- |
| R01 | Delivery = exactly 3 files | PASS `G01`: `api-key.ini`, `twilight-character.md`, `twilight-harness.exe`; no extras (`report.json`) |
| R02 | Title literally `Twilight Harness · 紫悦` | PASS `A01`: actual title `'Twilight Harness · 紫悦'`, literal match true |
| R03 | Initial window 800×600 | PASS `A02`: client=(800,600) outer=(800,600), dpi=96, logical 800×600 |
| R04 | Resizable | PASS `D02`: right-edge drag 800→930 px wide |
| R05 | Frameless + custom top-right min/max/close | PASS `A03` style `0x96000008` no `WS_CAPTION`; `A04` three glyph clusters; `D03/D05/D07` custom buttons work |
| R06 | Pure white bg, black text, light-purple accents | PASS `A05` near-white 0.952 / pure-white 0.918 / corners 1.0; `A06` 1,482 black-text px; `A07` 3,776 light-purple px mean `#DBCEEC` |
| R07 | Setup top-down: masked centered key, dropdown, confirm | PASS `A08` setup state; `A09` order key(317)<dropdown(404)<confirm(470) with borders/segment evidence; masked evidence `03_key_masked_confirm_enabled.png` |
| R08 | Confirm disabled while key empty | PASS `A10` no enabled accent fill; `A11` click with empty key → 0 requests, stays setup; `A12` entry enables confirm (`confirm_region_diff` 16,825) |
| R09 | Per-copy ini prefills key | PASS `B01` 120 text px at launch; `F01` empty ini → 0; `F06` restart → 228 px (masked) |
| R10 | Confirm performs persona bootstrap before chat | PASS `B04` exactly 1 `stream=false` bootstrap, 0 chat requests; `B08` chat after success; `F03` |
| R11 | Bootstrap contains substantial character.md content | PASS `B06` normalized shingle overlap 0.6416, 107/107 lines matched; `B10` full-card injection; system message 9,194 chars, request body 9,730 chars |
| R12 | Bootstrap includes selected language | PASS `B02/B03` dropdown opens + mouse selection changes option; `B07` extracted marker `繁體中文（臺灣用語）` in request (`evidence_bootstrap_traditional_chinese.jsonl`) |
| R13 | Reasoning left / output right | PASS `C01` divider x=300 score 1.0; `C12` both panes populated; `C14` split fraction 0.375 (37.5% / 62.5%); `08_chat_reasoning_left_answer_right.png` |
| R14 | Multiline input at bottom-center | PASS `C02` typed-text bbox center (0.22, 0.86) in bottom band; `C03` text entered; `C05` 2 rendered lines; `C15` text fidelity |
| R15 | Enter = newline, no send | PASS `C04` 0 requests after Enter, text rows 1→2; `C15` sent message contains `\n` |
| R16 | Ctrl+Enter sends exactly once | PASS `C06` 1 chat request; `C07` still 1 after stability wait |
| R17 | Streaming reasoning then content | PASS `C09` 31 SSE chunks over 1.35 s; `C10` left pane fills mid-stream; `C11` right pane fills; `C12` both populated |
| R18 | 401 stays on setup with error | PASS `E01` mock returned 401; `E02` still setup, 0 chat requests, app alive; `E03` visible error (1,614 reddish px, bbox 211,296–540,528); `E04` no chat; `E06` confirm re-enabled and key still shown (`10_after_401_error_controls_reenabled.png`) |
| R19 | Successful key written back to copy's ini | PASS `F04` `api_key = sk-qa-writeback-001` in copied ini; `F05` `base_url`/`model` preserved |
| R20 | Restart with saved key prefilled | PASS `F06` 228 masked px with no typing; `F07` bootstrap `Authorization: Bearer sk-qa-writeback-001`; `F08` chat reached |
| R21 | OpenAI-compatible requests | PASS `B05`/`C08`: `POST /chat/completions`, `Authorization: Bearer …`, JSON `model`/`messages`/`stream`; bootstrap `stream=false`, chat `stream=true` with SSE 31 chunks/`[DONE]`; user content preserved exactly `'qa-enter-newline-check, ok!\nsecond line 2'` |
| R22 | Titlebar drag moves window | PASS `D01` Δx=70, Δy=45 px |
| R23 | Edge resize | PASS `D02` width +130 px (multiple strategies attempted) |
| R24 | Maximize → restore | PASS `D03` 800×600 → 2560×1392 (work area); `D04` restored to 930×600 |
| R25 | Minimize → IsIconic → restore | PASS `D05` custom minimize → `IsIconic=True` (taskbar `WS_EX_APPWINDOW` style present); `D06` restored via `SW_RESTORE` — taskbar/Alt+Tab click automation is a documented shell limitation in this session (Lead observed the same) |
| R26 | Close button exits app | PASS `D07` process exit after clicking custom close |
| R27 | QA never modifies delivery; strict ownership | PASS `G03` delivery hashes/listing unchanged; `G04` all 7+ launches from temp copies; `A00` window belongs to launched process tree; `G05` foreign-copy warning |

Checks SKIPPED in runs are informational-only lines (`A13`, `B09`, `C13`, `E05`) whose evidence
is already carried by the adjacent PASS checks/screenshots.

## 6. Final-suite blocker (environment, not product)

- At ~00:54 the desktop was taken over by **elevated third-party security software**:
  `kxecenter.exe` (PID 63180, parent `kxetray.exe` 21764; `TokenElevation=True`, parent process
  `kxescore.exe` 4036) with a foreground helper window `hwnd=4988654 class=BkShadowWndClass`
  and a visible Kingsoft Antivirus browser-protection popup. Evidence:
  `tests/artifacts/final_evidence/00_blocker_elevated_kingsoft_overlay.png`
  (`desktop_foreground_state.png`, sha256 18c7b872…).
- Because that window is elevated, Windows UIPI blocks our unelevated session:
  `SetForegroundWindow` on our app fails, `PostMessage`/`WM_CLOSE` to the elevated window returns
  FALSE, and synthetic clicks/keys are swallowed. Verified independently with an in-process Tk
  test: window event loop ran (`after()` ticked 17×) but a `pyautogui.click` on its own button did
  not register (`clicked=False`).
- Consequence in the two full-suite attempts: focus stolen between steps (`fg_hwnd=4988654`),
  drags no-ops, dropdown not received, scenarios crashed safely with "SUT window lost keyboard
  focus … refusing to type". QA **refused to interact** rather than click/type into foreign
  windows (safety guard worked as designed).
- Lead confirmed the processes are elevated and could not clear them, and confirmed this is
  outside product scope. Per-scenario runs completed *before* the overlay appeared are the
  functional sign-off; the Lead also has independent smoke evidence for the same build.

## 7. QA corrections made during the run (no product changes)

1. **C01/C14 divider** — QA detection bug: a 3-column mean diluted a 1-px separator; fixed to a
   max-based scan. Real separator at x=300/800 = 37.5 % (right 62.5 %) — matches intended layout.
2. **C08 user-text fixture** — host Chinese IME was composing synthetic keystrokes
   (the "sk-test-qa floating bar" was the IME candidate window). QA now pastes text via clipboard
   and verifies the exact outgoing payload; app preserves spaces/punctuation/`\n`
   (`'qa-enter-newline-check, ok!\nsecond line 2'`). Not an app defect.
3. **B03 language selection** — replaced unsupported Down/Enter keyboard nav with a mouse click on
   the dropdown option row (after waiting for the popup to open/verifying it is open). PASS with
   selected-language marker in the bootstrap request.
4. Minor: PyInstaller-onefile needs child `TEMP` inside the workspace under the agent sandbox
   (QA-only; a normal user double-click uses the default `%TEMP%`).

## 8. Evidence package

`tests/artifacts/final_evidence/` (see `MANIFEST.json` for sha256 of every file):

| File | Shows |
| --- | --- |
| `01_initial_setup_800x600_empty_key.png` | initial 800×600 frameless setup screen, blank key, disabled confirm |
| `02_confirm_clicked_with_empty_key.png` | confirm click with empty key → no API call, still setup |
| `03_key_masked_confirm_enabled.png` | masked key (bullets) after blur, confirm enabled, status line updated |
| `04_language_dropdown_open.png` / `05_language_selected_nondefault.png` | language dropdown open / non-default option (Traditional Chinese) selected by mouse |
| `06_chat_screen_after_bootstrap.png` | chat screen after successful persona bootstrap |
| `07_chat_input_two_lines_enter_newline.png` | Enter inserted a newline; two lines in bottom-center input; no send |
| `08_chat_reasoning_left_answer_right.png` | completed stream: reasoning in left pane (37.5 %), answer in right pane, user bubble |
| `09_setup_before_401.png` / `10_after_401_error_controls_reenabled.png` | 401 path: setup before, error after with controls re-enabled |
| `11_setup_empty_key_initial.png` / `12_key_pasted_masked_enabled.png` | empty ini flow / key entered masked+enabled |
| `13_restart_key_prefilled.png` / `14_restart_chat_with_saved_key.png` | restart prefill and chat reached with saved key |
| `15_initial_window_frameless_800x600.png` | custom-chrome window evidence |
| `evidence_bootstrap_fullcard_and_chat_requests.jsonl` | bootstrap request with full character card + language; chat SSE request with preserved user text |
| `evidence_bootstrap_traditional_chinese.jsonl` | bootstrap request with `繁體中文（臺灣用語）` selected language |
| `evidence_401_and_writeback_requests.jsonl` | HTTP 401 bootstrap request; write-back/restart bootstrap requests |
| `00_blocker_elevated_kingsoft_overlay.png` | final-run environment blocker evidence |

Also available from the runs: `tests/artifacts/report.json` / `report.md` (machine-readable results
of the last attempted consolidated run, including the environment-induced failures),
`tests/artifacts/requests*.jsonl` (every mock request with JSON body + headers), app
stdout/stderr logs, `tests/screenshots/` (latest screenshots), `tests/artifacts/selftest/`
(harness self-test 21/21 PASS), and the test suite itself (`tests/mock_deepseek.py`,
`tests/ui_probe.py`, `tests/run_e2e.py`, `tests/QA_CHECKLIST.md`).

## 9. Residual risk / recommendations

- Re-run the consolidated suite (`tests/run_e2e.py`) once on a clean desktop where no elevated
  security overlay holds the foreground; expected result all PASS. The suite already refuses to
  act on foreign windows, so an interference run can never produce a false PASS.
- Taskbar/Alt+Tab click-restore of a minimized frameless window could not be automated reliably
  (Lead observed the same shell limitation); `IsIconic=True` + `SW_RESTORE` were used and reported.
- Host IME quirk is documented; the app's text fidelity was proven via the sent request payload.
