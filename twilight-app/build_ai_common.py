"""Shared build helpers for embedding the AI runtime and model packs.

Used by ``build_file.py`` / ``build_debug.py`` (attachment editions).  The
standard edition deliberately excludes every AI module and dependency, so it
never imports this file.

Design notes
------------
* ONNX Runtime is NOT packaged by PyInstaller (it is excluded).  Instead the
  installed ``onnxruntime`` package is packed as ``cpu.tar.zst`` and embedded as
  ``ai-runtime/cpu.tar.zst``.  ``twilight-app/ai_compat.py`` extracts the pack
  into the user's AI cache on demand; this keeps the one-file EXE smaller than
  a static bundle and allows swapping CUDA / DirectML packs without code
  changes.
* Additional packs (e.g. ``cuda.tar.zst`` built on a CUDA machine,
  ``directml.tar.zst``) can be passed with ``--ai-runtime-packs <dir>``.
* Final embedded models are staged by ``tools/ai_embed.py`` and passed with
  ``--ai-models <dir>`` (added as ``ai-models/`` data, read-only at runtime).
"""

from __future__ import annotations

import argparse
import sys
import tarfile
from pathlib import Path


def cli_option(name: str) -> str | None:
    args = sys.argv[1:]
    for index, arg in enumerate(args):
        if arg == name and index + 1 < len(args):
            return args[index + 1]
        if arg.startswith(name + "="):
            return arg.split("=", 1)[1]
    return None


def build_cpu_runtime_pack(dest_dir: Path) -> Path:
    """Compress the installed onnxruntime tree into a zstd tarball."""
    import zstandard

    try:
        import onnxruntime
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(
            "ERROR: onnxruntime is required to build the CPU AI runtime pack: " + str(exc)
        ) from exc
    package = Path(onnxruntime.__file__).resolve().parent
    if not package.is_dir():
        raise SystemExit(f"ERROR: onnxruntime package directory not found: {package}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "cpu.tar.zst"
    compressor = zstandard.ZstdCompressor(level=15)
    with open(dest, "wb") as handle:
        with compressor.stream_writer(handle) as writer:
            with tarfile.open(fileobj=writer, mode="w|") as tar:
                for path in sorted(package.rglob("*")):
                    if path.is_dir() or "__pycache__" in path.parts:
                        continue
                    tar.add(
                        str(path),
                        arcname=str(Path("onnxruntime") / path.relative_to(package)),
                    )
    print(f"built CPU AI runtime pack -> {dest} ({dest.stat().st_size} bytes)")
    return dest


def collect_ai_build_inputs() -> tuple[list[str], list[str]]:
    """Return (extra PyInstaller arguments, human-readable notes)."""
    extra: list[str] = []
    notes: list[str] = []
    # onnxruntime is loaded from the extracted runtime pack, never bundled.
    extra += ["--exclude-module", "onnxruntime", "--exclude-module", "onnx"]
    extra += ["--exclude-module", "scipy", "--exclude-module", "cv2"]
    extra += [
        "--hidden-import", "tokenizers",
        "--hidden-import", "zstandard",
        "--hidden-import", "ai_compat",
        "--hidden-import", "ai_resources",
        "--hidden-import", "ai_prompt",
        "--hidden-import", "ai_sd15",
        "--hidden-import", "ai_audio",
    ]

    packs: list[Path] = []
    packs_dir = cli_option("--ai-runtime-packs")
    if packs_dir:
        directory = Path(packs_dir).resolve()
        if not directory.is_dir():
            raise SystemExit(f"ERROR: --ai-runtime-packs is not a directory: {directory}")
        packs = sorted(directory.glob("*.tar.zst")) + sorted(directory.glob("*.zip"))
        if not packs:
            raise SystemExit(f"ERROR: no AI runtime packs (*.tar.zst/*.zip) in {directory}")
        notes.append(f"AI runtime packs: {[item.name for item in packs]}")
    else:
        packs = [build_cpu_runtime_pack(Path(__file__).resolve().parent.parent / "build" / "ai-runtime")]
        notes.append("AI runtime packs: cpu (built from installed onnxruntime)")
    for pack in packs:
        extra += ["--add-data", f"{pack};ai-runtime"]

    ai_models = cli_option("--ai-models")
    if ai_models:
        models_dir = Path(ai_models).resolve()
        if not models_dir.is_dir():
            raise SystemExit(f"ERROR: --ai-models is not a directory: {models_dir}")
        if not any(models_dir.rglob("*")):
            raise SystemExit(f"ERROR: --ai-models directory is empty: {models_dir}")
        extra += ["--add-data", f"{models_dir};ai-models"]
        notes.append(f"embedded AI models: {models_dir}")
    else:
        notes.append("embedded AI models: none (models can be placed in the AI home at runtime)")
    return extra, notes


def main() -> int:
    """Small CLI for developers: build a runtime pack or inspect the plan."""
    parser = argparse.ArgumentParser(description="Twilight Harness AI build helpers")
    parser.add_argument("--build-cpu-pack", metavar="DEST_DIR", help="write cpu.tar.zst into DEST_DIR")
    parser.add_argument("--print-plan", action="store_true", help="show what collect_ai_build_inputs() returns")
    args = parser.parse_args()
    if args.build_cpu_pack:
        build_cpu_runtime_pack(Path(args.build_cpu_pack).resolve())
    if args.print_plan:
        extra, notes = collect_ai_build_inputs()
        print("extra PyInstaller args:", extra)
        for note in notes:
            print("note:", note)
    if not (args.build_cpu_pack or args.print_plan):
        parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
