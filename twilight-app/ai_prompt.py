"""DeepSeek natural language -> Stable Diffusion prompt conversion (stage 1).

Asks the configured OpenAI-compatible DeepSeek endpoint for
``{"positive": ..., "negative": ...}`` using stdlib ``urllib`` only.  Transport,
HTTP and JSON failures **never raise**: they degrade to a fallback pair built
from the user text plus the default negative template.  The negative prompt
always ends up containing ``Wrong Anatomy`` (case-insensitive de-duplication).

``DEFAULT_NEGATIVE`` can be overridden through ``settings.default_negative`` or
``ai-config.json`` -> ``prompt.default_negative`` (read lazily; absent config is
not an error).  ``response_format`` is intentionally not sent, so plain
OpenAI-compatible proxies keep working; fenced/annotated JSON is stripped.
What cannot be verified without an API key: real DeepSeek output quality and
latency.  Transport/parse/fallback paths are unit-tested against a local HTTP
server and monkeypatched callables.
"""

from __future__ import annotations

import json
import logging
import re
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping

LOGGER = logging.getLogger("twilight_harness")
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-reasoner"
REQUEST_TIMEOUT = 60
MAX_TOKENS = 800
WRONG_ANATOMY = "Wrong Anatomy"
_WRONG_ANATOMY_LOWER = WRONG_ANATOMY.lower()
DEFAULT_NEGATIVE = (
    "lowres, bad anatomy, bad hands, text, error, cropped, worst quality, low quality, "
    "jpeg artifacts, signature, watermark, username, blurry, extra digits, fewer digits, "
    "Wrong Anatomy"
)
SYSTEM_PROMPT = (
    "你是 Stable Diffusion 1.5 的提示词工程师。用户会用自然语言描述想要的画面，"
    "你只输出一个 JSON 对象，不要输出任何解释或 Markdown。JSON 格式："
    '{"positive": "英文正向提示词", "negative": "英文负向提示词"}。'
    "positive 要具体、适合文生图（主体、构图、光影、画风、质量词）；"
    "negative 要包含常见的低质量与结构错误词，且必须包含短语 Wrong Anatomy。"
)


@dataclass
class PromptPair:
    """Structured prompt pair; ``source`` is ``"deepseek"`` or ``"fallback"``."""

    positive: str
    negative: str
    source: str


def ensure_wrong_anatomy(negative: str | None) -> str:
    """Return *negative* guaranteed to contain ``Wrong Anatomy`` exactly once."""
    text = (negative or "").strip()
    if _WRONG_ANATOMY_LOWER in text.lower():
        return text
    if not text:
        return WRONG_ANATOMY
    separator = " " if text.endswith((",", "，", ";", "；")) else ", "
    return f"{text}{separator}{WRONG_ANATOMY}"


def _setting(settings: Any, key: str, default: Any = "") -> Any:
    if settings is None:
        return default
    if isinstance(settings, Mapping):
        value = settings.get(key, default)
    else:
        value = getattr(settings, key, default)
    return default if value is None else value


def default_negative(settings: Any = None, logger: logging.Logger | None = None) -> str:
    """Configured default negative prompt, guaranteed to contain Wrong Anatomy."""
    logger = logger or LOGGER
    for key in ("default_negative", "negative"):
        value = _setting(settings, key, "")
        if isinstance(value, str) and value.strip():
            return ensure_wrong_anatomy(value.strip())
    try:
        import ai_resources  # lazy: keep this module importable on its own

        block = ai_resources.load_config().get("prompt", {})
        value = str((block or {}).get("default_negative", "") or "").strip()
        if value:
            return ensure_wrong_anatomy(value)
    except Exception as exc:  # noqa: BLE001 - config problems must not break prompts
        logger.debug("读取 AI 配置默认负向提示词失败：%s", exc)
    return ensure_wrong_anatomy(DEFAULT_NEGATIVE)


def _model(settings: Any) -> str:
    value = str(_setting(settings, "model", "") or "").strip()
    if value:
        return value
    try:
        from api_client import DEFAULT_MODEL as client_model  # type: ignore

        return client_model
    except Exception:  # noqa: BLE001
        return DEFAULT_MODEL


