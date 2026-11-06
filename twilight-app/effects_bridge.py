"""Bridge between the Twilight sidecar and the local media effect toolchain."""
from __future__ import annotations

import os
from pathlib import Path

_ENGINE = None


def tools_dir() -> Path | None:
    explicit = os.environ.get("BEOLD_TOOLS_DIR", "").strip()
    candidates = [Path(explicit)] if explicit else []
    ai_home = os.environ.get("TWILIGHT_AI_HOME", "").strip()
    if ai_home:
        candidates.append(Path(ai_home).resolve().parent / "tools")
    candidates.append(Path(__file__).resolve().parent.parent.parent / "_ai_install" / "TwilightHarnessPortable" / "tools")
    for candidate in candidates:
        if (candidate / "ffmpeg" / "bin" / "ffmpeg.exe").is_file():
            return candidate
    return None


def engine():
    global _ENGINE
    if _ENGINE is None:
        folder = tools_dir()
        if folder is None:
            raise RuntimeError("媒体工具链缺失")
        os.environ["BEOLD_TOOLS_DIR"] = str(folder)
        from effects_core import BeoldEngine
        _ENGINE = BeoldEngine(gpu=True)
    return _ENGINE


def output_dir() -> Path:
    ai_home = os.environ.get("TWILIGHT_AI_HOME", "").strip()
    base = Path(ai_home) / "outputs" / "effects" if ai_home else Path.cwd() / "effects_outputs"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _unique(folder: Path, stem: str, suffix: str) -> Path:
    candidate = folder / f"{stem}{suffix}"
    index = 2
    while candidate.exists():
        candidate = folder / f"{stem}_{index}{suffix}"
        index += 1
    return candidate


def _progress(progress, fraction, label):
    if progress is None:
        return
    try:
        progress({"event": "progress", "label": label, "fraction": max(0.0, min(1.0, float(fraction)))})
    except Exception:
        pass


def _opencl_run(mode: str, variable: str, value: float, source: Path, progress) -> Path | None:
    if os.environ.get("TWILIGHT_OPENCL_RENDERER", "1") in {"0", "false", "no"}:
        return None
    try:
        from opencl_renderer import OpenCLRenderer
        renderer = OpenCLRenderer()
        folder = output_dir()
        if mode == "patina" and source.suffix.lower() in {".gif", ".apng"}:
            return None
        if mode == "patina":
            target = _unique(folder, f"{source.stem}_PATINA_OCL", ".jpg")
            _progress(progress, 0.1, "OpenCL 包浆")
            return renderer.patina_image(source, target, value)
        if mode == "squeeze":
            import local_actions
            target = _unique(folder, f"{source.stem}_SQUEEZE_OCL", ".mp4")
            _progress(progress, 0.1, "OpenCL 压片")
            return renderer.squeeze_video(source, target, value,
                                          Path(local_actions._ffmpeg()))
    except Exception:
        return None
    return None


def run(mode: str, variable: str, value: float, source: Path, progress=None) -> Path:
    rendered = _opencl_run(mode, variable, value, source, progress)
    if rendered is not None:
        _progress(progress, 1.0, "完成")
        return rendered
    callback = None
    if progress is not None:
        def callback(fraction, text):  # noqa: ANN001
            try:
                progress({"event": "progress", "label": text or "媒体处理中...",
                          "fraction": max(0.0, min(1.0, float(fraction or 0) / 100.0))})
            except Exception:
                pass
    result = engine().run(source, output_dir(), mode, **{variable: float(value)})
    return Path(result.output)
