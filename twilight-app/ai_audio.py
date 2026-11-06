"""Built-in AI voice adapters for Twilight Harness ("语音适配", stage 1).

Three headless adapters share one contract:

* :class:`VocalSeparator` -- UVR / MDX-Net INT8 ONNX vocal separation using only
  numpy + onnxruntime (no torch / audio-separator): PCM WAV -> STFT ->
  overlapping model windows -> complex estimate -> Wiener split -> inverse STFT
  -> ``vocals.wav`` + ``instrumental.wav``.
* :class:`Transcriber` -- whisper.cpp Q5_1 CLI driver (subprocess), optional
  ``pywhispercpp`` fallback when that package happens to be importable.
* :class:`SoVitsVoice` -- GPT-SoVITS v2 INT8 adapter: session loading, I/O
  signature validation, reference-audio resampling and the external command
  route are real; the embedded acoustic step raises :class:`VoiceInferenceError`
  naming the missing model-pack config instead of pretending to synthesise.

``ai_resources`` / ``ai_compat`` are imported lazily and are stubbable from unit
tests.  When they are absent the module still reports exact Chinese
missing-model messages and never crashes.  Model weights are never downloaded or
bundled: absent files raise ``ai_resources.MissingModels``.  Progress callbacks
receive the app progress-board event dict::

    {"id": "ai-<kind>-<name>", "kind": ..., "name": ..., "fraction": 0..1,
     "state": "running"|"done"|"error"}
"""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from collections.abc import Mapping
from pathlib import Path

__all__ = [
    "MissingModels",
    "VoiceInferenceError",
    "UVR_MODEL_MISSING_MSG",
    "WHISPER_EXE_MISSING_MSG",
    "WHISPER_MODEL_MISSING_MSG",
    "WHISPER_MISSING_MSG",
    "SOVITS_MODEL_MISSING_MSG",
    "REFERENCE_AUDIO_MISSING_MSG",
    "VocalSeparator",
    "Transcriber",
    "SoVitsVoice",
    "missing_models_class",
    "UVR_SAMPLE_RATE",
    "REFERENCE_SAMPLE_RATE",
]

# ---------------------------------------------------------------------------
# Constants and canonical Chinese user messages
# ---------------------------------------------------------------------------

AI_HOME_ENV = "TWILIGHT_AI_HOME"
UVR_SAMPLE_RATE = 44100
WHISPER_SAMPLE_RATE = 16000
REFERENCE_SAMPLE_RATE = 32000
SSL_SAMPLE_RATE = 16000
REFERENCE_MAX_SECONDS = 10.0
COMMAND_TIMEOUT = 600.0

#: Fallback messages, used only while ``ai_resources`` is absent (parallel
#: development / unit tests); at runtime ai_resources owns the canonical text.
UVR_MODEL_MISSING_MSG = "人声分离模型缺失：AI 资源目录中未找到 UVR / MDX-Net INT8 ONNX 模型"
WHISPER_EXE_MISSING_MSG = "语音识别组件缺失：未找到 whisper.cpp 可执行文件（whisper-cli.exe）"
WHISPER_MODEL_MISSING_MSG = "语音识别模型缺失：未找到 whisper.cpp 的 ggml 模型文件（建议 ggml-base-q5_1.bin）"
WHISPER_MISSING_MSG = "语音识别组件缺失：未找到 whisper.cpp 可执行文件与 ggml 模型文件"
SOVITS_MODEL_MISSING_MSG = "语音克隆模型缺失：AI 资源目录中缺少 GPT-SoVITS v2 INT8 模型文件"
REFERENCE_AUDIO_MISSING_MSG = "参考音频缺失，克隆音色已禁用"  # product-spec exact

SOVITS_SLOTS = ("gpt", "sovits", "bert", "hubert")
SOVITS_NAMES = {"gpt": "gpt_int8.onnx", "sovits": "sovits_int8.onnx",
                "bert": "bert.onnx", "hubert": "hubert.onnx"}
WHISPER_EXE_NAMES = ("whisper-cli.exe", "whisper.exe", "whisper.cpp.exe", "main.exe",
                     "whisper-cli", "whisper", "whisper.cpp", "main")
DEFAULT_CONFIG = {
    "uvr": {"model": "", "segment": 256, "overlap": 0.25, "stft": 2048},
    "whisper": {"exe": "", "model": "", "language": "auto"},
    "gpt_sovits": {"model_dir": "", "command": ""},
}


class MissingModels(RuntimeError):
    """Fallback only: mirrors ``ai_resources.MissingModels`` when it is absent."""

    def __init__(self, missing=None, user_message=None):
        self.missing = list(missing or [])
        self.user_message = user_message or "\n".join(self.missing)
        super().__init__(self.user_message)


class VoiceInferenceError(RuntimeError):
    """GPT-SoVITS acoustic generation is unavailable without the model pack."""


# ---------------------------------------------------------------------------
# ai_resources / ai_compat adapters
# ---------------------------------------------------------------------------


def _optional_module(name):
    """Import *name* if present; ``None`` entries in sys.modules raise ImportError."""
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


def _missing(module, message):
    """Build ai_resources' MissingModels (or the local fallback) safely."""
    cls = getattr(module, "MissingModels", None) if module is not None else None
    if not (isinstance(cls, type) and issubclass(cls, BaseException)):
        cls = MissingModels
    try:
        return cls(user_message=message)
    except TypeError:  # pragma: no cover - defensively accept positional-only
        return cls(message)


def missing_models_class():
    """Exception class actually raised for absent model files at runtime."""
    module = _optional_module("ai_resources")
    cls = getattr(module, "MissingModels", None) if module is not None else None
    return cls if isinstance(cls, type) else MissingModels


def _user_message(exc):
    return str(getattr(exc, "user_message", "") or exc) if exc is not None else ""


def ai_home():
    """``TWILIGHT_AI_HOME`` else ai_resources' home else %LOCALAPPDATA%."""
    override = os.environ.get(AI_HOME_ENV)
    if override:
        return Path(override).expanduser().resolve()
    module = _optional_module("ai_resources")
    if module is not None:
        try:
            function = getattr(module, "ai_home", None)
            if callable(function):
                return Path(function())
            value = getattr(module, "AI_HOME", None)
            if value:
                return Path(value)
        except Exception:  # noqa: BLE001 - proxy attribute may raise
            pass
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return base / "TwilightHarness" / "ai"


def _deep_merge(base, override):
    result = copy.deepcopy(dict(base))
    if isinstance(override, Mapping):
        for key, value in override.items():
            if isinstance(value, Mapping) and isinstance(result.get(key), dict):
                result[key] = _deep_merge(result[key], value)
            else:
                result[key] = copy.deepcopy(value)
    return result


def _load_config():
    """Nested ai-config dict with defaults merged (broken config never crashes)."""
    data = {}
    module = _optional_module("ai_resources")
    if module is not None:
        try:
            config = module.load_config()
        except Exception:  # noqa: BLE001
            config = None
        if config is not None:
            if hasattr(config, "as_dict"):
                try:
                    data = dict(config.as_dict())
                except Exception:  # noqa: BLE001
                    data = {}
            elif isinstance(config, Mapping):
                data = dict(config)
    else:
        path = ai_home() / "ai-config.json"
        try:
            if path.is_file():
                data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
    return _deep_merge(DEFAULT_CONFIG, data)


def _section(config, name):
    section = config.get(name)
    return section if isinstance(section, Mapping) else {}


def _as_path(value):
    if isinstance(value, (str, os.PathLike)):
        try:
            return Path(value).expanduser()
        except (TypeError, ValueError):  # pragma: no cover
            return None
    return None


