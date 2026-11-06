"""Model-requested local tool calls and their execution.

The model may append commands such as:
  [[LOCAL_TOOL: {"tool":"derpi","query":"sunny starscout"}]]
to its reply.  The app strips these commands from the visible answer, executes
them locally, and displays the resulting files.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import derpi
import feature_flags

LOGGER = logging.getLogger("twilight_harness")


@dataclass
class ToolOutcome:
    tool: str
    paths: list[str] = field(default_factory=list)
    error: str = ""
    params: dict = field(default_factory=dict)
    text: str = ""
    previews: list[str] = field(default_factory=list)


def system_prompt(allow_file_tools: bool) -> str:
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    lines = [
        "你可以在回复末尾请求本地工具执行。格式必须是独立的一行或多行：",
        '[[LOCAL_TOOL: {"tool":"derpi","query":"..."}]]',
        '[[LOCAL_TOOL: {"tool":"qwen_image","prompt":"1girl, ...","negative":"..."}]]',
        "可用工具：",
        '- qwen_image：用本地 Qwen-Image-2.1-Turbo 生成高质量图片。参数：prompt（必填，英文或中文短句）、negative（可选）、size（可选，如 "768x768"）。质量优先时使用。',
        '  当用户要求“生成/画/做一张图片、插图、立绘、场景”时，优先使用 image（内部优先 Qwen，失败回退 SD1.5），而不是 derpi。',
        '- image：优先使用 Qwen-Image-2.1-Turbo Universal INT4 Adaptive 生成图片；Qwen 不可用时自动回退 Stable Diffusion 1.5。参数：prompt（必填）、negative（可选）、size（可选，如 "960x720"）、count（可选，1-4 张）、engine（可选：auto/sd15）。',
        '  用户明确要求 SD1.5 时传 engine="sd15"。',
        '  本轮附加了图片时，qwen_image/image 会把它作为参考图；用户说参考/照这张图/基于此图时直接使用该附件。',
        '- qwen_edit：对最新附加图片执行真正的逐像素 image-to-image 编辑。参数：instruction（必填，具体改哪里，如“把背景改成夜晚”“把头发改成蓝色”）。',
        '- plot：用 function-plot 绘制二维函数图。参数：expressions（必填，例如 "x^2; sin(x)"）、xmin/xmax（默认 -5/5）、ymin/ymax（可选）、title（可选）。',
        '  如果用户只说“我想改这张图/修改图片”但没有说明改哪里，先用一句话询问“想改哪里？”，不要调用 qwen_edit。',
        '  需要修改已有图片时必须使用 qwen_edit，不要用 qwen_image 的参考图模式代替。',
        '  derpi 只用于“搜索/找/下载已有图片”，不要用它代替生成。', 
        '- derpi：按用户需求搜索 Derpibooru（safe / suggestive / semi-grimdark），并从 derpicdn.net 下载到用户的 Downloads 文件夹。参数：query（必填）、limit（可选，1-5000；用户未指定数量时使用 10）。',
        '  用户没有明确说明数量时不要填写 limit，应用默认抓取 10 张。',
        '  query 必须使用逗号分隔的 Derpibooru 标签，标签内部的括号、英文句点、感叹号和问号必须原样保留；不要在 query 中加入 safe/suggestive/semi-grimdark 等评级词。示例："twilight sparkle (alicorn), alicorn"。',
        f'  当前日期是 {today}。用户如果给出时间段，请转换为 after/before 的 YYYY-MM-DD 日期；如果用户没有说时间段，则不要填写 after/before，应用会自动选择最新图片。',
        '  时间范围内结果超过 limit 时应用会自动随机挑选 limit 张；不需要你处理随机逻辑。',
    ]
    if allow_file_tools:
        lines += [
            '- age：对最新附加图片做旧（重复低质量 JPEG 缩放），模拟老照片；无参数。',
        '- code_review：审查本轮附加的代码文件，输出严重缺陷、边界条件、安全问题和精简建议；无参数。',
        '- compress：对最新附加视频执行压片；参数 value（0–1，默认 0.7）。',
        '- vhs：对最新附加视频执行 VHS 化；参数 value（0–1，默认 0.44）。用户说 VHS/录像带/老化效果时必须调用此工具，不要只回复文字。',
        '- video：用 MiniMax H3 生成视频（带音频）。参数：prompt（必填）。本轮附加图片时作为首帧/参考图。',
        '- upscale：用 Real-ESRGAN x4 动漫模型放大最新附加的图片；无参数。',
            '- video_upscale：对最新附加视频逐帧执行 Real-ESRGAN x4 超分，并保留音轨；无参数。',
            '- audio：分离最新附加视频的音轨；无参数。',
            '- transcode：转码最新附加视频。参数：format（mp4/webm/mkv/mov/gif）、video_bitrate（如 "2M"）、resolution（如 "720p"/"1280x720"）、audio_bitrate（如 "128k"）、fps（可选）。',
            '  当用户说“压片/极限压缩”时，使用 compress 工具（Beold SQUEEZE），不要使用 transcode。',
            "需要图片/视频附件的工具只能使用本轮用户最新附加的附件。",
        ]
    lines += [
        "只有在用户明确要求对应本地操作时才输出工具命令；不要声称文件已经下载或处理完成，应用会在回复后执行并展示结果。一次回复最多请求 3 个工具。",
        "当附件是代码文件（.py/.js/.ts/.cpp/.java 等）时，默认进行代码审查：指出严重缺陷、边界条件、性能与安全问题，并给出可直接替换的补全或精简补丁；用户只要求解释时不要擅自改文件。",
    ]
    return "\n".join(lines)


def review_source(code: str, name: str = "code") -> str:
    """Static review of a code string, used by /review and the local tool."""
    import ast
    import re as _re
    findings = []
    try:
        tree = ast.parse(code)
        funcs = sum(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) for node in ast.walk(tree))
        classes = sum(isinstance(node, ast.ClassDef) for node in ast.walk(tree))
        findings.append(f"函数 {funcs} 个，类 {classes} 个")
    except SyntaxError as exc:
        findings.append(f"严重：语法错误第 {exc.lineno} 行：{exc.msg}")
    long_lines = [i for i, line in enumerate(code.splitlines(), 1) if len(line) > 120]
    if long_lines:
        findings.append(f"过长行：{len(long_lines)} 处（示例第 {long_lines[:5]} 行）")
    todos = len(_re.findall(r"\b(TODO|FIXME|XXX)\b", code))
    if todos:
        findings.append(f"TODO/FIXME：{todos} 处")
    if "except:" in code or "except Exception:" in code:
        findings.append("存在宽泛异常捕获，建议缩小异常范围")
    if "eval(" in code or "exec(" in code:
        findings.append("发现 eval/exec，存在代码注入风险")
    if "subprocess" in code and "shell=True" in code:
        findings.append("subprocess 使用 shell=True，存在命令注入风险")
    return f"## {name}\n" + "\n".join(f"- {item}" for item in findings)


def _valid_call(call: Any, allow_file_tools: bool) -> bool:
    if not isinstance(call, dict):
        return False
    tool = str(call.get("tool") or "").strip().lower()
    allowed = {"derpi", "image", "qwen_image", "qwen_edit", "plot", "age", "code_review", "compress", "vhs", "video"}
    if allow_file_tools:
        allowed |= {"upscale", "video_upscale", "audio", "transcode"}
    if tool == "video" and not feature_flags.H3_ENABLED:
        return False
    if tool in {"qwen_image", "qwen_edit", "image"} and not feature_flags.LOCAL_MEDIA_ENABLED:
        return False
    if tool == "qwen_image" and feature_flags.IMAGE_ENGINE != "qwen":
        return False
    if tool in {"age", "compress", "vhs", "upscale", "video_upscale", "audio", "transcode", "code_review"} and not feature_flags.ATTACHMENTS_ENABLED:
        return False
    return tool in allowed


def extract_calls(text: str) -> tuple[str, list[dict]]:
    """Remove tool-command markers from model output and return parsed calls."""
    if not text:
        return text or "", []
    calls: list[dict] = []
    cleaned = text

    pattern = re.compile(r"\[\[\s*LOCAL_TOOL\s*:\s*(\{.*?\})\s*\]\]", re.DOTALL | re.IGNORECASE)
    for match in pattern.finditer(text):
        try:
            call = json.loads(match.group(1))
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(call, dict):
            calls.append(call)
    cleaned = pattern.sub("", cleaned)

    fenced = re.compile(
        r"```(?:local-tool|localtool)\s*\n(.*?)\n```",
        re.IGNORECASE | re.DOTALL,
    )
    for match in fenced.finditer(text):
        try:
            payload = json.loads(match.group(1))
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(payload, dict):
            calls.append(payload)
        elif isinstance(payload, list):
            calls.extend(item for item in payload if isinstance(item, dict))
    cleaned = fenced.sub("", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, calls[:3]


def _find_attachment(attachments: list, kind: str):
    suffix_sets = {
        "image": {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"},
        "video": {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".wmv", ".flv", ".ts"},
    }
    allowed = suffix_sets.get(kind, set())
    for item in attachments or []:
        if getattr(item, "suffix", "") in allowed:
            return item
    return None




def _first_video(items):
    for item in items or []:
        if getattr(item, "suffix", "") in {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".wmv", ".flv", ".ts"}:
            return item
    return None






def _export_text(path) -> str:
    target = Path(path)
    return "导出目录：" + target.parent.as_posix() + chr(10) + "文件：" + target.as_posix()

def _preview_paths(paths) -> list[str]:
    try:
        import attachments as attachment_mod
        out = []
        for path in paths or []:
            suffix = Path(path).suffix.lower()
            if suffix in {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".wmv", ".flv", ".ts"}:
                thumb = attachment_mod.video_thumbnail(Path(path))
                if thumb:
                    out.append(thumb)
            elif suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
                out.append(str(path))
        return out
    except Exception:
        return []

def execute_calls(
    calls: list[dict],
    attachments: list,
    allow_file_tools: bool,
    on_progress=None,
    task_progress=None,
) -> list[ToolOutcome]:
    outcomes: list[ToolOutcome] = []
    for call in calls[:3]:
        if not _valid_call(call, allow_file_tools):
            continue
        tool = str(call.get("tool") or "").strip().lower()
        params = call.get("params") if isinstance(call.get("params"), dict) else call
        LOGGER.info("工具调用开始：%s %s", tool, call)
        try:
            print(f"[tool] start {tool} {call}", flush=True)
        except Exception:
            pass
        if task_progress is not None and tool not in {"image", "derpi"}:
            try:
                task_progress({
                    "id": f"tool-{tool}",
                    "kind": "tool",
                    "label": f"正在执行工具：{tool}",
                    "fraction": 0.05,
                    "state": "running",
                })
            except Exception:
                pass
        if tool == "derpi":
            try:
                query = str(call.get("query") or "").strip()
                try:
                    limit = int(call.get("limit") or 10)
                except (TypeError, ValueError):
                    limit = 10
                after = str(call.get("after") or "").strip()
                before = str(call.get("before") or "").strip()
                selected = derpi.find_images(
                    query,
                    max(1, min(derpi.MAX_IMAGE_DOWNLOADS, limit)),
                    after=after,
                    before=before,
                )
                ids = [str(item.get("id")) for item in selected if item.get("id")]
                if on_progress is not None:
                    try:
                        on_progress("derpi", ids)
                    except Exception:  # noqa: BLE001
                        pass
                paths = derpi.download_selected(selected, max(1, min(derpi.MAX_IMAGE_DOWNLOADS, limit)), progress=task_progress)
                if not paths:
                    raise derpi.DerpiError(
                        "no downloadable derpicdn.net images were returned for the selected image IDs"
                    )
                outcomes.append(ToolOutcome(tool="derpi", paths=[str(p) for p in paths[:10]],
                                            text=f"已下载 {len(paths)} 张 Derpibooru 图片。"))
            except Exception as exc:  # noqa: BLE001
                outcomes.append(ToolOutcome(tool="derpi", error=str(exc)))
            continue
        if tool == "qwen_image":
            try:
                import qwen_image_runtime
                prompt = str(call.get("prompt") or "").strip()
                negative = str(call.get("negative") or "").strip()
                size = call.get("size") or "768x768"
                if not prompt:
                    raise ValueError("Qwen image prompt is empty")
                if not qwen_image_runtime.available():
                    raise RuntimeError("Qwen-Image-2.1-Turbo slim package or CUDA is unavailable")
                def report_qwen(current, total, label="Qwen 采样"):
                    if task_progress is None:
                        return
                    try:
                        task_progress({
                            "id": "image-qwen",
                            "kind": "image",
                            "label": label,
                            "name": prompt[:80],
                            "fraction": max(0.0, min(1.0, current / max(1, total))),
                            "state": "running" if current < total else "done",
                        })
                    except Exception:
                        pass
                _reference = _find_attachment(attachments or [], "image")
                path = qwen_image_runtime.generate(prompt, negative, size=size, progress=report_qwen,
                                                   reference_image=(_reference.path if _reference else None))
                outcomes.append(ToolOutcome(tool="qwen_image", paths=[str(path)], params=call))
            except Exception as exc:  # noqa: BLE001
                outcomes.append(ToolOutcome(tool="qwen_image", error=str(exc)))
            continue
        if tool == "video":
            if not feature_flags.H3_ENABLED:
                outcomes.append(ToolOutcome(tool="video", error="当前版本未启用 MiniMax H3 视频生成"))
                continue
            prompt = str(call.get("prompt") or call.get("instruction") or "").strip()
            if not prompt:
                outcomes.append(ToolOutcome(tool="video", error="视频提示词为空"))
                continue
            try:
                import minimax_h3_runtime
                if not minimax_h3_runtime.available():
                    raise RuntimeError("MiniMax H3 权重未安装或 CUDA 不可用")
                image = _find_attachment(attachments or [], "image")
                mode = str(call.get("mode") or "image").lower()
                path = minimax_h3_runtime.generate(
                    prompt,
                    reference=(image.path if image and mode == "reference" else None),
                    image=(image.path if image and mode != "reference" else None),
                    num_frames=int(call.get("frames") or 124),
                    width=int(call.get("width") or 960),
                    height=int(call.get("height") or 544),
                    progress=(lambda cur, total, label="H3 采样": task_progress({
                        "id": "video-h3", "kind": "video", "label": label,
                        "name": prompt[:80], "fraction": max(0.0, min(1.0, cur / max(1, total))),
                        "state": "running" if cur < total else "done"}) if task_progress else None),
                )
                outcomes.append(ToolOutcome(tool="video", paths=[str(path)],
                                            text=_export_text(path),
                                            previews=_preview_paths([path])))
            except Exception as exc:  # noqa: BLE001
                outcomes.append(ToolOutcome(tool="video", error=str(exc)))
            continue
        if tool == "plot":
            try:
                import plot_render
                if not plot_render.available():
                    raise RuntimeError("Edge/KaTeX/function-plot 渲染组件不可用")
                expressions = call.get("expressions") or call.get("functions") or call.get("fn") or ""
                if isinstance(expressions, list):
                    expressions = ";".join(str(item) for item in expressions)
                if not str(expressions).strip():
                    raise ValueError("请给出函数表达式，例如 y=x^2")
                path = plot_render.render_plot(
                    str(expressions),
                    xmin=float(call.get("xmin", -5)), xmax=float(call.get("xmax", 5)),
                    ymin=float(call["ymin"]) if call.get("ymin") is not None else None,
                    ymax=float(call["ymax"]) if call.get("ymax") is not None else None,
                    title=str(call.get("title") or ""),
                )
                outcomes.append(ToolOutcome(tool="plot", paths=[str(path)], params=call,
                                            text="函数图绘制完成。",
                                            previews=[str(path)]))
            except Exception as exc:  # noqa: BLE001
                outcomes.append(ToolOutcome(tool="plot", error=str(exc)))
            continue
        if tool == "qwen_edit":
            image = _find_attachment(attachments or [], "image")
            if image is None:
                outcomes.append(ToolOutcome(tool="qwen_edit", error="请先附加需要修改的图片"))
                continue
            instruction = str(call.get("instruction") or call.get("prompt") or "").strip()
            if not instruction:
                outcomes.append(ToolOutcome(tool="qwen_edit", error="请先说明想改哪里，例如“把背景改成夜晚”"))
                continue
            try:
                size = call.get("size") or "512x512"
                if feature_flags.SD15_EDIT_ENABLED:
                    import sd15_edit_runtime
                    path = sd15_edit_runtime.edit(instruction, image.path, size=size)
                    outcomes.append(ToolOutcome(tool="qwen_edit", paths=[str(path)], params=call,
                                                text="SD1.5 图像编辑完成。"))
                    continue
                import qwen_edit_runtime
                if not qwen_edit_runtime.available():
                    raise RuntimeError("Qwen-Image-2.1 编辑模型不可用")
                def edit_progress(current, total, label="编辑图片"):
                    if task_progress is None:
                        return
                    try:
                        task_progress({"id": "image-edit", "kind": "image", "label": label,
                                       "name": instruction[:80],
                                       "fraction": max(0.0, min(1.0, current / max(1, total))),
                                       "state": "running" if current < total else "done"})
                    except Exception:
                        pass
                path = qwen_edit_runtime.edit(instruction, image.path, size=size, progress=edit_progress)
                outcomes.append(ToolOutcome(tool="qwen_edit", paths=[str(path)], params=call,
                                            text="图片编辑完成。"))
            except Exception as exc:  # noqa: BLE001
                outcomes.append(ToolOutcome(tool="qwen_edit", error=str(exc)))
            continue
        if tool == "image":
            try:
                prompt = str(call.get("prompt") or "").strip()
                if not prompt:
                    raise ValueError("image prompt is empty")
                negative = str(call.get("negative") or "").strip()
                size = call.get("size") or (960, 720)
                engine = str(call.get("engine") or "auto").strip().lower()
                if feature_flags.IMAGE_ENGINE == "sd15":
                    engine = "sd15"
                if feature_flags.IMAGE_ENGINE == "none":
                    raise RuntimeError("当前版本未启用图片生成")
                if engine != "sd15":
                    try:
                        import qwen_image_runtime
                        if qwen_image_runtime.available():
                            try:
                                count = max(1, min(4, int(call.get("count") or 1)))
                            except (TypeError, ValueError):
                                count = 1
                            _reference = _find_attachment(attachments or [], "image")
                            paths = []
                            for index in range(count):
                                def report_qwen(current, total, label="Qwen 采样"):
                                    if task_progress is None:
                                        return
                                    try:
                                        task_progress({
                                            "id": "image-qwen",
                                            "kind": "image",
                                            "label": label,
                                            "name": prompt[:80],
                                            "fraction": max(0.0, min(1.0, current / max(1, total))),
                                            "state": "running" if current < total else "done",
                                        })
                                    except Exception:
                                        pass
                                paths.append(str(qwen_image_runtime.generate(
                                    prompt, negative, size=size, progress=report_qwen,
                                    reference_image=(_reference.path if _reference else None))))
                            outcomes.append(ToolOutcome(tool="image", paths=paths, params=call))
                            continue
                    except Exception as exc:  # noqa: BLE001
                        LOGGER.warning("Qwen image failed, falling back to SD1.5: %s", exc)
                        try:
                            qwen_image_runtime.reset()
                        except Exception:
                            pass
                import ai_sd15
                try:
                    _reference = _find_attachment(attachments or [], "image")
                    if _reference:
                        import attachments as _attachment_mod
                        _caption = _attachment_mod.image_caption(_reference.path)
                        if _caption:
                            prompt = prompt + chr(10) + "参考图：" + _caption
                except Exception:
                    pass
                try:
                    import gpu_scheduler
                    choice = gpu_scheduler.assign("sd15")
                    if choice.get("device_index") is not None:
                        import os as _os
                        _os.environ["TWILIGHT_SD_GPU"] = str(choice["device_index"])
                except Exception:
                    pass
                engine = ai_sd15.SD15Engine()

                def report_image(current, total):
                    total = max(1, int(total))
                    percent = int(max(0.0, min(1.0, current / total)) * 100)
                    LOGGER.info("图片生成中：%d/%d（%d%%）", current, total, percent)
                    try:
                        print(f"[sd] 图片生成中：{current}/{total}（{percent}%）", flush=True)
                    except Exception:
                        pass
                    if task_progress is None:
                        return
                    try:
                        task_progress({
                            "id": "image-sd15",
                            "kind": "image",
                            "label": "图片生成中",
                            "name": prompt[:80],
                            "fraction": current / total,
                            "state": "running" if current < total else "done",
                        })
                    except Exception:
                        pass

                try:
                    count = max(1, min(4, int(call.get("count") or 1)))
                except (TypeError, ValueError):
                    count = 1
                try:
                    import gpu_raid
                    gpu_ids = gpu_raid.device_ids()
                except Exception:
                    gpu_ids = []
                if count > 1 and gpu_ids:
                    def generate_on_gpu(index, gpu_index):
                        lane = ai_sd15.SD15Engine(gpu_index=gpu_index)
                        return lane.generate(prompt, negative, size=size, progress=None)
                    workers = min(count, len(gpu_ids)) if len(gpu_ids) > 1 else min(count, 2)
                    paths = gpu_raid.raid0_map(generate_on_gpu, range(count), max_workers=workers)
                else:
                    paths = [engine.generate(prompt, negative, size=size, progress=report_image)]
                outcomes.append(ToolOutcome(tool="image", paths=[str(path) for path in paths], params=call))
            except Exception as exc:  # noqa: BLE001
                outcomes.append(ToolOutcome(tool="image", error=str(exc)))
            continue
        if tool == "code_review":
            try:
                import ast
                import re as _re
                targets = []
                for item in attachments or []:
                    if getattr(item, "suffix", "") in {".py", ".js", ".ts", ".tsx", ".jsx", ".c", ".h", ".cpp", ".hpp", ".java", ".cs", ".go", ".rs"}:
                        targets.append(Path(item.path))
                if not targets:
                    raise ValueError("请先附加代码文件")
                lines_out = []
                for source in targets[:4]:
                    code = source.read_text(encoding="utf-8", errors="replace")
                    findings = []
                    if source.suffix == ".py":
                        try:
                            tree = ast.parse(code)
                            funcs = sum(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) for node in ast.walk(tree))
                            classes = sum(isinstance(node, ast.ClassDef) for node in ast.walk(tree))
                            findings.append(f"函数 {funcs} 个，类 {classes} 个")
                        except SyntaxError as exc:
                            findings.append(f"严重：Python 语法错误第 {exc.lineno} 行：{exc.msg}")
                    long_lines = [i for i, line in enumerate(code.splitlines(), 1) if len(line) > 120]
                    if long_lines:
                        findings.append(f"过长行：{len(long_lines)} 处（示例第 {long_lines[:5]} 行）")
                    todos = len(_re.findall(r"\b(TODO|FIXME|XXX)\b", code))
                    if todos:
                        findings.append(f"TODO/FIXME：{todos} 处")
                    if "except:" in code or "except Exception:" in code:
                        findings.append("存在宽泛异常捕获，建议缩小异常范围")
                    if "eval(" in code or "exec(" in code:
                        findings.append("发现 eval/exec，存在代码注入风险")
                    if "subprocess" in code and "shell=True" in code:
                        findings.append("subprocess 使用 shell=True，存在命令注入风险")
                    lines_out.append(f"## {source.name}\n" + "\n".join(f"- {item}" for item in findings))
                outcomes.append(ToolOutcome(tool="code_review", text="\n\n".join(lines_out)))
            except Exception as exc:  # noqa: BLE001
                outcomes.append(ToolOutcome(tool="code_review", error=str(exc)))
            continue
        if tool == "vhs":
            video = _first_video(attachments or [])
            if video is None:
                outcomes.append(ToolOutcome(tool="vhs", error="请先附加需要 VHS 化的视频"))
                continue
            try:
                value = float(params.get("value", 0.44))
                import effects_bridge
                output = effects_bridge.run("vhs", "q", max(0.0, min(1.0, value)), video.path)
                outcomes.append(ToolOutcome(tool="vhs", paths=[str(output)],
                                            text=f"VHS 化完成：q={value:.2f}" + chr(10) + _export_text(output),
                                            previews=_preview_paths([output])))
            except Exception as exc:  # noqa: BLE001
                outcomes.append(ToolOutcome(tool="vhs", error=str(exc)))
            continue
        if tool == "compress":
            video = _first_video(attachments or [])
            if video is None:
                outcomes.append(ToolOutcome(tool="compress", error="请先附加需要压片的视频"))
                continue
            try:
                value = float(params.get("value", params.get("crush", 0.7)))
                import effects_bridge
                output = effects_bridge.run("squeeze", "crush", max(0.0, min(1.0, value)), video.path)
                outcomes.append(ToolOutcome(tool="compress", paths=[str(output)],
                                            text=_export_text(output),
                                            previews=_preview_paths([output])))
            except Exception as exc:  # noqa: BLE001
                outcomes.append(ToolOutcome(tool="compress", error=str(exc)))
            continue
        if tool == "age":
            try:
                image = _find_attachment(attachments, "image")
                source = Path(call.get("path") or (image.path if image else ""))
                if not source or not source.is_file():
                    raise ValueError("please attach an image first")
                from PIL import Image
                import io
                import time
                original = Image.open(source).convert("RGB")
                width, height = original.size
                work = original
                for _ in range(3):
                    small = (max(8, width // 3), max(8, height // 3))
                    work = work.resize(small, Image.BILINEAR)
                    buffer = io.BytesIO()
                    work.save(buffer, format="JPEG", quality=8, optimize=False)
                    buffer.seek(0)
                    work = Image.open(buffer).convert("RGB")
                work = work.resize((width, height), Image.BILINEAR)
                out_dir = Path.home() / "Downloads" / "TwilightHarness"
                out_dir.mkdir(parents=True, exist_ok=True)
                target = out_dir / f"aged-{time.strftime('%Y%m%d-%H%M%S')}.jpg"
                work.save(target, format="JPEG", quality=20)
                outcomes.append(ToolOutcome(tool="age", paths=[str(target)], params=call))
            except Exception as exc:  # noqa: BLE001
                outcomes.append(ToolOutcome(tool="age", error=str(exc)))
            continue
        if not allow_file_tools:
            continue
        try:
            import local_actions

            if tool == "upscale":
                image = _find_attachment(attachments, "image")
                if image is None:
                    raise local_actions.LocalActionError("please attach an image first")
                paths = local_actions._upscale(Path(image.path), progress=task_progress)
            elif tool == "video_upscale":
                video = _find_attachment(attachments, "video")
                if video is None:
                    raise local_actions.LocalActionError("please attach a video first")
                paths = local_actions._video_upscale(Path(video.path), progress=task_progress)
            elif tool == "audio":
                video = _find_attachment(attachments, "video")
                if video is None:
                    raise local_actions.LocalActionError("please attach a video first")
                paths = local_actions._extract_audio(Path(video.path))
            elif tool == "transcode":
                video = _find_attachment(attachments, "video")
                if video is None:
                    raise local_actions.LocalActionError("please attach a video first")
                fmt = str(call.get("format") or "mp4").lower()
                params = {
                    key: call.get(key)
                    for key in ("video_bitrate", "resolution", "audio_bitrate", "fps")
                    if call.get(key)
                }
                paths = local_actions._transcode(Path(video.path), fmt, params)
            else:
                continue
            outcomes.append(ToolOutcome(tool=tool, paths=[str(p) for p in paths], params=call,
                                        text=chr(10).join(_export_text(p) for p in paths) if paths else "",
                                        previews=_preview_paths(paths)))
        except Exception as exc:  # noqa: BLE001
            outcomes.append(ToolOutcome(tool=tool, error=str(exc)))
    return outcomes
