"""RIFE ONNX frame interpolation used by /frame."""
from __future__ import annotations

import os
from pathlib import Path
import numpy as np


def model_path() -> Path | None:
    explicit = os.environ.get("TWILIGHT_RIFE_MODEL", "").strip()
    candidates = [Path(explicit)] if explicit else []
    ai_home = os.environ.get("TWILIGHT_AI_HOME", "").strip()
    if ai_home:
        candidates.append(Path(ai_home) / "models" / "rife" / "RIFE_fp32" / "RIFE_fp32.onnx")
    candidates.append(Path(__file__).resolve().parent.parent.parent / "_ai_install" / "TwilightHarnessPortable" / "ai" / "models" / "rife" / "RIFE_fp32" / "RIFE_fp32.onnx")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    # Auto-download the ONNX model when missing.
    try:
        from huggingface_hub import hf_hub_download
        target_root = candidates[-1].parent if candidates else Path.cwd()
        target_root.mkdir(parents=True, exist_ok=True)
        target = Path(hf_hub_download(
            "FuryTMP/RIFE_fp32", "RIFE_fp32.onnx",
            local_dir=str(target_root), local_dir_use_symlinks=False,
        ))
        if target.is_file():
            return target
    except Exception:
        pass
    return None


def _providers(gpu_index: int | None = None) -> list:
    if gpu_index is not None:
        try:
            import ai_compat
            ai_compat.get_onnxruntime("cuda")
        except Exception:
            pass
        return [("CUDAExecutionProvider", {"device_id": int(gpu_index)}), "CPUExecutionProvider"]
    try:
        import ai_compat
        info, _ = ai_compat.select_backend()
        name = getattr(info, "name", "cpu")
        _module, providers = ai_compat.get_onnxruntime(name)
        return [item for item in providers if item != "CPUExecutionProvider"] + ["CPUExecutionProvider"]
    except Exception:
        return ["CPUExecutionProvider"]


class RifeEngine:
    def __init__(self, gpu_index: int | None = None) -> None:
        if gpu_index is not None:
            import os
            candidates = []
            explicit = os.environ.get("TWILIGHT_CUDA_BIN", "").strip()
            if explicit:
                candidates.append(Path(explicit))
            ai_home = os.environ.get("TWILIGHT_AI_HOME", "").strip()
            if ai_home:
                candidates.append(Path(ai_home).resolve().parent / "cuda-bin")
            candidates.append(Path(__file__).resolve().parent.parent.parent / "_ai_install" / "TwilightHarnessPortable" / "cuda-bin")
            for candidate in candidates:
                if candidate.is_dir():
                    try:
                        os.add_dll_directory(str(candidate))
                    except OSError:
                        pass
                    os.environ["PATH"] = str(candidate) + os.pathsep + os.environ.get("PATH", "")
                    break
        import onnxruntime as ort
        path = model_path()
        if path is None:
            raise FileNotFoundError("RIFE ONNX model not found")
        self.gpu_index = gpu_index
        self.session = ort.InferenceSession(str(path), providers=_providers(gpu_index))
        self.input = self.session.get_inputs()[0].name

    def midpoint(self, first: np.ndarray, second: np.ndarray) -> np.ndarray:
        """first/second: HxWx3 float32 in [0,1]."""
        h, w = first.shape[:2]
        pad_h = (-h) % 32
        pad_w = (-w) % 32
        if pad_h or pad_w:
            first = np.pad(first, ((0, pad_h), (0, pad_w), (0, 0)), mode="edge")
            second = np.pad(second, ((0, pad_h), (0, pad_w), (0, 0)), mode="edge")
        batch = np.concatenate([first, second], axis=2).transpose(2, 0, 1)[None].astype(np.float32)
        output = self.session.run(None, {self.input: batch})[0][0].transpose(1, 2, 0)
        output = np.clip(output, 0.0, 1.0)
        return output[:h, :w]

    def between(self, first: np.ndarray, second: np.ndarray, count: int) -> list[np.ndarray]:
        """Return count-1 intermediate frames (count=2 -> one midpoint)."""
        if count <= 1:
            return []
        if count == 2:
            return [self.midpoint(first, second)]
        mid = self.midpoint(first, second)
        left_count = count // 2
        right_count = count - left_count
        left = self.between(first, mid, left_count)
        right = self.between(mid, second, right_count)
        return left + [mid] + right