def _endpoint(base_url: str) -> str:
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return ""
    return base if base.endswith("/chat/completions") else base + "/chat/completions"


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Parse a JSON object from plain, fenced or prose-wrapped model output."""
    raw = (text or "").strip()
    if not raw:
        return None
    fenced = re.sub(r"^\s*```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    fenced = re.sub(r"\s*```\s*$", "", fenced).strip()
    candidates = [fenced]
    start, end = fenced.find("{"), fenced.rfind("}")
    if start != -1 and end > start:
        candidates.append(fenced[start : end + 1])
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict):
            return data
    return None


def _response_content(payload: Any) -> str:
    """Extract assistant content from an OpenAI-compatible completion dict."""
    try:
        choice = payload["choices"][0]
        content = (choice.get("message") or {}).get("content")
        if content is None and isinstance(choice.get("text"), str):
            content = choice["text"]
        return content if isinstance(content, str) else ""
    except (KeyError, IndexError, TypeError, AttributeError):
        return ""


def _request_chat(natural_language: str, settings: Any, logger: logging.Logger) -> str | None:
    """POST /chat/completions and return assistant text (``None`` on any error)."""
    api_key = str(_setting(settings, "api_key", "") or "").strip()
    url = _endpoint(str(_setting(settings, "base_url", "") or "").strip())
    if not api_key or not url:
        logger.info("未配置 DeepSeek API Key 或 base_url，提示词转换使用回退模板")
        return None
    payload = {
        "model": _model(settings),
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "把下面的自然语言描述转换为 Stable Diffusion 1.5 的 JSON 正向/负向提示词。\n"
                    f"描述：{natural_language}\n只输出 JSON 对象。"
                ),
            },
        ],
        "stream": False,
        "temperature": 0.4,
        "max_tokens": MAX_TOKENS,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json",
        "User-Agent": "TwilightHarness/1.0 (Windows)",
    }
    request = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers=headers, method="POST"
    )
    try:
        context = ssl.create_default_context()
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT, context=context) as response:
            raw = response.read()
        data = json.loads(raw.decode("utf-8", errors="replace"))
        if isinstance(data, dict) and data.get("error"):
            logger.warning("DeepSeek 提示词转换返回错误：%s", data.get("error"))
            return None
        content = _response_content(data).strip()
        if not content:
            logger.warning("DeepSeek 提示词转换返回空内容")
            return None
        return content
    except urllib.error.HTTPError as exc:
        logger.warning("DeepSeek 提示词转换失败（HTTP %s）", exc.code)
    except urllib.error.URLError as exc:
        logger.warning("DeepSeek 提示词转换连接失败：%s", getattr(exc, "reason", exc))
    except (TimeoutError, OSError, ssl.SSLError) as exc:
        logger.warning("DeepSeek 提示词转换网络异常：%s", exc)
    except (ValueError, TypeError) as exc:
        logger.warning("DeepSeek 提示词转换响应无法解析：%s", exc)
    except Exception as exc:  # noqa: BLE001 - never raise for a prompt conversion
        logger.warning("DeepSeek 提示词转换出现未预期错误：%s", exc)
    return None


def _log_pair(pair: PromptPair, logger: logging.Logger) -> None:
    logger.info("提示词结果（source=%s）：positive=%s | negative=%s", pair.source, pair.positive, pair.negative)


def build_prompt(
    natural_language: str,
    settings: Any = None,
    logger: logging.Logger | None = None,
) -> PromptPair:
    """Convert natural language into a prompt pair; never raises.

    With a usable ``api_key`` + ``base_url`` the DeepSeek endpoint is asked for
    JSON; every transport/parse/validation failure falls back to the user text
    and :func:`default_negative` (which always contains ``Wrong Anatomy``).
    """
    logger = logger or LOGGER
    text = (natural_language or "").strip()
    negative_default = default_negative(settings, logger)
    fallback = PromptPair(positive=text, negative=negative_default, source="fallback")
    if not text:
        logger.warning("自然语言描述为空，提示词转换使用回退模板")
        _log_pair(fallback, logger)
        return fallback
    try:
        content = _request_chat(text, settings, logger)
        data = _extract_json_object(content) if content else None
        if data is None:
            if content is not None:
                logger.warning("DeepSeek 提示词不是合法 JSON，使用回退模板")
            _log_pair(fallback, logger)
            return fallback
        positive = str(data.get("positive", "") or "").strip()
        negative = str(data.get("negative", "") or "").strip()
        if not positive:
            logger.warning("DeepSeek 返回的 positive 为空，使用回退模板")
            _log_pair(fallback, logger)
            return fallback
        pair = PromptPair(
            positive=positive,
            negative=ensure_wrong_anatomy(negative or negative_default),
            source="deepseek",
        )
        _log_pair(pair, logger)
        return pair
    except Exception as exc:  # noqa: BLE001 - last-resort guard, always degrade
        logger.warning("提示词转换异常，使用回退模板：%s", exc)
        _log_pair(fallback, logger)
        return fallback


def deepseek_to_prompt(natural_language: str, settings: Any = None) -> PromptPair:
    """App-facing alias for :func:`build_prompt` (default logger/config)."""
    return build_prompt(natural_language, settings)