def _pick_path(container, preferred=(), suffixes=()):
    """First existing file path in a resource dict / path value."""
    if container is None:
        return None
    suffixes = tuple(suffix.lower() for suffix in suffixes)
    if not isinstance(container, Mapping):
        path = _as_path(container)
        return path if path is not None and path.is_file() else None
    for key in preferred:
        path = _as_path(container.get(key))
        if path is not None and path.is_file():
            return path
    for value in container.values():
        items = value if isinstance(value, (list, tuple)) else (value,)
        for item in items:
            path = _as_path(item)
            if path is not None and path.is_file() and (
                not suffixes or path.suffix.lower() in suffixes
            ):
                return path
    return None


def _config_path(value):
    """Resolve a configured path (relative paths are relative to AI_HOME)."""
    path = _as_path(value)
    if path is None:
        return None
    if path.is_absolute():
        return path if path.is_file() else None
    for candidate in (ai_home() / path, path):
        if candidate.is_file():
            return candidate
    return None


def _scan_for(directory, names):
    directory = Path(directory)
    if not directory.is_dir():
        return None
    lowered = {name.lower() for name in names}
    try:
        for entry in sorted(directory.iterdir(), key=lambda item: item.name.lower()):
            if entry.is_file() and entry.name.lower() in lowered:
                return entry
    except OSError:  # pragma: no cover
        return None
    return None


def _onnxruntime():
    """Return ``(onnxruntime_module, providers)``, via ai_compat when present."""
    module = _optional_module("ai_compat")
    if module is not None and hasattr(module, "get_onnxruntime"):
        backend_id = None
        try:
            function = getattr(module, "select_backend", None)
            if callable(function):
                selected = function()
                if isinstance(selected, (tuple, list)) and selected:
                    info = selected[0]
                    backend_id = getattr(info, "name", None) or (
                        str(info) if isinstance(info, str) else None
                    )
                elif isinstance(selected, str):
                    backend_id = selected
        except Exception:  # noqa: BLE001 - probe failure -> ORT defaults
            backend_id = None
        try:
            result = module.get_onnxruntime(backend_id or "onnxruntime")
        except TypeError:  # pragma: no cover - positional-only variant
            result = module.get_onnxruntime()
        if isinstance(result, (tuple, list)) and len(result) >= 2:
            return result[0], list(result[1] or [])
        return result, []
    try:
        import onnxruntime as ort  # noqa: PLC0415 - lazy, optional at app start
    except ImportError as exc:
        raise RuntimeError("ONNX Runtime 不可用：请安装 onnxruntime CPU 运行库后重试") from exc
    return ort, list(ort.get_available_providers())


def _make_session(ort, providers, model_path, label):
    options = ort.SessionOptions()
    try:
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        options.log_severity_level = 3
    except Exception:  # noqa: BLE001 - tuning is optional
        pass
    try:
        available = set(ort.get_available_providers())
    except Exception:  # noqa: BLE001
        available = set()
    usable = [provider for provider in providers if not available or provider in available]
    kwargs = {"providers": usable} if usable else {}
    try:
        return ort.InferenceSession(str(model_path), sess_options=options, **kwargs)
    except Exception as exc:  # noqa: BLE001 - surfaced as a Chinese RuntimeError
        raise RuntimeError(f"{label} 模型加载失败：{exc}") from exc


def _backend_label(providers):
    label = "onnxruntime"
    module = _optional_module("ai_compat")
    if module is not None and hasattr(module, "select_backend"):
        try:
            selected = module.select_backend()
            info = selected[0] if isinstance(selected, (tuple, list)) and selected else selected
            name = getattr(info, "name", None) or (info if isinstance(info, str) else None)
            if name:
                label = str(name)
        except Exception:  # noqa: BLE001 - status must never raise
            pass
    providers = [str(provider) for provider in providers if provider]
    return f"{label}（{', '.join(providers)}）" if providers else label


# ---------------------------------------------------------------------------
# WAV / numpy / STFT helpers (stdlib wave + numpy only)
# ---------------------------------------------------------------------------


def _require_numpy():
    try:
        import numpy  # noqa: PLC0415 - lazy: app import stays light
    except ImportError as exc:  # pragma: no cover - build dependency
        raise RuntimeError("numpy 不可用：AI 语音处理需要 numpy") from exc
    return numpy


