#!/usr/bin/env python3
"""OpenAI-compatible mock DeepSeek API server for Twilight Harness black-box QA.

Why this exists
---------------
The delivered app (`twilight-harness/twilight-harness.exe`) is only allowed to be
pointed at a fake backend through **its own per-copy `api-key.ini` base_url** --
no private test hooks are used.  This module is the fake backend.

Contract implemented (OpenAI-compatible subset)
-----------------------------------------------
* ``POST {base}/chat/completions`` (also tolerates ``/v1/chat/completions``,
  trailing slashes and duplicate slashes in the path).
* ``stream: true``  -> ``text/event-stream`` SSE:
      data: {"choices":[{"delta":{"reasoning_content": ...}}]}
      data: {"choices":[{"delta":{"content": ...}}]}
      data: {"choices":[{"delta":{},"finish_reason":"stop"}]}
      data: [DONE]
  Chunks are flushed one at a time with a configurable delay (default 45 ms) so
  a human/automation can observe the left "thinking" pane filling first and the
  right "output" pane second.
* ``stream: false`` (persona bootstrap) -> normal JSON completion with both
  ``choices[0].message.content`` and ``choices[0].message.reasoning_content``.
* Scenario ``success`` (default), ``401`` (invalid key) and ``500``.
* Every request (JSON body + headers) is appended as one JSON line to
  ``tests/artifacts/requests.jsonl``; the same records are kept in memory for
  precise per-scenario assertions (``server.records``).

Standalone use::

    <bundled-python> tests/mock_deepseek.py --port 8765 --scenario success
    # prints "MOCK_READY http://127.0.0.1:8765" then serves until Ctrl+C

    <bundled-python> tests/mock_deepseek.py --port 8766 --scenario 401

The module has **no third-party dependencies** on purpose (stdlib only) so it
can be launched by any Python 3.8+ interpreter.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import threading
import time
import traceback
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LOG = os.path.join(HERE, "artifacts", "requests.jsonl")

# --------------------------------------------------------------------------- #
# Canned content
# --------------------------------------------------------------------------- #
# A realistic persona bootstrap reply: greeting + reasoning.  It is
# intentionally content-rich so a screenshot can show both panes populated.
BOOTSTRAP_REASONING = (
    "用户刚刚确认了 API 密钥，触发了人格初始化。作为助手，我需要先确认角色设定已经加载，"
    "再以紫悦的口吻给出一句友好的开场问候；不能替用户说话，也不能一次抛出太多问题。"
    "接下来保持学术而温暖的语气，并提醒用户选择交流语言。"
)

BOOTSTRAP_CONTENT = (
    "【角色载入完成】你好，我是紫悦（Twilight Sparkle），友谊公主，来自小马镇。"
    "很高兴见到你！请告诉我你希望使用哪种语言或方言交流，我们就可以开始今天的对话了。"
)

# Chunks are concatenated by the client, so each one is a natural continuation.
REASONING_CHUNKS = [
    "让我先理清思路。",
    "用户提出了一个具体的问题，",
    "我需要先致意，",
    "再分层次给出条理清晰的回答。",
    "作为友谊公主，",
    "语气要精确、温暖而克制，",
    "不能浮夸，",
    "也不能替用户做决定。",
    "先确认概念，",
    "再给出可执行的步骤，",
    "最后留一个开放式的小问题。",
    "这样既符合学者式的表达，",
    "也保持了紫悦一贯的耐心。",
    "好，准备开始输出正文。",
]

CONTENT_CHUNKS = [
    "你好，我是紫悦。",
    "很高兴在这里与你交流。",
    "关于你刚才的问题，",
    "我想分三个层面来说明：",
    "第一，先厘清核心概念，",
    "让讨论有共同的起点；",
    "第二，给出可操作的步骤，",
    "每一步都尽量具体；",
    "第三，留出继续讨论的空间，",
    "因为理解往往是在对话中生长的。",
    "如果其中有任何一处需要更详细的展开，",
    "随时告诉我，",
    "我会用相应的语言和方言继续说明。",
    "期待你的回复。",
]

LANGUAGE_TOKENS = [
    "上海话", "广东粤语", "东北话", "四川话", "新疆话", "普通话", "北京官话",
    "港澳粤语", "台湾话", "新加坡及马来西亚地区华语",
    "美式英语", "英式英语",
    "法国方言", "加拿大魁北克方言",
    "德国方言", "奥地利方言",
    "意大利语", "西班牙语", "葡萄牙方言", "巴西方言",
    "日语", "韩语", "阿拉伯语", "希伯来语",
]

VALID_SCENARIOS = ("success", "401", "500")


# --------------------------------------------------------------------------- #
# State / server
# --------------------------------------------------------------------------- #
class MockState:
    """Thread-safe server state shared by all request-handler threads."""

    def __init__(self, scenario="success", log_path=DEFAULT_LOG,
                 chunk_delay=0.045, verbose=False):
        self.lock = threading.RLock()
        self.scenario = scenario if scenario in VALID_SCENARIOS else "success"
        self.log_path = log_path
        self.chunk_delay = float(chunk_delay)
        self.verbose = verbose
        self.records = []
        self._seq = 0
        self.requests_started = 0

    # -- config ----------------------------------------------------------- #
    def set_scenario(self, scenario):
        if scenario not in VALID_SCENARIOS:
            raise ValueError("unknown scenario %r (choose from %s)"
                             % (scenario, ", ".join(VALID_SCENARIOS)))
        with self.lock:
            self.scenario = scenario
        self.log("[scenario] -> %s" % scenario)

    # -- logging ---------------------------------------------------------- #
    def log(self, message):
        if self.verbose:
            print("[mock] %s" % message, flush=True)

    def append_record(self, record):
        with self.lock:
            self._seq += 1
            record["seq"] = self._seq
            self.records.append(record)
            seq = self._seq
            log_path = self.log_path
        # Write outside the lock; append is atomic enough for our single-run use.
        if log_path:
            try:
                directory = os.path.dirname(log_path)
                if directory:
                    os.makedirs(directory, exist_ok=True)
                with open(log_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                    fh.flush()
            except OSError as exc:  # never let logging break the mock API
                self.log("WARNING: cannot write request log: %s" % exc)
        return seq

    # -- snapshots -------------------------------------------------------- #
    def snapshot(self):
        with self.lock:
            return list(self.records)

    def mark(self):
        with self.lock:
            return len(self.records)

    def since(self, mark, kind=None):
        with self.lock:
            records = list(self.records)[mark:]
        if kind is None:
            return records
        return [r for r in records if r.get("request_kind") == kind]

    def wait_for(self, kind=None, mark=0, timeout=10.0, predicate=None,
                 interval=0.05):
        """Block until a matching record arrives (or timeout). Returns list."""
        deadline = time.time() + timeout
        while True:
            hits = self.since(mark, kind)
            if predicate is not None:
                hits = [r for r in hits if predicate(r)]
            if hits:
                return hits
            if time.time() >= deadline:
                return hits
            time.sleep(interval)


class MockServer:
    """Convenience wrapper around a threaded stdlib HTTP server.

    Usable in-process (``MockServer().start()``) and standalone via CLI.
    """

    def __init__(self, host="127.0.0.1", port=0, scenario="success",
                 log_path=DEFAULT_LOG, chunk_delay=0.045, verbose=False):
        self.state = MockState(scenario=scenario, log_path=log_path,
                               chunk_delay=chunk_delay, verbose=verbose)
        self._httpd = _ThreadingHTTPServer((host, port), _MockHandler)
        self._httpd.state = self.state
        self._httpd.daemon_threads = True
        self._thread = None
        self.port = self._httpd.server_address[1]
        self.host = self._httpd.server_address[0]
        self._stopped = False

    # -- lifecycle -------------------------------------------------------- #
    def start(self):
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        kwargs={"poll_interval": 0.05},
                                        name="mock-deepseek",
                                        daemon=True)
        self._thread.start()
        self.state.log("MOCK_READY http://%s:%s" % (self.host, self.port))
        return self

    def stop(self):
        if self._stopped:
            return
        self._stopped = True
        try:
            self._httpd.shutdown()
        except Exception:
            pass
        try:
            self._httpd.server_close()
        except Exception:
            pass
        if self._thread is not None:
            self._thread.join(timeout=3)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc_info):
        self.stop()

    # -- accessors -------------------------------------------------------- #
    @property
    def base_url(self):
        return "http://%s:%d" % (self.host, self.port)

    @property
    def base_url_v1(self):
        return self.base_url + "/v1"

    # -- delegation to state --------------------------------------------- #
    def set_scenario(self, scenario):
        self.state.set_scenario(scenario)

    @property
    def scenario(self):
        return self.state.scenario

    @property
    def records(self):
        return self.state.snapshot()

    def mark(self):
        return self.state.mark()

    def since(self, mark, kind=None):
        return self.state.since(mark, kind)

    def wait_for(self, kind=None, mark=0, timeout=10.0, predicate=None,
                 interval=0.05):
        return self.state.wait_for(kind=kind, mark=mark, timeout=timeout,
                                   predicate=predicate, interval=interval)


# --------------------------------------------------------------------------- #
# HTTP plumbing
# --------------------------------------------------------------------------- #
class _ThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _normalize_path(raw_path):
    path = urllib.parse.urlsplit(raw_path).path or "/"
    path = re.sub(r"/+", "/", path)
    if len(path) > 1:
        path = path.rstrip("/")
    return path


def _is_chat_completions(path):
    # /chat/completions, /v1/chat/completions, /some/base/chat/completions
    return path == "/chat/completions" or path.endswith("/chat/completions")


def _model_from_body(body):
    model = body.get("model") if isinstance(body, dict) else None
    if not isinstance(model, str) or not model:
        return "deepseek-reasoner"
    return model


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()) + ".%03d" % (
        int(time.time() * 1000) % 1000)


def _new_completion_id(kind):
    return "chatcmpl-mock-%s-%s" % (kind, uuid.uuid4().hex[:12])


class _MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "MockDeepSeek/1.0"
    sys_version = ""

    # -- helpers ---------------------------------------------------------- #
    @property
    def state(self):
        return self.server.state

    def log_message(self, fmt, *args):  # noqa: A003 - stdlib hook name
        if self.state.verbose:
            sys.stderr.write("[mock-http] %s\n" % (fmt % args))

    def _send_json(self, status, payload, extra_headers=None):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.send_header("x-request-id", "mock-%s" % uuid.uuid4().hex[:8])
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)
        self.wfile.flush()
        self.close_connection = True

    def _start_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "close")
        self.end_headers()

    def _write_chunk(self, data):
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")
        self.wfile.flush()

    def _end_chunked(self):
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()
        self.close_connection = True

    def _sse_event(self, payload):
        self._write_chunk(
            "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
        )

    # -- HTTP verbs ------------------------------------------------------- #
    def do_GET(self):  # noqa: N802 - stdlib hook name
        path = _normalize_path(self.path)
        if path in ("/", "/health", "/api/health", "/v1/health"):
            self._send_json(200, {
                "ok": True,
                "service": "mock-deepseek",
                "scenario": self.state.scenario,
                "requests": len(self.state.records),
            })
            return
        self._send_json(404, {"error": {"message": "not found: %s" % path}})

    def do_POST(self):  # noqa: N802 - stdlib hook name
        started = time.time()
        path = _normalize_path(self.path)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        raw_body = b""
        if length > 0:
            try:
                raw_body = self.rfile.read(length)
            except (ConnectionError, OSError):
                raw_body = b""

        body = None
        body_parse_error = None
        if raw_body:
            try:
                body = json.loads(raw_body.decode("utf-8", "replace"))
            except (ValueError, UnicodeDecodeError) as exc:
                body_parse_error = str(exc)

        if not _is_chat_completions(path):
            record = self._make_record(path, body, raw_body, body_parse_error,
                                       request_kind="unknown")
            record["response_status"] = 404
            record["duration_s"] = round(time.time() - started, 4)
            self.state.append_record(record)
            self._send_json(404, {"error": {
                "message": "mock: unsupported path %s (expected POST "
                           "*/chat/completions)" % (path or self.path),
                "type": "invalid_request_error",
            }})
            return

        is_stream = bool(isinstance(body, dict) and body.get("stream"))
        request_kind = "chat" if is_stream else "bootstrap"
        record = self._make_record(path, body, raw_body, body_parse_error,
                                   request_kind=request_kind)
        scenario = self.state.scenario
        record["scenario"] = scenario

        if scenario == "401":
            record["response_status"] = 401
            record["response_kind"] = "json"
            record["duration_s"] = round(time.time() - started, 4)
            record["response_body"] = _ERROR_401
            self.state.append_record(record)
            self._send_json(401, _ERROR_401,
                            {"WWW-Authenticate": "Bearer"})
            return
        if scenario == "500":
            payload = {"error": {"message": "mock: internal server error",
                                 "type": "server_error"}}
            record["response_status"] = 500
            record["response_kind"] = "json"
            record["duration_s"] = round(time.time() - started, 4)
            record["response_body"] = payload
            self.state.append_record(record)
            self._send_json(500, payload)
            return

        model = _model_from_body(body)
        try:
            if is_stream:
                chunks_sent = self._stream_chat(model, record)
                record["response_kind"] = "sse"
                record["chunks_sent"] = chunks_sent
            else:
                self._reply_bootstrap(model, record)
                record["response_kind"] = "json"
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError,
                OSError) as exc:
            record["client_disconnected"] = True
            record["client_disconnect_reason"] = "%s: %s" % (
                type(exc).__name__, exc)
            self.state.log("client disconnected: %s" % exc)
        finally:
            record["duration_s"] = round(time.time() - started, 4)
            self.state.append_record(record)

    # -- request/response builders --------------------------------------- #
    def _make_record(self, path, body, raw_body, body_parse_error,
                     request_kind):
        header_items = list(self.headers.items())
        return {
            "time": _now_iso(),
            "epoch": round(time.time(), 3),
            "method": self.command,
            "path": self.path,
            "normalized_path": path,
            "request_kind": request_kind,
            "headers": dict(header_items),
            "headers_lc": {k.lower(): v for k, v in header_items},
            "body": body,
            "body_raw_len": len(raw_body),
            "body_parse_error": body_parse_error,
            "response_status": None,
        }

    def _reply_bootstrap(self, model, record):
        payload = {
            "id": _new_completion_id("bootstrap"),
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": BOOTSTRAP_CONTENT,
                    "reasoning_content": BOOTSTRAP_REASONING,
                },
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": 1024,
                "completion_tokens": 128,
                "total_tokens": 1152,
            },
        }
        record["response_status"] = 200
        record["model_echo"] = model
        self._send_json(200, payload)

    def _stream_chat(self, model, record):
        completion_id = _new_completion_id("chat")
        created = int(time.time())
        self._start_sse()
        sent = 0

        def emit(delta, finish_reason=None):
            nonlocal sent
            self._sse_event({
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{
                    "index": 0,
                    "delta": delta,
                    "finish_reason": finish_reason,
                }],
            })
            sent += 1

        # 1) role preamble, like the real API
        emit({"role": "assistant", "content": ""})

        # 2) reasoning_content stream (left pane)
        for chunk in REASONING_CHUNKS:
            time.sleep(self.state.chunk_delay)
            emit({"reasoning_content": chunk})

        # 3) content stream (right pane)
        for chunk in CONTENT_CHUNKS:
            time.sleep(self.state.chunk_delay)
            emit({"content": chunk})

        # 4) finish + [DONE]
        time.sleep(self.state.chunk_delay)
        emit({}, "stop")
        self._write_chunk("data: [DONE]\n\n")
        sent += 1
        self._end_chunked()
        record["response_status"] = 200
        record["model_echo"] = model
        return sent


_ERROR_401 = {
    "error": {
        "message": "Invalid API key provided: mock simulated 401. "
                   "Please check your api-key.ini.",
        "type": "invalid_request_error",
        "param": None,
        "code": "invalid_api_key",
    }
}


# --------------------------------------------------------------------------- #
# Standalone entrypoint
# --------------------------------------------------------------------------- #
def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="OpenAI-compatible mock DeepSeek server for QA.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0,
                        help="0 = pick a free port automatically")
    parser.add_argument("--scenario", default="success",
                        choices=VALID_SCENARIOS)
    parser.add_argument("--log", default=DEFAULT_LOG,
                        help="JSONL request log path (default: %s)" % DEFAULT_LOG)
    parser.add_argument("--chunk-delay", type=float, default=0.045,
                        help="seconds between SSE chunks (default 0.045)")
    parser.add_argument("--duration", type=float, default=0,
                        help="stop after N seconds (0 = run until Ctrl+C)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    port = args.port or _free_port()
    server = MockServer(host=args.host, port=port, scenario=args.scenario,
                        log_path=args.log, chunk_delay=args.chunk_delay,
                        verbose=args.verbose)
    server.start()
    print("MOCK_READY %s" % server.base_url, flush=True)
    print("MOCK_SCENARIO %s" % args.scenario, flush=True)
    print("MOCK_LOG %s" % args.log, flush=True)

    try:
        if args.duration:
            time.sleep(args.duration)
        else:
            while True:
                time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
        print("MOCK_STOPPED", flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # pragma: no cover - last-resort debug aid
        traceback.print_exc()
        sys.exit(1)
