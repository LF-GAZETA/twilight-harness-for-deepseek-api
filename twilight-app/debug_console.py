"""Interactive console for the debug build.

The normal GUI keeps its console log-only.  The debug executable additionally
accepts local commands on stdin, so network diagnostics and local tools can be
exercised without touching the GUI.

Examples::

    /derpi PING 104.26.15.212:443
    /derpi PING https://derpicdn.net/
    /derpi HEAD https://derpicdn.net/img/2026/10/26/3920091/thumb_small.jpg
    /derpi GET  https://derpicdn.net/img/2026/10/26/3920091/thumb_small.jpg
    /derpi twilight sparkle
    /upscale C:\\path\\image.png
    /vhs C:\\path\\video.mp4 240p
    /status
    /quit
"""

from __future__ import annotations

import ipaddress
import logging
import os
import shlex
import socket
import ssl
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

PROMPT = "twilight-debug> "
_STOP = threading.Event()
_DEFAULT_TIMEOUT = 8.0
_LAST_PROGRESS: dict[str, float] = {}
_CONSOLE_LOCK = threading.RLock()
_PROMPT_VISIBLE = False
_COMMAND_ACTIVE = False


def start(app: Any = None, root: Any = None) -> threading.Thread | None:
    """Start the console loop as a daemon thread (debug build only)."""
    if os.environ.get("TWILIGHT_DEBUG_CONSOLE", "1").strip().lower() in {"0", "false", "off", "no"}:
        return None
    if sys.stdin is None:
        return None
    _install_log_handler()
    thread = threading.Thread(target=_loop, args=(app, root),
                              name="twilight-debug-console", daemon=True)
    thread.start()
    return thread


def _banner() -> None:
    print("=" * 66)
    print("Twilight Harness Debug Console")
    print("本地命令：/help  网络诊断：/derpi PING 104.26.15.212:443")
    print("=" * 66, flush=True)


def _loop(app: Any, root: Any) -> None:
    global _COMMAND_ACTIVE
    try:
        _banner()
    except Exception:  # noqa: BLE001
        pass
    while not _STOP.is_set():
        try:
            line = _read_line()
        except KeyboardInterrupt:
            print()
            continue
        except (OSError, RuntimeError) as exc:
            print(f"\n[console] stdin 不可用：{exc}", flush=True)
            return
        if line is None:
            print()
            return
        line = line.strip()
        if not line:
            continue
        _COMMAND_ACTIVE = True
        try:
            _dispatch(line, app, root)
        except Exception as exc:  # noqa: BLE001 - a bad command must not kill the console
            print(f"[error] {type(exc).__name__}: {exc}", flush=True)
            if os.environ.get("TWILIGHT_DEBUG_TRACE", "").strip() == "1":
                traceback.print_exc()
        finally:
            _COMMAND_ACTIVE = False


def _install_log_handler() -> None:
    """Route console logs through the prompt-aware writer.

    The normal build keeps a plain StreamHandler.  In the debug console we
    replace it so a log that arrives while the prompt is visible does not
    overwrite the prompt.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
            root.removeHandler(handler)
    handler = _ConsoleLogHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)


class _ConsoleLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            _write_log(self.format(record))
        except Exception:  # noqa: BLE001
            self.handleError(record)


def _write_prompt() -> None:
    global _PROMPT_VISIBLE
    with _CONSOLE_LOCK:
        sys.stdout.write(PROMPT)
        sys.stdout.flush()
        _PROMPT_VISIBLE = True


def _read_line() -> str | None:
    global _PROMPT_VISIBLE
    _write_prompt()
    try:
        line = sys.stdin.readline()
    finally:
        with _CONSOLE_LOCK:
            _PROMPT_VISIBLE = False
    if line == "":
        return None
    return line.rstrip("\r\n")


def _write_log(message: str) -> None:
    global _PROMPT_VISIBLE
    with _CONSOLE_LOCK:
        redraw = _PROMPT_VISIBLE
        if redraw:
            sys.stdout.write("\r" + " " * len(PROMPT) + "\r")
            _PROMPT_VISIBLE = False
        sys.stdout.write(message + "\n")
        sys.stdout.flush()
        if redraw and not _COMMAND_ACTIVE and not _STOP.is_set():
            _write_prompt()


def _dispatch(line: str, app: Any, root: Any) -> None:
    first, _, rest = line.partition(" ")
    command = first.strip().lower()
    rest = rest.strip()
    if command in {"/quit", "/exit", "quit", "exit"}:
        _quit(root)
    elif command in {"/help", "help", "?"}:
        _help()
    elif command in {"/ping", "ping"}:
        _cmd_ping(rest)
    elif command in {"/derpi", "/derpibooru"}:
        _cmd_derpi(rest)
    elif command in {"/upscale", "/vupscale", "/vhs", "/transcode", "/audio", "/compress"}:
        _cmd_local(command, rest)
    elif command in {"/ai", "/sd", "/voice", "/embed"}:
        _cmd_ai(command, rest, app)
    elif command == "/status":
        _cmd_status()
    elif command in {"/config", "/cfg"}:
        _cmd_config()
    elif command in {"/paths", "/path"}:
        _cmd_paths()
    elif command == "/log":
        _cmd_log(rest)
    elif command == "/clear":
        _clear()
    else:
        print(f"[console] 未知命令：{first}", flush=True)
        print("输入 /help 查看可用命令。", flush=True)


def _help() -> None:
    print(
        """\
