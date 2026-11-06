"""AI 推理后端兼容层（Twilight Harness）。

探测 CUDA / ROCm / ZLUDA / DirectML / Vulkan / CPU 是否可用（中文原因、硬件、CPU Offload），
按 ``CUDA → ROCm → ZLUDA → DirectML → Vulkan → CPU`` 选择后端并记录降级原因，并管理运行时包
（``<backend>.zip`` / ``<backend>.tar.zst``）：解压到缓存、校验 manifest.json、把包根
加入 ``sys.path`` / DLL 搜索路径后再导入 onnxruntime。

模块导入必须轻量：顶层仅标准库，不导入 onnxruntime/numpy/torch。探测阶段也不导入
onnxruntime——一旦导入会锁死当前版本、令运行时包失效，因此探测依据发行版元数据与提供器
DLL 判断，真实列表由 :func:`get_onnxruntime` 导入后校验。失败抛
:class:`BackendUnavailable`（``.user_message`` 可直接展示，不含 traceback）。
运行时包解压由模块级 ``threading.RLock`` 保护，重复调用不会重复解压。

环境变量：``TWILIGHT_AI_HOME``（默认 ``%LOCALAPPDATA%/TwilightHarness/ai``）、
``TWILIGHT_AI_RUNTIME_PACKS``（运行时包目录，优先于 ``sys._MEIPASS/ai-runtime`` 与仓库
``assets/ai-runtime``）、``TWILIGHT_AI_FORCE_BACKEND``（等价于
``probe_backends(force_runtime=...)``，供 ``--ai-selfcheck`` 强制 CPU 等场景）。
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import logging
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

LOGGER = logging.getLogger("twilight_harness")

def _register_cuda_runtime_dirs() -> None:
    """Add portable CUDA/cuDNN DLL folders to the Windows loader search path."""
    if os.name != "nt":
        return
    roots = []
    try:
        executable = Path(sys.executable).resolve()
        roots.extend([executable.parent, executable.parent.parent])
    except Exception:
        pass
    ai_home = os.environ.get("TWILIGHT_AI_HOME", "").strip()
    if ai_home:
        roots.append(Path(ai_home).resolve().parent)
    extra = os.environ.get("TWILIGHT_CUDA_BIN", "").strip()
    if extra:
        roots.append(Path(extra))
    for root in roots:
        candidate = root / "cuda-bin"
        if not candidate.is_dir():
            continue
        try:
            if hasattr(os, "add_dll_directory"):
                os.add_dll_directory(str(candidate))
        except OSError:
            pass
        current = os.environ.get("PATH", "")
        if str(candidate) not in current:
            os.environ["PATH"] = str(candidate) + os.pathsep + current

_register_cuda_runtime_dirs()
def _register_directml_runtime() -> None:
    """Expose the optional DirectML ORT build on AMD/Intel Windows systems."""
    if os.name != "nt":
        return
    requested = os.environ.get("TWILIGHT_BACKEND", "").strip().lower()
    if requested not in {"directml", "dml", "amd", "intel"}:
        try:
            import hardware_accel
            vendor = hardware_accel.vendors()
            if (vendor.get("amd") or vendor.get("intel")) and not vendor.get("nvidia"):
                requested = "directml"
        except Exception:
            pass
    if requested not in {"directml", "dml", "amd", "intel"}:
        return
    roots = []
    try:
        executable = Path(sys.executable).resolve()
        roots.extend([executable.parent.parent / "accel" / "directml",
                      executable.parent.parent.parent / "accel" / "directml"])
    except Exception:
        pass
    ai_home = os.environ.get("TWILIGHT_AI_HOME", "").strip()
    if ai_home:
        roots.append(Path(ai_home).resolve().parent / "accel" / "directml")
    for candidate in roots:
        if candidate.is_dir():
            torch_dir = candidate / "torch241"
            if torch_dir.is_dir():
                sys.path.insert(0, str(torch_dir))
            sys.path.insert(0, str(candidate))
            try:
                if hasattr(os, "add_dll_directory"):
                    os.add_dll_directory(str(candidate))
                    if torch_dir.is_dir():
                        os.add_dll_directory(str(torch_dir))
                        os.add_dll_directory(str(torch_dir / "torch" / "lib"))
                    os.add_dll_directory(str(candidate / "onnxruntime" / "capi"))
            except OSError:
                pass
            return

_register_directml_runtime()
BACKEND_ORDER = ("cuda", "rocm", "zluda", "directml", "vulkan", "cpu")
_LABELS = {"cuda": "CUDA", "rocm": "ROCm", "zluda": "ZLUDA", "directml": "DirectML", "vulkan": "Vulkan", "cpu": "CPU"}
_PROVIDERS = {"cuda": "CUDAExecutionProvider", "rocm": "ROCMExecutionProvider", "zluda": "CUDAExecutionProvider", "directml": "DmlExecutionProvider"}
_OFFLOAD = {"cuda": True, "rocm": True, "zluda": True, "directml": False, "vulkan": None, "cpu": None}
_FORCE_ENV = "TWILIGHT_AI_FORCE_BACKEND"
_PACKS_ENV = "TWILIGHT_AI_RUNTIME_PACKS"
_MARKER = ".twilight-runtime-ok.json"
_LOCK = threading.RLock()
_DLL_HANDLES: list[object] = []  # os.add_dll_directory 句柄必须常驻，否则搜索路径失效
_RUNTIME_LABELS: dict[str, str] = {}
_ORT_PROVIDERS: set[str] | None = None
_ORT_DISTS = {"onnxruntime-gpu": "CUDAExecutionProvider", "onnxruntime-directml": "DmlExecutionProvider",
              "onnxruntime-rocm": "ROCMExecutionProvider"}
_PROVIDER_LIBS = {
    "CUDAExecutionProvider": ("onnxruntime_providers_cuda.dll", "onnxruntime_providers_cuda.so"),
    "ROCMExecutionProvider": ("onnxruntime_providers_rocm.so", "onnxruntime_providers_rocm.dll"),
    "DmlExecutionProvider": ("DirectML.dll", "onnxruntime_providers_dml.dll"),
}


class BackendUnavailable(RuntimeError):
    """后端/runtime 不可用；``user_message`` 为可直接展示的中文安全文案。"""

    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


@dataclass
class BackendInfo:
    """单个 AI 后端的探测结果。"""

    name: str
    available: bool
    reason: str = ""
    runtime: str = ""
    hardware: str = ""
    cpu_offload: bool | None = None
    notes: list[str] | None = None


def ai_home() -> Path:
    """AI 数据目录：``TWILIGHT_AI_HOME``，否则 ``%LOCALAPPDATA%/TwilightHarness/ai``。"""
    override = os.environ.get("TWILIGHT_AI_HOME")
    if override:
        return Path(override).expanduser()
    local = os.environ.get("LOCALAPPDATA")
    return Path(local) / "TwilightHarness" / "ai" if local else Path.home() / ".twilight-harness" / "ai"


def _label(name: str) -> str:
    return _LABELS.get(name, name)


def _normalize_backend(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    text = {"dml": "directml", "nvidia": "cuda", "amd": "rocm", "zluda": "zluda", "onnxruntime": "cpu", "ort": "cpu"}.get(text, text)
    return text if text in BACKEND_ORDER else None


def _offload_label(value: bool | None) -> str:
    return "不适用" if value is None else ("支持" if value else "不支持")


def _run_quiet(args: list[str], timeout: float = 4.0) -> str:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=timeout, creationflags=flags, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (done.stdout or "").strip() if done.returncode == 0 else ""


def _nvidia_hardware() -> str:
    """检测 NVIDIA 显卡型号（nvidia-smi 优先，Windows 回退 WMI），失败返回空串。"""
    name = (_run_quiet(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"]).splitlines() or [""])[0].strip()
    if not name and os.name == "nt":
        words = ("NVIDIA", "GEFORCE", "QUADRO", "RTX", "GTX", "TESLA")
        rows = _run_quiet(["wmic", "path", "win32_VideoController", "get", "name"], timeout=6.0).splitlines()
        name = next((row.strip() for row in rows if any(word in row.upper() for word in words)), "")
    return name


def _amd_hardware() -> str:
    """检测 AMD 显卡型号；ZLUDA 依赖 AMD GPU，失败返回空串。"""
    if os.name == "nt":
        rows = _run_quiet(["wmic", "path", "win32_VideoController", "get", "name"], timeout=6.0).splitlines()
        return next((row.strip() for row in rows if "AMD" in row.upper() or "RADEON" in row.upper()), "")
    try:
        text = _run_quiet(["lspci"], timeout=4.0)
    except Exception:  # noqa: BLE001
        return ""
    return next((line.strip() for line in text.splitlines() if "amd" in line.lower() or "radeon" in line.lower()), "")


def _find_zluda() -> Path | None:
    """在 ZLUDA_PATH、程序目录、System32 与 PATH 中查找 ZLUDA 组件。"""
    override = os.environ.get("ZLUDA_PATH") or os.environ.get("TWILIGHT_ZLUDA_PATH")
    roots = [Path(override).expanduser()] if override else []
    here = Path(__file__).resolve().parent
    roots += [here, here.parent]
    if os.environ.get("SystemRoot"):
        roots.append(Path(os.environ["SystemRoot"]) / "System32")
    roots.extend(Path(entry) for entry in (os.environ.get("PATH") or "").split(os.pathsep) if entry)
    names = ("zluda.exe", "zluda.dll", "libzluda.so", "zluda_ld.dll")
    for root in roots[:80]:
        try:
            for name in names:
                if (root / name).is_file():
                    return root / name
        except OSError:
            continue
    return None


def _torch_backend() -> tuple[str, str]:
    """PyTorch 报告的 GPU：返回 ``(runtime, hardware)``，没有则空串。"""
    try:
        if importlib.util.find_spec("torch") is None:
            return ("", "")
        import torch  # noqa: PLC0415 - 延迟导入，避免拖慢启动

        version = getattr(torch, "version", None)
        if getattr(version, "hip", None):
            return ("torch-rocm", f"AMD ROCm {version.hip}")
        if torch.cuda.is_available():
            return ("torch-cuda", str(torch.cuda.get_device_name(0)))
    except Exception as exc:  # noqa: BLE001 - 探测失败不能影响启动
        LOGGER.info("PyTorch 后端探测失败：%s", exc)
    return ("", "")


def _installed_ort_providers() -> set[str]:
    """不导入 onnxruntime，估计已安装版本支持的执行提供器集合。

    导入 onnxruntime 会锁定当前版本、使运行时包失效，故这里读取发行版元数据与包内
    提供器 DLL；真实列表由 :func:`get_onnxruntime` 导入后再次校验。
    """
    global _ORT_PROVIDERS
    if _ORT_PROVIDERS is not None:
        return set(_ORT_PROVIDERS)
    providers: set[str] = set()
    try:
        spec = importlib.util.find_spec("onnxruntime")
    except (ImportError, ValueError):
        spec = None
    if spec is not None:
        providers.add("CPUExecutionProvider")
        for dist, provider in _ORT_DISTS.items():
            try:
                importlib.metadata.version(dist)
            except importlib.metadata.PackageNotFoundError:
                continue
            providers.add(provider)
        try:
            capi = Path(spec.origin or "").resolve().parent / "capi"
            providers.update(p for p, libs in _PROVIDER_LIBS.items() if any((capi / lib).is_file() for lib in libs))
        except (OSError, AttributeError):
            pass
    _ORT_PROVIDERS = set(providers)
    LOGGER.info("已安装 onnxruntime 提供器（磁盘探测）：%s", "、".join(sorted(providers)) or "未检测到")
    return set(providers)


def _system_dll(name: str) -> Path | None:
    """在 System32、打包目录、程序目录与 PATH 中查找系统 DLL。"""
    roots = [Path(os.environ["SystemRoot"]) / "System32"] if os.environ.get("SystemRoot") else []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass))
    if getattr(sys, "frozen", False):
        roots.append(Path(sys.executable).resolve().parent)
    roots.extend([Path(__file__).resolve().parent.parent])
    roots.extend(Path(entry) for entry in (os.environ.get("PATH") or "").split(os.pathsep) if entry)
    for root in roots[:60]:
        try:
            if (root / name).is_file():
                return root / name
        except OSError:
            continue
    return None


def _realesrgan_tool() -> Path | None:
    """定位 Real-ESRGAN ncnn Vulkan 可执行文件。"""
    exe = "realesrgan-ncnn-vulkan.exe" if os.name == "nt" else "realesrgan-ncnn-vulkan"
    here = Path(__file__).resolve().parent
    meipass = getattr(sys, "_MEIPASS", None)
    roots = [here.parent / "tools" / "realesrgan", here / "tools" / "realesrgan"]
    if meipass:
        roots += [Path(meipass) / "realesrgan-bin", Path(meipass) / "tools" / "realesrgan"]
    for root in roots:
        try:
            if (root / exe).is_file():
                return root / exe
        except OSError:
            continue
    return None


def _find_pack(backend: str) -> Path | None:
    """按环境变量 → 打包目录 → 仓库 assets 的顺序查找 ``<backend>.zip/.tar.zst``。"""
    override = os.environ.get(_PACKS_ENV)
    meipass = getattr(sys, "_MEIPASS", None)
    directories = [Path(override).expanduser()] if override else []
    if meipass:
        directories.append(Path(meipass) / "ai-runtime")
    directories.append(Path(__file__).resolve().parent.parent / "assets" / "ai-runtime")
    for directory in dict.fromkeys(directories):
        for name in (f"{backend}.tar.zst", f"{backend}.zip"):
            try:
                if (directory / name).is_file() and (directory / name).stat().st_size > 0:
                    return directory / name
            except OSError:
                continue
    return None


def _read_marker(root: Path) -> dict | None:
    try:
        data = json.loads((root / _MARKER).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _extracted_runtime(backend: str) -> Path | None:
    """返回已解压且带完成标记的 ``<AI_HOME>/runtime/<backend>-<hash>/``。"""
    try:
        entries = sorted((ai_home() / "runtime").glob(f"{backend}-*"))
    except OSError:
        return None
    return next((root for root in entries if str((_read_marker(root) or {}).get("backend") or "") == backend), None)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_target(root: Path, name: str) -> bool:
    try:
        resolved_root, target = root.resolve(), (root / name).resolve()
    except OSError:
        return False
    return target == resolved_root or resolved_root in target.parents


def _extract_zip(pack: Path, target: Path) -> None:
    try:
        with zipfile.ZipFile(pack) as archive:
            for member in archive.infolist():
                if not _safe_target(target, member.filename):
                    raise BackendUnavailable(f"运行时包 {pack.name} 包含非法路径，已拒绝解压。")
            archive.extractall(target)
    except zipfile.BadZipFile as exc:
        raise BackendUnavailable(f"AI 运行时包已损坏或不完整：{pack.name}。请重新获取运行时包后再试。") from exc
    except OSError as exc:
        raise BackendUnavailable(f"无法解压 AI 运行时包 {pack.name}：{exc}。请检查磁盘空间与权限。") from exc


def _extract_tar_zst(pack: Path, target: Path) -> None:
    try:
        import zstandard  # noqa: PLC0415 - 仅 .tar.zst 需要
    except ImportError as exc:
        raise BackendUnavailable("缺少 zstandard 组件，无法解压 .tar.zst 运行时包；请安装 zstandard 或改用 .zip 包。") from exc
    try:
        with pack.open("rb") as raw, zstandard.ZstdDecompressor().stream_reader(raw) as reader:
            with tarfile.open(fileobj=reader, mode="r|") as archive:
                for member in archive:
                    if not _safe_target(target, member.name):
                        raise BackendUnavailable(f"运行时包 {pack.name} 包含非法路径，已拒绝解压。")
                    archive.extract(member, target, filter="data")
    except BackendUnavailable:
        raise
    except (tarfile.TarError, zstandard.ZstdError, EOFError, OSError, ValueError) as exc:
        raise BackendUnavailable(f"AI 运行时包已损坏或不完整：{pack.name}。请重新获取运行时包后再试。") from exc


def _verify_manifest(root: Path, backend: str) -> None:
    """校验可选 manifest.json：backend 必须匹配，files 中声明 sha256 的文件必须匹配。"""
    path = root / "manifest.json"
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise BackendUnavailable("运行时包中的 manifest.json 无法解析，包可能已损坏。") from exc
    if not isinstance(data, dict):
        raise BackendUnavailable("运行时包中的 manifest.json 格式无效，包可能已损坏。")
    declared = str(data.get("backend") or "").strip().lower()
    if declared and declared != backend:
        raise BackendUnavailable(f"运行时包类型不匹配：需要 {backend}，实际为 {declared}。")
    files = data.get("files") or []
    if isinstance(files, dict):
        files = [{"path": key, "sha256": value} for key, value in files.items()]
    checked = 0
    for entry in files if isinstance(files, list) else []:
        if not isinstance(entry, dict) or not entry.get("path"):
            continue
        relative = str(entry["path"]).replace("\\", "/")
        if not _safe_target(root, relative) or not (root / relative).is_file():
            raise BackendUnavailable(f"运行时包缺少文件：{relative}，包可能不完整。")
        expected = str(entry.get("sha256") or "").strip().lower()
        if expected and _sha256_file(root / relative) != expected:
            raise BackendUnavailable(f"运行时包文件校验失败：{relative}（sha256 不匹配），请重新获取运行时包。")
        checked += 1
    LOGGER.info("运行时包 manifest 校验通过：%s（%d 项）", backend, checked)


def _activate_pack(backend: str, root: Path) -> str:
    """把包根加入导入 / DLL 搜索路径并返回运行时标签。"""
    root_text = str(root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    if os.name == "nt" and hasattr(os, "add_dll_directory"):
        for sub in ("", "bin", "lib", "capi", "onnxruntime", "onnxruntime/capi"):
            try:
                directory = root / sub if sub else root
                if directory.is_dir():
                    _DLL_HANDLES.append(os.add_dll_directory(str(directory)))
            except (OSError, AttributeError):
                continue
    if root_text not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = root_text + os.pathsep + os.environ.get("PATH", "")
    os.environ[f"TWILIGHT_{backend.upper()}_RUNTIME"] = root_text
    _RUNTIME_LABELS[backend] = label = f"运行时包（{root.name}）"
    LOGGER.info("已激活 %s 运行时包：%s", _label(backend), root)
    return label


def _ensure_pack_runtime(backend: str, expected_provider: str | None) -> str:
    cached_root = _extracted_runtime(backend)
    if cached_root is not None:
        return _activate_pack(backend, cached_root)
    pack = _find_pack(backend)
    if pack is None:
        raise BackendUnavailable(
            f"未找到 {_label(backend)} 运行时：请将 {backend}.zip 或 {backend}.tar.zst 放到 "
            f"assets/ai-runtime/（或设置 {_PACKS_ENV}），或安装支持 {_label(backend)} 的 onnxruntime。"
        )
    cache_base = ai_home() / "runtime"
    try:
        cache_base.mkdir(parents=True, exist_ok=True)
        digest = _sha256_file(pack)
    except OSError as exc:
        raise BackendUnavailable(f"无法准备 AI 运行时缓存（{cache_base}）：{exc}。请检查磁盘空间与权限。") from exc
    target = cache_base / f"{backend}-{digest[:12]}"
    if str((_read_marker(target) or {}).get("backend") or "") == backend:
        return _activate_pack(backend, target)
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
        if target.exists():
            raise BackendUnavailable(f"无法清理损坏的 AI 运行时缓存：{target}。请关闭程序后删除该目录再试。")
    temp_dir = Path(tempfile.mkdtemp(prefix=f".{backend}-", dir=str(cache_base)))
    try:
        (_extract_zip if pack.name.lower().endswith(".zip") else _extract_tar_zst)(pack, temp_dir)
        _verify_manifest(temp_dir, backend)
        try:
            os.replace(str(temp_dir), str(target))
        except OSError as exc:
            raise BackendUnavailable(f"无法写入 AI 运行时缓存：{target}（{exc}）。") from exc
        label = _activate_pack(backend, target)
        try:
            import onnxruntime  # noqa: PLC0415 - 激活运行时后才导入

            providers = [str(provider) for provider in onnxruntime.get_available_providers()]
            if expected_provider and expected_provider not in providers:
                raise BackendUnavailable(
                    f"{_label(backend)} 运行时缺少 {expected_provider} 执行提供器（实际：{'、'.join(providers) or '无'}）；"
                    "请更换匹配的运行时包，或改用 CPU 后端。"
                )
        except BackendUnavailable:
            while str(target) in sys.path:
                sys.path.remove(str(target))
            _RUNTIME_LABELS.pop(backend, None)
            shutil.rmtree(target, ignore_errors=True)
            raise
        except Exception as exc:  # noqa: BLE001 - 导入失败按运行时损坏处理
            while str(target) in sys.path:
                sys.path.remove(str(target))
            _RUNTIME_LABELS.pop(backend, None)
            shutil.rmtree(target, ignore_errors=True)
            raise BackendUnavailable(
                f"{_label(backend)} 运行时包已解压，但无法导入 onnxruntime；请确认运行时包完整、与当前 Python 版本匹配后重试。"
            ) from exc
        try:
            marker = {"backend": backend, "pack": pack.name, "sha256": digest,
                      "created": time.strftime("%Y-%m-%d %H:%M:%S")}
            (target / _MARKER).write_text(json.dumps(marker, ensure_ascii=False, indent=2), "utf-8")
        except OSError as exc:
            LOGGER.warning("写入运行时完成标记失败：%s", exc)
        LOGGER.info("已解压 %s 运行时包：%s -> %s", _label(backend), pack.name, target)
        return label
    finally:
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)


def ensure_runtime(backend: str) -> str:
    """让指定后端的推理运行时可用，返回短标签；失败抛 :class:`BackendUnavailable`。"""
    name = _normalize_backend(backend)
    if name is None:
        raise BackendUnavailable(f"不支持的后端“{backend}”，可选后端：CUDA、ROCm、ZLUDA、DirectML、Vulkan、CPU。")
    with _LOCK:
        if name in _RUNTIME_LABELS:
            return _RUNTIME_LABELS[name]
        if name == "vulkan":
            if _realesrgan_tool() is None:
                raise BackendUnavailable("Vulkan 后端依赖 realesrgan-ncnn-vulkan 工具，当前未找到该程序；请重新安装完整版本，或改用 CPU 后端。")
            _RUNTIME_LABELS[name] = "realesrgan-ncnn-vulkan"
            return "realesrgan-ncnn-vulkan"
        installed = _installed_ort_providers()
        if name == "cpu" and installed:
            LOGGER.info("CPU 后端直接使用已安装的 onnxruntime，无需运行时包")
            _RUNTIME_LABELS[name] = "onnxruntime-cpu"
            return "onnxruntime-cpu"
        expected = _PROVIDERS.get(name)
        if expected and expected in installed:
            LOGGER.info("%s 后端使用已安装的 onnxruntime（提供器 %s）", _label(name), expected)
            _RUNTIME_LABELS[name] = f"onnxruntime-{name}"
            return f"onnxruntime-{name}"
        return _ensure_pack_runtime(name, expected)


def get_onnxruntime(backend: str) -> tuple[object, list[str]]:
    """确保运行时后导入 onnxruntime，返回 ``(模块, 提供器名称列表)``；模块导入保持延迟。"""
    name = _normalize_backend(backend)
    if name is None:
        raise BackendUnavailable(f"不支持的后端“{backend}”，可选后端：CUDA、ROCm、ZLUDA、DirectML、Vulkan、CPU。")
    label = ensure_runtime(name)
    if name == "vulkan":
        LOGGER.info("Vulkan 仅用于 Real-ESRGAN（ncnn）放大，ONNX 推理使用已安装的 onnxruntime")
    try:
        import onnxruntime  # noqa: PLC0415 - 必须延迟导入
    except Exception as exc:  # noqa: BLE001
        raise BackendUnavailable(
            f"无法加载 {_label(name)} 的 onnxruntime 推理运行时（{label}）；请检查运行时包是否完整，或安装 CPU 版 onnxruntime 作为兜底。"
        ) from exc
    providers = [str(provider) for provider in onnxruntime.get_available_providers()]
    expected = _PROVIDERS.get(name)
    if expected and expected not in providers:
        raise BackendUnavailable(
            f"{_label(name)} 运行时未提供 {expected}，无法执行该后端的 ONNX 推理（当前可用：{'、'.join(providers) or '无'}）。"
            "请安装匹配的运行时包，或在 /ai 中改用 CPU 后端。"
        )
    LOGGER.info("onnxruntime 就绪：后端=%s 标签=%s 提供器=%s", name, label, "、".join(providers))
    return onnxruntime, providers


def _probe_cuda() -> BackendInfo:
    info = BackendInfo("cuda", False, cpu_offload=True, hardware=_nvidia_hardware())
    torch_runtime, torch_hardware = _torch_backend()
    if "CUDAExecutionProvider" in _installed_ort_providers():
        info.available, info.runtime = True, "onnxruntime-cuda"
    elif torch_runtime == "torch-cuda":
        info.available, info.runtime, info.hardware = True, torch_runtime, torch_hardware or info.hardware
    else:
        cached_root = _extracted_runtime("cuda")
        pack = _find_pack("cuda") if cached_root is None else None
        if cached_root is not None:
            info.available, info.runtime = True, f"运行时包（{cached_root.name}）"
        elif pack is not None:
            info.available, info.runtime = True, f"运行时包（待解压：{pack.name}）"
            info.notes = ["运行时包将在首次推理时解压并校验 manifest"]
    if not info.available:
        info.reason = (f"未找到 CUDA 运行时包（已检测到 NVIDIA 硬件：{info.hardware}）" if info.hardware
                       else "未找到 CUDA 运行时包，且未检测到 NVIDIA 显卡或支持 CUDA 的 onnxruntime")
    return info


def _probe_rocm() -> BackendInfo:
    if os.name == "nt":
        return BackendInfo("rocm", False, reason="ROCm 在 Windows 目标上不可用（仅 Linux）", cpu_offload=True)
    info = BackendInfo("rocm", False, cpu_offload=True)
    torch_runtime, torch_hardware = _torch_backend()
    rocm_path = os.environ.get("ROCM_PATH") or os.environ.get("HIP_PATH") or ""
    if "ROCMExecutionProvider" in _installed_ort_providers():
        info.available, info.runtime = True, "onnxruntime-rocm"
    elif torch_runtime == "torch-rocm":
        info.available, info.runtime = True, torch_runtime
    elif rocm_path and Path(rocm_path).is_dir() and (_extracted_runtime("rocm") is not None or _find_pack("rocm") is not None):
        info.available, info.runtime = True, "运行时包"
    else:
        info.reason = "未找到 ROCm 运行时（仅 Linux 目标支持，需要 ROCM_PATH 或 ROCm 版 onnxruntime）"
    info.hardware = torch_hardware or (f"AMD ROCm（{rocm_path}）" if rocm_path else "")
    return info


def _probe_zluda() -> BackendInfo:
    info = BackendInfo("zluda", False, cpu_offload=True, hardware=_amd_hardware())
    found = _find_zluda()
    if found is None:
        info.reason = "未找到 ZLUDA 组件（zluda.exe / zluda.dll），且没有可用的 CUDA 转译运行时"
        return info
    if not info.hardware and os.name == "nt":
        info.reason = "已找到 ZLUDA 组件，但未检测到 AMD 显卡；ZLUDA 通常只用于 AMD GPU"
        return info
    info.available = True
    info.runtime = f"ZLUDA（{found.name}）"
    info.notes = ["ZLUDA 将 CUDA 调用转译到 AMD GPU；需要匹配的 ZLUDA 版本与 AMD 驱动"]
    return info


def _probe_directml() -> BackendInfo:
    info = BackendInfo("directml", False, cpu_offload=False)
    dll = _system_dll("directml.dll")
    provider_installed = "DmlExecutionProvider" in _installed_ort_providers()
    cached_root = _extracted_runtime("directml")
    pack = _find_pack("directml") if cached_root is None else None
    if dll is None:
        info.reason = "未找到 directml.dll（DirectML 系统组件）"
    elif not (provider_installed or cached_root is not None or pack is not None):
        info.reason = "未找到 onnxruntime-directml 运行时（可放置 directml.zip / directml.tar.zst 运行时包）"
    else:
        info.available = True
        info.runtime = "onnxruntime-directml" if provider_installed else f"运行时包（{cached_root.name if cached_root is not None else '待解压'}）"
        info.notes = ["DirectML 不支持 CPU Offload"]
    return info


def _probe_vulkan() -> BackendInfo:
    info = BackendInfo("vulkan", False, cpu_offload=None)
    loader, tool = _system_dll("vulkan-1.dll"), _realesrgan_tool()
    if loader is not None and tool is not None:
        info.available = True
        info.runtime = "realesrgan-ncnn-vulkan"
        info.hardware = f"Vulkan 加载器（{loader.parent}）"
        info.notes = ["Vulkan 仅用于 Real-ESRGAN（ncnn）图像放大；通用 ONNX 推理不可用"]
        return info
    info.reason = "未找到 Vulkan 组件：" + "、".join(
        name for name, value in (("vulkan-1.dll", loader), ("realesrgan-ncnn-vulkan 工具", tool)) if value is None)
    return info


def _probe_cpu() -> BackendInfo:
    installed = _installed_ort_providers()
    LOGGER.info("CPU 后端始终可用（onnxruntime %s）", "已安装" if installed else "未安装，将使用运行时包")
    return BackendInfo("cpu", True, runtime="onnxruntime（已安装）" if installed else "CPU 运行时包（待解压）",
                       cpu_offload=None, notes=["CPU Offload 不适用"])


def probe_backends(force_runtime: str | None = None) -> list[BackendInfo]:
    """探测全部后端，按 :data:`BACKEND_ORDER` 顺序返回。

    ``force_runtime``（或 ``TWILIGHT_AI_FORCE_BACKEND``）用于自检：强制指定后端为可用，
    其余非 CPU 后端标记为不可用并附中文原因（CPU 始终可用）。
    """
    requested = force_runtime if force_runtime is not None else os.environ.get(_FORCE_ENV)
    forced = _normalize_backend(requested) if requested else None
    if requested and forced is None:
        LOGGER.warning("未知的强制运行时后端“%s”，已忽略该强制设置", requested)
    result: list[BackendInfo] = []
    for probe in (_probe_cuda, _probe_rocm, _probe_zluda, _probe_directml, _probe_vulkan, _probe_cpu):
        try:
            info = probe()
        except Exception as exc:  # noqa: BLE001 - 单个后端探测失败不能影响整体
            name = probe.__name__.removeprefix("_probe_")
            LOGGER.exception("探测 AI 后端 %s 时发生异常", name)
            info = BackendInfo(name, False, reason=f"探测失败（内部错误：{exc}）", cpu_offload=_OFFLOAD.get(name))
        LOGGER.info("AI 后端探测：%s 可用=%s 原因=%s 运行时=%s 硬件=%s",
                    info.name, info.available, info.reason or "-", info.runtime or "-", info.hardware or "-")
        result.append(info)
    if forced is not None:
        LOGGER.info("已强制指定 AI 运行时后端：%s", _label(forced))
        for info in result:
            if info.name == forced:
                if not info.available:
                    info.notes = list(info.notes or []) + [f"强制指定的运行时将在首次推理时验证（原探测：{info.reason}）"]
                info.available, info.reason, info.runtime = True, "", "强制指定"
            elif info.name != "cpu":
                info.available, info.reason = False, f"已强制指定运行时：{_label(forced)}"
    return result


def _choose(backends: list[BackendInfo], prefer: str | None = None,
            strict_cpu_offload: bool = True) -> tuple[BackendInfo, list[str]]:
    by_name = {info.name: info for info in backends}
    notes: list[str] = []

    def note(message: str) -> None:
        if message not in notes:
            notes.append(message)

    def skip(info: BackendInfo) -> str:
        return f"未使用 {_label(info.name)}：{info.reason or '不可用'}"

    prefer_name = _normalize_backend(prefer) if prefer else None
    if prefer and prefer_name is None:
        note(f"未知的后端名称“{prefer}”，已忽略该偏好")
        LOGGER.warning("未知的后端偏好“%s”，已忽略", prefer)
    if prefer_name is not None:
        target = by_name.get(prefer_name)
        if target is None or not target.available:
            reason = f"：{target.reason}" if target is not None and target.reason else ""
            note(f"请求的后端 {_label(prefer_name)} 不可用{reason}；已自动降级")
            LOGGER.info("请求的后端 %s 不可用，自动降级", prefer_name)
        elif strict_cpu_offload and target.cpu_offload is False:
            note(f"请求的后端 {_label(prefer_name)} 不支持 CPU Offload（strict_cpu_offload=True）；已自动降级")
            LOGGER.info("请求的后端 %s 不支持 CPU Offload，自动降级", prefer_name)
        else:
            for info in backends:
                if info.name == prefer_name:
                    break
                note(skip(info) if not info.available else f"未使用 {_label(info.name)}：已按偏好选择 {_label(prefer_name)}")
            LOGGER.info("按偏好选择 AI 后端：%s", prefer_name)
            return target, notes
    for info in backends:
        if not info.available:
            note(skip(info))
            continue
        if strict_cpu_offload and info.cpu_offload is False:
            note(f"未使用 {_label(info.name)}：不支持 CPU Offload（strict_cpu_offload=True）")
            LOGGER.info("跳过后端 %s：不支持 CPU Offload", info.name)
            continue
        LOGGER.info("已选择 AI 后端：%s（降级说明 %d 条）", info.name, len(notes))
        return info, notes
    fallback = by_name.get("cpu") or BackendInfo("cpu", True, cpu_offload=None)
    note("已回退到 CPU 后端")
    LOGGER.warning("所有 AI 后端均不可用，回退 CPU")
    return fallback, notes


def select_backend(prefer: str | None = None, strict_cpu_offload: bool = True) -> tuple[BackendInfo, list[str]]:
    """按优先级选择后端，返回 ``(BackendInfo, 降级说明列表)``。"""
    return _choose(probe_backends(), prefer=prefer, strict_cpu_offload=strict_cpu_offload)


def cpu_offload_support(backend: str) -> bool | None:
    """CUDA/ROCm/ZLUDA 支持 CPU Offload，DirectML 不支持，Vulkan/CPU 不适用。"""
    name = _normalize_backend(backend)
    if name is None:
        LOGGER.warning("未知 AI 后端：%s", backend)
        return None
    return _OFFLOAD[name]


def backend_report_lines(backends: list[BackendInfo] | None = None) -> list[str]:
    """生成 /ai 命令使用的中文报告：当前选择 + 各后端状态 + 硬件 + 降级说明。"""
    infos = list(backends) if backends else probe_backends()
    selected, degradation = _choose(infos)
    lines = ["AI 推理后端优先级：" + " → ".join(_label(name) for name in BACKEND_ORDER)]
    headline = f"当前选择：{_label(selected.name)}（CPU Offload：{_offload_label(selected.cpu_offload)}，运行时：{selected.runtime or '待确定'}"
    if selected.hardware:
        headline += f"，硬件：{selected.hardware}"
    lines.append(headline + "）")
    for info in infos:
        if info.available:
            line = f"- {_label(info.name)}：可用；运行时：{info.runtime or '未知'}"
            if info.hardware:
                line += f"；硬件：{info.hardware}"
            line += f"；CPU Offload：{_offload_label(info.cpu_offload)}"
        else:
            line = f"- {_label(info.name)}：不可用；原因：{info.reason or '未知'}"
            if info.hardware:
                line += f"；硬件：{info.hardware}"
        lines.append(line + "".join(f"；说明：{item}" for item in info.notes or []))
    if degradation:
        lines.append("降级说明：" + "；".join(degradation))
    LOGGER.info("生成 AI 后端报告：选择 %s，降级 %d 条", selected.name, len(degradation))
    return lines
