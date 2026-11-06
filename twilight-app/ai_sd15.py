"""Stable Diffusion 1.5 INT8 ONNX text-to-image adapter (stage 1).

Weights are **not bundled**: :meth:`SD15Engine.generate` raises
``ai_resources.MissingModels`` (exact Chinese message) instead of crashing, and
importing this module never imports onnxruntime/tokenizers (both lazy).

Locked parameters: ``SAMPLER=EulerAncestralDiscreteScheduler``, ``STEPS=25``,
``CFG_SCALE=8.0``, ``CPU_OFFLOAD=True``, ``STRICT_CPU_OFFLOAD=True``, default
size ``960x720``.  Only the size may be overridden; ``sampler``/``steps``/
``cfg_scale``/``cpu_offload`` attempts log a warning and are ignored.

Assumptions / implementation notes (unverified without real weights)
--------------------------------------------------------------------
* ORT tensor names follow optimum-diffusers defaults (text encoder
  ``input_ids``/``attention_mask`` -> ``last_hidden_state``; UNet ``sample``/
  ``timestep``/``encoder_hidden_states`` -> ``out_sample``; VAE
  ``latent_sample`` -> ``sample``), overridable via ``ai-config.json`` ->
  ``sd15.io``.
* Euler-Ancestral follows ``diffusers.EulerAncestralDiscreteScheduler.step``
  (epsilon prediction): ``x0 = x - sigma*eps``; ``sigma_up =
  min(sigma_next, sqrt(sigma_next^2*(sigma^2-sigma_next^2)/sigma^2))``;
  ``sigma_down = sqrt(sigma_next^2 - sigma_up^2)``; ``x_next = x + eps *
  (sigma_down - sigma) + noise*sigma_up`` (no noise when ``sigma_next == 0``).
* Karras sigma schedule ``linspace(sigma_max^(1/rho), sigma_min^(1/rho),
  steps)^rho + [0]`` with sigma_max=14.6146, sigma_min=0.0292, rho=7.0
  (diffusers ``use_karras_sigmas`` formulation; the plain beta-interpolated
  schedule is not reproduced because stage 1 fixes the sigma endpoints).
* UNet input = ``latents / sqrt(1 + sigma^2)``; discrete timesteps are derived
  from the SD1.5 scaled-linear beta schedule (1000 train steps).
* CFG runs as one batch of two (uncond first, cond second); if the exported
  graph rejects batch 2 it automatically falls back to two single calls.
* VAE input = ``final_latents / 0.18215`` and output is assumed in ``[-1, 1]``
  (``sd15.vae_output_range = "zero_one"`` for the other convention).

Still unverified (needs real weights): image quality, dtypes/dynamic-batch
support of actual exported graphs, and the exact timestep convention of a given
SD1.5 ONNX pack.
"""

from __future__ import annotations

import copy
import functools
import logging
import math
import os
import time
from pathlib import Path
from typing import Any, Callable

import numpy as np

LOGGER = logging.getLogger("twilight_harness")
SAMPLER = "EulerAncestralDiscreteScheduler"
STEPS = 20
CFG_SCALE = 8.0
CPU_OFFLOAD = True
STRICT_CPU_OFFLOAD = True
DEFAULT_SIZE = (960, 720)  # (width, height)
SIGMA_MAX = 14.6146
SIGMA_MIN = 0.0292
SCHEDULER_RHO = 7.0
NUM_TRAIN_TIMESTEPS = 1000
LATENT_CHANNELS = 4
LATENT_SCALE = 8
VAE_SCALE_FACTOR = 0.18215
TOKENIZER_MAX_LENGTH = 77
LOCKED_OVERRIDES = (
    "sampler", "steps", "num_inference_steps", "cfg_scale", "guidance_scale",
    "cpu_offload", "strict_cpu_offload",
)
_MIN_SIZE, _MAX_SIZE = 128, 2048
_CPU_OFFLOAD_STRICT_MESSAGE = "当前后端不支持 CPU Offload，已停止本次推理（strict_cpu_offload=true）"
_ONNX_BACKENDS = frozenset({"cpu", "cuda", "rocm", "directml"})
_PROVIDER_NAMES = {
    "cuda": "CUDAExecutionProvider",
    "rocm": "ROCMExecutionProvider",
    "directml": "DmlExecutionProvider",
    "cpu": "CPUExecutionProvider",
}
DEFAULT_IO: dict[str, dict[str, str]] = {
    "text_encoder": {
        "input_ids": "input_ids",
        "attention_mask": "attention_mask",
        "output": "last_hidden_state",
    },
    "unet": {
        "sample": "sample",
        "timestep": "timestep",
        "encoder_hidden_states": "encoder_hidden_states",
        "output": "out_sample",
        "timestep_dtype": "int64",
    },
    "vae_decoder": {"latent_sample": "latent_sample", "output": "sample"},
}