本地命令
  /derpi PING <host[:port]|URL> [SNI=host] [IP=addr]  TCP + DNS + TLS/HTTP HEAD 诊断
  /derpi HEAD <URL>                只取 HTTP 响应头
  /derpi GET <URL> [输出路径]      下载到 Downloads/TwilightHarness 或指定路径
  /derpi <tags>                    搜索并下载 Derpibooru 图片
  /upscale <图片>                  Real-ESRGAN 图片超分
  /vupscale <视频>                 视频逐帧超分
  /vhs <视频> [240p|288p|360p]     视频 VHS 化
  /transcode <视频> [mp4|mkv|webm] 视频转码
  /audio <视频>                    提取音轨
  /compress <视频>                 极限压片
  /ai                              AI 兼容层/资源状态
  /sd <描述>                       本地 SD1.5 文生图
  /voice                           UVR / Whisper / GPT-SoVITS 状态
  /embed                           训练/内嵌预检
  /status                          环境、API、AI 后端与模型摘要
  /config                          当前 API 配置（Key 掩码）
  /paths                           运行目录与下载目录
  /log [行数]                      查看日志尾部（默认 40 行）
  /clear                           清屏
  /quit                            关闭程序

提示：网络诊断默认超时 8 秒；IP:443 需要 SNI 时用 SNI=derpicdn.net；
      DNS 被污染时可用 IP= 指定真实地址，例如
      /derpi PING derpibooru.org:443 IP=104.26.15.212。