def _read_wav(path):
    """Read a PCM WAV file -> ``(float32 (channels, frames), sample_rate)``."""
    source = Path(path)
    try:
        with wave.open(str(source), "rb") as handle:
            channels = handle.getnchannels()
            width = handle.getsampwidth()
            frame_rate = handle.getframerate()
            frame_count = handle.getnframes()
            compression = handle.getcomptype()
            raw = handle.readframes(frame_count)
    except (wave.Error, EOFError, OSError) as exc:
        raise RuntimeError(
            f"无法读取 WAV 音频：{source}（仅支持未压缩 PCM WAV；错误：{exc}）"
        ) from exc
    if compression != "NONE":
        raise RuntimeError(f"不支持的 WAV 编码：{compression}（仅支持未压缩 PCM）")
    if channels <= 0 or frame_rate <= 0 or not raw:
        raise RuntimeError(f"WAV 音频为空或参数无效：{source}")
    numpy = _require_numpy()
    if width == 1:
        values = (numpy.frombuffer(raw, dtype=numpy.uint8).astype(numpy.float32) - 128.0) / 128.0
    elif width == 2:
        values = numpy.frombuffer(raw, dtype="<i2").astype(numpy.float32) / 32768.0
    elif width == 3:
        triplets = numpy.frombuffer(raw, dtype=numpy.uint8).reshape(-1, 3).astype(numpy.int32)
        ints = triplets[:, 0] | (triplets[:, 1] << 8) | (triplets[:, 2] << 16)
        ints = numpy.where(ints >= (1 << 23), ints - (1 << 24), ints).astype(numpy.float32)
        values = ints / float(1 << 23)
    elif width == 4:
        values = numpy.frombuffer(raw, dtype="<i4").astype(numpy.float32) / 2147483648.0
    else:
        raise RuntimeError(f"不支持的 WAV 位深：{width * 8} 位（仅支持 8/16/24/32 位 PCM）")
    usable = (values.size // channels) * channels
    values = values[:usable].reshape(-1, channels).T
    return numpy.ascontiguousarray(values, dtype=numpy.float32), int(frame_rate)


def _write_wav_pcm16(path, samples, sample_rate):
    """Write ``(channels, frames)`` float samples as 16-bit PCM WAV."""
    numpy = _require_numpy()
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = numpy.asarray(samples, dtype=numpy.float32)
    if data.ndim == 1:
        data = data[numpy.newaxis, :]
    data = numpy.clip(numpy.nan_to_num(data, nan=0.0, posinf=0.0, neginf=0.0), -1.0, 1.0)
    pcm = numpy.round(data * 32767.0).astype("<i2")
    with wave.open(str(target), "wb") as handle:
        handle.setnchannels(int(pcm.shape[0]))
        handle.setsampwidth(2)
        handle.setframerate(int(sample_rate))
        handle.writeframes(numpy.ascontiguousarray(pcm.T).tobytes())
    return target


def _mono(samples):
    data = _require_numpy().asarray(samples, dtype=_require_numpy().float32)
    return data if data.ndim == 1 else data.mean(axis=0, dtype=_require_numpy().float32)


def _resample(samples, source_rate, target_rate):
    """Linear resampling (numpy only; adequate for model input preparation)."""
    numpy = _require_numpy()
    data = numpy.asarray(samples, dtype=numpy.float32)
    if data.ndim == 1:
        data = data[numpy.newaxis, :]
    if int(source_rate) == int(target_rate) or data.shape[1] == 0:
        return data
    frames_in = data.shape[1]
    frames_out = max(1, int(round(frames_in * float(target_rate) / float(source_rate))))
    x_old = numpy.arange(frames_in, dtype=numpy.float64) / float(source_rate)
    x_new = numpy.arange(frames_out, dtype=numpy.float64) / float(target_rate)
    result = numpy.empty((data.shape[0], frames_out), dtype=numpy.float32)
    for channel in range(data.shape[0]):
        result[channel] = numpy.interp(x_new, x_old, data[channel]).astype(numpy.float32)
    return result


def _hann(size):
    numpy = _require_numpy()
    indices = numpy.arange(size, dtype=numpy.float64)
    return (0.5 - 0.5 * numpy.cos(2.0 * numpy.pi * indices / size)).astype(numpy.float32)


def _prepare_frames(samples, n_fft, hop):
    """Pad a signal so every STFT frame is addressable; return metadata."""
    numpy = _require_numpy()
    _, frames = samples.shape
    pad = n_fft // 2
    padded = numpy.pad(
        samples, ((0, 0), (pad, 0)), mode="reflect" if frames > pad else "constant"
    ).astype(numpy.float32)
    if padded.shape[1] < n_fft:
        padded = numpy.pad(padded, ((0, 0), (0, n_fft - padded.shape[1])), mode="constant")
    span = padded.shape[1] - n_fft
    frame_total = int(math.ceil(span / hop)) + 1 if span > 0 else 1
    needed = n_fft + (frame_total - 1) * hop
    if needed > padded.shape[1]:
        padded = numpy.pad(padded, ((0, 0), (0, needed - padded.shape[1])), mode="constant")
    return padded, frame_total, pad


def _stft_segment(padded, start, count, n_fft, hop, window):
    """STFT frames ``[start, start+count)`` -> complex64 ``(channels, freq, count)``."""
    numpy = _require_numpy()
    offsets = (start * hop + numpy.arange(n_fft)[numpy.newaxis, :]
               + hop * numpy.arange(count)[:, numpy.newaxis])
    frames = padded[:, offsets] * window[numpy.newaxis, numpy.newaxis, :]
    spectrum = numpy.fft.rfft(frames, n=n_fft, axis=-1)
    return numpy.transpose(spectrum, (0, 2, 1)).astype(numpy.complex64)


def _ola_add(buffer, weight, spectrum, start, n_fft, hop, window):
    """Inverse-STFT one segment and overlap-add it into ``buffer``."""
    numpy = _require_numpy()
    count = spectrum.shape[2]
    frames = numpy.fft.irfft(numpy.transpose(spectrum, (0, 2, 1)), n=n_fft, axis=-1)
    frames = frames.astype(numpy.float32) * window[numpy.newaxis, numpy.newaxis, :]
    square = (window * window).astype(numpy.float64)
    for index in range(count):
        position = (start + index) * hop
        buffer[:, position:position + n_fft] += frames[:, index, :]
        if weight is not None:
            weight[position:position + n_fft] += square


def _is_mask_like(values):
    numpy = _require_numpy()
    return bool(values.size and numpy.nanmin(values) >= -0.05 and numpy.nanmax(values) <= 1.05)


def _wiener_split(vocal_estimate, mix_spectrum):
    """Wiener-style mask computed from the model's complex vocal estimate."""
    numpy = _require_numpy()
    vocal_power = numpy.abs(vocal_estimate) ** 2
    residual_power = numpy.abs(mix_spectrum - vocal_estimate) ** 2
    vocals = (mix_spectrum * vocal_power / (vocal_power + residual_power + 1e-8))
    vocals = vocals.astype(numpy.complex64)
    return vocals, (mix_spectrum - vocals).astype(numpy.complex64)


def _report(progress, kind, name, fraction, state="running"):
    """Emit one progress-dict event; a broken UI callback must not break work."""
    if progress is None:
        return
    event = {"id": f"ai-{kind}-{name}", "kind": kind, "name": str(name),
             "fraction": round(max(0.0, min(1.0, float(fraction))), 4), "state": state}
    try:
        progress(event)
    except Exception:  # noqa: BLE001 - progress reporting is best effort
        pass


def _run_process(command, timeout):
    kwargs = {"capture_output": True, "text": True, "encoding": "utf-8",
              "errors": "replace", "timeout": float(timeout)}
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    return subprocess.run([str(part) for part in command], **kwargs)


# ---------------------------------------------------------------------------
# (1) UVR / MDX-Net vocal separator
# ---------------------------------------------------------------------------


def _uvr_model_state():
    """Return ``(model_path | None, error | None)`` for the UVR ONNX weight."""
    module = _optional_module("ai_resources")
    if module is not None and hasattr(module, "require_uvr"):
        try:
            request = module.require_uvr()
        except Exception as exc:  # noqa: BLE001 - MissingModels carries the message
            return None, exc
        path = _pick_path(request, preferred=("model",), suffixes=(".onnx",))
        return (path, None) if path is not None else (None, _missing(module, UVR_MODEL_MISSING_MSG))
    path = _config_path(os.environ.get("TWILIGHT_UVR_MODEL"))
    if path is not None:
        return path, None
    root = ai_home() / "models" / "uvr"
    for candidate in (root / "uvr_mdx_net_int8.onnx", root / "mdx_net_int8.onnx",
                      root / "model.onnx", *sorted(root.glob("*.onnx"))):
        if candidate.is_file():
            return candidate, None
    return None, _missing(None, UVR_MODEL_MISSING_MSG)


def _uvr_dsp_params(config):
    """Read STFT / segment parameters (uvr.stft int|dict, segment, overlap)."""
    section = _section(config, "uvr")
    raw_stft = section.get("stft", 2048)
    if isinstance(raw_stft, Mapping):
        n_fft = int(raw_stft.get("n_fft", 2048))
        hop = int(raw_stft.get("hop", max(1, n_fft // 2)))
        dim_f_default = int(raw_stft.get("dim_f", n_fft))
    elif isinstance(raw_stft, (list, tuple)):
        n_fft = int(raw_stft[0]) if raw_stft else 2048
        hop = int(raw_stft[1]) if len(raw_stft) > 1 else max(1, n_fft // 2)
        dim_f_default = n_fft
    else:
        n_fft = int(raw_stft or 2048)
        hop = int(section.get("hop", max(1, n_fft // 2)))
        dim_f_default = n_fft
    n_fft = max(64, n_fft)
    if hop <= 0 or hop > n_fft:
        raise RuntimeError(f"UVR MDX-Net STFT 参数无效：n_fft={n_fft}, hop={hop}")
    segment = int(section.get("segment", 256) or 256)
    if segment <= 0:
        raise RuntimeError(f"UVR MDX-Net 分段参数无效：segment={segment}")
    try:
        overlap = float(section.get("overlap", 0.25) or 0.0)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"UVR MDX-Net 重叠参数无效：overlap={section.get('overlap')!r}") from exc
    return n_fft, hop, dim_f_default, segment, max(0.0, min(0.95, overlap))


class VocalSeparator:
    """UVR / MDX-Net vocal separation (INT8 ONNX, numpy + onnxruntime only)."""

    def __init__(self):
        self._session = self._model_path = self._input_name = None
        self._output_names = []
        self._model_channels, self._model_freq, self._model_time = 4, None, None
        self._n_fft, self._hop = 2048, 1024
        self._segment = self._step = 256

    def available(self):
        """True when an ONNX weight file is present; never raises."""
        path, _ = _uvr_model_state()
        return path is not None and Path(path).is_file()

    def missing_report(self):
        """Chinese one-liner describing what is missing (``""`` when ready)."""
        path, error = _uvr_model_state()
        if path is not None and Path(path).is_file():
            return ""
        return _user_message(error) or UVR_MODEL_MISSING_MSG

    def load(self):
        """Open the ONNX session lazily and validate the MDX architecture."""
        if self._session is not None:
            return self
        path, error = _uvr_model_state()
        if path is None:
            raise error or _missing(None, UVR_MODEL_MISSING_MSG)
        self._n_fft, self._hop, dim_f_default, segment, overlap = _uvr_dsp_params(
            _load_config()
        )
        ort, providers = _onnxruntime()
        session = _make_session(ort, providers, path, "UVR MDX-Net")
        self._inspect_session(session, dim_f_default, segment)
        self._segment, self._step = segment, max(1, int(round(segment * (1 - overlap))))
        if self._model_time is not None:
            self._segment = min(self._segment, int(self._model_time))
            self._step = max(1, int(round(self._segment * (1 - overlap))))
        self._session, self._model_path = session, path
        return self

    def _inspect_session(self, session, dim_f_default, segment_default):
        inputs, outputs = session.get_inputs(), session.get_outputs()
        if not inputs:
            raise RuntimeError("UVR MDX-Net 模型没有输入节点，模型文件可能无效")
        tensor, shape = inputs[0], list(inputs[0].shape or [])
        if len(shape) != 4:
            raise RuntimeError(
                f"UVR MDX-Net 模型输入维度不受支持：期望 [batch, channels, freq, time] "
                f"四维，实际 {shape}")
        if "float" not in str(tensor.type):
            raise RuntimeError(f"UVR MDX-Net 模型输入类型不受支持：{tensor.type}（需要 float32）")

        def concrete(index, fallback):
            value = shape[index] if index < len(shape) else None
            return int(value) if isinstance(value, int) and value > 0 else fallback

        channels = concrete(1, 4)
        if channels not in (2, 4):
            raise RuntimeError(
                f"UVR MDX-Net 模型输入通道数不受支持：{channels}（仅支持 2 或 4 通道复频谱）")
        if not outputs or len(list(outputs[0].shape or [])) != 4:
            actual = list(outputs[0].shape or []) if outputs else []
            raise RuntimeError(
                f"UVR MDX-Net 模型输出维度不受支持：期望四维，实际 {actual}")
        self._model_channels, self._model_freq = channels, concrete(2, dim_f_default)
        self._model_time = concrete(3, segment_default)
        self._input_name = tensor.name
        self._output_names = [output.name for output in outputs]

    def _model_input_from_segment(self, mix_segment, count):
        numpy = _require_numpy()
        model_f, model_t = int(self._model_freq), int(self._model_time or count)
        spectrum = mix_segment
        if spectrum.shape[1] < model_f:
            spectrum = numpy.pad(spectrum, ((0, 0), (0, model_f - spectrum.shape[1]), (0, 0)))
        elif spectrum.shape[1] > model_f:
            spectrum = spectrum[:, :model_f, :]
        if count < model_t:
            spectrum = numpy.pad(spectrum, ((0, 0), (0, 0), (0, model_t - count)))
        if self._model_channels == 4:
            stacked = numpy.stack([spectrum[0].real, spectrum[0].imag,
                                   spectrum[1].real, spectrum[1].imag], axis=0)
        else:
            mono = (spectrum[0] + spectrum[1]) * 0.5
            stacked = numpy.stack([mono.real, mono.imag], axis=0)
        return stacked[numpy.newaxis, ...].astype(numpy.float32)

    def _split_segment(self, output, mix_segment):
        """Interpret one model output segment -> (vocals, instrumental) spectra."""
        numpy = _require_numpy()
        output = numpy.asarray(output, dtype=numpy.float32)
        if output.ndim != 4 or output.shape[0] < 1:
            raise RuntimeError(f"UVR MDX-Net 模型输出维度不受支持：{list(output.shape)}")
        freq, count = mix_segment.shape[1], mix_segment.shape[2]
        channels_out, freq_out, time_out = output[0].shape
        cropped = output[0][:, :min(freq_out, freq), :min(time_out, count)]
        usable_freq, usable_time = cropped.shape[1], cropped.shape[2]
        if channels_out >= 4:  # complex stereo estimate -> Wiener split
            padded = numpy.zeros((2, freq, count), dtype=numpy.complex64)
            padded[:, :usable_freq, :usable_time] = numpy.stack(
                [cropped[0] + 1j * cropped[1], cropped[2] + 1j * cropped[3]])
            return _wiener_split(padded, mix_segment)
        if channels_out == 2 and self._model_channels == 2:  # mono complex model
            padded = numpy.zeros((2, freq, count), dtype=numpy.complex64)
            mono = (cropped[0] + 1j * cropped[1]).astype(numpy.complex64)
            padded[0, :usable_freq, :usable_time] = mono
            padded[1, :usable_freq, :usable_time] = mono
            return _wiener_split(padded, mix_segment)
        if channels_out == 2:  # real stereo mask -> direct model output
            full = numpy.zeros((2, freq, count), dtype=numpy.float32)
            full[:, :usable_freq, :usable_time] = numpy.clip(cropped, -1.0, 1.0)
            vocals = (mix_segment * full).astype(numpy.complex64)
            return vocals, (mix_segment - vocals).astype(numpy.complex64)
        values = cropped[0] if channels_out == 1 else cropped.mean(axis=0)
        if _is_mask_like(values):  # direct mask output
            full = numpy.zeros((freq, count), dtype=numpy.float32)
            full[:usable_freq, :usable_time] = numpy.clip(values, 0.0, 1.0)
            vocals = (mix_segment * full[numpy.newaxis, ...]).astype(numpy.complex64)
            return vocals, (mix_segment - vocals).astype(numpy.complex64)
        magnitude = numpy.zeros((freq, count), dtype=numpy.float32)  # magnitude fallback
        magnitude[:usable_freq, :usable_time] = numpy.abs(values)
        phase = numpy.exp(1j * numpy.angle(mix_segment))
        vocals = (magnitude[numpy.newaxis, ...] * phase).astype(numpy.complex64)
        return vocals, (mix_segment - vocals).astype(numpy.complex64)

    def separate(self, input_audio, out_dir, progress=None):
        """Separate ``input_audio`` into ``vocals.wav`` + ``instrumental.wav``."""
        numpy = _require_numpy()
        source = Path(input_audio)
        if not source.is_file():
            raise RuntimeError(f"输入音频不存在：{source}")
        self.load()
        label = source.stem
        _report(progress, "uvr", label, 0.0)
        samples, sample_rate = _read_wav(source)
        if samples.shape[1] == 0:
            raise RuntimeError(f"输入音频为空：{source}")
        if samples.shape[0] > 2:
            samples = samples[:2]
        elif samples.shape[0] == 1:
            samples = numpy.vstack([samples, samples])
        samples = _resample(samples, sample_rate, UVR_SAMPLE_RATE)
        target_frames = samples.shape[1]
        window = _hann(self._n_fft)
        padded, frame_total, pad = _prepare_frames(samples, self._n_fft, self._hop)
        vocals_acc = numpy.zeros((2, padded.shape[1]), dtype=numpy.float32)
        inst_acc = numpy.zeros((2, padded.shape[1]), dtype=numpy.float32)
        weight = numpy.zeros(padded.shape[1], dtype=numpy.float64)
        total = max(1, int(math.ceil(frame_total / self._step)))
        _report(progress, "uvr", label, 0.02)
        done = 0
        for start in range(0, frame_total, self._step):
            count = min(self._segment, frame_total - start)
            mix = _stft_segment(padded, start, count, self._n_fft, self._hop, window)
            model_input = self._model_input_from_segment(mix, count)
            try:
                outputs = self._session.run(self._output_names, {self._input_name: model_input})
            except Exception as exc:  # noqa: BLE001 - surfaced as Chinese RuntimeError
                raise RuntimeError(f"UVR MDX-Net 模型推理失败：{exc}") from exc
            vocals_segment, inst_segment = self._split_segment(outputs[0], mix)
            _ola_add(vocals_acc, weight, vocals_segment, start, self._n_fft, self._hop, window)
            _ola_add(inst_acc, None, inst_segment, start, self._n_fft, self._hop, window)
            done += 1
            _report(progress, "uvr", label, 0.02 + 0.86 * done / total)
        weight[weight < 1e-8] = 1.0
        vocals = (vocals_acc / weight[numpy.newaxis, :])[:, pad:pad + target_frames]
        instrumental = (inst_acc / weight[numpy.newaxis, :])[:, pad:pad + target_frames]
        target_dir = Path(out_dir)
        vocal_path = _write_wav_pcm16(target_dir / "vocals.wav", vocals, UVR_SAMPLE_RATE)
        inst_path = _write_wav_pcm16(target_dir / "instrumental.wav", instrumental, UVR_SAMPLE_RATE)
        _report(progress, "uvr", label, 1.0, "done")
        return {"vocals": vocal_path, "instrumental": inst_path}


# ---------------------------------------------------------------------------
# (2) whisper.cpp transcriber
# ---------------------------------------------------------------------------


def _locate_whisper_exe(config, module_result=None):
    """Configured -> ai_resources slot -> bundled dirs -> PATH."""
    section = _section(config, "whisper")
    exe = (_config_path(os.environ.get("TWILIGHT_WHISPER_EXE"))
           or _config_path(section.get("exe"))
           or _pick_path(module_result, ("exe", "cli", "binary"), (".exe", ".bat", ".cmd")))
    if exe is not None:
        return exe
    for root in (ai_home() / "models" / "whisper", ai_home() / "whisper",
                 ai_home() / "tools" / "whisper", ai_home() / "runtime" / "whisper"):
        found = _scan_for(root, WHISPER_EXE_NAMES)
        if found is not None:
            return found
    for name in WHISPER_EXE_NAMES:
        located = shutil.which(name)
        if located:
            return Path(located)
    return None


def _locate_whisper_model(config):
    section = _section(config, "whisper")
    model = (_config_path(os.environ.get("TWILIGHT_WHISPER_MODEL"))
             or _config_path(section.get("model")))
    if model is not None:
        return model
    root = ai_home() / "models" / "whisper"
    if not root.is_dir():
        return None
    for pattern in ("*q5_1*.bin", "ggml-*.bin", "*.bin", "*.gguf"):
        for candidate in sorted(root.glob(pattern)):
            if candidate.is_file():
                return candidate
    return None


def _whisper_state():
    """Discover the whisper CLI + ggml model, keeping resource error objects."""
    module = _optional_module("ai_resources")
    config = _load_config()
    state = {"module": module, "model": None, "exe": None,
             "model_error": None, "exe_error": None}
    module_result = None
    if module is not None and hasattr(module, "require_whisper"):
        try:
            module_result = module.require_whisper()
        except Exception as exc:  # noqa: BLE001 - MissingModels carries the message
            state["model_error"] = exc
        if isinstance(module_result, Mapping):
            state["model"] = _pick_path(module_result, ("model",), (".bin", ".gguf", ".ggml"))
    state["model"] = state["model"] or _locate_whisper_model(config)
    if state["model"] is None and state["model_error"] is None:
        state["model_error"] = _missing(module, WHISPER_MODEL_MISSING_MSG)
    state["exe"] = _locate_whisper_exe(config, module_result)
    if state["exe"] is None:
        state["exe_error"] = _missing(module, WHISPER_EXE_MISSING_MSG)
    return state


def _whisper_report(state):
    lines = []
    if state["model"] is None:
        lines.append(_user_message(state["model_error"]) or WHISPER_MODEL_MISSING_MSG)
    if state["exe"] is None:
        lines.append(_user_message(state["exe_error"]) or WHISPER_EXE_MISSING_MSG)
    if len(lines) == 2 and state["module"] is None:
        return WHISPER_MISSING_MSG
    return "；".join(lines)


def _raise_whisper_missing(state):
    if state["model"] is None:
        if state["exe"] is None and state["module"] is None:
            raise _missing(None, WHISPER_MISSING_MSG)
        if state["model_error"] is not None:
            raise state["model_error"]
        raise _missing(state["module"], WHISPER_MISSING_MSG if state["exe"] is None
                       else WHISPER_MODEL_MISSING_MSG)
    if state["exe"] is None:
        if state["exe_error"] is not None:
            raise state["exe_error"]
        raise _missing(state["module"], WHISPER_EXE_MISSING_MSG)


_TIMESTAMP_RE = re.compile(
    r"\[?\s*\d{1,2}:\d{2}:\d{2}(?:[.,]\d{1,3})?\s*-->\s*\d{1,2}:\d{2}:\d{2}"
    r"(?:[.,]\d{1,3})?\s*\]?")
_BLANK_TAG_RE = re.compile(
    r"\[(?:BLANK_AUDIO|BLANK AUDIO|SILENCE|MUSIC|NOISE|LAUGHTER|APPLAUSE|"
    r"静音|音乐|无声|空白音频)\]", re.IGNORECASE)


def _clean_whisper_text(raw):
    """Drop whisper timestamp tags / ``[BLANK_AUDIO]`` markers, keep spoken text."""
    lines = []
    for line in str(raw).replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = _BLANK_TAG_RE.sub(" ", _TIMESTAMP_RE.sub(" ", line))
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines).strip()


def _write_whisper_input(source, target):
    """Convert any PCM WAV into the 16 kHz mono PCM16 WAV whisper.cpp wants."""
    numpy = _require_numpy()
    samples, sample_rate = _read_wav(source)
    converted = _resample(_mono(samples), sample_rate, WHISPER_SAMPLE_RATE)
    return _write_wav_pcm16(target, converted[numpy.newaxis, :], WHISPER_SAMPLE_RATE)


class Transcriber:
    """whisper.cpp Q5_1 transcriber (subprocess), ``pywhispercpp`` optional fallback."""

    @staticmethod
    def _pywhispercpp_available():
        try:
            import pywhispercpp  # noqa: F401,PLC0415 - optional fallback
        except Exception:  # noqa: BLE001
            return False
        return True

    def available(self):
        state = _whisper_state()
        return state["model"] is not None and (
            state["exe"] is not None or self._pywhispercpp_available())

    def missing_report(self):
        report = _whisper_report(_whisper_state())
        return "" if not report and self._pywhispercpp_available() else report

    def transcribe(self, wav_path, language=None, progress=None):
        """Transcribe a PCM WAV file and return cleaned plain text."""
        state = _whisper_state()
        if state["model"] is None or (
            state["exe"] is None and not self._pywhispercpp_available()
        ):
            _raise_whisper_missing(state)
        source = Path(wav_path)
        if not source.is_file():
            raise RuntimeError(f"待识别音频不存在：{source}")
        whisper_config = _section(_load_config(), "whisper")
        chosen = language or whisper_config.get("language") or None
        if isinstance(chosen, str) and chosen.strip().lower() in ("", "auto"):
            chosen = None
        label = source.stem
        _report(progress, "whisper", label, 0.0)
        if state["exe"] is None:  # pragma: no cover - pywhispercpp is not shipped
            return self._transcribe_with_pywhispercpp(
                source, state["model"], chosen, progress, label)
        workdir = Path(tempfile.mkdtemp(prefix="twilight-whisper-"))
        try:
            converted = _write_whisper_input(source, workdir / "input16k.wav")
            output_base = workdir / "transcript"
            command = [state["exe"], "-m", state["model"], "-f", converted,
                       "-otxt", "-of", output_base]
            if chosen:
                command.extend(["-l", str(chosen)])
            if whisper_config.get("no_timestamps", True):
                command.append("--no-timestamps")
            _report(progress, "whisper", label, 0.3)
            result = _run_process(command, float(whisper_config.get("timeout", COMMAND_TIMEOUT)))
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "").strip()
                raise RuntimeError(
                    f"whisper.cpp 识别失败（退出码 {result.returncode}）："
                    f"{detail[-500:] or '无错误输出'}")
            text_path = Path(str(output_base) + ".txt")
            if not text_path.is_file():
                raise RuntimeError(f"whisper.cpp 未生成文本结果（-otxt 输出缺失）：{text_path}")
            text = _clean_whisper_text(text_path.read_text(encoding="utf-8", errors="replace"))
            _report(progress, "whisper", label, 1.0, "done")
            return text
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def _transcribe_with_pywhispercpp(
        self, source, model_path, language, progress, label
    ):  # pragma: no cover - optional package / not bundled
        try:
            from pywhispercpp.model import Model  # noqa: PLC0415
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"pywhispercpp 不可用：{exc}") from exc
        _report(progress, "whisper", label, 0.3)
        segments = Model(str(model_path)).transcribe(str(source), language=language or "auto")
        text = _clean_whisper_text("\n".join(segment.text for segment in segments))
        _report(progress, "whisper", label, 1.0, "done")
        return text


# ---------------------------------------------------------------------------
# (3) GPT-SoVITS v2 INT8 voice adapter
# ---------------------------------------------------------------------------


def _sovits_reference_dir():
    module = _optional_module("ai_resources")
    if module is not None:
        try:
            request = module.require_gpt_sovits()
            directory = _as_path(request.get("dir")) if isinstance(request, Mapping) else None
            if directory is not None:
                return directory / "reference_audio"
        except Exception:  # noqa: BLE001 - missing models must not break status
            pass
    env_dir = _config_path(os.environ.get("TWILIGHT_GPT_SOVITS_DIR"))
    if env_dir is not None:
        return env_dir / "reference_audio"
    return ai_home() / "models" / "gpt_sovits" / "reference_audio"


def _find_reference_audio():
    """User-provided reference audio files (WAV preferred, sorted)."""
    module = _optional_module("ai_resources")
    found = []
    if module is not None and hasattr(module, "find_reference_audio"):
        try:
            found = [Path(item) for item in (module.find_reference_audio() or [])]
        except Exception:  # noqa: BLE001
            found = []
    if not found:
        directory = _sovits_reference_dir()
        if directory.is_dir():
            found = sorted(item for item in directory.iterdir()
                           if item.is_file() and item.suffix.lower() in (".wav", ".flac", ".mp3"))
    wav_files = [item for item in found if item.suffix.lower() == ".wav"]
    return wav_files or found


def _sovits_state():
    """Resolved model paths, missing list, model dir and reference audio."""
    module = _optional_module("ai_resources")
    state = {"module": module, "paths": {}, "missing": [], "dir": None,
             "refs": [], "error": None}
    if module is not None and hasattr(module, "require_gpt_sovits"):
        try:
            request = module.require_gpt_sovits()
        except Exception as exc:  # noqa: BLE001 - MissingModels carries the message
            state.update(error=exc, missing=[str(exc)], refs=_find_reference_audio())
            return state
        if isinstance(request, Mapping):
            for slot in SOVITS_SLOTS:
                path = _as_path(request.get(slot))
                if path is not None and path.is_file():
                    state["paths"][slot] = path
            state["dir"] = _as_path(request.get("dir"))
            reference = request.get("reference_audio")
            if isinstance(reference, (list, tuple)):
                state["refs"] = [Path(item) for item in reference if Path(item).is_file()]
        state["missing"] = [SOVITS_NAMES[slot] for slot in SOVITS_SLOTS
                            if slot not in state["paths"]]
        if state["missing"]:
            state["error"] = _missing(module, SOVITS_MODEL_MISSING_MSG)
        if state["dir"] is None and state["paths"]:
            state["dir"] = next(iter(state["paths"].values())).parent
        if not state["refs"]:
            state["refs"] = _find_reference_audio()
        return state
    base = (_config_path(os.environ.get("TWILIGHT_GPT_SOVITS_DIR"))
            or ai_home() / "models" / "gpt_sovits")
    if base.is_dir():
        for slot in SOVITS_SLOTS:
            candidate = base / SOVITS_NAMES[slot]
            if candidate.is_file():
                state["paths"][slot] = candidate
        if len(state["paths"]) < len(SOVITS_SLOTS):
            for candidate in sorted(base.glob("*.onnx")):
                for slot in SOVITS_SLOTS:
                    if slot in candidate.name.lower() and slot not in state["paths"]:
                        state["paths"][slot] = candidate
    state["dir"] = base
    state["missing"] = [SOVITS_NAMES[slot] for slot in SOVITS_SLOTS
                        if slot not in state["paths"]]
    if state["missing"]:
        state["error"] = _missing(None, SOVITS_MODEL_MISSING_MSG)
    state["refs"] = _find_reference_audio()
    return state


def _pipeline_config_files(model_dir):
    """Optional tokenizer/config descriptors shipped inside the model pack."""
    if model_dir is None or not Path(model_dir).is_dir():
        return []
    found = []
    for pattern in ("tokenizer*.json", "vocab*.json", "config*.json", "*.yaml"):
        found.extend(path for path in sorted(Path(model_dir).glob(pattern)) if path.is_file())
    return found


def _cache_dir(base):
    try:
        target = Path(base) / ".prepared"
        target.mkdir(parents=True, exist_ok=True)
        return target
    except OSError:
        fallback = Path(tempfile.gettempdir()) / "twilight-ai-audio" / "reference"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def _split_command_template(template):
    """Parse an external command template into argv tokens (no shell involved)."""
    if isinstance(template, (list, tuple)):
        return [str(token) for token in template]
    tokens = shlex.split(str(template or "").strip(), posix=False) if str(template or "").strip() else []
    cleaned = []
    for token in tokens:
        if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
            token = token[1:-1]
        cleaned.append(token)
    return cleaned


class SoVitsVoice:
    """GPT-SoVITS v2 INT8 adapter (sessions + reference audio are real)."""

    def __init__(self):
        self._sessions, self._signatures = {}, {}
        self._model_dir = self._reference_dir = None

    def available(self):
        return not _sovits_state()["missing"]

    def missing_report(self):
        state = _sovits_state()
        lines = []
        if state["missing"]:
            lines.append(_user_message(state["error"]) or SOVITS_MODEL_MISSING_MSG)
        if not state["refs"]:
            lines.append(REFERENCE_AUDIO_MISSING_MSG)
        return "；".join(lines)

    def load(self):
        """Open and validate all four GPT-SoVITS INT8 ONNX sessions."""
        if self._sessions:
            return self
        state = _sovits_state()
        if state["missing"]:
            if state["error"] is not None:
                raise state["error"]
            raise _missing(state["module"], SOVITS_MODEL_MISSING_MSG)
        ort, providers = _onnxruntime()
        for slot in SOVITS_SLOTS:
            path = state["paths"][slot]
            session = _make_session(ort, providers, path, f"GPT-SoVITS {slot}")
            inputs, outputs = session.get_inputs(), session.get_outputs()
            if not inputs:
                raise VoiceInferenceError(f"GPT-SoVITS 模型 {path.name} 没有输入节点，模型文件可能无效")
            if not outputs:
                raise VoiceInferenceError(f"GPT-SoVITS 模型 {path.name} 没有输出节点，模型文件可能无效")
            self._sessions[slot] = session
            self._signatures[slot] = {
                "path": str(path),
                "inputs": [{"name": item.name, "shape": list(item.shape or []),
                            "type": item.type} for item in inputs],
                "outputs": [{"name": item.name, "shape": list(item.shape or []),
                             "type": item.type} for item in outputs]}
        self._model_dir = state["dir"]
        self._reference_dir = _sovits_reference_dir()
        return self

    def load_reference(self, audio=None):
        """Load + resample a reference voice sample to 32 kHz mono PCM WAV."""
        module = _optional_module("ai_resources")
        source = None
        if audio is not None:
            source = Path(audio)
            if not source.is_file():
                raise _missing(module, REFERENCE_AUDIO_MISSING_MSG)
        elif module is not None and hasattr(module, "require_reference_audio"):
            try:
                source = Path(module.require_reference_audio())
            except Exception:  # noqa: BLE001 - MissingModels carries the exact message
                raise _missing(module, REFERENCE_AUDIO_MISSING_MSG)
        else:
            references = _find_reference_audio()
            source = references[0] if references else None
        if source is None or not Path(source).is_file():
            raise _missing(module, REFERENCE_AUDIO_MISSING_MSG)
        return self._prepare_reference(Path(source))

    def _prepare_reference(self, source):
        numpy = _require_numpy()
        if source.suffix.lower() != ".wav":
            raise RuntimeError(f"参考音频格式不受支持：{source}（仅支持 PCM WAV，"
                               "请先用音频工具转换为 WAV）")
        samples, sample_rate = _read_wav(source)
        mono = _resample(_mono(samples), sample_rate, REFERENCE_SAMPLE_RATE)
        limit = int(REFERENCE_MAX_SECONDS * REFERENCE_SAMPLE_RATE)
        if mono.shape[1] > limit:
            mono = mono[:, :limit]
        try:
            stat = source.stat()
            key = hashlib.sha1(
                f"{source.resolve()}|{stat.st_size}|{stat.st_mtime_ns}".encode("utf-8")
            ).hexdigest()[:16]
        except OSError:  # pragma: no cover - stat race
            key = hashlib.sha1(str(source).encode("utf-8")).hexdigest()[:16]
        target = _cache_dir(self._reference_dir or _sovits_reference_dir()) / (
            f"ref_{key}_{REFERENCE_SAMPLE_RATE}hz.wav")
        if not target.is_file():
            _write_wav_pcm16(target, mono[numpy.newaxis, :], REFERENCE_SAMPLE_RATE)
        return target

    def _prepare_ssl_reference(self, prepared):
        """16 kHz mono copy for the HuBERT SSL front-end (deterministic prep)."""
        numpy = _require_numpy()
        samples, sample_rate = _read_wav(prepared)
        converted = _resample(_mono(samples), sample_rate, SSL_SAMPLE_RATE)
        target = _cache_dir(self._reference_dir or _sovits_reference_dir()) / (
            f"{Path(prepared).stem}_ssl16k.wav")
        if not target.is_file():
            _write_wav_pcm16(target, converted[numpy.newaxis, :], SSL_SAMPLE_RATE)
        return target

    def _default_output_path(self):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        try:
            directory = ai_home() / "outputs" / "tts"
            directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            directory = Path(tempfile.gettempdir()) / "twilight-ai-audio" / "tts"
            directory.mkdir(parents=True, exist_ok=True)
        return directory / f"sovits_{stamp}.wav"

    def _run_external_command(self, template, text, reference, out_path, progress, label):
        tokens = _split_command_template(template)
        if not tokens:
            raise VoiceInferenceError("gpt_sovits.command 模板为空，无法调用外部合成")
        command = [token.replace("{text}", text).replace("{ref}", str(reference))
                   .replace("{out}", str(out_path)) for token in tokens]
        timeout = float(_section(_load_config(), "gpt_sovits").get("command_timeout",
                                                                  COMMAND_TIMEOUT))
        _report(progress, "sovits", label, 0.2)
        try:
            result = _run_process(command, timeout)
        except subprocess.TimeoutExpired as exc:
            raise VoiceInferenceError(
                f"外部语音合成命令超时（{timeout:.0f} 秒）：{command[0]}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise VoiceInferenceError(
                f"外部语音合成命令失败（退出码 {result.returncode}）："
                f"{detail[-500:] or '无错误输出'}")
        target = Path(out_path)
        if not target.is_file() or target.stat().st_size <= 44:
            raise VoiceInferenceError(f"外部语音合成命令未生成有效 WAV 输出：{target}")
        _report(progress, "sovits", label, 1.0, "done")
        return target


# ---------------------------------------------------------------------------
# GPT-SoVITS Rust ONNX runner (gpt-sovits-onnx-rs)
# ---------------------------------------------------------------------------
_RUST_MODEL_LINKS = {
    "vits.onnx": ("sovits_fp32.onnx", "sovits_int8.onnx"),
    "ssl.onnx": ("hubert.onnx",),
    "t2s_encoder.onnx": ("gpt_prompt_encoder_int8.onnx",),
    "t2s_fs_decoder.onnx": ("gpt_int8.onnx",),
    "t2s_s_decoder.onnx": ("gpt_s_decoder_int8.onnx",),
    "bert.onnx": ("bert.onnx",),
    "g2pW.onnx": ("g2pW.onnx",),
    "sv.onnx": ("sv.onnx",),
    "tokenizer.json": ("tokenizer.json",),
    "config.json": ("config.json",),
}


def _gpt_rust_binary():
    """Locate the bundled gpt-sovits-onnx-rs CLI."""
    override = os.environ.get("TWILIGHT_GPT_SOVITS_RS")
    candidates = []
    if override:
        candidates.append(Path(override).expanduser())
    repo = Path(__file__).resolve().parent.parent
    candidates.append(repo / "ai-individuals" / "gpt-sovits" / "bin" / "gpt_sovits_demo.exe")
    bundle = getattr(sys, "_MEIPASS", "")
    if bundle:
        candidates.append(Path(bundle) / "ai-individuals" / "gpt-sovits" / "bin" / "gpt_sovits_demo.exe")
    candidates.append(ai_home() / "models" / "gpt_sovits" / "rs" / "gpt_sovits_demo.exe")
    return next((path for path in candidates if path.is_file()), None)


def _link_or_copy(source: Path, target: Path) -> None:
    if target.exists():
        return
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def _prepare_rust_model_dir(model_dir: Path, reference: Path, ref_text: str) -> Path:
    """Create the filename layout required by gpt-sovits-onnx-rs (hardlinks)."""
    rs_dir = model_dir / "rs"
    rs_dir.mkdir(parents=True, exist_ok=True)
    for target_name, sources in _RUST_MODEL_LINKS.items():
        target = rs_dir / target_name
        if target.exists():
            continue
        source = next((model_dir / name for name in sources if (model_dir / name).is_file()), None)
        if source is None:
            raise VoiceInferenceError(f"GPT-SoVITS Rust 模型文件缺失：{target_name}")
        _link_or_copy(source, target)
    g2p_target = rs_dir / "g2p_en"
    g2p_source = model_dir / "g2p_en"
    if g2p_source.is_dir():
        g2p_target.mkdir(exist_ok=True)
        for item in g2p_source.iterdir():
            if item.is_file():
                _link_or_copy(item, g2p_target / item.name)
    shutil.copy2(reference, rs_dir / "ref.wav")
    (rs_dir / "ref.txt").write_text(ref_text.strip() + "\n", encoding="utf-8")
    return rs_dir


def _rust_sovits_language(reference: Path) -> str:
    name = reference.name.lower()
    if "zh" in name or "cn" in name or "chinese" in name:
        return "zh"
    return "en"


class _RustSovitsMixin:
    def _synthesize_rust(self, text, reference, target, progress, label, binary):
        reference = Path(reference)
        ref_text_file = reference.with_suffix(".txt")
        if not ref_text_file.is_file():
            lang = _rust_sovits_language(reference)
            candidates = []
            for folder in (reference.parent, reference.parent.parent):
                candidates.extend(sorted(folder.glob("*.txt")))
            preferred = [path for path in candidates if lang in path.name.lower()]
            ref_text_file = (preferred or candidates or [None])[0]
        if ref_text_file is None or not Path(ref_text_file).is_file():
            raise VoiceInferenceError(f"参考音频缺少逐字文本：{reference.with_suffix('.txt')}")
        ref_text = Path(ref_text_file).read_text(encoding="utf-8", errors="replace").strip()
        model_dir = Path(getattr(self, "_model_dir", "") or _sovits_state()["dir"] or "")
        if not model_dir.is_dir():
            raise VoiceInferenceError("GPT-SoVITS 模型目录不存在")
        rs_dir = _prepare_rust_model_dir(model_dir, reference, ref_text)
        env = dict(os.environ)
        env["PATH"] = str(binary.parent) + os.pathsep + env.get("PATH", "")
        env["ORT_DYLIB_PATH"] = str(binary.parent / "onnxruntime.dll")
        settings = _section(_load_config(), "gpt_sovits")
        command = [
            str(binary), "--model-path", str(rs_dir), "--text", text,
            "--lang", _rust_sovits_language(reference), "--ref-text", ref_text,
            "--top-k", str(int(settings.get("top_k", 45))),
            "--top-p", str(float(settings.get("top_p", 0.95))),
            "--temperature", str(float(settings.get("temperature", 0.75))),
            "--repetition-penalty", str(float(settings.get("repetition_penalty", 1.3))),
            "--output", str(target),
        ]
        _report(progress, "sovits", label, 0.2)
        try:
            result = subprocess.run(
                command, capture_output=True, timeout=COMMAND_TIMEOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise VoiceInferenceError(f"GPT-SoVITS Rust 合成超时（{COMMAND_TIMEOUT:.0f} 秒）") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or b"").decode("utf-8", errors="replace").strip()
            raise VoiceInferenceError(
                f"GPT-SoVITS Rust 合成失败（退出码 {result.returncode}）：{detail[-500:] or '无错误输出'}")
        target = Path(target)
        if not target.is_file() or target.stat().st_size <= 44:
            raise VoiceInferenceError(f"GPT-SoVITS Rust 未生成有效 WAV：{target}")
        _report(progress, "sovits", label, 1.0, "done")
        return target

    def synthesize(self, text, reference_audio=None, out_path=None, progress=None):
        """Synthesise ``text`` into a WAV path (external command or honest error)."""
        content = (text or "").strip()
        if not content:
            raise VoiceInferenceError("待合成文本为空，无法生成语音")
        label = f"sovits-{int(time.time() * 1000) % 100000}"
        config = _section(_load_config(), "gpt_sovits")
        command = os.environ.get("TWILIGHT_GPT_SOVITS_COMMAND") or config.get("command")
        target = Path(out_path) if out_path is not None else self._default_output_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        if command:
            reference = self.load_reference(reference_audio)
            _report(progress, "sovits", label, 0.05)
            return self._run_external_command(command, content, reference, target,
                                             progress, label)
        self.load()
        rust_binary = _gpt_rust_binary()
        if rust_binary is not None:
            reference = self.load_reference(reference_audio)
            _report(progress, "sovits", label, 0.05)
            return self._synthesize_rust(content, reference, target, progress, label, rust_binary)
        reference = self.load_reference(reference_audio)
        ssl_reference = self._prepare_ssl_reference(reference)
        _report(progress, "sovits", label, 0.3)
        configs = _pipeline_config_files(self._model_dir)
        missing_config = ("未找到 tokenizer/config 描述文件："
                          + "、".join(sorted(path.name for path in configs))
                          if configs else
                          f"未找到 tokenizer/config 描述文件（目录：{self._model_dir}）")
        raise VoiceInferenceError(
            "GPT-SoVITS v2 内嵌声学推理未完成：缺少模型包内的推理描述。"
            "已完成：4 个 INT8 ONNX 会话加载与输入/输出签名校验；"
            f"参考音频已加载并重采样（{REFERENCE_SAMPLE_RATE} Hz 声学、"
            f"{SSL_SAMPLE_RATE} Hz SSL：{Path(ssl_reference).name}）；"
            f"未完成：{missing_config}、文本→音素 token 映射、GPT 自回归采样参数、"
            "SoVITS 声码器 I/O 适配。请把完整模型包（含 tokenizer.json / "
            "config.json）放入模型目录，或在 ai-config.json 的 gpt_sovits.command "
            "配置外部合成命令（占位符 {text} {ref} {out}）。")

    def status(self):
        """Chinese one-liners for the ``/voice`` command; never raises."""
        lines = []
        try:
            _, providers = _onnxruntime()
            lines.append(f"推理后端：{_backend_label(providers)}")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"推理后端：不可用（{_user_message(exc) or exc}）")
        state = _sovits_state()
        present = len(state["paths"])
        if state["missing"]:
            if present:
                lines.append(f"GPT-SoVITS v2 INT8 模型：不完整（{present}/4，缺少 "
                             + "、".join(state["missing"]) + "）")
            else:
                lines.append(f"GPT-SoVITS v2 INT8 模型缺失：{SOVITS_MODEL_MISSING_MSG}")
        else:
            lines.append("GPT-SoVITS v2 INT8 模型：已就绪（4/4 ONNX 会话可加载）")
        references = state["refs"] or _find_reference_audio()
        if references:
            lines.append(f"参考音频：{len(references)} 个可用（reference_audio/）")
        else:
            lines.append(f"参考音频：{REFERENCE_AUDIO_MISSING_MSG}")
        config = _section(_load_config(), "gpt_sovits")
        command = os.environ.get("TWILIGHT_GPT_SOVITS_COMMAND") or config.get("command")
        lines.append("外部合成命令：已配置（gpt_sovits.command）" if command
                     else "外部合成命令：未配置（内嵌声学推理需完整模型包描述）")
        configs = _pipeline_config_files(state["dir"])
        if configs:
            lines.append("模型包描述文件：已检测到 " + "、".join(path.name for path in configs))
        else:
            lines.append("模型包描述文件：未检测到 tokenizer/config（内嵌推理需要）")
        return lines

SoVitsVoice._synthesize_rust = _RustSovitsMixin._synthesize_rust
SoVitsVoice.synthesize = _RustSovitsMixin.synthesize