class BackendUnavailable(RuntimeError):
    """The selected inference backend cannot run this request."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.user_message = message


# -- lazy imports / backend errors ------------------------------------------
def _import_ai_resources():
    try:
        import ai_resources
    except ImportError as exc:  # pragma: no cover - ships next to this module
        raise RuntimeError("缺少 ai_resources 模块，无法读取 SD1.5 资源") from exc
    return ai_resources


def _import_ai_compat():
    try:
        import ai_compat  # type: ignore
    except Exception:  # noqa: BLE001 - optional until the compat layer ships
        return None
    return ai_compat


def _import_tokenizers():
    try:
        import tokenizers  # type: ignore
    except Exception:  # noqa: BLE001
        return None
    return tokenizers


def _backend_unavailable(message: str) -> Exception:
    compat = _import_ai_compat()
    error_type = getattr(compat, "BackendUnavailable", None) if compat is not None else None
    if isinstance(error_type, type):
        try:
            return error_type(message)
        except Exception:  # noqa: BLE001
            pass
    return BackendUnavailable(message)


def _missing_error(pairs: list[tuple[str, Path]]) -> Exception:
    try:
        return _import_ai_resources().MissingModels(pairs)
    except Exception:  # noqa: BLE001
        return FileNotFoundError("; ".join(f"{name}: {path}" for name, path in pairs))


# -- locked parameter handling ----------------------------------------------
def _split_size(value: str) -> list[str]:
    return value.replace("×", "x").replace("*", "x").replace(",", "x").split("x")


def _parse_size(user_size: Any) -> tuple[int, int] | None:
    if isinstance(user_size, str):
        parts = [part for part in _split_size(user_size) if part.strip()]
        if len(parts) != 2:
            return None
        try:
            return int(parts[0]), int(parts[1])
        except ValueError:
            return None
    if isinstance(user_size, (tuple, list)) and len(user_size) == 2:
        try:
            width, height = int(user_size[0]), int(user_size[1])
            if float(user_size[0]) != width or float(user_size[1]) != height:
                return None
            return width, height
        except (TypeError, ValueError):
            return None
    return None


def resolve_size(user_size: Any = None, logger: logging.Logger | None = None) -> tuple[int, int]:
    """Resolve the size override (width, height); invalid values use the default."""
    logger = logger or LOGGER
    if user_size is None:
        return DEFAULT_SIZE
    parsed = _parse_size(user_size)
    if parsed is None:
        logger.warning("尺寸参数无效：%r，已使用默认尺寸 %dx%d", user_size, *DEFAULT_SIZE)
        return DEFAULT_SIZE
    width, height = parsed
    if width % 8 or height % 8 or not (_MIN_SIZE <= width <= _MAX_SIZE) or not (_MIN_SIZE <= height <= _MAX_SIZE):
        logger.warning(
            "尺寸参数不受支持：%r（需为 8 的倍数且位于 %d..%d），已使用默认尺寸 %dx%d",
            user_size, _MIN_SIZE, _MAX_SIZE, *DEFAULT_SIZE,
        )
        return DEFAULT_SIZE
    return width, height


def reject_locked_overrides(overrides: dict[str, Any] | None, logger: logging.Logger | None = None) -> dict[str, Any]:
    """Warn about and drop locked overrides; nothing is ever applied (returns ``{}``)."""
    logger = logger or LOGGER
    for key, value in dict(overrides or {}).items():
        if str(key).strip().lower() in LOCKED_OVERRIDES:
            logger.warning(
                "推理参数 %s=%r 已被锁定，忽略用户覆盖（sampler=%s, steps=%d, cfg_scale=%.1f, cpu_offload=%s）",
                key, value, SAMPLER, STEPS, CFG_SCALE, CPU_OFFLOAD,
            )
        else:
            logger.warning("未知推理参数 %s=%r 已忽略（SD1.5 仅允许覆盖 size）", key, value)
    return {}


# -- Euler-Ancestral scheduler (numpy) --------------------------------------
def euler_ancestral_sigmas(
    steps: int = STEPS,
    sigma_max: float = SIGMA_MAX,
    sigma_min: float = SIGMA_MIN,
    rho: float = SCHEDULER_RHO,
) -> np.ndarray:
    """Karras-style sigma schedule: ``steps + 1`` values ending with ``0.0``."""
    if steps < 1:
        raise ValueError("steps 必须至少为 1")
    if sigma_max <= 0 or sigma_min <= 0 or sigma_min >= sigma_max or rho <= 0:
        raise ValueError("sigma_max/sigma_min/rho 参数无效")
    inverse = np.linspace(sigma_max ** (1.0 / rho), sigma_min ** (1.0 / rho), steps, dtype=np.float64)
    return np.concatenate([inverse ** rho, np.zeros(1, dtype=np.float64)]).astype(np.float32)


def euler_ancestral_step(
    sample: np.ndarray,
    model_output: np.ndarray,
    sigma: float,
    sigma_next: float,
    noise: np.ndarray | None = None,
) -> np.ndarray:
    """One Euler-Ancestral update (epsilon prediction, diffusers math)."""
    current = np.asarray(sample, dtype=np.float32)
    epsilon = np.asarray(model_output, dtype=np.float32)
    sigma_from, sigma_to = float(sigma), float(sigma_next)
    if sigma_from <= 0:
        return current.copy()
    if sigma_to <= 0:
        return (current - sigma_from * epsilon).astype(np.float32)  # predicted x0
    sigma_up = min(
        sigma_to, math.sqrt(max(sigma_to ** 2 * (sigma_from ** 2 - sigma_to ** 2) / sigma_from ** 2, 0.0))
    )
    sigma_down = math.sqrt(max(sigma_to ** 2 - sigma_up ** 2, 0.0))
    previous = current + epsilon * (sigma_down - sigma_from)
    if noise is not None and sigma_up > 0:
        previous = previous + np.asarray(noise, dtype=np.float32) * np.float32(sigma_up)
    return previous.astype(np.float32)


def sample_euler_ancestral(
    latents: np.ndarray,
    sigmas: np.ndarray,
    eps_fn: Callable[[np.ndarray, int], np.ndarray],
    rng: np.random.Generator | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> np.ndarray:
    """Run the sampler; ``eps_fn(latents/sqrt(1+sigma^2), step_index)`` -> eps."""
    sigmas = np.asarray(sigmas, dtype=np.float32)
    current = np.asarray(latents, dtype=np.float32)
    generator = rng if rng is not None else np.random.default_rng()
    total_steps = max(0, len(sigmas) - 1)
    for index in range(total_steps):
        sigma_from, sigma_to = float(sigmas[index]), float(sigmas[index + 1])
        scaled = current / np.float32(math.sqrt(sigma_from ** 2 + 1.0))
        epsilon = np.asarray(eps_fn(scaled, index), dtype=np.float32)
        if epsilon.shape != current.shape:
            raise ValueError(f"UNet 输出形状 {epsilon.shape} 与潜变量形状 {current.shape} 不一致")
        noise = generator.standard_normal(current.shape).astype(np.float32) if sigma_to > 0 else None
        current = euler_ancestral_step(current, epsilon, sigma_from, sigma_to, noise)
        if progress is not None:
            try:
                progress(index + 1, total_steps)
            except Exception:  # noqa: BLE001 - progress must not break sampling
                pass
    return current


@functools.lru_cache(maxsize=1)
def _train_log_sigmas() -> np.ndarray:
    """Log sigmas of the SD1.5 scaled-linear beta schedule (1000 train steps)."""
    betas = np.linspace(0.00085 ** 0.5, 0.012 ** 0.5, NUM_TRAIN_TIMESTEPS, dtype=np.float64) ** 2
    alphas_cumprod = np.cumprod(1.0 - betas)
    return np.log(np.sqrt((1.0 - alphas_cumprod) / alphas_cumprod))


def sigma_to_timestep(sigmas: np.ndarray) -> np.ndarray:
    """Map sigma values to discrete timesteps via diffusers log-sigma interpolation."""
    values = np.asarray(sigmas, dtype=np.float64).reshape(-1)
    log_train = _train_log_sigmas()
    log_sigma = np.log(np.maximum(values, 1e-10))
    low_idx = np.clip(np.searchsorted(log_train, log_sigma, side="right") - 1, 0, len(log_train) - 2)
    low, high = log_train[low_idx], log_train[low_idx + 1]
    weight = np.clip((low - log_sigma) / (low - high), 0.0, 1.0)
    timesteps = (1.0 - weight) * low_idx + weight * (low_idx + 1)
    return np.clip(np.round(timesteps), 0, NUM_TRAIN_TIMESTEPS - 1).astype(np.int64)


# -- prompt normalisation ---------------------------------------------------
def ensure_negative(negative: str | None) -> str:
    """Guarantee ``Wrong Anatomy`` in the negative prompt, de-duplicated."""
    text = (negative or "").strip()
    if not text:
        try:
            import ai_prompt

            return ai_prompt.default_negative()
        except Exception:  # noqa: BLE001
            try:
                block = _import_ai_resources().load_config().get("prompt") or {}
                text = str(block.get("default_negative", "") or "").strip()
            except Exception:  # noqa: BLE001
                text = ""
    if not text:
        text = "lowres, bad quality, Wrong Anatomy"
    try:
        import ai_prompt

        return ai_prompt.ensure_wrong_anatomy(text)
    except Exception:  # noqa: BLE001
        if "wrong anatomy" in text.lower():
            return text
        return text.rstrip(", ") + ", Wrong Anatomy"


# -- ONNX helpers -----------------------------------------------------------
class _ShardedSession:
    """Two-stage ONNX pipeline: stage1 GPU0, stage2 GPU1."""
    def __init__(self, first, second):
        self.first = first
        self.second = second
        self._first_outputs = {item.name: item for item in first.get_outputs()}
        self._second_inputs = {item.name: item for item in second.get_inputs()}

    def get_inputs(self):
        return self.first.get_inputs()

    def get_outputs(self):
        return self.second.get_outputs()

    def run(self, output_names=None, input_feed=None):
        feed = dict(input_feed or {})
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.set_device(0)
        except Exception:
            pass
        first_feed = {item.name: feed[item.name] for item in self.first.get_inputs() if item.name in feed}
        first_values = self.first.run(None, first_feed)
        intermediates = {self.first.get_outputs()[i].name: first_values[i] for i in range(len(first_values))}
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.set_device(1)
        except Exception:
            pass
        second_feed = {}
        for item in self.second.get_inputs():
            if item.name in intermediates:
                second_feed[item.name] = intermediates[item.name]
            elif item.name in feed:
                second_feed[item.name] = feed[item.name]
        values = self.second.run(None, second_feed)
        if not output_names:
            return values
        by_name = {self.second.get_outputs()[i].name: values[i] for i in range(len(values))}
        return [by_name[name] for name in output_names if name in by_name]

def _session_run(session: Any, inputs: dict[str, np.ndarray], output_name: str | None = None) -> np.ndarray:
    names: list[str] | None = None
    if output_name:
        try:
            available = {getattr(output, "name", "") for output in session.get_outputs()}
            names = [output_name] if output_name in available else None
        except Exception:  # noqa: BLE001 - fake sessions may not expose get_outputs
            names = None
    try:
        outputs = session.run(names, inputs)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"ONNX 会话推理失败：{exc}") from exc
    if not outputs:
        raise RuntimeError("ONNX 会话没有返回任何输出")
    return outputs[0]


def _pick_provider(backend_name: str, providers: list[str] | None) -> str | None:
    available = [str(provider) for provider in (providers or [])]
    preferred = _PROVIDER_NAMES.get(str(backend_name).lower())
    if preferred and preferred in available:
        return preferred
    if "CPUExecutionProvider" in available:
        return "CPUExecutionProvider"
    return available[0] if available else None


def _dedupe_notes(notes: list[str]) -> list[str]:
    unique: list[str] = []
    for note in notes:
        if note not in unique:
            unique.append(note)
    return unique


def _config_section(config: Any, section: str) -> dict[str, Any]:
    if config is None:
        return {}
    try:
        value = config.get(section, {}) if hasattr(config, "get") else None
    except Exception:  # noqa: BLE001
        return {}
    return value if isinstance(value, dict) else {}


# -- engine -----------------------------------------------------------------
class SD15Engine:
    """Lazy SD1.5 INT8 ONNX text-to-image engine (one image per call)."""

    def __init__(self, config: Any = None, logger: logging.Logger | None = None,
                 gpu_index: int | None = None) -> None:
        self.logger = logger or LOGGER
        self._config = config
        self._requested_gpu = gpu_index
        self._io = copy.deepcopy(DEFAULT_IO)
        self._vae_scale_factor = VAE_SCALE_FACTOR
        self._vae_output_range = "minus_one_one"
        self._allow_cfg_batch = True
        self._sessions: dict[str, Any] = {}
        self._tokenizer: Any = None
        self._backend: Any = None
        self._provider: str | None = None
        self._gpu_index: int | None = None
        self._loaded = False

    # status ---------------------------------------------------------------
    def available(self) -> bool:
        """True only when SD1.5 models exist and an inference backend is usable."""
        try:
            _import_ai_resources().require_sd15()
        except Exception:  # noqa: BLE001 - missing models is the normal case
            return False
        try:
            self._select_backend()
            return True
        except Exception as exc:  # noqa: BLE001
            self.logger.debug("SD1.5 后端不可用：%s", exc)
            return False

    def missing_report(self) -> str:
        """Chinese human-readable report of what prevents inference."""
        try:
            _import_ai_resources().require_sd15()
        except Exception as exc:  # noqa: BLE001
            return getattr(exc, "user_message", "") or str(exc) or "SD1.5 模型缺失"
        try:
            info, notes = self._select_backend()
        except Exception as exc:  # noqa: BLE001
            return getattr(exc, "user_message", "") or str(exc) or "SD1.5 推理后端不可用"
        return "\n".join([f"SD1.5 模型可用，推理后端：{getattr(info, 'name', info)}", *notes])

    # config ---------------------------------------------------------------
    def _config_or_load(self) -> Any:
        if self._config is None:
            try:
                self._config = _import_ai_resources().load_config()
            except Exception as exc:  # noqa: BLE001
                self.logger.debug("读取 AI 配置失败：%s", exc)
                self._config = {}
        return self._config

    def _apply_config(self) -> None:
        block = _config_section(self._config_or_load(), "sd15")
        io = copy.deepcopy(DEFAULT_IO)
        overrides = block.get("io")
        if isinstance(overrides, dict):
            for session_name, values in overrides.items():
                if session_name in io and isinstance(values, dict):
                    for key, value in values.items():
                        if isinstance(value, str) and value.strip():
                            io[session_name][key] = value.strip()
        self._io = io
        try:
            self._vae_scale_factor = float(block.get("vae_scaling_factor", VAE_SCALE_FACTOR) or VAE_SCALE_FACTOR)
        except (TypeError, ValueError):
            self._vae_scale_factor = VAE_SCALE_FACTOR
        output_range = str(block.get("vae_output_range", "minus_one_one") or "minus_one_one").strip().lower()
        self._vae_output_range = "zero_one" if output_range in {"zero_one", "0_1", "01"} else "minus_one_one"
        self._allow_cfg_batch = bool(block.get("cfg_batch", True))

    # backend --------------------------------------------------------------
    def _require_compat(self):
        compat = _import_ai_compat()
        if compat is None:
            raise _backend_unavailable("缺少 AI 兼容层模块 ai_compat，无法选择 SD1.5 推理后端")
        return compat

    def _degrade_backend(self, compat: Any, notes: list[str], current_name: str):
        try:
            candidates = compat.probe_backends()
        except Exception:  # noqa: BLE001
            return None
        for candidate in candidates or []:
            name = getattr(candidate, "name", str(candidate))
            if name == current_name or not getattr(candidate, "available", False):
                continue
            try:
                support = compat.cpu_offload_support(name)
            except Exception:  # noqa: BLE001
                support = None
            if support is not False:
                notes.append(f"当前后端 {current_name} 不支持 CPU Offload，已降级到 {name}")
                self.logger.warning("当前后端 %s 不支持 CPU Offload，降级到 %s", current_name, name)
                return candidate, _dedupe_notes(notes)
        return None

    def _select_backend(self, prefer: str | None = None):
        compat = self._require_compat()
        info, notes = compat.select_backend(prefer=prefer, strict_cpu_offload=STRICT_CPU_OFFLOAD)
        notes = list(notes or [])
        name = getattr(info, "name", str(info))
        if name not in _ONNX_BACKENDS:
            # ai_compat may offer Vulkan only for the ncnn tool; general ONNX
            # inference cannot run there, so degrade to CPU explicitly.
            notes.append(f"后端 {name} 不支持通用 ONNX 推理，已降级到 CPU")
            self.logger.warning("后端 %s 不支持通用 ONNX 推理，降级到 CPU", name)
            info, extra = compat.select_backend(prefer="cpu", strict_cpu_offload=STRICT_CPU_OFFLOAD)
            notes.extend(extra or [])
            name = getattr(info, "name", str(info))
        support = compat.cpu_offload_support(name)
        if support is False:
            if STRICT_CPU_OFFLOAD:
                degraded = self._degrade_backend(compat, notes, name)
                if degraded is not None:
                    return degraded
                raise _backend_unavailable(_CPU_OFFLOAD_STRICT_MESSAGE)
            notes.append(f"后端 {name} 不支持 CPU Offload，strict_cpu_offload=false，继续执行")
        elif support is None:
            notes.append(f"CPU Offload 不适用（后端 {name}）")
        return info, _dedupe_notes(notes)

    @staticmethod
    def _cuda_free_mb(gpu_index: int = 0) -> int | None:
        try:
            import subprocess
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                values = [int(line.strip()) for line in result.stdout.splitlines() if line.strip().isdigit()]
                if values:
                    return values[min(max(gpu_index, 0), len(values) - 1)]
        except Exception:
            pass
        return None

    def _vae_provider(self) -> str | None:
        mode = str(os.environ.get("TWILIGHT_SD_VAE_CPU", "auto")).strip().lower()
        if mode in {"1", "true", "yes", "cpu"}:
            return "CPUExecutionProvider"
        if mode in {"0", "false", "no", "cuda"}:
            return None
        free = self._cuda_free_mb()
        if free is not None and free < 2500:
            self.logger.info("VAE 使用 CPU：显存空闲仅 %s MB（阈值 2500 MB）", free)
            return "CPUExecutionProvider"
        return None

    # loading --------------------------------------------------------------
    def load(self, backend: str | None = None) -> "SD15Engine":
        """Create the three ORT sessions lazily; raises MissingModels when absent."""
        try:
            candidates = [Path(__file__).resolve().parent.parent / "cuda-bin"]
            ai_home = os.environ.get("TWILIGHT_AI_HOME", "").strip()
            if ai_home:
                candidates.append(Path(ai_home).resolve().parent / "cuda-bin")
            for cuda_dir in candidates:
                if cuda_dir.is_dir():
                    os.add_dll_directory(str(cuda_dir))
                    os.environ["PATH"] = str(cuda_dir) + os.pathsep + os.environ.get("PATH", "")
                    break
        except Exception:
            pass
        if self._loaded:
            return self
        paths = _import_ai_resources().require_sd15()
        self._apply_config()
        info, notes = self._select_backend(backend)
        for note in notes:
            self.logger.info("%s", note)
        compat = self._require_compat()
        try:
            ort, providers = compat.get_onnxruntime(getattr(info, "name", "cpu"))
        except Exception as exc:  # noqa: BLE001 - degrade to CPU when possible
            if getattr(info, "name", "") == "cpu":
                raise
            self.logger.warning("后端 %s 推理运行时不可用（%s），降级到 CPU", getattr(info, "name", "?"), exc)
            info, _ = self._select_backend("cpu")
            ort, providers = compat.get_onnxruntime("cpu")
        provider = _pick_provider(getattr(info, "name", "cpu"), providers)
        try:
            session_options = ort.SessionOptions()
            session_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            try:
                threads = int(os.environ.get("TWILIGHT_SD_THREADS", "0") or 0)
            except ValueError:
                threads = 0
            if threads <= 0:
                threads = max(1, os.cpu_count() or 1)
            session_options.intra_op_num_threads = threads
            session_options.inter_op_num_threads = 1
            session_options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        except Exception:  # noqa: BLE001
            session_options = None

        if self._requested_gpu is not None:
            self._gpu_index = int(self._requested_gpu)
        else:
            try:
                self._gpu_index = int(os.environ.get("TWILIGHT_SD_GPU", "-1"))
            except ValueError:
                self._gpu_index = -1
        provider_entry: Any = provider
        if provider == "CUDAExecutionProvider" and self._gpu_index >= 0:
            provider_entry = ("CUDAExecutionProvider", {"device_id": self._gpu_index})

        def make_session(path: Path, provider_override: str | None = None) -> Any:
            try:
                selected = provider_override or provider
                entry: Any = selected
                if selected == "CUDAExecutionProvider" and self._gpu_index >= 0:
                    entry = ("CUDAExecutionProvider", {"device_id": self._gpu_index})
                elif selected == "DmlExecutionProvider" and self._gpu_index >= 0:
                    entry = ("DmlExecutionProvider", {"device_id": self._gpu_index})
                return ort.InferenceSession(
                    str(path), sess_options=session_options, providers=[entry] if entry else None
                )
            except Exception as exc:  # noqa: BLE001
                raise _backend_unavailable(f"无法创建 SD1.5 ONNX 推理会话（{Path(path).name}）：{exc}") from exc

        unet_session = make_session(paths["unet"])
        shard_dir = os.environ.get("TWILIGHT_SD_UNET_SHARD_DIR", "").strip()
        if not shard_dir:
            sibling = Path(paths["unet"]).parent / "unet_shards"
            if sibling.is_dir():
                shard_dir = str(sibling)
        if shard_dir and os.environ.get("TWILIGHT_SD_SHARD", "1") not in {"0", "false", "no"}:
            first_path = Path(shard_dir) / "stage1.onnx"
            second_path = Path(shard_dir) / "stage2.onnx"
            if first_path.is_file() and second_path.is_file():
                def shard_session(path: Path, device: int):
                    if provider == "CUDAExecutionProvider":
                        try:
                            import torch
                            torch.cuda.init()
                            torch.cuda.set_device(device)
                        except Exception:
                            pass
                        entry = ("CUDAExecutionProvider", {"device_id": device})
                    elif provider == "DmlExecutionProvider":
                        entry = ("DmlExecutionProvider", {"device_id": device})
                    else:
                        entry = provider
                    return ort.InferenceSession(str(path), sess_options=session_options,
                                                providers=[entry] if entry else None)
                unet_session = _ShardedSession(shard_session(first_path, 0), shard_session(second_path, 1))
        vae_session = make_session(paths["vae"], self._vae_provider() if provider == "CUDAExecutionProvider" else None)
        vae_shard_dir = Path(paths["vae"]).parent / "vae_shards"
        if os.environ.get("TWILIGHT_SD_SHARD", "1") not in {"0", "false", "no"} and os.environ.get("TWILIGHT_SD_VAE_SHARD", "1") not in {"0", "false", "no"} and vae_shard_dir.is_dir():
            def shard_vae(path: Path, device: int):
                if provider == "CUDAExecutionProvider":
                    try:
                        import torch
                        torch.cuda.init()
                        torch.cuda.set_device(device)
                    except Exception:
                        pass
                    entry = ("CUDAExecutionProvider", {"device_id": device})
                elif provider == "DmlExecutionProvider":
                    entry = ("DmlExecutionProvider", {"device_id": device})
                else:
                    entry = provider
                return ort.InferenceSession(str(path), sess_options=session_options,
                                            providers=[entry] if entry else None)
            vae_session = _ShardedSession(shard_vae(vae_shard_dir / "stage1.onnx", 0),
                                          shard_vae(vae_shard_dir / "stage2.onnx", 1))
        self._sessions = {
            "text_encoder": make_session(paths["text_encoder"]),
            "unet": unet_session,
            "vae": vae_session,
        }
        try:
            self._unet_fp16 = "float16" in str(self._sessions["unet"].get_inputs()[0].type)
        except Exception:
            self._unet_fp16 = False
        self._ort = ort
        self._session_options = session_options
        self._vae_path = paths["vae"]
        self._vae_cpu_fallback = False
        self._tokenizer = self._load_tokenizer(Path(paths["tokenizer"]))
        self._backend, self._provider, self._loaded = info, provider, True
        self.logger.info("SD1.5 推理引擎加载完成：后端=%s，provider=%s", getattr(info, "name", info), provider)
        return self

    def _load_tokenizer(self, path: Path):
        tokenizers = _import_tokenizers()
        if tokenizers is None:
            raise RuntimeError("缺少 tokenizers 依赖，无法进行 SD1.5 文本编码；请安装 tokenizers 后重试。")
        if not path.is_file():
            raise _missing_error([(path.name, path)])
        try:
            return tokenizers.Tokenizer.from_file(str(path))
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"无法加载 CLIP tokenizer（{path.name}）：{exc}") from exc

    # tokenisation / text encoding ------------------------------------------
    @staticmethod
    def _padding_token_id(tokenizer: Any) -> int:
        padding = getattr(tokenizer, "padding", None)
        if isinstance(padding, dict) and isinstance(padding.get("pad_id"), int):
            return int(padding["pad_id"])
        for token in ("<|endoftext|>", "<pad>", "!"):
            try:
                token_id = tokenizer.token_to_id(token)
            except Exception:  # noqa: BLE001
                token_id = None
            if isinstance(token_id, int) and token_id >= 0:
                return token_id
        return 0

    def _tokenize(self, text: str) -> tuple[np.ndarray, np.ndarray]:
        tokenizer = self._tokenizer
        if tokenizer is None:
            raise RuntimeError("SD1.5 tokenizer 尚未加载")
        encoded = tokenizer.encode(text or "")
        ids = [int(token) for token in list(encoded.ids)]
        attention = [1] * len(ids)
        if len(ids) > TOKENIZER_MAX_LENGTH:
            ids, attention = ids[: TOKENIZER_MAX_LENGTH - 1], attention[: TOKENIZER_MAX_LENGTH - 1]
            ids.append(int(encoded.ids[-1]) if encoded.ids else self._padding_token_id(tokenizer))
            attention.append(1)
        pad_id = self._padding_token_id(tokenizer)
        ids.extend([pad_id] * (TOKENIZER_MAX_LENGTH - len(ids)))
        attention.extend([0] * (TOKENIZER_MAX_LENGTH - len(attention)))
        return np.asarray([ids], dtype=np.int64), np.asarray([attention], dtype=np.int64)

    def _encode(self, text: str) -> np.ndarray:
        if self._sessions.get("text_encoder") is None:
            raise RuntimeError("SD1.5 文本编码器尚未加载")
        block = self._io["text_encoder"]
        ids, mask = self._tokenize(text)
        inputs: dict[str, np.ndarray] = {block["input_ids"]: ids}
        if block.get("attention_mask"):
            inputs[block["attention_mask"]] = mask
        return np.asarray(_session_run(self._sessions["text_encoder"], inputs, block.get("output")), dtype=np.float32)

    # inference -------------------------------------------------------------
    def _unet_predict(self, sample: np.ndarray, timestep: int, encoder_hidden_states: np.ndarray) -> np.ndarray:
        block = self._io["unet"]
        dtype = {"int32": np.int32, "float32": np.float32, "int64": np.int64}.get(
            str(block.get("timestep_dtype", "int64")).lower(), np.int64
        )
        input_dtype = np.float16 if getattr(self, "_unet_fp16", False) else np.float32
        inputs = {
            block["sample"]: np.asarray(sample, dtype=input_dtype),
            block["timestep"]: np.asarray([timestep], dtype=dtype),
            block["encoder_hidden_states"]: np.asarray(encoder_hidden_states, dtype=input_dtype),
        }
        return np.asarray(_session_run(self._sessions["unet"], inputs, block.get("output")), dtype=np.float32)

    def _eps_function(
        self, cond_embeddings: np.ndarray, uncond_embeddings: np.ndarray, timesteps: np.ndarray
    ) -> Callable[[np.ndarray, int], np.ndarray]:
        batch_mode = [bool(self._allow_cfg_batch)]

        def eps_fn(scaled_latents: np.ndarray, index: int) -> np.ndarray:
            timestep = int(timesteps[index])
            if batch_mode[0]:
                sample = np.concatenate([scaled_latents, scaled_latents], axis=0)
                embeddings = np.concatenate([uncond_embeddings, cond_embeddings], axis=0)
                try:
                    prediction = self._unet_predict(sample, timestep, embeddings)
                except Exception as exc:  # noqa: BLE001 - static-batch graphs
                    batch_mode[0] = False
                    self.logger.warning("UNet 不支持 CFG 批量推理（%s），改为逐次推理", exc)
                else:
                    if prediction.shape[0] == 2:
                        uncond_part, cond_part = prediction[0:1], prediction[1:2]
                        return uncond_part + CFG_SCALE * (cond_part - uncond_part)
                    batch_mode[0] = False
                    self.logger.warning("UNet 批量输出维度异常 %s，改为逐次推理", prediction.shape)
            uncond = self._unet_predict(scaled_latents, timestep, uncond_embeddings)
            cond = self._unet_predict(scaled_latents, timestep, cond_embeddings)
            return uncond + CFG_SCALE * (cond - uncond)

        return eps_fn

    def _sample_latents(
        self,
        latents: np.ndarray,
        sigmas: np.ndarray,
        cond_embeddings: np.ndarray,
        uncond_embeddings: np.ndarray,
        rng: np.random.Generator | None = None,
        progress: Callable[[int, int], None] | None = None,
    ) -> np.ndarray:
        timesteps = sigma_to_timestep(np.asarray(sigmas[:-1], dtype=np.float32))
        eps_fn = self._eps_function(cond_embeddings, uncond_embeddings, timesteps)
        return sample_euler_ancestral(latents, sigmas, eps_fn, rng=rng, progress=progress)

    def _decode_tiled(self, latents: np.ndarray) -> np.ndarray:
        tile = int(os.environ.get("TWILIGHT_SD_VAE_TILE", "32") or 32)
        tile = max(16, min(64, tile))
        overlap = max(4, tile // 8)
        block = self._io["vae_decoder"]
        array = np.asarray(latents, dtype=np.float32)
        if array.ndim != 4:
            raise RuntimeError(f"VAE 输入维度异常：{array.shape}")
        channels, height, width = array.shape[1], array.shape[2], array.shape[3]
        scale = 8
        out = np.zeros((3, height * scale, width * scale), dtype=np.float32)
        weights = np.zeros((height * scale, width * scale), dtype=np.float32)
        def starts(total: int) -> list[int]:
            if total <= tile:
                return [0]
            values = [0]
            while values[-1] + tile < total:
                values.append(values[-1] + tile - overlap)
            values.append(total - tile)
            return sorted(set(values))
        for y in starts(height):
            for x in starts(width):
                h = min(tile, height - y)
                w = min(tile, width - x)
                patch = array[:, :, y:y + h, x:x + w]
                decoded = np.asarray(_session_run(self._sessions["vae"], {block["latent_sample"]: patch}, block.get("output")), dtype=np.float32)
                if decoded.ndim == 4:
                    decoded = decoded[0]
                hh, ww = decoded.shape[1], decoded.shape[2]
                ramp_h = np.ones(hh, dtype=np.float32)
                ramp_w = np.ones(ww, dtype=np.float32)
                edge = max(1, min(hh, ww) // 8)
                for i in range(min(edge, hh)):
                    ramp_h[i] = max(0.05, (i + 1) / edge)
                    ramp_h[-1 - i] = max(0.05, (i + 1) / edge)
                for i in range(min(edge, ww)):
                    ramp_w[i] = max(0.05, (i + 1) / edge)
                    ramp_w[-1 - i] = max(0.05, (i + 1) / edge)
                weight = np.outer(ramp_h, ramp_w)
                out[:, y * scale:y * scale + hh, x * scale:x * scale + ww] += decoded * weight[None, :, :]
                weights[y * scale:y * scale + hh, x * scale:x * scale + ww] += weight
        weights = np.maximum(weights, 1e-6)
        return out / weights[None, :, :]

    def _decode(self, latents: np.ndarray) -> np.ndarray:
        block = self._io["vae_decoder"]
        inputs = {block["latent_sample"]: np.asarray(latents, dtype=np.float32)}
        try:
            if self._provider == "CUDAExecutionProvider" and os.environ.get("TWILIGHT_SD_VAE_TILED", "1") != "0" and (latents.shape[2] > 48 or latents.shape[3] > 48):
                return self._decode_tiled(latents)
            return np.asarray(_session_run(self._sessions["vae"], inputs, block.get("output")), dtype=np.float32)
        except Exception as exc:  # noqa: BLE001
            if self._provider == "CUDAExecutionProvider" and not self._vae_cpu_fallback:
                self.logger.warning("VAE CUDA 解码失败，回退 CPU VAE：%s", exc)
                self._sessions["vae"] = self._ort.InferenceSession(
                    str(self._vae_path), sess_options=self._session_options, providers=["CPUExecutionProvider"]
                )
                self._vae_cpu_fallback = True
                return np.asarray(_session_run(self._sessions["vae"], inputs, block.get("output")), dtype=np.float32)
            raise

    def _to_uint8_image(self, decoded: np.ndarray) -> np.ndarray:
        array = np.asarray(decoded, dtype=np.float32)
        if array.ndim == 4:
            array = array[0]
        if array.ndim != 3:
            raise RuntimeError(f"VAE 解码输出维度异常：{array.shape}")
        if array.shape[0] == 3:
            array = np.transpose(array, (1, 2, 0))
        if array.shape[-1] != 3:
            raise RuntimeError(f"VAE 解码输出通道数异常：{array.shape}")
        if self._vae_output_range == "minus_one_one":
            array = array / 2.0 + 0.5
        return (np.clip(array, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)

    def _save_image(self, image: np.ndarray, output_path: str | Path | None) -> Path:
        if output_path is not None:
            target = Path(output_path).expanduser()
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
        else:
            try:
                folder = _import_ai_resources().outputs_dir("images")
            except Exception:  # noqa: BLE001
                folder = Path.cwd() / "outputs" / "images"
            try:
                folder.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise RuntimeError(f"无法创建 SD1.5 输出目录 {folder}：{exc}") from exc
            stamp = time.strftime("%Y%m%d_%H%M%S")
            target, suffix = folder / f"sd15_{stamp}.png", 1
            while target.exists():
                target = folder / f"sd15_{stamp}_{suffix}.png"
                suffix += 1
        try:
            from PIL import Image
        except ImportError as exc:  # pragma: no cover - Pillow is a build dependency
            raise RuntimeError("缺少 Pillow 依赖，无法保存 SD1.5 图像") from exc
        try:
            Image.fromarray(image).save(target, format="PNG")
        except OSError as exc:
            raise RuntimeError(f"SD1.5 图像保存失败：{target}（{exc}）") from exc
        return target

    # public API -------------------------------------------------------------
    def generate(
        self,
        positive: str,
        negative: str,
        size: Any = None,
        output_path: str | Path | None = None,
        backend: str | None = None,
        progress: Callable[[int, int], None] | None = None,
        **overrides: Any,
    ) -> Path:
        """Generate one PNG and return its path.

        Locked sampler settings cannot be overridden (warnings are logged).
        Missing weights raise ``ai_resources.MissingModels``; the process never
        crashes, so the UI can show ``user_message``.
        """
        reject_locked_overrides(overrides, self.logger)
        self.load(backend=backend)
        width, height = resolve_size(size, self.logger)
        positive_text = (positive or "").strip()
        if not positive_text:
            raise ValueError("SD1.5 正向提示词为空，已停止本次生成。")
        negative_text = ensure_negative(negative)
        cond_embeddings = self._encode(positive_text)
        uncond_embeddings = self._encode(negative_text)
        sigmas = euler_ancestral_sigmas(STEPS)
        try:
            seed_value = int(os.environ.get("TWILIGHT_SD_SEED", "-1"))
        except ValueError:
            seed_value = -1
        rng = np.random.default_rng(None if seed_value < 0 else seed_value)
        # SD1.5's UNet downsamples 3x, so latent H/W must be a multiple of 8
        # (pixel H/W a multiple of 64).  The locked default 960x720 is not
        # (720/64 = 11.25), so pad the latent grid and center-crop the decoded
        # image back to the requested size.
        latent_h = int(math.ceil(height / LATENT_SCALE))
        latent_w = int(math.ceil(width / LATENT_SCALE))
        pad_h = (-latent_h) % 8
        pad_w = (-latent_w) % 8
        latents = rng.standard_normal(
            (1, LATENT_CHANNELS, latent_h + pad_h, latent_w + pad_w)
        ).astype(np.float32) * np.float32(float(sigmas[0]))
        self.logger.info(
            "SD1.5 开始生成：%dx%d，步数=%d，cfg=%.1f，采样器=%s", width, height, STEPS, CFG_SCALE, SAMPLER
        )
        if pad_h or pad_w:
            self.logger.info(
                "SD1.5 潜变量对齐：%dx%d -> %dx%d（UNet 需 8 的倍数），生成后居中裁剪",
                latent_w, latent_h, latent_w + pad_w, latent_h + pad_h,
            )
        total_steps = max(1, len(sigmas) - 1)
        if progress is not None:
            try:
                progress(0, total_steps)
            except Exception:  # noqa: BLE001
                pass
        latents = self._sample_latents(
            latents, sigmas, cond_embeddings, uncond_embeddings, rng, progress=progress
        )
        if progress is not None:
            try:
                progress(total_steps, total_steps)
            except Exception:  # noqa: BLE001
                pass
        decoded = self._decode(latents / np.float32(max(self._vae_scale_factor, 1e-8)))
        image = self._to_uint8_image(decoded)
        if pad_h or pad_w:
            top = max(0, (image.shape[0] - height) // 2)
            left = max(0, (image.shape[1] - width) // 2)
            image = image[top:top + height, left:left + width]
        target = self._save_image(image, output_path)
        self.logger.info("SD1.5 图像已保存：%s", target)
        return target
