"""DeepSeek-compatible API client, configuration and character-file loading.

The application deliberately depends only on the Python standard library so the
PyInstaller build stays compact and self-contained.
"""

from __future__ import annotations

import base64
import configparser
import json
import logging
import os
import re
import shutil
import socket
import ssl
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import character_data
import paths

LOGGER = logging.getLogger("twilight_harness")

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-reasoner"
CONFIG_FILE_NAME = "api-key.ini"
CHARACTER_FILE_NAME = "twilight-character.md"

_HTTP_ERRORS = {
    401: "API Key 无效或未通过验证（HTTP 401）。请检查 Key 后重新输入。",
    402: "DeepSeek 账户余额不足（HTTP 402），请充值后再试。",
    403: "访问被拒绝（HTTP 403）。请检查 API Key 的权限或账户状态。",
    404: "接口地址或模型不存在（HTTP 404）。请检查 api-key.ini 中的 base_url 和 model。",
    429: "请求太频繁或额度受限（HTTP 429），请稍后再试。",
}


class CharacterFileError(FileNotFoundError):
    """twilight-character.md could not be found."""


class ApiError(Exception):
    """A user-presentable API failure."""

    def __init__(
        self,
        user_message: str,
        *,
        status: int | None = None,
        detail: str = "",
        model_error: bool = False,
    ) -> None:
        super().__init__(user_message)
        self.user_message = user_message
        self.status = status
        self.detail = detail
        self.model_error = model_error


@dataclass
class Settings:
    api_key: str = ""
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL


@dataclass(frozen=True)
class BootstrapReply:
    content: str
    reasoning: str
    model: str


@dataclass(frozen=True)
class ChatReply:
    content: str
    reasoning: str
    model: str


# ---------------------------------------------------------------------------
# Paths / configuration
# ---------------------------------------------------------------------------
def default_home() -> Path:
    """Runtime data folder; on the frozen build this is the sibling temp/."""
    return paths.data_home()


def config_path(home: Path) -> Path:
    return Path(home) / CONFIG_FILE_NAME


def backup_config_path() -> Path:
    return paths.persistent_dir() / CONFIG_FILE_NAME


def prepare_config(home: Path) -> None:
    """Keep the required temp/api-key.ini while surviving temp deletion.

    The authoritative session file is temp/api-key.ini.  A per-user backup is
    mirrored so that after the app deletes temp/ on exit the next launch can
    recreate the temp file before loading it.
    """
    home = Path(home)
    try:
        home.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    temp_file = config_path(home)
    backup = backup_config_path()
    try:
        if temp_file.is_file():
            if temp_file.stat().st_size > 0:
                shutil.copyfile(temp_file, backup)
            return
        if backup.is_file() and backup.stat().st_size > 0:
            shutil.copyfile(backup, temp_file)
    except OSError as exc:
        LOGGER.warning("Could not synchronise API config: %s", exc)


def ensure_character_file(home: Path) -> Path:
    """Extract the embedded character card into temp/twilight-character.md.

    The card is embedded in the executable as base64; it is written only when
    missing and is removed again as soon as the bootstrap has read it.
    """
    home = Path(home)
    try:
        home.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CharacterFileError(
            f"无法创建临时目录 {home}：{exc.strerror or exc}。"
        ) from exc
    path = home / CHARACTER_FILE_NAME
    try:
        payload = base64.b64decode(character_data.CHARACTER_MD_BASE64)
        path.write_bytes(payload)
    except (OSError, ValueError) as exc:
        raise CharacterFileError(
            f"无法解压角色资料到 {path}：{exc}。"
        ) from exc
    return path


def cleanup_character(home: Path) -> None:
    try:
        (Path(home) / CHARACTER_FILE_NAME).unlink(missing_ok=True)
    except OSError:
        pass


def read_character(home: Path) -> tuple[str, Path]:
    path = ensure_character_file(home)
    text = ""
    try:
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise CharacterFileError(
                f"无法读取角色资料 {CHARACTER_FILE_NAME}：{exc.strerror or exc}。"
            ) from exc
    finally:
        cleanup_character(home)
    text = text.strip()
    if not text:
        raise CharacterFileError("内嵌角色资料为空，请重新启动程序。")
    LOGGER.info("Character card read and removed from %s", path)
    return text, path


def _clean_value(value: str) -> str:
    value = (value or "").strip().strip('"').strip("'").strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    if value.startswith("<") and value.endswith(">"):
        return ""
    return value


