"""Minimal, AI-friendly interface to the Beold processing core.

Only three normalized variables are needed:

  q      VHS strength       0.0 .. 1.0
  crush  SQUEEZE strength   0.0 .. 1.0
  age    PATINA age         0.0 .. 1.0

Example:
    from beold_core import BeoldEngine
    engine = BeoldEngine(tools_dir=r"D:\\Beold\\tools")
    result = engine.vhs("in.mp4", "out", q=0.7)
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .core import Processor, Toolchain, clamp, media_kind, probe_media

LogCallback = Callable[[str, str], None]
ProgressCallback = Callable[[Optional[int], str], None]

VARIABLES = {
    "q": {"min": 0.0, "max": 1.0, "mode": "vhs"},
    "crush": {"min": 0.0, "max": 1.0, "mode": "squeeze"},
    "age": {"min": 0.0, "max": 1.0, "mode": "patina"},
}


def to_level(value: float) -> int:
    """Map 0..1 to Beold's 1..100 level."""
    return int(round(clamp(float(value), 0.0, 1.0) * 99.0)) + 1


@dataclass
class Result:
    output: Path
    mode: str
    seconds: float
    variables: dict[str, float]

    def as_dict(self) -> dict:
        return {
            "output": str(self.output),
            "mode": self.mode,
            "seconds": self.seconds,
            "variables": dict(self.variables),
        }


class BeoldEngine:
    def __init__(
        self,
        tools_dir: Optional[os.PathLike[str] | str] = None,
        gpu: bool = False,
        log: Optional[LogCallback] = None,
        progress: Optional[ProgressCallback] = None,
    ):
        if tools_dir is not None:
            os.environ["BEOLD_TOOLS_DIR"] = str(Path(tools_dir).resolve())
        self.gpu = bool(gpu)
        self.log = log
        self.progress = progress
        # Fail early with a clear message if tools are missing.
        Toolchain.discover()

    def _processor(self) -> Processor:
        return Processor(
            self.log or (lambda message, kind: None),
            self.progress or (lambda value, text: None),
            use_gpu=self.gpu,
        )

    def run(
        self,
        source: os.PathLike[str] | str,
        output_dir: os.PathLike[str] | str,
        mode: str,
        *,
        q: Optional[float] = None,
        crush: Optional[float] = None,
        age: Optional[float] = None,
    ) -> Result:
        source = Path(source).resolve()
        output_dir = Path(output_dir).resolve()
        if mode == "vhs":
            value = 0.7 if q is None else float(q)
            level = to_level(value)
            variables = {"q": value}
        elif mode == "squeeze":
            value = 0.7 if crush is None else float(crush)
            level = to_level(value)
            variables = {"crush": value}
        elif mode == "patina":
            value = 0.7 if age is None else float(age)
            level = to_level(value)
            variables = {"age": value}
        else:
            raise ValueError(f"unknown mode: {mode}")

        started = time.perf_counter()
        result = self._processor().run(source, output_dir, mode, level)
        return Result(
            output=Path(result.output),
            mode=mode,
            seconds=time.perf_counter() - started,
            variables=variables,
        )

    # Convenience wrappers -------------------------------------------------

    def vhs(self, source, output_dir, *, q: float = 0.7) -> Result:
        return self.run(source, output_dir, "vhs", q=q)

    def squeeze(self, source, output_dir, *, crush: float = 0.7) -> Result:
        return self.run(source, output_dir, "squeeze", crush=crush)

    def patina(self, source, output_dir, *, age: float = 0.7) -> Result:
        return self.run(source, output_dir, "patina", age=age)

    # Introspection --------------------------------------------------------

    @staticmethod
    def variables() -> dict:
        return VARIABLES