""",
        flush=True,
    )


# ---------------------------------------------------------------------------
# /derpi ...
# ---------------------------------------------------------------------------
def _cmd_derpi(rest: str) -> None:
    upper = rest.upper()
    if upper == "PING" or upper.startswith("PING "):
        _cmd_ping(rest[4:].strip())
        return
    if upper.startswith("HEAD "):
        _cmd_http("HEAD", rest[5:].strip())
        return
    if upper.startswith("GET "):
        _cmd_http("GET", rest[4:].strip())
        return
    query = rest or "twilight sparkle"
    try:
        import derpi
    except Exception as exc:  # noqa: BLE001
        print(f"[derpi] 无法导入 derpi：{exc}", flush=True)
        return
    print(f"[derpi] 搜索：{query}", flush=True)
    try:
        images = derpi.find_images(query, limit=10)
        ids = [str(image.get("id")) for image in images]
        print(f"[derpi] 找到 {len(images)} 张：{', '.join(ids)}", flush=True)
        if not images:
            return
        paths = derpi.download_selected(images, 10, progress=_progress)
        for path in paths:
            print(f"[derpi] 已保存：{path}", flush=True)
        print(f"[derpi] 完成：{len(paths)} 个文件", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[derpi] 失败：{exc}", flush=True)
        print("[derpi] 若为 API 超时，请检查 derpibooru.org 的 DNS/代理；derpicdn.net 可单独 PING。", flush=True)


def _cmd_ping(rest: str) -> None:
    target, sni, ip_override = _ping_args(rest)
    if not target:
        print("用法：/derpi PING <host[:port]|URL> [SNI=hostname] [IP=address]", flush=True)
        return
    try:
        host, port, scheme, path = _parse_target(target)
    except Exception as exc:  # noqa: BLE001
        print(f"[ping] 目标无效：{exc}", flush=True)
        return
    timeout = _timeout()
    suffix = f"；SNI={sni}" if sni else ""
    suffix += f"；IP={ip_override}" if ip_override else ""
    print(f"[ping] {host}:{port}（TCP 连接，超时 {timeout:.1f}s{suffix}）", flush=True)
    started = time.perf_counter()
    addresses: list[str] = []
    if ip_override:
        try:
            ipaddress.ip_address(ip_override)
        except ValueError:
            print(f"[ping] IP 覆盖值无效：{ip_override}", flush=True)
            return
        addresses = [ip_override]
        print(f"[ping] DNS：跳过（IP={ip_override}）", flush=True)
    else:
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            print(f"[ping] DNS 失败：{exc}", flush=True)
            return
        for info in infos:
            address = info[4][0]
            if address not in addresses:
                addresses.append(address)
        print(f"[ping] DNS：{', '.join(addresses)}（{time.perf_counter()-started:.0f} ms）", flush=True)
    if not addresses:
        print("[ping] 没有可用地址", flush=True)
        return
    any_ok = False
    for address in addresses:
        try:
            _probe_address(address, host, port, scheme, path, timeout, sni=sni)
            any_ok = True
        except Exception as exc:  # noqa: BLE001
            print(f"[ping] {address}:{port} FAIL：{type(exc).__name__}: {exc}", flush=True)
    print(f"[ping] 结果：{'OK' if any_ok else 'FAIL'}", flush=True)


def _ping_args(text: str) -> tuple[str, str | None, str | None]:
    args = _split_args(text) or []
    if not args:
        return "", None, None
    target = args[0]
    sni = None
    ip_override = None
    index = 1
    while index < len(args):
        part = args[index]
        lower = part.lower()
        if lower.startswith("sni="):
            sni = part[4:].strip().strip('"').strip("'")
        elif lower.startswith("ip="):
            ip_override = part[3:].strip().strip('"').strip("'")
        elif lower in {"--sni", "sni"} and index + 1 < len(args):
            index += 1
            sni = args[index].strip().strip('"').strip("'")
        elif lower in {"--ip", "--resolve", "ip"} and index + 1 < len(args):
            index += 1
            ip_override = args[index].strip().strip('"').strip("'")
        index += 1
    return target, sni or None, ip_override or None


def _parse_target(target: str) -> tuple[str, int, str | None, str]:
    if "://" in target:
        parsed = urllib.parse.urlparse(target)
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https"}:
            raise ValueError(f"不支持的 URL 协议 {scheme!r}")
        host = parsed.hostname
        if not host:
            raise ValueError("URL 缺少主机名")
        port = parsed.port or (443 if scheme == "https" else 80)
        return host, int(port), scheme, parsed.path or "/"
    raw = target
    if raw.startswith("["):
        host, _, tail = raw[1:].partition("]")
        port = int(tail[1:]) if tail.startswith(":") and tail[1:] else 443
    elif raw.count(":") == 1:
        host, port_text = raw.rsplit(":", 1)
        port = int(port_text)
    else:
        host, port = raw, 443
    if not host:
        raise ValueError("缺少主机名")
    if not (1 <= port <= 65535):
        raise ValueError(f"端口超出范围：{port}")
    return host, port, None, "/"


def _probe_address(address: str, host: str, port: int, scheme: str | None,
                   path: str, timeout: float, sni: str | None = None) -> None:
    tcp_start = time.perf_counter()
    sock = socket.create_connection((address, port), timeout=timeout)
    tcp_ms = (time.perf_counter() - tcp_start) * 1000.0
    try:
        print(f"[ping] [{address}]:{port} TCP OK（{tcp_ms:.0f} ms）", flush=True)
        if port in (443, 8443) or scheme == "https":
            try:
                _probe_tls(sock, address, host, port, path, timeout, sni=sni)
            except Exception as exc:  # noqa: BLE001 - TCP already succeeded
                print(f"[ping] TLS/HTTP 检查失败（TCP 已连通）：{type(exc).__name__}: {exc}", flush=True)
        elif port == 80 or scheme == "http":
            try:
                _probe_http(sock, host, path, timeout)
            except Exception as exc:  # noqa: BLE001
                print(f"[ping] HTTP 检查失败（TCP 已连通）：{type(exc).__name__}: {exc}", flush=True)
        else:
            print("[ping] 非 HTTP(S) 端口，已完成 TCP 连通性检查", flush=True)
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _probe_tls(sock: socket.socket, address: str, host: str, port: int,
               path: str, timeout: float, sni: str | None = None) -> None:
    sock.settimeout(timeout)
    try:
        ip_literal = ipaddress.ip_address(host)
    except ValueError:
        ip_literal = None
    server_name = sni or (host if ip_literal is None else None)
    verify = server_name is not None
    if verify:
        context = ssl.create_default_context()
    else:
        context = ssl._create_unverified_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    try:
        tls = context.wrap_socket(sock, server_hostname=server_name)
    except ssl.SSLCertVerificationError as exc:
        print(f"[ping] [{address}] TLS 证书校验失败（TCP 已连通）：{exc}", flush=True)
        return
    except ssl.SSLError as exc:
        if ip_literal is not None and not sni:
            print(
                "[ping] TLS 未执行：IP 目标未提供 SNI（服务器可能要求 SNI）。"
                "用 SNI=derpicdn.net 重试，或直接 PING 主机名；TCP 已连通。",
                flush=True,
            )
        else:
            print(f"[ping] [{address}] TLS 握手失败（TCP 已连通）：{exc}", flush=True)
        return
    with tls:
        print(f"[ping] [{address}] TLS {tls.version()} / {tls.cipher()[0]}", flush=True)
        cert = tls.getpeercert() if verify else None
        if cert:
            subject = ", ".join("=".join(part) for item in cert.get("subject", ()) for part in item)
            issuer = ", ".join("=".join(part) for item in cert.get("issuer", ()) for part in item)
            print(f"[ping] 证书 subject：{subject}", flush=True)
            print(f"[ping] 证书 issuer ：{issuer}；到期：{cert.get('notAfter', '?')}", flush=True)
        elif not verify:
            print("[ping] TLS 证书未校验（IP 目标未提供 SNI）", flush=True)
        _http_head(tls, server_name or host, path)


def _probe_http(sock: socket.socket, host: str, path: str, timeout: float) -> None:
    sock.settimeout(timeout)
    _http_head(sock, host, path)


def _http_head(sock: Any, host: str, path: str) -> None:
    request = (
        f"HEAD {path or '/'} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        "User-Agent: TwilightHarness/1.0 (Windows)\r\n"
        "Accept: */*\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii", errors="replace")
    sock.sendall(request)
    buffer = b""
    while b"\r\n\r\n" not in buffer and len(buffer) < 16_384:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buffer += chunk
    head = buffer.split(b"\r\n\r\n", 1)[0].decode("latin-1", errors="replace")
    lines = head.split("\r\n")
    status = lines[0] if lines else "(no response)"
    try:
        code = int(status.split()[1])
    except (IndexError, ValueError):
        code = 0
    if code >= 400:
        print(f"[ping] HTTP：{status}（服务器可达；该路径返回 {code}，不是连接失败）", flush=True)
    else:
        print(f"[ping] HTTP：{status}", flush=True)
    for line in lines[1:]:
        name, _, value = line.partition(":")
        if name.strip().lower() in {"server", "content-type", "content-length", "cf-ray", "location"}:
            print(f"[ping]   {name.strip()}: {value.strip()}", flush=True)


def _cmd_http(method: str, rest: str) -> None:
    args = _split_args(rest)
    if args is None:
        print("[http] 参数引号不匹配", flush=True)
        return
    if not args:
        print(f"用法：/derpi {method} <URL> [输出路径]", flush=True)
        return
    url = args[0]
    output = Path(args[1]).expanduser() if len(args) > 1 else None
    if not url.lower().startswith(("http://", "https://")):
        print("[http] 仅支持 http/https URL", flush=True)
        return
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) TwilightHarness/1.0",
        "Accept": "*/*",
    }
    if "derpicdn.net" in url or "derpibooru.org" in url:
        headers["Referer"] = "https://derpibooru.org/"
    request = urllib.request.Request(url, headers=headers, method=method)
    print(f"[http] {method} {url}", flush=True)
    try:
        with urllib.request.urlopen(request, timeout=_timeout()) as response:
            print(f"[http] 状态：{response.status} {response.reason}", flush=True)
            print(f"[http] Content-Type：{response.headers.get('Content-Type', '?')}", flush=True)
            print(f"[http] Content-Length：{response.headers.get('Content-Length', '?')}", flush=True)
            if method == "HEAD":
                return
            if output is None:
                import derpi

                folder = derpi.save_dir()
                suffix = Path(urllib.parse.urlparse(url).path).suffix or ".bin"
                output = folder / f"debug_download_{int(time.time())}{suffix}"
            output.parent.mkdir(parents=True, exist_ok=True)
            total = 0
            with output.open("wb") as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
                    total += len(chunk)
            print(f"[http] 已保存：{output}（{total} 字节）", flush=True)
    except urllib.error.HTTPError as exc:
        print(f"[http] HTTP {exc.code} {exc.reason}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[http] 失败：{type(exc).__name__}: {exc}", flush=True)


# ---------------------------------------------------------------------------
# local attachment tools
# ---------------------------------------------------------------------------
def _cmd_local(command: str, rest: str) -> None:
    args = _split_args(rest)
    if args is None:
        print("[local] 参数引号不匹配", flush=True)
        return
    if not args:
        print(f"用法：{command} <文件> ...", flush=True)
        return
    path = Path(args[0]).expanduser()
    if not path.is_file():
        print(f"[local] 文件不存在：{path}", flush=True)
        return
    try:
        import attachments
        import local_actions
    except Exception as exc:  # noqa: BLE001
        print(f"[local] 模块导入失败：{exc}", flush=True)
        return
    try:
        attachment = attachments.extract_attachment(path)
        print(f"[local] 已解析附件：{path.name}（{len(attachment.text)} 字符）", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[local] 附件解析失败：{exc}", flush=True)
        return
    if command == "/transcode":
        target = args[1] if len(args) > 1 else "mp4"
        text = f"/transcode {target}"
    elif command == "/vhs":
        mode = args[1] if len(args) > 1 else "240p"
        text = f"/vhs {mode}"
    else:
        text = command
    request = local_actions.parse_request(text, [attachment])
    if request is None:
        print("[local] 无法构造本地请求", flush=True)
        return
    if request.error:
        print(f"[local] 缺少：{request.error}", flush=True)
        return
    print(f"[local] 开始：{request.kind} {text}", flush=True)
    started = time.perf_counter()
    try:
        paths = local_actions.run(request, progress=_progress)
        for item in paths:
            print(f"[local] 输出：{item}", flush=True)
        print(f"[local] 完成：{len(paths)} 个文件（{time.perf_counter()-started:.1f}s）", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[local] 失败：{type(exc).__name__}: {exc}", flush=True)


# ---------------------------------------------------------------------------
# AI / status
# ---------------------------------------------------------------------------
def _cmd_ai(command: str, rest: str, app: Any) -> None:
    if app is None or not hasattr(app, "_run_ai_command"):
        print("[ai] GUI 尚未初始化，无法执行该命令", flush=True)
        return
    print(f"[ai] {command} 执行中…", flush=True)
    started = time.perf_counter()
    try:
        text = app._run_ai_command(command, rest)
        print(text, flush=True)
        print(f"[ai] 完成（{time.perf_counter()-started:.1f}s）", flush=True)
    except Exception as exc:  # noqa: BLE001
        message = getattr(exc, "user_message", None) or f"{type(exc).__name__}: {exc}"
        print(f"[ai] 失败：{message}", flush=True)


def _cmd_status() -> None:
    print("[status] Twilight Harness debug console", flush=True)
    print(f"[status] frozen={getattr(sys, 'frozen', False)} executable={sys.executable}", flush=True)
    try:
        import api_client
        import paths

        home = paths.data_home()
        settings = api_client.load_settings(home)
        print(f"[status] 配置：{api_client.config_path(home)}", flush=True)
        print(f"[status] base_url：{settings.base_url}；model：{settings.model}", flush=True)
        print(f"[status] api_key：{_mask(settings.api_key)}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[status] API 配置读取失败：{exc}", flush=True)
    try:
        import ai_resources

        print(f"[status] AI_HOME：{ai_resources.ai_home()}", flush=True)
        for name in ("require_sd15", "require_uvr", "require_whisper", "require_gpt_sovits"):
            fn = getattr(ai_resources, name, None)
            if fn is None:
                continue
            try:
                fn()
                print(f"[status] {name}: OK", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"[status] {name}: {getattr(exc, 'user_message', exc)}", flush=True)
        print(f"[status] LoRA：{ai_resources.lora_status()}", flush=True)
        print(f"[status] 参考音频：{ai_resources.reference_audio_status()}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[status] AI 资源读取失败：{exc}", flush=True)
    try:
        import ai_compat

        for line in ai_compat.backend_report_lines():
            print(f"[status] {line}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[status] 后端探测失败：{exc}", flush=True)
    try:
        import derpi

        print(f"[status] 下载目录：{derpi.save_dir()}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[status] 下载目录不可用：{exc}", flush=True)


def _cmd_config() -> None:
    try:
        import api_client
        import paths

        home = paths.data_home()
        settings = api_client.load_settings(home)
        print(f"[config] 文件：{api_client.config_path(home)}", flush=True)
        print(f"[config] api_key：{_mask(settings.api_key)}", flush=True)
        print(f"[config] base_url：{settings.base_url}", flush=True)
        print(f"[config] model：{settings.model}", flush=True)
        print(f"[config] key 来源：{'环境变量' if settings.api_key_from_env else '配置文件'}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[config] 失败：{exc}", flush=True)


def _cmd_paths() -> None:
    try:
        import ai_resources
        import paths

        print(f"[paths] data_home：{paths.data_home()}", flush=True)
        print(f"[paths] persistent：{paths.persistent_dir()}", flush=True)
        print(f"[paths] AI_HOME：{ai_resources.ai_home()}", flush=True)
        print(f"[paths] 模型：{ai_resources.models_dir()}", flush=True)
        print(f"[paths] SD1.5：{ai_resources.sd15_dir()}", flush=True)
        print(f"[paths] GPT-SoVITS：{ai_resources.gpt_sovits_dir()}", flush=True)
        print(f"[paths] UVR：{ai_resources.uvr_dir()}", flush=True)
        print(f"[paths] Whisper：{ai_resources.whisper_dir()}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[paths] 失败：{exc}", flush=True)
    try:
        import derpi

        print(f"[paths] 下载：{derpi.save_dir()}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[paths] 下载目录：{exc}", flush=True)


def _cmd_log(rest: str) -> None:
    try:
        count = int(rest) if rest.strip() else 40
    except ValueError:
        count = 40
    count = max(1, min(500, count))
    try:
        import paths

        home = Path(paths.data_home())
        candidates = [home / "twilight-harness.log", home / "temp" / "twilight-harness.log"]
        log_path = next((item for item in candidates if item.is_file()), candidates[0])
        if not log_path.is_file():
            print(f"[log] 不存在：{log_path}", flush=True)
            return
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        print(f"[log] {log_path}（最后 {min(count, len(lines))} 行）", flush=True)
        for line in lines[-count:]:
            print(line, flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[log] 失败：{exc}", flush=True)


def _clear() -> None:
    try:
        print("\033[2J\033[H", end="", flush=True)
    except Exception:  # noqa: BLE001
        pass


def _quit(root: Any) -> None:
    _STOP.set()
    print("[console] 正在关闭 Twilight Harness…", flush=True)
    if root is None:
        return
    try:
        root.event_generate("<<DebugQuit>>", when="tail")
    except Exception:  # noqa: BLE001
        try:
            root.after(0, root.destroy)
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _split_args(text: str) -> list[str] | None:
    """Split console arguments without eating Windows backslashes."""
    try:
        parts = shlex.split(text, posix=False)
    except ValueError:
        return None
    return [part.strip('"').strip("'") for part in parts]


def _timeout() -> float:
    try:
        value = float(os.environ.get("TWILIGHT_PING_TIMEOUT", str(_DEFAULT_TIMEOUT)))
    except ValueError:
        value = _DEFAULT_TIMEOUT
    return max(1.0, min(120.0, value))


def _mask(key: str) -> str:
    if not key:
        return "（空）"
    if len(key) <= 10:
        return key[:3] + "***"
    return key[:6] + "…" + key[-4:]


def _progress(event: dict) -> None:
    try:
        state = str(event.get("state") or "")
        key = str(event.get("id") or event.get("name") or "")
        fraction = float(event.get("fraction") or 0.0)
        last = _LAST_PROGRESS.get(key, -1.0)
        if state in {"done", "error"} or fraction - last >= 0.10:
            _LAST_PROGRESS[key] = fraction
            name = event.get("name") or key
            print(f"[progress] {event.get('kind', '?')} {name} {fraction*100:.0f}% {state}", flush=True)
    except Exception:  # noqa: BLE001
        pass
