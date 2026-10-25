"""Model-requested local tool calls and their execution.

The model may append commands such as:
  [[LOCAL_TOOL: {"tool":"derpi","query":"sunny starscout"}]]
to its reply.  The app strips these commands from the visible answer, executes
them locally, and displays the resulting files.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import derpi


@dataclass
class ToolOutcome:
    tool: str
    paths: list[str] = field(default_factory=list)
    error: str = ""
    params: dict = field(default_factory=dict)


def system_prompt(allow_file_tools: bool) -> str:
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    lines = [
        "你可以在回复末尾请求本地工具执行。格式必须是独立的一行或多行：",
        '[[LOCAL_TOOL: {"tool":"derpi","query":"..."}]]',
        "可用工具：",
        '- derpi：按用户需求搜索 Derpibooru（safe / suggestive / semi-grimdark），并从 derpicdn.net 下载到用户的 Downloads 文件夹。参数：query（必填）、limit（可选，1-8）。',
        '  query 必须使用逗号分隔的 Derpibooru 标签，标签内部的括号、英文句点、感叹号和问号必须原样保留；不要在 query 中加入 safe/suggestive/semi-grimdark 等评级词。示例："twilight sparkle (alicorn), alicorn"。',
        f'  当前日期是 {today}。用户如果给出时间段，请转换为 after/before 的 YYYY-MM-DD 日期；如果用户没有说时间段，则不要填写 after/before，应用会自动选择最新图片。',
        '  时间范围内结果超过 limit 时应用会自动随机挑选 limit 张；不需要你处理随机逻辑。',
    ]
    if allow_file_tools:
        lines += [
            '- upscale：用 Real-ESRGAN x4 动漫模型放大最新附加的图片；无参数。',
            '- audio：分离最新附加视频的音轨；无参数。',
            '- transcode：转码最新附加视频。参数：format（mp4/webm/mkv/mov/gif）、video_bitrate（如 "2M"）、resolution（如 "720p"/"1280x720"）、audio_bitrate（如 "128k"）、fps（可选）。',
            "需要图片/视频附件的工具只能使用本轮用户最新附加的附件。",
        ]
    lines += [
        "只有在用户明确要求对应本地操作时才输出工具命令；不要声称文件已经下载或处理完成，应用会在回复后执行并展示结果。一次回复最多请求 3 个工具。",
    ]
    return "\n".join(lines)


def _valid_call(call: Any, allow_file_tools: bool) -> bool:
    if not isinstance(call, dict):
        return False
    tool = str(call.get("tool") or "").strip().lower()
    allowed = {"derpi"}
    if allow_file_tools:
        allowed |= {"upscale", "audio", "transcode"}
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


def execute_calls(
    calls: list[dict],
    attachments: list,
    allow_file_tools: bool,
) -> list[ToolOutcome]:
    outcomes: list[ToolOutcome] = []
    for call in calls[:3]:
        if not _valid_call(call, allow_file_tools):
            continue
        tool = str(call.get("tool") or "").strip().lower()
        if tool == "derpi":
            try:
                query = str(call.get("query") or "").strip()
                try:
                    limit = int(call.get("limit") or 4)
                except (TypeError, ValueError):
                    limit = 4
                after = str(call.get("after") or "").strip()
                before = str(call.get("before") or "").strip()
                paths = derpi.search_download(
                    query,
                    max(1, min(8, limit)),
                    after=after,
                    before=before,
                )
                outcomes.append(ToolOutcome(tool="derpi", paths=[str(p) for p in paths]))
            except Exception as exc:  # noqa: BLE001
                outcomes.append(ToolOutcome(tool="derpi", error=str(exc)))
            continue
        if not allow_file_tools:
            continue
        try:
            import local_actions

            if tool == "upscale":
                image = _find_attachment(attachments, "image")
                if image is None:
                    raise local_actions.LocalActionError("please attach an image first")
                paths = local_actions._upscale(Path(image.path))
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
            outcomes.append(ToolOutcome(tool=tool, paths=[str(p) for p in paths], params=call))
        except Exception as exc:  # noqa: BLE001
            outcomes.append(ToolOutcome(tool=tool, error=str(exc)))
    return outcomes
