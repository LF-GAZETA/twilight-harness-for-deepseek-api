"""Twilight Harness native sidecar.

Local-only HTTP/SSE backend for the native C++ GUI.  It reuses the existing
Python character, chat and local-AI code rather than embedding CPython in C++.

Run:
    python sidecar/server.py --port 0 --token <random>

The process prints one handshake line:
    TWILIGHT_SIDECAR {"port": 12345, "token": "..."}
and writes sidecar/sidecar.json for launchers.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEBUG_BACKEND = os.environ.get("TWILIGHT_DEBUG_BACKEND", "0") in {"1", "true", "yes"}


def debug_log(message: str) -> None:
    if not DEBUG_BACKEND:
        return
    try:
        with (ROOT / "sidecar-debug.log").open("a", encoding="utf-8") as handle:
            handle.write(time.strftime("[%H:%M:%S] ") + message + chr(10))
    except OSError:
        pass
    print("[backend] " + message, flush=True)
APP = ROOT / "twilight-app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

import api_client  # noqa: E402
import derpi  # noqa: E402
import hardware_index  # noqa: E402
import gpu_scheduler  # noqa: E402
import hardware_accel  # noqa: E402
import web_search  # noqa: E402
import attachments  # noqa: E402
import feature_flags  # noqa: E402
import local_tools  # noqa: E402
import persona  # noqa: E402


try:
    _vendor = hardware_accel.vendors()
    if _vendor.get("nvidia"):
        _backend = "cuda"
    elif _vendor.get("amd"):
        _backend = "rocm" if _vendor.get("os") == "Linux" else "directml"
    elif _vendor.get("intel"):
        _backend = "xpu" if _vendor.get("os") == "Linux" else "directml"
    else:
        _backend = "cpu"
    os.environ.setdefault("TWILIGHT_BACKEND", _backend)
    try:
        import hardware_index
        if len(hardware_index.index().get("nvidia", [])) >= 2:
            os.environ.setdefault("TWILIGHT_TTS_PARALLEL", "1")
    except Exception:
        pass
    print(f"[accel] backend={os.environ['TWILIGHT_BACKEND']}", flush=True)
except Exception as _accel_exc:  # noqa: BLE001
    print(f"[accel] detection failed: {_accel_exc}", flush=True)


def ai_home() -> Path:
    raw = os.environ.get("TWILIGHT_AI_HOME", "").strip()
    return Path(raw) if raw else Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "TwilightHarness" / "ai"



def _port_open(port: int) -> bool:
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.4):
            return True
    except OSError:
        return False


def ensure_tts_api() -> None:
    if _port_open(9881) and _port_open(9883):
        return
    root = ai_home().parent
    python = Path(sys.executable)
    portable_python = root / "python" / "Scripts" / "python.exe"
    if portable_python.is_file():
        python = portable_python
    api_dir = root / "api" / "GPT-SoVITS"
    script = api_dir / "api_v2.py"
    if not script.is_file():
        raise RuntimeError("未找到 GPT-SoVITS api_v2.py")
    env = dict(os.environ)
    backend = os.environ.get("TWILIGHT_BACKEND", "").lower()
    if backend in {"directml", "dml"}:
        # GPT-SoVITS on Windows AMD/Intel: torch 2.4.1 CPU + DirectML package.
        accel = root / "accel" / "directml"
        torch241 = accel / "torch241"
        env["PYTHONPATH"] = (str(torch241) + os.pathsep + str(accel) + os.pathsep +
                             str(api_dir / "GPT_SoVITS") + os.pathsep + str(api_dir))
        env["CUDA_VISIBLE_DEVICES"] = ""
        env["TWILIGHT_TTS_DEVICE"] = "cpu"
        env["PATH"] = str(torch241 / "torch" / "lib") + os.pathsep + env.get("PATH", "")
    else:
        if os.environ.get("TWILIGHT_TTS_SHARD", "0") in {"1", "true", "yes"}:
            env["CUDA_VISIBLE_DEVICES"] = "0,1"
            env["TWILIGHT_TTS_SHARD"] = "1"
        else:
            env["CUDA_VISIBLE_DEVICES"] = "1"
        env["PYTHONPATH"] = str(api_dir / "GPT_SoVITS") + os.pathsep + str(api_dir)
    parallel = os.environ.get("TWILIGHT_TTS_PARALLEL", "0") in {"1", "true", "yes"}
    ports = [(9881, "cn"), (9883, "en")]
    if parallel:
        ports.append((9885, "cn"))
    for port, lang in ports:
        if _port_open(port):
            continue
        cfg_name = f"{lang}.yaml"
        if os.environ.get("TWILIGHT_BACKEND", "").lower() in {"directml", "dml"} and (ai_home() / "models" / "gpt_sovits" / "api" / f"{lang}_cpu.yaml").is_file():
            cfg_name = f"{lang}_cpu.yaml"
        cfg = ai_home() / "models" / "gpt_sovits" / "api" / cfg_name
        child_env = dict(env)
        child_env["CUDA_VISIBLE_DEVICES"] = "1" if port in (9881, 9883) else "0"
        subprocess.Popen(
            [str(python), str(script), "-a", "127.0.0.1", "-p", str(port), "-c", str(cfg)],
            cwd=str(api_dir), env=child_env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    deadline = time.time() + 150
    while time.time() < deadline:
        required = [port for port, _ in ports]
        if all(_port_open(port) for port in required):
            return
        time.sleep(2)
    raise RuntimeError("GPT-SoVITS API 启动超时")


def image_gallery(paths) -> str:
    converted = [str(path).replace("\\", "/") for path in paths]
    if not converted:
        return ""
    if len(converted) == 1:
        return f"![image]({converted[0]})"
    return "[[GALLERY:" + "|".join(converted) + "]]"

def video_result_message(title: str, output) -> str:
    thumb = attachments.video_thumbnail(output) if hasattr(attachments, "video_thumbnail") else ""
    output_path = Path(output)
    parts = [title]
    if thumb:
        parts.append("[[GALLERY:" + Path(thumb).as_posix() + "]]")
    parts.append("导出目录：" + output_path.parent.as_posix())
    parts.append("文件：" + output_path.as_posix())
    return chr(10) + chr(10).join(parts)


def reference_image_path(body: dict) -> str:
    path = str(body.get("attachment_path") or "").strip()
    if path and Path(path).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}:
        return path
    for item in body.get("images") or []:
        value = str(item or "").strip()
        if value and not value.startswith("data:") and Path(value).suffix.lower() in {
                ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}:
            if Path(value).is_file():
                return value
    return ""


def speech_only(text: str) -> str:
    import re
    value = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    value = re.sub(r"`[^`]*`", " ", value)
    value = re.sub(r"~~[^~]*~~", " ", value)
    value = re.sub(r"_[^_\n]{1,100}_", " ", value)
    value = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", value)
    value = re.sub(r"\[[^\]]*\]\([^)]*\)", " ", value)
    value = re.sub(r"\[\[.*?\]\]", " ", value, flags=re.DOTALL)
    value = re.sub(r"(?m)^\s*(?:导出目录|文件|附件|图片|图像)\s*[:：].*$", " ", value)
    value = re.sub(r"(?m)^\s*/.*$", " ", value)
    value = re.sub(r"(?:^|\s)/[A-Za-z_]+(?:\s+[0-9.]+)?", " ", value)
    value = re.sub(r"\*\*[^*]+\*\*", " ", value)
    value = re.sub(r"\*[^*]+\*", " ", value)
    value = re.sub(r"(?i)(?:[A-Z]:[\/]|/)[^\s，。；、)）]+", " ", value)
    value = re.sub(r"(?i)\bqwen_image\b|\bLOCAL_TOOL\b|\bqwen_[0-9-]+\.png\b", " ", value)
    value = re.sub(r"(?i)Qwen-Image[-0-9. Turbo]*\s*生成完成", "图片生成完成", value)
    acronyms = r"\b(VHS|DIG|OCR|API|GPU|CPU|TTS|PDF|URL|FPS|RIFE|ESRGAN|SD)\b"
    value = re.sub(acronyms, lambda m: " ".join(m.group(1)), value)
    quoted = re.findall(r"[「『“\"](.*?)[」』”\"]", value, re.DOTALL)
    if quoted:
        return re.sub(r"\s+", " ", " ".join(quoted)).strip()
    value = re.sub(r"[（(][^）)]*[）)]", " ", value)
    action_words = "从把将抬放推拿合打转走坐站伸低皱笑叹看望靠退停点摇"
    prefix = re.compile(r"^[" + action_words + r"][^，。！？]{0,45}[，,]\s*")
    for _ in range(4):
        match = prefix.match(value)
        if not match:
            break
        value = value[match.end():]
    pieces = value.split(None, 1)
    if len(pieces) == 2 and pieces[0][:1] in action_words and not re.search(r"[你您我请吗呢吧？?]", pieces[0]):
        value = pieces[1]
    parts = re.split(r"(?<=[。！？!?])", value)
    kept = []
    for sentence in parts:
        item = sentence.strip()
        if not item:
            continue
        if (item[:1] in action_words and not re.search(r"[你您请吗呢吧？?]", item) and len(item) < 80):
            continue
        kept.append(item)
    value = "".join(kept)
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", " ", value)
    value = re.sub(r"(?m)^\s*>\s?", " ", value)
    value = re.sub(r"[—–]+|\s--\s", " , ", value)
    value = re.sub(r"\s*([,，;；:])\s*", r"\1 ", value)
    value = re.sub(r"([,，;；:])(?:\s*[,，;；:])+", r"\1", value)
    value = re.sub(r"\.{3,}", "…", value)
    value = re.sub(r"\s+", " ", value).strip(" ,，;；:：—–-")
    return value

def _speech_language(text: str, requested: str = "") -> str:
    import re
    cjk = len(re.findall(r"[\u3400-\u9fff]", text))
    kana = len(re.findall(r"[\u3040-\u30ff]", text))
    hangul = len(re.findall(r"[\uac00-\ud7af]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    requested = str(requested or "").strip().lower()
    if kana > 4:
        return "ja"
    if hangul > 4:
        return "ko"
    if cjk >= 2 and cjk >= latin * 0.15:
        return "yue" if requested in {"yue", "粤语", "粵語", "cantonese"} else "zh"
    if latin >= 8:
        return "en"
    return {"zh": "zh", "chinese": "zh", "en": "en", "english": "en",
            "ja": "ja", "japanese": "ja", "ko": "ko", "korean": "ko",
            "yue": "yue", "cantonese": "yue"}.get(requested, "en")


def tts_profile(language_label: str) -> tuple[str, str]:
    """Return (checkpoint_key, text_language) for the selected locale."""
    value = str(language_label or "").strip().lower()
    if "粤语" in value or "粵語" in value or "cantonese" in value or "yue" in value:
        return "zh", "yue"
    if ("中文" in value or "华语" in value or "華語" in value or "chinese" in value
            or "mandarin" in value or value in {"zh", "简体中文", "繁體中文"}):
        return "zh", "zh"
    return "en", "en"


def _math_speech(text: str, language: str) -> str:
    import re
    value = text
    if language == "en":
        value = re.sub(r"(?i)\bln\b", "natural log", value)
        value = re.sub(r"(?i)\bsin\b", "sine", value)
        value = re.sub(r"(?i)\bcos\b", "cosine", value)
        value = re.sub(r"(?i)\btan\b", "tangent", value)
        value = re.sub(r"(?i)\bexp\b", "exponential", value)
        value = value.replace("π", " pi ").replace("∞", " infinity ")
        value = value.replace("²", " squared ").replace("³", " cubed ")
        value = re.sub(r"\^(-?\d+)", lambda m: {"2": " squared ", "3": " cubed "}.get(m.group(1), " to the power " + m.group(1) + " "), value)
        value = value.replace("×", " times ").replace("÷", " divided by ")
        value = re.sub(r"(?<=\d)\s*\+\s*(?=\d)", " plus ", value)
        value = re.sub(r"(?<=\d)\s*-\s*(?=\d)", " minus ", value)
        value = re.sub(r"(?<=\d)\s*=\s*(?=-?\d)", " equals ", value)
        value = re.sub(r"(?<=\d)\s*/\s*(?=\d)", " divided by ", value)
        value = re.sub(r"(?<=\d)\s*\*\s*(?=\d)", " times ", value)
        value = value.replace("(", " ").replace(")", " ")
        value = value.replace("（", " ").replace("）", " ")
        return re.sub(r"\s+", " ", value).strip()
    value = re.sub(r"(?i)\bln\b", "自然对数", value)
    value = re.sub(r"(?i)\bsin\b", "sine", value)
    value = re.sub(r"(?i)\bcos\b", "cosine", value)
    value = re.sub(r"(?i)\btan\b", "tangent", value)
    value = re.sub(r"(?i)\bexp\b", "指数", value)
    value = value.replace("π", " pi ").replace("∞", "无穷")
    value = value.replace("²", "的平方").replace("³", "的立方")
    value = re.sub(r"\^(-?\d+)", lambda m: "的" + {"2": "平方", "3": "立方"}.get(m.group(1), m.group(1) + "次方"), value)
    value = value.replace("×", "乘").replace("*", "乘")
    value = value.replace("+", "加").replace("÷", "除以").replace("=", " 等于 ").replace("/", "除以")
    value = re.sub(r"(?<=[0-9A-Za-z)])\s*-\s*(?=[0-9A-Za-z(])", "减", value)
    value = re.sub(r"([A-Za-z])['′]", r"\1撇", value)
    value = value.replace("(", " ").replace(")", " ").replace("（", " ").replace("）", " ")
    return re.sub(r"\s+", " ", value).strip()


def normalize_math_speech(text: str, language: str = "zh") -> str:
    import re
    if not re.search(r"\$|\[A-Za-z]|\^|[0-9]\s*[=+\-*/<>]\s*[0-9A-Za-z]|\b(?:sin|cos|tan|log|ln|exp)\s*\(|[²³×÷π∞]", text):
        return text
    return _math_speech(text, language)


def _clip_speech(text: str, limit: int = 500) -> str:
    if len(text) <= limit:
        return text
    window = text[:limit]
    cut = max(window.rfind(ch) for ch in ".!?。！？")
    if cut >= limit // 2:
        return window[:cut + 1].strip()
    return window.rsplit(" ", 1)[0].strip() + "."


class State:
    def __init__(self, token: str):
        self.token = token
        self.key = ""
        self.language = "简体中文"
        self.profile = ""
        self.client: api_client.DeepSeekClient | None = None
        self.lock = threading.Lock()

    def ensure_client(self) -> api_client.DeepSeekClient:
        if self.client is None:
            settings = api_client.load_settings(api_client.default_home())
            self.client = api_client.DeepSeekClient(self.key, settings.base_url, settings.model)
        return self.client


STATE: State


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[sidecar] " + fmt % args + "\n")
        sys.stderr.flush()

    def _authorized(self) -> bool:
        return self.headers.get("X-Twilight-Token", "") == STATE.token

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8"))

    def do_GET(self):
        if self.path.split("?", 1)[0] == "/tts/languages":
            self._json(200, {"supported": self.TTS_LANGUAGES, "map": self.TTS_LANGUAGE_MAP,
                             "note": "v2Pro accepts zh/en/ja/ko/yue/auto/auto_yue; accent follows reference audio."})
            return
        if self.path == "/hardware":
            if not self._authorized():
                self._json(401, {"error": "token"})
                return
            try:
                payload = hardware_index.index()
                payload["plan"] = gpu_scheduler.plan(["sd15", "tts", "upscale"])
                self._json(200, payload)
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"error": str(exc)})
            return
        if self.path != "/health":
            self._json(404, {"error": "not found"})
            return
        if not self._authorized():
            self._json(401, {"error": "token"})
            return
        self._json(200, {"ok": True, "tts_ready": self._tts_ready()})

    def do_POST(self):
        if not self._authorized():
            self._json(401, {"error": "token"})
            return
        try:
            if self.path == "/bootstrap":
                self._bootstrap()
            elif self.path == "/chat":
                self._chat()
            elif self.path == "/tts":
                self._tts()
            elif self.path == "/tools":
                self._tools()
            elif self.path == "/benchmark.json":
                if not feature_flags.BENCHMARK_ENABLED:
                    self._json(403, {"error": "当前版本不提供 Benchmark"})
                else:
                    import local_benchmark
                    result = local_benchmark.run(full=True)
                    hw = result.get("hardware", {})
                    gpus = [item.get("name", "") for item in hw.get("nvidia", [])]
                    lines = [
                        "Devices detected:",
                        "CPU: " + str(hw.get("cpu", "unknown")),
                        "GPU: " + (" / ".join(gpus) or "none"),
                        "RAM: " + str(round(float(hw.get("memory_gb", 0)))) + " GB",
                        "",
                        "Testing Qwen Image...",
                        "Testing Real-ESRGAN...",
                        "Testing NTSC-QT/VHS...",
                        "",
                        "Your score is " + str(result.get("total_pt", 0.0)) + " points.",
                    ]
                    for key in ("qwen", "esrgan", "ntsc", "ffmpeg", "cpu", "pil"):
                        if key in result.get("pt", {}):
                            lines.append(key + " : " + str(result["pt"][key]) + " pt")
                    lines.append("")
                    lines.append("Partial total: " + str(result.get("partial_total_pt", 0.0)) + " pt")
                    if result.get("errors"):
                        lines.append("Errors: " + ", ".join(result["errors"].keys()))
                    self._json(200, {"result": result, "report": chr(10).join(lines)})
            elif self.path == "/attachments":
                if not feature_flags.ATTACHMENTS_ENABLED:
                    self._json(403, {"error": "当前版本不支持附件"})
                else:
                    self._attachments()
            elif self.path == "/shutdown":
                self._json(200, {"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self._json(404, {"error": "not found"})
        except Exception as exc:  # noqa: BLE001
            api_client.LOGGER.exception("Sidecar request failed")
            self._json(500, {"error": str(exc)})

    @staticmethod
    def _tts_ready() -> bool:
        import socket
        try:
            with socket.create_connection(("127.0.0.1", 9881), timeout=0.5):
                return True
        except OSError:
            return False

    def _bootstrap(self):
        body = self._read_json()
        STATE.key = str(body.get("key") or "").strip()
        STATE.language = str(body.get("language") or "简体中文")
        profile, _ = api_client.read_character(api_client.default_home())
        STATE.profile = profile
        STATE.client = api_client.DeepSeekClient(
            STATE.key,
            api_client.load_settings(api_client.default_home()).base_url,
            api_client.load_settings(api_client.default_home()).model,
        )
        messages = persona.build_bootstrap_messages(profile, STATE.language)
        reply = STATE.client.bootstrap(messages)
        self._json(200, {"content": reply.content, "reasoning": reply.reasoning, "model": reply.model})

    def _chat(self):
        body = self._read_json()
        if DEBUG_BACKEND:
            msgs = body.get("messages") or []
            last_debug = str(msgs[-1].get("content") or "") if msgs else ""
            debug_log("POST /chat " + json.dumps({"content": last_debug[:300],
                                                    "attachment": body.get("attachment_path", "")},
                                                   ensure_ascii=False))
        messages = body.get("messages") or []
        last = ""
        for item in reversed(messages):
            if item.get("role") == "user":
                last = str(item.get("content") or "")
                break
        # Native clients prepend attachment metadata to the user content.
        # Command routing must inspect the actual command line, not the
        # metadata header, otherwise /vhs 1 falls through to the model.
        _lines = [line.strip() for line in last.splitlines() if line.strip()]
        if _lines:
            _slash = next((line for line in _lines if line.startswith("/")), "")
            if _slash:
                last = last[last.find(_slash):]
            else:
                last = _lines[-1]
        if ("压片" in last or "压个片" in last) and not last.strip().startswith("/"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            def emit_compress(event):
                self.wfile.write(("data: " + json.dumps(event, ensure_ascii=False) + chr(10) + chr(10)).encode("utf-8"))
                self.wfile.flush()
            import re as _re
            match = _re.search(r"(\d{1,3})\s*%", last)
            value = min(1.0, max(0.0, (int(match.group(1)) if match else 12) / 100.0))
            source = str(body.get("attachment_path") or "").strip()
            if not source or not Path(source).is_file():
                emit_compress({"event": "error", "error": "请先附加需要压片的视频"})
                self.close_connection = True
                return
            try:
                import effects_bridge
                emit_compress({"event": "progress", "label": "压片处理中...", "fraction": 0.05})
                def compress_progress(info):
                    data = dict(info or {})
                    data["event"] = "progress"
                    emit_compress(data)
                output = effects_bridge.run("squeeze", "crush", value, Path(source), progress=compress_progress)
                emit_compress({"event": "done",
                               "content": video_result_message(f"压片完成：crush={value:.2f}", output),
                               "reasoning": ""})
            except Exception as exc:  # noqa: BLE001
                emit_compress({"event": "error", "error": str(exc)})
            self.close_connection = True
            return
        if last.strip().lower().startswith(("/vhs", "/vhs化", "/compress", "/压片", "/aging", "/包浆")):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            def emit_effect(event):
                self.wfile.write(("data: " + json.dumps(event, ensure_ascii=False) + chr(10) + chr(10)).encode("utf-8"))
                self.wfile.flush()
            lowered = last.strip().lower()
            if lowered.startswith(("/vhs化", "/vhs")):
                mode, variable, command, display = "vhs", "q", "/vhs", "vhs化"
            elif lowered.startswith(("/压片", "/compress")):
                mode, variable, command, display = "squeeze", "crush", "/compress", "压片"
            else:
                mode, variable, command, display = "patina", "age", "/aging", "包浆"
            raw = last.strip().split(None, 1)
            try:
                value = float(raw[1]) if len(raw) > 1 else 0.7
            except ValueError:
                value = -1.0
            if value < 0.0 or value > 1.0:
                emit_effect({"event": "slider", "id": variable, "command": command,
                            "label": {"q": "q · vhs化", "crush": "crush · 压片", "age": "age · 包浆"}[variable],
                            "min": 0.0, "max": 1.0, "value": 0.7})
                emit_effect({"event": "error", "error": "参数范围是 0.0–1.0"})
                self.close_connection = True
                return
            source = str(body.get("attachment_path") or "").strip()
            if not source or not Path(source).is_file():
                emit_effect({"event": "error", "error": "请先附加需要处理的媒体文件"})
                self.close_connection = True
                return
            try:
                import effects_bridge
                emit_effect({"event": "progress", "label": "媒体处理中...", "fraction": 0.05})
                output = effects_bridge.run(mode, variable, value, Path(source), progress=emit_effect)
                content = video_result_message(f"{display} 完成：{variable}={value:.2f}", output)
                emit_effect({"event": "done", "content": content, "reasoning": ""})
            except Exception as exc:  # noqa: BLE001
                emit_effect({"event": "error", "error": str(exc)})
            self.close_connection = True
            return
        # Natural-language VHS routing for clients that send prose rather
        # than the explicit /vhs化 command. Only when a video is attached and
        # the user actually asks for the effect, never for "what is VHS?".
        _natural_source = str(body.get("attachment_path") or "").strip()
        _natural_text = last.strip()
        _natural_lower = _natural_text.lower()
        if (_natural_source and Path(_natural_source).suffix.lower() in {
                ".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".wmv", ".flv", ".ts"
            } and ("vhs" in _natural_lower or "录像带" in _natural_text)
            and any(word in _natural_text for word in
                    ("化", "效果", "处理", "强度", "做成", "弄成", "q="))):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

            def emit_natural(event):
                self.wfile.write(("data: " + json.dumps(event, ensure_ascii=False) + chr(10) + chr(10)).encode("utf-8"))
                self.wfile.flush()

            try:
                import re as _re
                _match = _re.search(r"(?:强度|q|value|数值)?\s*([01](?:\.\d+)?|0?\.\d+|\d{1,3})\s*%?", _natural_lower)
                _value = 0.7
                if _match:
                    _raw = _match.group(1)
                    _value = float(_raw)
                    if _raw.endswith("%") or _value > 1.0:
                        _value /= 100.0
                    _value = max(0.0, min(1.0, _value))
                    if "强度" in _natural_text and 1.0 <= _value <= 100.0 and _value > 1.0:
                        _value /= 100.0
                emit_natural({"event": "progress", "label": "VHS 处理中...", "id": "effect-vhs",
                              "fraction": 0.05, "state": "running"})
                import effects_bridge
                _output = effects_bridge.run("vhs", "q", _value, Path(_natural_source),
                                             progress=emit_natural)
                emit_natural({"event": "done",
                              "content": video_result_message(f"VHS 化完成：q={_value:.2f}", _output),
                              "reasoning": ""})
            except Exception as exc:  # noqa: BLE001
                emit_natural({"event": "error", "error": str(exc)})
            self.close_connection = True
            return
        if last.strip().lower().startswith("/frame"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            def emit_frame(event):
                self.wfile.write(("data: " + json.dumps(event, ensure_ascii=False) + chr(10) + chr(10)).encode("utf-8"))
                self.wfile.flush()
            frame_command = last.strip()
            if frame_command.lower().startswith("/frame-interpolation"):
                raw = frame_command[len("/frame-interpolation"):].strip()
            elif frame_command.lower().startswith("/frame-upscale"):
                raw = frame_command[len("/frame-upscale"):].strip()
            else:
                raw = frame_command[6:].strip()
            try:
                factor = int(raw)
            except ValueError:
                factor = 0
            if factor < 2 or factor > 30:
                emit_frame({"event": "slider", "id": "frame", "label": "补帧倍数",
                            "min": 2, "max": 30, "value": 2})
                emit_frame({"event": "error", "error": "请选择 2–30 的补帧倍数"})
                self.close_connection = True
                return
            source = str(body.get("attachment_path") or "").strip()
            if not source or not Path(source).is_file():
                emit_frame({"event": "error", "error": "请先附加需要补帧的视频"})
                self.close_connection = True
                return
            try:
                import local_actions
                emit_frame({"event": "progress", "label": "RIFE 补帧...", "fraction": 0.02})
                def frame_progress(info):
                    data = dict(info or {})
                    data["event"] = "progress"
                    emit_frame(data)
                output = local_actions._frame_interpolate(Path(source), factor, progress=frame_progress)[0]
                content = video_result_message(f"补帧完成：{factor}×", output)
                emit_frame({"event": "done", "content": content, "reasoning": ""})
            except Exception as exc:  # noqa: BLE001
                emit_frame({"event": "error", "error": str(exc)})
            self.close_connection = True
            return
        if last.strip().lower().startswith(("/benchmark", "/bench")):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            def emit_bench(event):
                self.wfile.write(("data: " + json.dumps(event, ensure_ascii=False) + chr(10) + chr(10)).encode("utf-8"))
                self.wfile.flush()
            try:
                if not feature_flags.BENCHMARK_ENABLED:
                    raise RuntimeError("当前版本不提供 Benchmark")
                emit_bench({"event": "progress", "label": "完整 Benchmark...", "fraction": 0.05})
                import local_benchmark
                result = local_benchmark.run(full=True)
                emit_bench({"event": "progress", "label": "完整 Benchmark...", "fraction": 1.0, "state": "done"})
                emit_bench({"event": "done", "content": "```json" + chr(10) + json.dumps(result, ensure_ascii=False, indent=2) + chr(10) + "```", "reasoning": ""})
            except Exception as exc:  # noqa: BLE001
                emit_bench({"event": "error", "error": str(exc)})
            self.close_connection = True
            return
        if last.strip().lower().startswith("/review"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            code = last.strip()[7:].strip()
            def emit_review(event):
                self.wfile.write(("data: " + json.dumps(event, ensure_ascii=False) + chr(10) + chr(10)).encode("utf-8"))
                self.wfile.flush()
            if not code:
                emit_review({"event": "error", "error": "用法：/review 后跟代码文本，或先附加代码文件再发送 /review"})
            else:
                try:
                    emit_review({"event": "done", "content": local_tools.review_source(code, "附件代码"), "reasoning": ""})
                except Exception as exc:  # noqa: BLE001
                    emit_review({"event": "error", "error": str(exc)})
            self.close_connection = True
            return
        if last.strip().lower().startswith("/search"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

            def emit_search(event: dict):
                self.wfile.write(("data: " + json.dumps(event, ensure_ascii=False) + chr(10) + chr(10)).encode("utf-8"))
            query = last.strip()[7:].strip()
            try:
                emit_search({"event": "progress", "label": "互联网检索中...", "detail": query,
                             "fraction": 0.1})
                results = web_search.search(query, 8)
                lines = [f"## 搜索结果：{query}"]
                for item in results:
                    lines.append(f"- [{item['title']}]({item['url']})\n  {item.get('snippet', '')}")
                message = "\n".join(lines) if results else "没有找到搜索结果。"
                emit_search({"event": "progress", "label": "互联网检索中...", "fraction": 1.0,
                             "state": "done"})
                emit_search({"event": "done", "content": message, "reasoning": ""})
            except Exception as exc:  # noqa: BLE001
                emit_search({"event": "error", "error": str(exc)})
            self.close_connection = True
            return
        if last.strip().lower().startswith("/math"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

            def emit_math(event: dict):
                self.wfile.write(("data: " + json.dumps(event, ensure_ascii=False) + chr(10) + chr(10)).encode("utf-8"))
                self.wfile.flush()

            try:
                latex = last.strip()[5:].strip()
                if not latex:
                    raise ValueError(r"用法：/math \int_0^1 x^2 dx")
                import plot_render
                if not plot_render.available():
                    raise RuntimeError("Edge/KaTeX 渲染组件不可用")
                path = plot_render.render_math(latex)
                message = ("公式渲染完成。" + chr(10) + chr(10) + image_gallery([str(path)])
                           + chr(10) + chr(10) + "导出目录：" + Path(path).parent.as_posix())
                emit_math({"event": "content", "text": message})
                emit_math({"event": "tools", "outcomes": [{"tool": "math", "paths": [str(path)],
                                                              "previews": [str(path)], "error": ""}]})
                emit_math({"event": "done", "content": message, "reasoning": ""})
            except Exception as exc:  # noqa: BLE001
                emit_math({"event": "error", "error": str(exc)})
            self.close_connection = True
            return
        if last.strip().lower().startswith("/plot"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

            def emit_plot(event: dict):
                self.wfile.write(("data: " + json.dumps(event, ensure_ascii=False) + chr(10) + chr(10)).encode("utf-8"))
                self.wfile.flush()

            try:
                raw = last.strip()[5:].strip()
                options = {"xmin": -5.0, "xmax": 5.0, "ymin": None, "ymax": None, "title": ""}
                for key in ("xmin", "xmax", "ymin", "ymax"):
                    match = re.search(rf"--{key}\s*=\s*(-?\d+(?:\.\d+)?)", raw)
                    if match:
                        options[key] = float(match.group(1))
                        raw = re.sub(rf"--{key}\s*=\s*-?\d+(?:\.\d+)?", "", raw)
                title = re.search(r"--title\s*=\s*([^;]+?)(?=\s+--|$)", raw)
                if title:
                    options["title"] = title.group(1).strip()
                    raw = re.sub(r"--title\s*=\s*[^;]+?(?=\s+--|$)", "", raw)
                expressions = raw.replace("，", ";").strip()
                if not expressions:
                    raise ValueError("用法：/plot x^2; sin(x)")
                import plot_render
                if not plot_render.available():
                    raise RuntimeError("Edge/KaTeX/function-plot 渲染组件不可用")
                path = plot_render.render_plot(expressions, **options)
                message = ("函数图完成。" + chr(10) + chr(10) + image_gallery([str(path)])
                           + chr(10) + chr(10) + "导出目录：" + Path(path).parent.as_posix())
                emit_plot({"event": "content", "text": message})
                emit_plot({"event": "tools", "outcomes": [{"tool": "plot", "paths": [str(path)],
                                                              "previews": [str(path)], "error": ""}]})
                emit_plot({"event": "done", "content": message, "reasoning": ""})
            except Exception as exc:  # noqa: BLE001
                emit_plot({"event": "error", "error": str(exc)})
            self.close_connection = True
            return
        if last.strip().lower().startswith("/edit"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

            def emit_edit(event: dict):
                self.wfile.write(("data: " + json.dumps(event, ensure_ascii=False) + chr(10) + chr(10)).encode("utf-8"))
                self.wfile.flush()

            try:
                instruction = last.strip()[5:].strip()
                if not instruction:
                    raise ValueError("用法：/edit 具体修改要求")
                source = str(body.get("attachment_path") or "").strip()
                if not source or Path(source).suffix.lower() not in {
                        ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}:
                    raise ValueError("请先附加需要编辑的图片")
                if not feature_flags.ATTACHMENTS_ENABLED:
                    raise RuntimeError("当前版本不支持图片编辑")
                def edit_progress(current, total, label="编辑图片"):
                    emit_edit({"event": "progress", "label": label, "id": "image-edit",
                               "fraction": max(0.0, min(1.0, current / max(1, total))),
                               "state": "running" if current < total else "done"})
                if feature_flags.SD15_EDIT_ENABLED:
                    import sd15_edit_runtime
                    output = sd15_edit_runtime.edit(instruction, source, size="512x512", progress=edit_progress)
                else:
                    import qwen_edit_runtime
                    if not qwen_edit_runtime.available():
                        raise RuntimeError("Qwen 图片编辑模型不可用")
                    output = qwen_edit_runtime.edit(instruction, source, size="512x512",
                                                    progress=edit_progress)
                message = ("图片编辑完成。" + chr(10) + chr(10) + image_gallery([str(output)])
                           + chr(10) + chr(10) + "导出目录：" + Path(output).parent.as_posix()
                           + chr(10) + "文件：" + Path(output).as_posix())
                emit_edit({"event": "content", "text": message})
                emit_edit({"event": "tools", "outcomes": [{"tool": "qwen_edit",
                                                             "paths": [str(output)],
                                                             "previews": [str(output)],
                                                             "error": ""}]})
                emit_edit({"event": "done", "content": message, "reasoning": ""})
            except Exception as exc:  # noqa: BLE001
                emit_edit({"event": "error", "error": str(exc)})
            self.close_connection = True
            return
        if last.strip().lower().startswith("/video"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

            def emit_video(event: dict):
                self.wfile.write(("data: " + json.dumps(event, ensure_ascii=False) + chr(10) + chr(10)).encode("utf-8"))
                self.wfile.flush()

            try:
                raw = last.strip()[6:].strip()
                if not raw:
                    raise ValueError("用法：/video [--ref] 视频描述")
                if not feature_flags.H3_ENABLED:
                    raise RuntimeError("当前版本未启用 MiniMax H3 视频生成")
                import minimax_h3_runtime
                if not minimax_h3_runtime.available():
                    raise RuntimeError("MiniMax H3 权重未安装或 CUDA 不可用")
                emit_video({"event": "progress", "label": "MiniMax H3 准备中", "id": "video-h3", "fraction": 0.02})
                source = str(body.get("attachment_path") or "").strip()
                source = source if source and Path(source).suffix.lower() in {
                    ".png", ".jpg", ".jpeg", ".webp", ".bmp"} else None
                use_ref = raw.lower().startswith("--ref")
                if use_ref:
                    raw = raw[5:].strip()
                def video_progress(current, total, label="H3 采样"):
                    emit_video({"event": "progress", "label": label, "id": "video-h3",
                                "fraction": max(0.0, min(1.0, current / max(1, total))),
                                "state": "running" if current < total else "done"})
                output = minimax_h3_runtime.generate(
                    raw, reference=(source if use_ref else None),
                    image=(source if source and not use_ref else None),
                    progress=video_progress)
                message = video_result_message("MiniMax H3 视频生成完成。", output)
                emit_video({"event": "content", "text": message})
                emit_video({"event": "tools", "outcomes": [{"tool": "video", "paths": [str(output)],
                                                              "previews": [attachments.video_thumbnail(output)], "error": ""}]})
                emit_video({"event": "done", "content": message, "reasoning": ""})
            except Exception as exc:  # noqa: BLE001
                emit_video({"event": "error", "error": str(exc)})
            self.close_connection = True
            return
        if last.strip().lower().startswith("/sd"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

            def emit_sd(event: dict):
                self.wfile.write(("data: " + json.dumps(event, ensure_ascii=False) + chr(10) + chr(10)).encode("utf-8"))
                self.wfile.flush()

            try:
                raw = last.strip()[3:].strip()
                if not raw:
                    raise ValueError("用法：/sd 图片描述 [768x512]")
                prompt = raw
                size = (768, 768)
                import re as _re
                match = _re.search(r"(?:^|\s)(\d{3,4}x\d{3,4})$", raw)
                if match:
                    prompt = raw[:match.start()].strip()
                    size = tuple(int(part) for part in match.group(1).split("x", 1))
                emit_sd({"event": "progress", "label": "SD1.5 生成中...", "id": "image-sd15", "fraction": 0.02})
                import ai_sd15
                path = ai_sd15.SD15Engine().generate(prompt, "", size=size)
                message = "SD1.5 生成完成。" + chr(10) + chr(10) + image_gallery([str(path)])
                emit_sd({"event": "content", "text": message})
                emit_sd({"event": "tools", "outcomes": [{"tool": "image", "paths": [str(path)], "error": ""}]})
                emit_sd({"event": "done", "content": message, "reasoning": ""})
            except Exception as exc:  # noqa: BLE001
                emit_sd({"event": "error", "error": str(exc)})
            self.close_connection = True
            return
        if last.strip().lower().startswith("/image"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

            def emit_image(event: dict):
                self.wfile.write(("data: " + json.dumps(event, ensure_ascii=False) + chr(10) + chr(10)).encode("utf-8"))
                self.wfile.flush()

            try:
                prompt = last.strip()[6:].strip()
                if not prompt:
                    raise ValueError("用法：/image 图片描述")
                emit_image({"event": "progress", "label": "图片生成中", "id": "image-qwen", "fraction": 0.02})
                path = None
                qwen_error = ""
                try:
                    import qwen_image_runtime
                    if feature_flags.IMAGE_ENGINE == "none":
                        raise RuntimeError("当前版本未启用图片生成")
                    if feature_flags.IMAGE_ENGINE == "qwen" and qwen_image_runtime.available():
                        def image_progress(current, total, label="Qwen 采样"):
                            emit_image({"event": "progress", "label": label, "id": "image-qwen",
                                        "fraction": max(0.0, min(1.0, current / max(1, total))),
                                        "state": "running" if current < total else "done"})
                        path = qwen_image_runtime.generate(prompt, size="512x512", progress=image_progress,
                                                            reference_image=reference_image_path(body))
                except Exception as exc:  # noqa: BLE001
                    qwen_error = f"{type(exc).__name__}: {exc}"
                    print("[qwen-image] fallback:", qwen_error, flush=True)
                    try:
                        qwen_image_runtime.reset()
                    except Exception:
                        pass
                    path = None
                if path is None and feature_flags.IMAGE_ENGINE == "sd15":
                    try:
                        _ref = reference_image_path(body)
                        if _ref:
                            _caption = attachments.image_caption(_ref)
                            if _caption:
                                prompt = prompt + chr(10) + "参考图：" + _caption
                    except Exception:
                        pass
                    import ai_sd15
                    path = ai_sd15.SD15Engine().generate(prompt, "", size=(768, 768))
                if path is None:
                    raise RuntimeError(qwen_error or "当前版本未启用图片生成")
                message = "图片生成完成。" + chr(10) + chr(10) + image_gallery([str(path)])
                emit_image({"event": "content", "text": message})
                emit_image({"event": "tools", "outcomes": [{"tool": "image", "paths": [str(path)], "error": ""}]})
                emit_image({"event": "done", "content": message, "reasoning": ""})
            except Exception as exc:  # noqa: BLE001
                emit_image({"event": "error", "error": str(exc)})
            self.close_connection = True
            return
        if last.strip().lower().startswith("/qwen"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

            def emit_qwen(event: dict):
                self.wfile.write(("data: " + json.dumps(event, ensure_ascii=False) + chr(10) + chr(10)).encode("utf-8"))
                self.wfile.flush()

            try:
                prompt = last.strip()[5:].strip()
                if not prompt:
                    raise ValueError("用法：/qwen 图片描述")
                if feature_flags.IMAGE_ENGINE != "qwen":
                    raise RuntimeError("当前版本未启用 Qwen-Image")
                import qwen_image_runtime
                emit_qwen({"event": "progress", "label": "Qwen 图片生成中", "id": "image-qwen", "fraction": 0.02})
                def qwen_progress(current, total, label="Qwen 采样"):
                    emit_qwen({"event": "progress", "label": label, "id": "image-qwen",
                               "fraction": max(0.0, min(1.0, current / max(1, total))),
                               "state": "running" if current < total else "done"})
                path = qwen_image_runtime.generate(prompt, size="768x768", progress=qwen_progress,
                                                    reference_image=reference_image_path(body))
                message = "Qwen-Image-2.1-Turbo 生成完成。" + chr(10) + chr(10) + image_gallery([str(path)])
                emit_qwen({"event": "content", "text": message})
                emit_qwen({"event": "tools", "outcomes": [{"tool": "qwen_image", "paths": [str(path)], "error": ""}]})
                emit_qwen({"event": "done", "content": message, "reasoning": ""})
            except Exception as exc:  # noqa: BLE001
                emit_qwen({"event": "error", "error": str(exc)})
            self.close_connection = True
            return
        if last.strip().lower().startswith(("/derpi", "/derpibooru")):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

            def emit(event: dict):
                self.wfile.write(("data: " + json.dumps(event, ensure_ascii=False) + chr(10) + chr(10)).encode("utf-8"))
                self.wfile.flush()

            try:
                query = derpi.extract_query(last)
                emit({"event": "progress", "label": "搜索 Derpibooru...", "id": "derpi-search", "detail": query,
                      "fraction": 1.0, "state": "done"})
                selected = derpi.find_images(query, 10)
                emit({"event": "progress", "label": "下载 Derpibooru 图片中...",
                      "detail": f"找到 {len(selected)} 张", "fraction": 0.08})

                def derpi_progress(info):
                    data = dict(info or {})
                    data["event"] = "progress"
                    data.setdefault("label", "下载 Derpibooru 图片中...")
                    emit(data)

                paths = derpi.download_selected(selected, 10, progress=derpi_progress)
                if not paths:
                    raise derpi.DerpiError("未找到可下载图片")
                message = f"已从 Derpibooru 下载 {len(paths)} 张图片。\n\n"
                images = image_gallery(paths[:10])
                message += images
                emit({"event": "content", "text": message})
                emit({"event": "tools", "outcomes": [
                    {"tool": "derpi", "paths": [str(p) for p in paths], "error": ""}
                ]})
                emit({"event": "done", "content": message, "reasoning": ""})
            except Exception as exc:  # noqa: BLE001
                emit({"event": "error", "error": str(exc)})
            self.close_connection = True
            return
        if not STATE.profile:
            profile, _ = api_client.read_character(api_client.default_home())
            STATE.profile = profile
        # Video frames/OCR are lazy: only an explicit video-analysis request
        # triggers decoding, so it cannot race with transcode/VHS/frames.
        attachment_path = str(body.get("attachment_path") or "").strip()
        last_user_text = str(messages[-1].get("content") or "") if messages else ""
        if attachment_path and Path(attachment_path).suffix.lower() in {
            ".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".wmv", ".flv", ".ts"
        } and any(key in last_user_text for key in
                  ("分析视频", "看视频", "视频内容", "识别视频", "视频OCR", "提取视频帧", "视频帧")):
            try:
                import attachments
                frame_text, frame_uris = attachments.extract_video_frames(Path(attachment_path))
                messages = list(messages[:-1]) + [{
                    "role": messages[-1].get("role", "user"),
                    "content": frame_text + chr(10) + chr(10) + last_user_text,
                }]
                if frame_uris:
                    body["images"] = list(frame_uris)
            except Exception:
                pass
        # Image vision is lazy too: only an explicit image-description request
        # loads BLIP. Importing an image remains fast.
        if attachment_path and Path(attachment_path).suffix.lower() in {
            ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"
        } and any(key in last_user_text for key in
                  ("描述图片", "看图", "识别图片", "图片内容", "图片OCR", "OCR", "视觉描述", "这是什么")):
            try:
                note = attachments.image_caption(attachment_path)
                if note:
                    messages = list(messages[:-1]) + [{
                        "role": messages[-1].get("role", "user"),
                        "content": f"[本地视觉描述] {note}" + chr(10) + chr(10) + last_user_text,
                    }]
            except Exception:
                pass
        system_prompt = persona.build_system_prompt(STATE.profile, STATE.language)
        system_prompt += "\n\n" + local_tools.system_prompt(feature_flags.ATTACHMENTS_ENABLED)
        images = body.get("images") or []
        if images and messages:
            last_text = str(messages[-1].get("content") or "")
            local_note = ""
            if os.environ.get("TWILIGHT_LOCAL_VISION", "1") not in {"0", "false", "no"}:
                try:
                    import vision_local
                    answers = vision_local.answer_many(last_text, [str(image) for image in images])
                    notes = []
                    for slide_number, answer_text in enumerate(answers, 1):
                        if answer_text:
                            notes.append(f"第 {slide_number} 页：{answer_text}")
                    local_note = "；".join(notes)
                except Exception:
                    local_note = ""
            if local_note:
                messages = list(messages[:-1]) + [{
                    "role": messages[-1].get("role", "user"),
                    "content": "[local-vision] " + local_note + chr(10) + last_text,
                }]
            elif os.environ.get("TWILIGHT_VISION", "1") not in {"0", "false", "no"}:
                parts = [{"type": "text", "text": last_text}]
                for image in images[:4]:
                    parts.append({"type": "image_url", "image_url": {"url": str(image)}})
                messages = list(messages[:-1]) + [{"role": messages[-1].get("role", "user"), "content": parts}]
        full = [{"role": "system", "content": system_prompt}] + messages
        client = STATE.ensure_client()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        def emit(event: dict):
            try:
                self.wfile.write(("data: " + json.dumps(event, ensure_ascii=False) + chr(10) + chr(10)).encode("utf-8"))
                self.wfile.flush()
            except OSError:
                pass

        try:
            reply = client.stream_chat(
                full,
                on_reasoning=lambda chunk: emit({"event": "reasoning", "text": chunk}),
                on_content=lambda chunk: emit({"event": "content", "text": chunk}),
            )
            clean_content, tool_calls = local_tools.extract_calls(reply.content)
            if tool_calls:
                def progress_sink(info):
                    data = dict(info or {})
                    data["event"] = "progress"
                    data.setdefault("label", data.get("kind", "本地工具"))
                    emit(data)

                tool_attachments = []
                attachment_path = str(body.get("attachment_path") or "").strip()
                if attachment_path and Path(attachment_path).is_file():
                    try:
                        tool_attachments = [attachments.extract_attachment(Path(attachment_path))]
                    except Exception:
                        tool_attachments = []
                outcomes = local_tools.execute_calls(
                    tool_calls, tool_attachments, feature_flags.ATTACHMENTS_ENABLED,
                    task_progress=progress_sink,
                )
                debug_log("tool_outcomes " + json.dumps([
                    {"tool": item.tool, "paths": item.paths, "previews": getattr(item, "previews", []), "error": item.error}
                    for item in outcomes], ensure_ascii=False)[:1000])
                emit({"event": "tools", "outcomes": [
                    {"tool": item.tool, "paths": item.paths, "error": item.error}
                    for item in outcomes
                ]})
                review_text = "\n\n".join(item.text for item in outcomes if getattr(item, "text", ""))
                if review_text:
                    clean_content += "\n\n" + review_text
                image_paths = []
                for item in outcomes:
                    image_paths.extend(item.paths or [])
                if image_paths:
                    clean_content += "\n\n" + image_gallery(image_paths)
            try:
                import plot_render
                if "$$" in clean_content or r"\[" in clean_content:
                    clean_content = plot_render.render_markdown(clean_content, max_equations=6)
            except Exception as exc:  # noqa: BLE001
                debug_log("math render failed: " + str(exc))
            emit({"event": "done", "content": clean_content, "reasoning": reply.reasoning})
        except Exception as exc:  # noqa: BLE001
            emit({"event": "error", "error": str(exc)})
        self.close_connection = True

    TTS_LANGUAGE_MAP = {
        "简体中文": "zh", "繁體中文": "zh", "zh": "zh", "chinese": "zh",
        "English": "en", "en": "en", "Français": "en", "Deutsch": "en",
        "Español": "en", "Português": "en", "Italiano": "en", "Русский": "en",
        "日本語": "ja", "ja": "ja", "한국어": "ko", "ko": "ko",
        "粤语": "yue", "粵語": "yue", "yue": "yue", "Cantonese": "yue",
    }
    TTS_LANGUAGES = ["zh", "en", "ja", "ko", "yue", "auto", "auto_yue"]

    def _tts(self):
        body = self._read_json()
        text = str(body.get("text") or "").strip()
        import re as _re
        text = _re.sub(r"[（(][^）)]*[）)]", "", text).strip()
        text = speech_only(text)
        language = str(body.get("language") or STATE.language)
        key, text_lang = tts_profile(language)
        text = normalize_math_speech(text, "en" if key == "en" else "zh")
        text = _clip_speech(text, 500)
        if not text:
            self._json(400, {"error": "empty text"})
            return
        ensure_tts_api()
        runtime = ai_home() / "tts-runtime.json"
        data = json.loads(runtime.read_text(encoding="utf-8-sig"))
        entry = data.get(key) or data.get("zh")
        ref = Path(str(entry.get("reference", "")))
        if not ref.is_absolute():
            ref = ai_home().parent / ref
        prompt = ref.with_suffix(".txt")
        prompt_text = ""
        if prompt.is_file():
            prompt_text = prompt.read_text(encoding="utf-8", errors="replace").strip()[:200]
        payload = {
            "text": text,
            "text_lang": text_lang,
            "ref_audio_path": ref.as_posix(),
            "prompt_text": prompt_text,
            "prompt_lang": "zh" if key == "zh" else "en",
            "top_k": 45,
            "top_p": 0.95,
            "temperature": 0.75,
            "repetition_penalty": 1.3,
            "speed_factor": 0.9,
            "fragment_interval": 0.15,
            "text_split_method": "cut1",
            "batch_size": 1,
            "media_type": "wav",
            "streaming_mode": False,
        }
        def request_bytes(url: str, body: dict) -> bytes:
            request = urllib.request.Request(
                url.rstrip("/") + "/tts",
                data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=300) as response:
                return response.read()

        parallel = os.environ.get("TWILIGHT_TTS_PARALLEL", "0") in {"1", "true", "yes"}
        urls = [str(entry["api_url"]).rstrip("/")]
        if parallel and key == "zh" and _port_open(9885):
            urls.append("http://127.0.0.1:9885")
        try:
            if len(urls) >= 2 and len(text) >= 16:
                import concurrent.futures
                midpoint = len(text) // 2
                split_at = text.rfind("，", 0, midpoint)
                if split_at < 0:
                    split_at = midpoint
                first = dict(payload); first["text"] = text[:split_at + 1]
                second = dict(payload); second["text"] = text[split_at + 1:]
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    parts = list(pool.map(lambda pair: request_bytes(*pair),
                                          [(urls[0], first), (urls[1], second)]))
                import io, wave
                frames = []
                params = None
                for part in parts:
                    with wave.open(io.BytesIO(part), "rb") as source:
                        if params is None:
                            params = source.getparams()
                        frames.append(source.readframes(source.getnframes()))
                out = ai_home() / "outputs" / "tts"
                out.mkdir(parents=True, exist_ok=True)
                target = out / f"sidecar-{time.strftime('%Y%m%d-%H%M%S')}.wav"
                with wave.open(str(target), "wb") as merged:
                    merged.setparams(params)
                    for frame in frames:
                        merged.writeframes(frame)
                self._json(200, {"path": str(target), "parallel": True})
                return
            data = request_bytes(urls[0], payload)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            self._json(500, {"error": f"TTS HTTP {exc.code}: {detail[:500]}"})
            return
        except Exception as exc:
            self._json(500, {"error": str(exc)[:500]})
            return
        if not data.startswith(b"RIFF"):
            self._json(500, {"error": data[:240].decode("utf-8", errors="replace")})
            return
        out = ai_home() / "outputs" / "tts"
        out.mkdir(parents=True, exist_ok=True)
        target = out / f"sidecar-{time.strftime('%Y%m%d-%H%M%S')}.wav"
        target.write_bytes(data)
        self._json(200, {"path": str(target)})

    def _tools(self):
        body = self._read_json()
        calls = body.get("calls") or []
        outcomes = local_tools.execute_calls(calls, [], feature_flags.ATTACHMENTS_ENABLED)
        self._json(200, {"outcomes": [
        ]})

    def _attachments(self):
        body = self._read_json()
        raw_paths = [str(path) for path in (body.get("paths") or [])][:32]
        total_bytes = 0
        for raw in raw_paths:
            try:
                total_bytes += Path(raw).stat().st_size
            except OSError:
                pass
        if total_bytes > 2 * 1024**3:
            self._json(413, {"items": [], "errors": ["附件总大小超过 2GB 上限"], "text": ""})
            return

        def parse_one(raw):
            try:
                item = attachments.extract_attachment(raw)
                lines = [line.strip() for line in (item.text or "").splitlines() if line.strip()]
                duration = next((line for line in lines if line.startswith("时长：")), "")
                resolution = next((line for line in lines if line.startswith("分辨率：")), "")
                meta = "附件：" + item.name
                if duration:
                    meta += " · " + duration
                if resolution:
                    meta += " · " + resolution
                return {"ok": True, "item": {"name": item.name, "text": item.text,
                                              "image_uri": item.image_uri,
                                              "image_uris": list(item.image_uris), "meta": meta}}
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{Path(raw).name}: {exc}"}

        items = []
        errors = []
        if raw_paths:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=min(4, len(raw_paths))) as pool:
                results = list(pool.map(parse_one, raw_paths))
            for result in results:
                if result.get("ok"):
                    items.append(result["item"])
                else:
                    errors.append(result.get("error", ""))
        flat_text = "\n\n".join(str(item.get("text") or "") for item in items if item.get("text"))
        self._json(200, {
            "items": items,
            "errors": errors,
            "text": flat_text,
            "name": items[0]["name"] if items else "",
            "image_uri": items[0].get("image_uri", "") if items else "",
            "image_uris": [uri for item in items for uri in (item.get("image_uris") or ([item.get("image_uri")] if item.get("image_uri") else []))],
            "meta": items[0].get("meta", "") if items else "",
        })


def main() -> int:
    global STATE
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--token", default="")
    parser.add_argument("--write-handshake", default="")
    args = parser.parse_args()
    token = args.token or os.urandom(16).hex()
    STATE = State(token)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    port = server.server_address[1]

    def _warm_qwen() -> None:
        if feature_flags.VARIANT == "benchmark":
            return
        if os.environ.get("TWILIGHT_QWEN_WARM", "1").strip().lower() in {"0", "false", "no"}:
            return
        try:
            import qwen_image_runtime
            if not qwen_image_runtime.available():
                return
            qwen_image_runtime._WARMING.set()
            time.sleep(2)
            qwen_image_runtime._pause_h3_downloads()
            try:
                qwen_image_runtime._load()
                print("[qwen] warm pipeline ready", flush=True)
            finally:
                qwen_image_runtime._WARMING.clear()
                qwen_image_runtime._resume_h3_downloads()
        except Exception as exc:  # noqa: BLE001
            print("[qwen] warm failed:", exc, flush=True)

    threading.Thread(target=_warm_qwen, name="qwen-warm", daemon=True).start()
    handshake = {"port": port, "token": token, "pid": os.getpid()}
    print("TWILIGHT_SIDECAR " + json.dumps(handshake), flush=True)
    if args.write_handshake:
        target = Path(args.write_handshake)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(handshake), encoding="utf-8")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
