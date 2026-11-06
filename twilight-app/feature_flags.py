"""Build-time/runtime feature switches for the five client variants."""
from __future__ import annotations

import os

VARIANT = os.environ.get("TWILIGHT_VARIANT", "plus").strip().lower()
if VARIANT not in {"plus", "debug", "standard", "chat", "benchmark"}:
    VARIANT = "plus"

ATTACHMENTS_ENABLED = VARIANT not in {"chat", "benchmark"}
BENCHMARK_ENABLED = VARIANT != "chat"
LOCAL_MEDIA_ENABLED = VARIANT != "chat"
H3_ENABLED = VARIANT in {"debug", "standard"}
IMAGE_ENGINE = "qwen" if VARIANT in {"plus", "debug"} else ("sd15" if VARIANT == "standard" else "none")
QWEN_EDIT_ENABLED = VARIANT in {"plus", "debug"}
SD15_EDIT_ENABLED = VARIANT == "standard"
