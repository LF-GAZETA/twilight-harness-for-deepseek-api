from __future__ import annotations
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import cpu_esrgan


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    model = cpu_esrgan.load_model()
    source = Path(args.input)
    target = Path(args.output)
    target.mkdir(parents=True, exist_ok=True)
    for image in sorted(source.glob("*")):
        if image.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
            cpu_esrgan.upscale_file(model, image, target / f"{image.stem}.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