def _parse_ini(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    parser = configparser.RawConfigParser(interpolation=None)
    try:
        parser.read(path, encoding="utf-8-sig")
    except (configparser.Error, OSError) as exc:
        LOGGER.warning("Could not parse %s: %s", path, exc)
        try:
            raw = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            raw = ""
        match = re.search(r"sk-[A-Za-z0-9_\-]{8,}", raw)
        return {"api_key": match.group(0)} if match else {}
    result: dict[str, str] = {}
    for section in ("deepseek", "api", "settings"):
        if not parser.has_section(section):
            continue
        for option in ("api_key", "key", "base_url", "model"):
            if parser.has_option(section, option) and option not in result:
                result[option] = parser.get(section, option)
    if "api_key" not in result and "key" in result:
        result["api_key"] = result["key"]
    if "api_key" not in result:
        # Hand-edited bare key file, or a key placed outside a section.
        try:
            raw = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            raw = ""
        match = re.search(r"(^|\s)(sk-[A-Za-z0-9_\-]{8,})", raw)
        if match:
            result["api_key"] = match.group(2)
    return result


def load_settings(home: Path) -> Settings:
    prepare_config(home)
    values = _parse_ini(config_path(home))
    settings = Settings(
        api_key=_clean_value(values.get("api_key", "")),
        base_url=_clean_value(values.get("base_url", "")) or DEFAULT_BASE_URL,
        model=_clean_value(values.get("model", "")) or DEFAULT_MODEL,
    )
    settings.api_key = _clean_value(os.environ.get("TWILIGHT_API_KEY", "") or settings.api_key)
    settings.base_url = _clean_value(
        os.environ.get("TWILIGHT_API_BASE", "") or settings.base_url
    )
    settings.model = _clean_value(os.environ.get("TWILIGHT_MODEL", "") or settings.model)
    return settings


def save_settings(home: Path, settings: Settings) -> None:
    """Persist the API key/config to temp/api-key.ini after a successful check."""
    path = config_path(home)
    home = Path(home)
    try:
        home.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    def one_line(value: str) -> str:
        return (value or "").replace("\r", "").replace("\n", "").strip()

    payload = (
        "; Twilight Harness — DeepSeek API configuration\n"
        "; api_key 会在成功确认后自动写入；也可以手动编辑本文件后重启程序。\n"
        "; base_url 可指向任何兼容 OpenAI Chat Completions 的 DeepSeek 接口。\n"
        "[deepseek]\n"
        f"api_key = {one_line(settings.api_key)}\n"
        f"base_url = {one_line(settings.base_url) or DEFAULT_BASE_URL}\n"
        f"model = {one_line(settings.model) or DEFAULT_MODEL}\n"
    )
    try:
        path.write_text(payload, encoding="utf-8", newline="\n")
    except OSError as exc:
        raise OSError(f"无法写入 {path}：{exc.strerror or exc}") from exc
    try:
        backup = backup_config_path()
        shutil.copyfile(path, backup)
    except OSError as exc:
        LOGGER.warning("Could not persist API config backup: %s", exc)


# ---------------------------------------------------------------------------
# HTTP / SSE client
# ---------------------------------------------------------------------------
def _clean_url(base_url: str) -> str:
    base = (base_url or DEFAULT_BASE_URL).strip().rstrip("/")
    if not base:
        base = DEFAULT_BASE_URL
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def _log_request(url: str) -> None:
    LOGGER.debug("POST %s", url)


class DeepSeekClient:
    """Small OpenAI-compatible chat client with reasoner support."""

    def __init__(self, api_key: str, base_url: str = DEFAULT_BASE_URL, model: str = DEFAULT_MODEL):
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or DEFAULT_BASE_URL).strip()
        self.model = (model or DEFAULT_MODEL).strip()
        self._last_model = self.model

    # -- public API ---------------------------------------------------------
    def bootstrap(self, messages: list[dict[str, str]]) -> BootstrapReply:
        """Non-streaming persona-adoption call used by the setup screen."""
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "max_tokens": 900,
        }
        data, model = self._call_json_with_fallback(payload, timeout=90)
        content = _extract_message_text(data)
        reasoning = _extract_reasoning_text(data)
        self._last_model = model
        self.model = model
        if not content:
            # A reasoner that only returned reasoning is not useful as an opening.
            raise ApiError(
                "模型只返回了思考内容，没有生成开场白。请再次点击“确认”重试。"
            )
        return BootstrapReply(content=content.strip(), reasoning=reasoning.strip(), model=model)

    def stream_chat(
        self,
        messages: list[dict[str, str]],
        on_reasoning: Callable[[str], None] | None = None,
        on_content: Callable[[str], None] | None = None,
        cancel_event=None,
    ) -> ChatReply:
        """Stream one assistant reply. Callbacks are invoked from the worker thread."""
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "max_tokens": 8192,
        }
        last_model_error: ApiError | None = None
        for model in self._candidate_models():
            if cancel_event is not None and cancel_event.is_set():
                return ChatReply("", "", model)
            attempt = dict(payload)
            attempt["model"] = model
            saw_output = False

            def wrapped_reasoning(text: str, _model: str = model) -> None:
                nonlocal saw_output
                saw_output = True
                if on_reasoning is not None:
                    on_reasoning(text)

            def wrapped_content(text: str, _model: str = model) -> None:
                nonlocal saw_output
                saw_output = True
                if on_content is not None:
                    on_content(text)

            try:
                content, reasoning = self._stream_once(
                    attempt,
                    on_reasoning=wrapped_reasoning,
                    on_content=wrapped_content,
                    cancel_event=cancel_event,
                )
            except ApiError as exc:
                if exc.model_error and not saw_output and not (
                    cancel_event is not None and cancel_event.is_set()
                ):
                    last_model_error = exc
                    LOGGER.warning("Model %s rejected; trying fallback. %s", model, exc.detail)
                    continue
                raise
            self._last_model = model
            self.model = model
            if not content.strip() and not reasoning.strip():
                raise ApiError("API 返回了空回复，请稍后重试。")
            return ChatReply(content=content, reasoning=reasoning, model=model)
        if last_model_error is not None:
            raise last_model_error
        raise ApiError("未能连接可用的 DeepSeek 模型，请稍后重试。")

    # -- implementation -----------------------------------------------------
    def _candidate_models(self) -> list[str]:
        result: list[str] = []
        for candidate in (self.model, DEFAULT_MODEL, "deepseek-chat"):
            candidate = (candidate or "").strip()
            if candidate and candidate not in result:
                result.append(candidate)
        return result or [DEFAULT_MODEL]

    def _open(
        self,
        payload: dict,
        *,
        timeout: int,
        stream: bool,
    ):
        if not self.api_key:
            raise ApiError("尚未设置 API Key。")
        url = _clean_url(self.base_url)
        _log_request(url)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "text/event-stream" if stream else "application/json",
            "User-Agent": "TwilightHarness/1.0 (Windows)",
        }
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            context = ssl.create_default_context()
            return urllib.request.urlopen(request, timeout=timeout, context=context)
        except urllib.error.HTTPError as exc:
            raise _error_from_http(exc) from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (ssl.SSLError,)):
                raise ApiError("HTTPS 证书校验失败。请检查网络代理或 base_url 设置。") from exc
            raise ApiError(f"无法连接到 API（{reason}）。请检查网络后重试。") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise ApiError("连接 API 超时。请检查网络后重试。") from exc
        except (ssl.SSLError, OSError) as exc:
            raise ApiError(f"网络连接失败：{exc}。请检查网络后重试。") from exc
        except Exception as exc:  # noqa: BLE001 - present a friendly message instead
            LOGGER.exception("Unexpected HTTP error")
            raise ApiError(f"请求 API 时出错：{exc}") from exc

    def _call_json_with_fallback(self, payload: dict, timeout: int) -> tuple[dict, str]:
        last_model_error: ApiError | None = None
        for model in self._candidate_models():
            attempt = dict(payload)
            attempt["model"] = model
            try:
                response = self._open(attempt, timeout=timeout, stream=False)
            except ApiError as exc:
                if exc.model_error:
                    last_model_error = exc
                    LOGGER.warning("Model %s rejected; trying fallback. %s", model, exc.detail)
                    continue
                raise
            try:
                raw = response.read()
            finally:
                response.close()
            try:
                data = json.loads(raw.decode("utf-8", errors="replace"))
            except json.JSONDecodeError as exc:
                raise ApiError("API 返回了无法解析的响应，请稍后重试。") from exc
            if isinstance(data, dict) and data.get("error"):
                raise _error_from_payload(data["error"])
            choices = data.get("choices") if isinstance(data, dict) else None
            if not choices:
                raise ApiError("API 响应中没有找到回复内容，请稍后重试。")
            return data, model
        if last_model_error is not None:
            raise last_model_error
        raise ApiError("未能连接可用的 DeepSeek 模型，请稍后重试。")

    def _stream_once(
        self,
        payload: dict,
        *,
        on_reasoning: Callable[[str], None] | None,
        on_content: Callable[[str], None] | None,
        cancel_event,
    ) -> tuple[str, str]:
        response = self._open(payload, timeout=180, stream=True)
        reasoning_parts: list[str] = []
        content_parts: list[str] = []
        saw_any_line = False
        saw_sse = False
        raw_prefix = ""
        try:
            for raw_line in response:
                if cancel_event is not None and cancel_event.is_set():
                    break
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                if not saw_any_line:
                    saw_any_line = True
                    raw_prefix = line[:300]
                if line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    continue
                saw_sse = True
                data_text = line[5:].strip()
                if data_text == "[DONE]":
                    break
                if not data_text:
                    continue
                try:
                    chunk = json.loads(data_text)
                except json.JSONDecodeError:
                    continue
                if not isinstance(chunk, dict):
                    continue
                if chunk.get("error"):
                    raise _error_from_payload(chunk["error"])
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                choice = choices[0] or {}
                delta = choice.get("delta") or {}
                message = choice.get("message") or {}
                reason = _first_text(
                    delta.get("reasoning_content"),
                    delta.get("reasoning"),
                    delta.get("thinking"),
                    message.get("reasoning_content"),
                )
                text = _first_text(delta.get("content"), message.get("content"))
                if reason:
                    reasoning_parts.append(reason)
                    if on_reasoning is not None:
                        on_reasoning(reason)
                if text:
                    content_parts.append(text)
                    if on_content is not None:
                        on_content(text)
        finally:
            try:
                response.close()
            except Exception:  # noqa: BLE001
                pass
        if not saw_sse:
            detail = raw_prefix or "（空响应）"
            if detail.startswith("{"):
                try:
                    candidate = json.loads(detail)
                    if isinstance(candidate, dict) and candidate.get("error"):
                        raise _error_from_payload(candidate["error"])
                except json.JSONDecodeError:
                    pass
            raise ApiError(f"API 返回了非流式响应，无法读取回复：{detail[:160]}")
        content = "".join(content_parts)
        reasoning = "".join(reasoning_parts)
        if not content.strip():
            raise ApiError("模型只返回了思考内容，没有生成最终回复。请再试一次。")
        return content, reasoning


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _extract_message_text(data: dict) -> str:
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return ""
    content = message.get("content")
    return _coerce_text(content)


