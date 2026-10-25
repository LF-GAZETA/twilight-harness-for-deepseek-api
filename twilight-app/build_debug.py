"""Build the debug console variant of the attachment edition.

This executable uses PyInstaller --console so double-clicking it opens the GUI
and a visible debug backend console; no .cmd launcher is required.

Usage (bundled Python):
    python twilight-app/build_debug.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "twilight-app"
CONTENT = ROOT / "content"
BUILD = ROOT / "build"
DIST = BUILD / "debug-build" / "dist"
DELIVERY = ROOT / "twilight-harness-file-debug"
FONTS_SRC = ROOT / "assets" / "fonts"
TAG_TABLE = ROOT / "assets" / "derpibooru-tags.json.gz"
FONT_FILES = (
    "UniNeue-Regular.ttf",
    "UniNeue-Bold.ttf",
    "UniNeue-RegularItalic.ttf",
    "UniNeue-BoldItalic.ttf",
    "msyh.ttc",
    "SourceHanSansJP-Normal.otf",
)

API_KEY_TEMPLATE = """; Twilight Harness — DeepSeek API Key
; 可以在这里填入 API Key，也可以启动程序后在界面中输入。
; 成功后程序会自动把 Key 写回本文件；请勿把本文件分享给他人。
[deepseek]
api_key =
base_url = https://api.deepseek.com
model = deepseek-reasoner
"""

VERSION_INFO = """VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=(1, 0, 0, 0),
    prodvers=(1, 0, 0, 0),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('080404b0', [
        StringStruct('CompanyName', 'Unofficial fan project'),
        StringStruct('FileDescription', 'Twilight Harness — chat with Twilight Sparkle'),
        StringStruct('FileVersion', '1.0.0.0'),
        StringStruct('InternalName', 'twilight-harness'),
        StringStruct('OriginalFilename', 'twilight-harness.exe'),
        StringStruct('ProductName', 'Twilight Harness'),
        StringStruct('ProductVersion', '1.0.0.0'),
        StringStruct('LegalCopyright', 'Unofficial fan project; My Little Pony © Hasbro')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"""


def run(cmd: list[str], cwd: Path) -> None:
    print("+", " ".join(str(part) for part in cmd))
    subprocess.run(cmd, cwd=cwd, check=True)


def write_feature_flags(enabled: bool) -> Path:
    module = APP / "feature_flags.py"
    module.write_text(
        '"""Build-time feature switches for the two Twilight Harness variants."""\n\n'
        f'ATTACHMENTS_ENABLED = {enabled}\n',
        encoding="utf-8",
    )
    print(f"wrote feature flags: ATTACHMENTS_ENABLED={enabled}")
    return module


def build_character_module(character: Path) -> Path:
    """Embed the markdown card as base64 in a Python module."""
    import base64

    encoded = base64.b64encode(character.read_bytes()).decode("ascii")
    module = APP / "character_data.py"
    module.write_text(
        '"""Embedded twilight-character.md content (base64 UTF-8 bytes)."""\n\n'
        f'CHARACTER_MD_BASE64 = "{encoded}"\n',
        encoding="utf-8",
    )
    print(f"embedded character card -> {module} ({module.stat().st_size} bytes)")
    return module


def main() -> int:
    character = CONTENT / "twilight-character.md"
    if not character.is_file() or character.stat().st_size < 1000:
        print(f"ERROR: missing/too-small character card: {character}", file=sys.stderr)
        return 2
    missing_fonts = [name for name in FONT_FILES if not (FONTS_SRC / name).is_file()]
    if missing_fonts:
        print(f"ERROR: missing bundled font files: {missing_fonts}", file=sys.stderr)
        return 2
    write_feature_flags(True)
    character_module = build_character_module(character)
    icon = BUILD / "app.ico"
    if not icon.is_file():
        run([sys.executable, str(BUILD / "make_icon.py")], cwd=ROOT)
        if not icon.is_file():
            print("ERROR: icon generation failed", file=sys.stderr)
            return 2
    version_file = BUILD / "version_info.txt"
    (BUILD / "debug-build").mkdir(parents=True, exist_ok=True)
    version_file.write_text(VERSION_INFO, encoding="utf-8")
    try:
        import imageio_ffmpeg

        ffmpeg_binary = Path(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:
        found = shutil.which("ffmpeg")
        ffmpeg_binary = Path(found) if found else None
    if ffmpeg_binary is None or not ffmpeg_binary.is_file():
        print("ERROR: ffmpeg binary is required for the video-enabled file build", file=sys.stderr)
        return 2
    print(f"bundling ffmpeg: {ffmpeg_binary}")
    realesrgan = ROOT / "tools" / "realesrgan"
    required_tool_files = [
        realesrgan / "realesrgan-ncnn-vulkan.exe",
        realesrgan / "vcomp140.dll",
        realesrgan / "models" / "realesrgan-x4plus-anime.bin",
        realesrgan / "models" / "realesrgan-x4plus-anime.param",
    ]
    missing_tool_files = [str(path) for path in required_tool_files if not path.is_file()]
    if missing_tool_files:
        print("ERROR: Real-ESRGAN tool files are missing: " + ", ".join(missing_tool_files), file=sys.stderr)
        return 2
    print(f"bundling Real-ESRGAN: {realesrgan}")

    # Compile-check every app module before a slow PyInstaller build.
    modules = [
        APP / "main.py",
        APP / "app.py",
        APP / "widgets.py",
        APP / "api_client.py",
        APP / "persona.py",
        APP / "theme.py",
        APP / "app_icon.py",
        APP / "design_assets.py",
        APP / "character_data.py",
        APP / "paths.py",
        APP / "font_manager.py",
        APP / "ui_strings.py",
        APP / "attachments.py",
        APP / "markdown_tables.py",
        APP / "local_actions.py",
        APP / "derpi.py",
        APP / "local_tools.py",
        APP / "tag_table.py",
        APP / "feature_flags.py",
    ]
    if not TAG_TABLE.is_file():
        print(f"ERROR: missing Derpibooru tag table {TAG_TABLE}", file=sys.stderr)
        return 2
    for module in modules:
        if not module.is_file():
            print(f"ERROR: missing source module {module}", file=sys.stderr)
            return 2
    subprocess.run(
        [sys.executable, "-m", "py_compile", *[str(m) for m in modules]],
        cwd=ROOT,
        check=True,
    )

    # Rebuild from a clean PyInstaller workspace.
    for stale in (DIST, BUILD / "debug-build" / "work", BUILD / "debug-build" / "twilight-harness-file-debug.spec"):
        try:
            if stale.is_dir():
                shutil.rmtree(stale)
            elif stale.exists():
                stale.unlink()
        except OSError:
            pass

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--console",
        "--optimize",
        "2",
        "--exclude-module",
        "tkinter.test",
        "--exclude-module",
        "unittest",
        "--exclude-module",
        "doctest",
        "--exclude-module",
        "pydoc",
        "--exclude-module",
        "test",
        "--exclude-module",
        "xmlrpc",
        "--exclude-module",
        "sqlite3",
        "--exclude-module",
        "lib2to3",
        "--exclude-module",
        "imageio_ffmpeg",
        "--add-binary",
        f"{ffmpeg_binary};ffmpeg-bin",
        "--add-data",
        f"{realesrgan};realesrgan-bin",
        "--hidden-import",
        "pypdf",
        "--hidden-import",
        "docx",
        "--hidden-import",
        "pptx",
        "--hidden-import",
        "openpyxl",
        "--hidden-import",
        "win32com.client",
        "--hidden-import",
        "win32com.client.dynamic",
        "--hidden-import",
        "pythoncom",
        "--hidden-import",
        "pywintypes",
        "--hidden-import",
        "winrt.windows.media.ocr",
        "--hidden-import",
        "winrt.windows.graphics.imaging",
        "--hidden-import",
        "winrt.windows.storage.streams",
        "--hidden-import",
        "winrt.windows.foundation",
        "--hidden-import",
        "winrt.windows.foundation.collections",
        "--hidden-import",
        "winrt.windows.globalization",
        "--hidden-import",
        "winrt.windows.media",
        "--collect-submodules",
        "winrt",
        "--runtime-tmpdir",
        ".",
        "--add-data",
        f"{FONTS_SRC};fonts",
        "--add-data",
        f"{TAG_TABLE};assets",
        "--name",
        "twilight-harness-file-debug",
        "--icon",
        str(icon),
        "--version-file",
        str(version_file),
        "--paths",
        str(APP),
        "--distpath",
        str(DIST),
        "--workpath",
        str(BUILD / "debug-build" / "work"),
        "--specpath",
        str(BUILD),
        "--log-level",
        "WARN",
        str(APP / "main.py"),
    ]
    run(cmd, cwd=ROOT)
    exe = DIST / "twilight-harness-file-debug.exe"
    if not exe.is_file():
        print("ERROR: PyInstaller did not produce twilight-harness-file-debug.exe", file=sys.stderr)
        return 3

    # The delivery is exactly one exe.  Fonts and the temporary character/key
    # workspace are unpacked beside the exe on first use.
    if DELIVERY.exists():
        if DELIVERY.resolve() != (ROOT / "twilight-harness-file-debug").resolve():
            print("ERROR: refusing to remove unexpected delivery path", file=sys.stderr)
            return 4
        shutil.rmtree(DELIVERY)
    DELIVERY.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(exe, DELIVERY / "twilight-harness-file-debug.exe")
    (DELIVERY / "DEBUG_README.txt").write_text(
        """Twilight Harness File - Debug Console Build
==========================================

Double-click twilight-harness-file-debug.exe.
The GUI and a visible debug console open together; no .cmd launcher is needed.

Debug logs are printed to the console and written to temp/twilight-harness.log.
Closing the GUI closes the debug console as well.
Downloads is used for user-requested local tool output and is never deleted.
""",
        encoding="utf-8",
    )

    items = sorted(DELIVERY.iterdir())
    print("\nDELIVERY:", DELIVERY)
    total = 0
    for item in items:
        total += item.stat().st_size if item.is_file() else 0
        print(f"  {item.name:24s} {item.stat().st_size / 1024 / 1024:.2f} MiB")
    print(f"  {'TOTAL':24s} {total / 1024 / 1024:.2f} MiB")
    expected_files = {"twilight-harness-file-debug.exe", "DEBUG_README.txt"}
    actual_files = {item.name for item in items if item.is_file()}
    actual_dirs = {item.name for item in items if item.is_dir()}
    if actual_files != expected_files or actual_dirs:
        print(
            f"ERROR: delivery item set mismatch: files={actual_files} dirs={actual_dirs}",
            file=sys.stderr,
        )
        return 5
    print("OK - single-file debug console build delivered.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
