"""AI resource layout, configuration and requirement checks (stage 1).

Model weights are **absent by design**: every helper reports what is missing
through :class:`MissingModels` with the exact Chinese message
``未找到模型 {name}，路径 {path}；请放置权重或修改配置`` instead of crashing.

Layout under ``AI_HOME`` (env ``TWILIGHT_AI_HOME``, else
``%LOCALAPPDATA%\\TwilightHarness\\ai``)::

    ai-config.json
    models/sd15/{unet_int8.onnx, vae_fp16.onnx, text_encoder_int8.onnx,
                 tokenizer.json} + lora/ training_images/ quantized/
    models/gpt_sovits/{gpt_int8.onnx, sovits_int8.onnx, bert.onnx, hubert.onnx}
                 + reference_audio/ quantized/
    models/uvr/, models/whisper/, training/{images,audio}/, refs/, cache/,
    runtime/, outputs/{images,audio}/

Read-only embedded roots (``sys._MEIPASS/ai-models``, repo ``assets/ai-models``)
are searched after the writable layout, in both ``<root>/<section>/<file>`` and
``<root>/models/<section>/<file>`` shapes.  ``ai-config.json`` is deep-merged
over :data:`DEFAULT_CONFIG`; a corrupt file is copied to
``ai-config.json.corrupt-<timestamp>`` and defaults are used.  The whisper
default model name ``ggml-base-q5_1.bin`` is an assumption: any
``*q5_1*.bin``/``ggml-*.bin`` in ``models/whisper/`` is accepted.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Sequence

LOGGER = logging.getLogger("twilight_harness")
APP_NAME = "TwilightHarness"
AI_HOME_ENV = "TWILIGHT_AI_HOME"
CONFIG_FILE_NAME = "ai-config.json"
DEFAULT_NEGATIVE = (
    "lowres, bad anatomy, bad hands, text, error, cropped, worst quality, low quality, "
    "jpeg artifacts, signature, watermark, username, blurry, extra digits, fewer digits, "
    "Wrong Anatomy"
)
DEFAULT_SD15_FILES = {
    "unet": "unet_int8.onnx",
    "vae": "vae_fp16.onnx",
    "text_encoder": "text_encoder_int8.onnx",
    "tokenizer": "tokenizer.json",
}
DEFAULT_UVR_MODEL = "uvr_mdx_net_int8.onnx"
DEFAULT_WHISPER_MODEL = "ggml-base-q5_1.bin"  # assumption; any *q5_1*.bin also works
DEFAULT_GPT_SOVITS_FILES = {
    "gpt": "gpt_int8.onnx",
    "sovits": "sovits_int8.onnx",
    "bert": "bert.onnx",
    "hubert": "hubert.onnx",
}
DEFAULT_CONFIG: dict[str, Any] = {
    "sd15": {**DEFAULT_SD15_FILES, "model_dir": "", "lora": "", "io": {}},
    "gpt_sovits": {**DEFAULT_GPT_SOVITS_FILES, "model_dir": "", "command": ""},
    "uvr": {"model": DEFAULT_UVR_MODEL, "segment": 256, "overlap": 0.25, "stft": 2048},
    "whisper": {"exe": "", "model": "", "language": "auto"},
    "runtime": {"backend": "auto"},
    "prompt": {"default_negative": DEFAULT_NEGATIVE},
    "training": {"images_dir": "", "audio_dir": ""},
}
CONFIG_FIELDS = tuple(DEFAULT_CONFIG)
LAYOUT_DIRS = (
    "models/sd15", "models/sd15/lora", "models/sd15/training_images",
    "models/sd15/quantized", "models/gpt_sovits",
    "models/gpt_sovits/reference_audio", "models/gpt_sovits/quantized",
    "models/uvr", "models/whisper", "training/images", "training/audio",
    "refs", "cache", "outputs/images", "outputs/audio", "runtime",
)
#: Reserved slots that later stages fill with user data; keys are stable.
RESERVED_SLOTS = {
    "sd15_training_images": "models/sd15/training_images",
    "sd15_lora": "models/sd15/lora",
    "gpt_sovits_reference_audio": "models/gpt_sovits/reference_audio",
    "training_images": "training/images",
    "training_audio": "training/audio",
    "runtime_packs": "runtime",
}
IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"})
AUDIO_EXTENSIONS = frozenset({".wav", ".mp3", ".flac", ".m4a", ".ogg", ".aac", ".wma", ".opus"})
_ENV_MODEL_OVERRIDES = {
    ("uvr", "model"): "TWILIGHT_UVR_MODEL",
    ("whisper", "model"): "TWILIGHT_WHISPER_MODEL",
    ("whisper", "exe"): "TWILIGHT_WHISPER_EXE",
}
_ENV_DIR_OVERRIDES = {"sd15": "TWILIGHT_SD15_DIR", "gpt_sovits": "TWILIGHT_GPT_SOVITS_DIR"}
_WHISPER_EXE_NAMES = ("whisper-cli.exe", "whisper.exe", "main.exe")


# -- paths ------------------------------------------------------------------
def ai_home() -> Path:
    """Writable AI data root (``TWILIGHT_AI_HOME`` or LOCALAPPDATA default)."""
    override = os.environ.get(AI_HOME_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    local = os.environ.get("LOCALAPPDATA", "").strip()
    base = Path(local) if local else Path.home() / "AppData" / "Local"
    return base / APP_NAME / "ai"


def config_file() -> Path:
    return ai_home() / CONFIG_FILE_NAME


def model_roots() -> list[Path]:
    """Ordered, de-duplicated roots that may contain model folders."""
    roots: list[Path] = []

    def add(path: Any) -> None:
        try:
            candidate = Path(path)
        except (TypeError, ValueError):
            return
        if candidate not in roots:
            roots.append(candidate)

    add(ai_home() / "models")
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        add(Path(meipass) / "ai-models")
    add(Path(__file__).resolve().parent.parent / "assets" / "ai-models")
    return roots


def models_dir() -> Path:
    return ai_home() / "models"


def outputs_dir(kind: str = "images") -> Path:
    return ai_home() / "outputs" / (kind or "images")


def _resolve_path(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (ai_home() / path)


def _dir_override(section: str, config: "AIConfig | None") -> Path | None:
    env_name = _ENV_DIR_OVERRIDES.get(section, "")
    if env_name and os.environ.get(env_name, "").strip():
        return _resolve_path(os.environ[env_name].strip())
    if config is not None:
        value = str((getattr(config, section, {}) or {}).get("model_dir", "") or "").strip()
        if value:
            return _resolve_path(value)
    return None


def sd15_dir(config: "AIConfig | None" = None) -> Path:
    return _dir_override("sd15", config) or (ai_home() / "models" / "sd15")


def gpt_sovits_dir(config: "AIConfig | None" = None) -> Path:
    return _dir_override("gpt_sovits", config) or (ai_home() / "models" / "gpt_sovits")


def uvr_dir() -> Path:
    return ai_home() / "models" / "uvr"


def whisper_dir() -> Path:
    return ai_home() / "models" / "whisper"


def reference_audio_dir(config: "AIConfig | None" = None) -> Path:
    return gpt_sovits_dir(config) / "reference_audio"


def training_images_dir(config: "AIConfig | None" = None) -> Path:
    value = str(((config.training if config else {}) or {}).get("images_dir", "") or "").strip()
    return _resolve_path(value) if value else (ai_home() / "training" / "images")


def training_audio_dir(config: "AIConfig | None" = None) -> Path:
    value = str(((config.training if config else {}) or {}).get("audio_dir", "") or "").strip()
    return _resolve_path(value) if value else (ai_home() / "training" / "audio")


def __getattr__(name: str) -> Any:
    """Expose ``AI_HOME`` dynamically so importers see the current environment."""
    if name == "AI_HOME":
        return ai_home()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# -- missing models ---------------------------------------------------------
def missing_model_line(name: str, path: Any) -> str:
    """Exact one-line Chinese missing-model message."""
    return f"未找到模型 {name}，路径 {path}；请放置权重或修改配置"


class MissingModels(RuntimeError):
    """Required model file(s) are absent; ``.missing`` lines join into ``.user_message``."""

    def __init__(
        self,
        missing: Sequence[object] | str | None = None,
        user_message: str | Sequence[object] | None = None,
    ) -> None:
        lines = _expand_missing(missing) + _expand_missing(user_message)
        self.missing: list[str] = lines or ["未找到所需模型，请放置权重或修改配置"]
        self.user_message = "\n".join(self.missing)
        super().__init__(self.user_message)


def _expand_missing(source: Any) -> list[str]:
    if source is None:
        return []
    if isinstance(source, str):
        return [line.strip() for line in source.splitlines() if line.strip()]
    try:
        items = list(source)
    except TypeError:
        return [str(source)]
    lines: list[str] = []
    for item in items:
        if isinstance(item, str):
            lines.extend(line.strip() for line in item.splitlines() if line.strip())
        elif isinstance(item, (tuple, list)) and len(item) == 2:
            lines.append(missing_model_line(str(item[0]), item[1]))
        elif hasattr(item, "name") and hasattr(item, "path"):
            lines.append(missing_model_line(str(item.name), item.path))
        else:
            lines.append(str(item))
    return lines


# -- configuration ----------------------------------------------------------
def _fresh_defaults(section: str) -> dict[str, Any]:
    return copy.deepcopy(DEFAULT_CONFIG[section])


@dataclass
class AIConfig:
    """``ai-config.json`` value object with attribute **and** dict-like access."""

    sd15: dict[str, Any] = field(default_factory=lambda: _fresh_defaults("sd15"))
    gpt_sovits: dict[str, Any] = field(default_factory=lambda: _fresh_defaults("gpt_sovits"))
    uvr: dict[str, Any] = field(default_factory=lambda: _fresh_defaults("uvr"))
    whisper: dict[str, Any] = field(default_factory=lambda: _fresh_defaults("whisper"))
    runtime: dict[str, Any] = field(default_factory=lambda: _fresh_defaults("runtime"))
    prompt: dict[str, Any] = field(default_factory=lambda: _fresh_defaults("prompt"))
    training: dict[str, Any] = field(default_factory=lambda: _fresh_defaults("training"))
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {name: getattr(self, name) for name in CONFIG_FIELDS}
        for key, value in self.extra.items():
            result.setdefault(key, value)
        return result

    def get(self, key: str, default: Any = None) -> Any:
        if key in CONFIG_FIELDS:
            return getattr(self, key)
        return self.extra.get(key, default)

    def __getitem__(self, key: str) -> Any:
        if key in CONFIG_FIELDS:
            return getattr(self, key)
        return self.extra[key]

    def __contains__(self, key: object) -> bool:
        return key in CONFIG_FIELDS or key in self.extra

    def __iter__(self) -> Iterator[str]:
        return iter(self.as_dict())


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _from_mapping(data: dict[str, Any]) -> AIConfig:
    merged = _deep_merge(copy.deepcopy(DEFAULT_CONFIG), data if isinstance(data, dict) else {})
    kwargs: dict[str, Any] = {}
    for name in CONFIG_FIELDS:
        value = merged.pop(name, None)
        if not isinstance(value, dict):
            if value is not None:
                LOGGER.warning("AI 配置字段 %s 类型错误，已使用默认值", name)
            value = copy.deepcopy(DEFAULT_CONFIG[name])
        kwargs[name] = value
    return AIConfig(**kwargs, extra=merged)


def load_config(path: str | Path | None = None) -> AIConfig:
    """Load ``ai-config.json`` deep-merged over defaults; never raises."""
    config_path = Path(path) if path is not None else config_file()
    data: Any = {}
    try:
        if config_path.is_file():
            raw = config_path.read_text(encoding="utf-8")
            data = json.loads(raw) if raw.strip() else {}
            if not isinstance(data, dict):
                raise ValueError("root must be a JSON object")
    except (OSError, ValueError, TypeError) as exc:
        LOGGER.warning("AI 配置读取失败：%s（%s）；已备份并使用默认值", config_path, exc)
        _backup_corrupt_config(config_path)
        data = {}
    return _from_mapping(data if isinstance(data, dict) else {})


def _backup_corrupt_config(path: Path) -> None:
    try:
        backup = path.with_name(f"{path.name}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}")
        backup.write_bytes(path.read_bytes())
        LOGGER.warning("损坏的 AI 配置已备份到 %s", backup)
    except OSError as exc:
        LOGGER.warning("AI 配置备份失败：%s", exc)


def save_config(config: AIConfig | None = None, path: str | Path | None = None) -> Path:
    """Persist the config (defaults + overrides + unknown keys) as UTF-8 JSON."""
    target = Path(path) if path is not None else config_file()
    payload = (config or load_config()).as_dict()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, target)
    except OSError as exc:
        LOGGER.warning("AI 配置保存失败：%s（%s）", target, exc)
        raise
    return target


def ensure_layout() -> AIConfig:
    """Create every reserved AI folder and return the loaded configuration."""
    config = load_config()
    for relative in LAYOUT_DIRS:
        try:
            (ai_home() / relative).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            LOGGER.warning("无法创建 AI 目录 %s：%s", relative, exc)
    return config


# -- model file resolution --------------------------------------------------
def _configured_value(config: AIConfig, section: str, key: str, env_name: str | None) -> str:
    if env_name and os.environ.get(env_name, "").strip():
        return os.environ[env_name].strip()
    return str((getattr(config, section, {}) or {}).get(key, "") or "").strip()


def _find_model(
    config: AIConfig, section: str, key: str, default_name: str, env_name: str | None = None
) -> tuple[Path | None, Path]:
    """Find one model file; return ``(found_or_None, expected_path)``."""
    section_dir = sd15_dir(config) if section == "sd15" else (
        gpt_sovits_dir(config) if section == "gpt_sovits" else (ai_home() / "models" / section)
    )
    configured = _configured_value(config, section, key, env_name)
    expected: Path | None = None
    if configured:
        raw = Path(configured).expanduser()
        candidate = (section_dir / raw.name) if raw.parent == Path(".") else _resolve_path(configured)
        if candidate.is_file():
            return candidate, candidate
        expected = candidate
    layout_candidate = section_dir / default_name
    if expected is None:
        expected = layout_candidate
    writable_root = ai_home() / "models"
    candidates = [layout_candidate]
    for root in model_roots():
        if root != writable_root:
            candidates.extend((root / section / default_name, root / "models" / section / default_name))
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate, candidate
        except OSError:
            continue
    return None, expected


def _require_group(section: str, items: Sequence[tuple[str, str, str | None]]) -> dict[str, Path]:
    config = load_config()
    resolved: dict[str, Path] = {}
    missing: list[tuple[str, Path]] = []
    for key, default_name, env_name in items:
        found, expected = _find_model(config, section, key, default_name, env_name)
        if found is None:
            missing.append((expected.name, expected))
        else:
            resolved[key] = found
    if missing:
        raise MissingModels(missing)
    return resolved


# -- requirement helpers (exact Chinese messages) ---------------------------
def require_sd15() -> dict[str, Any]:
    """Required SD1.5 INT8 files: UNet, VAE, text encoder, tokenizer."""
    config = load_config()
    resolved: dict[str, Any] = _require_group(
        "sd15",
        [(key, name, None) for key, name in DEFAULT_SD15_FILES.items()],
    )
    resolved["dir"] = sd15_dir(config)
    resolved["lora"] = find_lora(config)
    return resolved


def require_uvr() -> dict[str, Path]:
    """Required UVR MDX-Net INT8 separation model."""
    config = load_config()
    found, expected = _find_model(
        config, "uvr", "model", DEFAULT_UVR_MODEL, _ENV_MODEL_OVERRIDES[("uvr", "model")]
    )
    if found is None:
        raise MissingModels([(expected.name, expected)])
    return {"model": found, "dir": uvr_dir()}


def require_whisper() -> dict[str, Any]:
    """Required whisper.cpp Q5_1 ggml model; the CLI executable is optional."""
    config = load_config()
    found, expected = _find_model(
        config, "whisper", "model", DEFAULT_WHISPER_MODEL, _ENV_MODEL_OVERRIDES[("whisper", "model")]
    )
    if found is None:
        for pattern in ("*q5_1*.bin", "ggml-*.bin"):
            matches = sorted(whisper_dir().glob(pattern))
            if matches:
                found = matches[0]
                break
    if found is None:
        raise MissingModels([(expected.name, expected)])
    return {"model": found, "exe": _optional_whisper_exe(config), "dir": whisper_dir()}


def _optional_whisper_exe(config: AIConfig) -> Path | None:
    configured = _configured_value(config, "whisper", "exe", _ENV_MODEL_OVERRIDES[("whisper", "exe")])
    if configured:
        candidate = _resolve_path(configured)
        if candidate.is_file():
            return candidate
        LOGGER.warning("已配置的 whisper.cpp 可执行文件不存在：%s", candidate)
        return None
    for name in _WHISPER_EXE_NAMES:
        candidate = whisper_dir() / name
        if candidate.is_file():
            return candidate
    return None


def find_reference_audio(config: AIConfig | None = None) -> list[Path]:
    """Audio files available as GPT-SoVITS voice-clone references."""
    config = config if config is not None else load_config()
    writable_root = ai_home() / "models"
    folders = [reference_audio_dir(config)]
    for root in model_roots():
        if root != writable_root:
            folders.extend((root / "gpt_sovits" / "reference_audio",
                            root / "models" / "gpt_sovits" / "reference_audio"))
    found: list[Path] = []
    for folder in folders:
        try:
            files = sorted(
                path for path in folder.iterdir()
                if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS
            )
        except OSError:
            continue
        found.extend(path for path in files if path not in found)
    return found


def require_gpt_sovits() -> dict[str, Any]:
    """Required GPT-SoVITS v2 INT8 files; reference audio is optional."""
    config = load_config()
    resolved: dict[str, Any] = _require_group(
        "gpt_sovits",
        [(key, name, None) for key, name in DEFAULT_GPT_SOVITS_FILES.items()],
    )
    resolved["reference_audio"] = find_reference_audio(config)
    resolved["dir"] = gpt_sovits_dir(config)
    resolved["command"] = str(config.gpt_sovits.get("command", "") or "").strip()
    return resolved


def reference_audio_status(config: AIConfig | None = None) -> str:
    """One-line Chinese status for the reserved reference-audio slot."""
    count = len(find_reference_audio(config))
    return f"参考音频可用：{count} 个" if count else "参考音频缺失，克隆音色已禁用"


def require_reference_audio(config: AIConfig | None = None) -> Path:
    """First usable reference audio or ``MissingModels`` with the locked text."""
    files = find_reference_audio(config)
    if not files:
        raise MissingModels(user_message="参考音频缺失，克隆音色已禁用")
    return files[0]


# -- LoRA / training data / status helpers ----------------------------------
def _lora_dirs(config: AIConfig | None = None) -> list[Path]:
    writable_root = ai_home() / "models"
    folders = [sd15_dir(config) / "lora"]
    for root in model_roots():
        if root != writable_root:
            folders.extend((root / "sd15" / "lora", root / "models" / "sd15" / "lora"))
    return folders


def find_lora(config: AIConfig | None = None) -> Path | None:
    """First ``*.safetensors`` LoRA in the reserved slot, if any."""
    config = config if config is not None else load_config()
    configured = str(((config.sd15 if config else {}) or {}).get("lora", "") or "").strip()
    if configured:
        candidate = _resolve_path(configured)
        if candidate.is_file():
            return candidate
    for folder in _lora_dirs(config):
        try:
            matches = sorted(folder.glob("*.safetensors"))
        except OSError:
            continue
        if matches:
            return matches[0]
    return None


def lora_status(config: AIConfig | None = None) -> str:
    """One-line Chinese LoRA status; the missing text is locked by the spec."""
    lora = find_lora(config)
    if lora is None:
        return "LoRA 未找到，SD1.5 将仅加载基座（LoRA 未启用）"
    return f"LoRA 已找到：{lora.name}"


def _list_files(folder: Path, extensions: frozenset[str]) -> list[Path]:
    try:
        return sorted(
            path for path in folder.iterdir()
            if path.is_file() and path.suffix.lower() in extensions
        )
    except OSError:
        return []


def list_training_images(config: AIConfig | None = None) -> list[Path]:
    return _list_files(training_images_dir(config), IMAGE_EXTENSIONS)


def list_training_audio(config: AIConfig | None = None) -> list[Path]:
    return _list_files(training_audio_dir(config), AUDIO_EXTENSIONS)


def check_training_data(config: AIConfig | None = None) -> list[str]:
    """Return Chinese problems for empty training folders; empty list = ok."""
    config = config if config is not None else load_config()
    problems: list[str] = []
    if not list_training_images(config):
        problems.append(f"训练用图文件夹为空：{training_images_dir(config)}")
    if not list_training_audio(config):
        problems.append(f"训练音频文件夹为空：{training_audio_dir(config)}")
    return problems