def _extract_reasoning_text(data: dict) -> str:
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return ""
    return _coerce_text(
        _first_text(
            message.get("reasoning_content"),
            message.get("reasoning"),
            message.get("thinking"),
        )
    )


def _first_text(*values) -> str:
    for value in values:
        text = _coerce_text(value)
        if text:
            return text
    return ""


def _coerce_text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        pieces: list[str] = []
        for item in value:
            if isinstance(item, str):
                pieces.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    pieces.append(text)
        return "".join(pieces)
    return ""


def _truncate(text: str, limit: int = 240) -> str:
    text = " ".join((text or "").split())
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _error_from_payload(error) -> ApiError:
    detail = ""
    if isinstance(error, dict):
        detail = str(error.get("message") or error.get("detail") or "")
        code = error.get("code") or error.get("type") or ""
        if code:
            detail = f"{detail} ({code})" if detail else str(code)
    else:
        detail = str(error or "")
    detail = _truncate(detail)
    lowered = detail.lower()
    model_error = "model" in lowered or "模型" in detail
    return ApiError(
        f"API 返回错误：{detail or '未知错误'}",
        detail=detail,
        model_error=model_error,
    )


def _error_from_http(exc: urllib.error.HTTPError) -> ApiError:
    status = int(getattr(exc, "code", 0) or 0)
    try:
        raw = exc.read(65536)
    except Exception:  # noqa: BLE001
        raw = b""
    text = raw.decode("utf-8", errors="replace")
    detail = ""
    try:
        payload = json.loads(text)
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            detail = str(error.get("message") or error.get("detail") or "")
            code = error.get("code") or error.get("type") or ""
            if code:
                detail = f"{detail} ({code})" if detail else str(code)
        elif isinstance(payload, dict) and payload.get("message"):
            detail = str(payload["message"])
    except json.JSONDecodeError:
        detail = _truncate(text)
    detail = _truncate(detail)
    lowered = detail.lower()
    model_error = status in (400, 404, 422) and ("model" in lowered or "模型" in detail)
    message = _HTTP_ERRORS.get(status)
    if message is None:
        if 500 <= status < 600:
            message = f"DeepSeek 服务暂时不可用（HTTP {status}），请稍后再试。"
        elif detail:
            message = f"API 请求未成功（HTTP {status}）：{detail}"
        else:
            message = f"API 请求未成功（HTTP {status}），请稍后重试。"
    if status == 400 and detail:
        message = f"API 请求未成功（HTTP 400）：{detail}"
    return ApiError(message, status=status, detail=detail, model_error=model_error)
