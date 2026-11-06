"""KaTeX and function-plot rendering through the bundled headless Edge.

The native client is Win32/GDI+.  Instead of embedding a browser, equations and
function plots are rendered to PNG with the WebView2/Edge runtime that is already
present on Windows, then inserted into the normal image pipeline.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from ai_resources import outputs_dir

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "sidecar" / "assets"
KATEX = ASSETS / "katex" / "dist"
FUNCTION_PLOT = ASSETS / "function-plot" / "dist" / "function-plot.js"
EDGE_CANDIDATES = [
    Path(os.environ.get("TWILIGHT_EDGE", "")),
    Path(r"C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
    Path(r"C:/Program Files/Microsoft/Edge/Application/msedge.exe"),
]
COLORS = ["#7c3aed", "#0ea5e9", "#db2777", "#16a34a", "#f59e0b"]
_ALLOWED_EXPR = re.compile(r"^[0-9A-Za-z_+\-*/^().,!<>= ]+$")


def _edge() -> Path | None:
    for candidate in EDGE_CANDIDATES:
        if str(candidate) and candidate.is_file():
            return candidate
    return None


def available() -> bool:
    return bool(_edge() and (KATEX / "katex.min.js").is_file()
                and (KATEX / "katex.min.css").is_file() and FUNCTION_PLOT.is_file())


def _work_dir() -> Path:
    path = outputs_dir("render")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _html(script: str, width: int, height: int, extra: str = "") -> str:
    katex_css = (KATEX / "katex.min.css").as_uri()
    katex_js = (KATEX / "katex.min.js").as_uri()
    plot_js = FUNCTION_PLOT.as_uri()
    return f"""<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="{katex_css}">
<style>
html,body{{margin:0;background:#fff;font-family:'Segoe UI',sans-serif;color:#161616}}
body{{display:inline-block;padding:10px}}
#plot{{width:{width}px;height:{height}px}}
.katex{{color:#161616}}
</style></head><body>
{extra}
<script src="{katex_js}"></script>
<script src="{plot_js}"></script>
<script>{script}</script>
</body></html>"""


def _capture(html: str, width: int, height: int, stem: str) -> Path:
    edge = _edge()
    if edge is None:
        raise RuntimeError("找不到 Microsoft Edge，无法渲染公式")
    work = _work_dir()
    source = work / f"{stem}.html"
    target = work / f"{stem}.png"
    source.write_text(html, encoding="utf-8")
    if target.exists():
        target.unlink()
    profile = Path(tempfile.mkdtemp(prefix="twilight-render-"))
    try:
        command = [
            str(edge), "--headless=new", "--disable-gpu", "--no-sandbox",
            "--hide-scrollbars", "--allow-file-access-from-files",
            "--virtual-time-budget=2500", f"--window-size={width},{height}",
            f"--user-data-dir={profile}", f"--screenshot={target}", source.as_uri(),
        ]
        result = subprocess.run(command, timeout=45, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(30):
            if target.is_file() and target.stat().st_size > 100:
                break
            time.sleep(0.2)
        if result.returncode not in (0, None) or not target.is_file():
            raise RuntimeError("Edge 渲染失败")
        return _trim(target)
    finally:
        shutil.rmtree(profile, ignore_errors=True)
        source.unlink(missing_ok=True)


def _trim(path: Path) -> Path:
    try:
        from PIL import Image, ImageChops
        image = Image.open(path).convert("RGB")
        background = Image.new("RGB", image.size, (255, 255, 255))
        box = ImageChops.difference(image, background).getbbox()
        if box:
            padding = 8
            box = (max(0, box[0] - padding), max(0, box[1] - padding),
                   min(image.width, box[2] + padding), min(image.height, box[3] + padding))
            image.crop(box).save(path, optimize=True)
    except Exception:
        pass
    return path


def _cached(kind: str, payload: str, render) -> Path:
    digest = hashlib.sha256((kind + "\0" + payload).encode("utf-8")).hexdigest()[:24]
    target = _work_dir() / f"{kind}_{digest}.png"
    if target.is_file() and target.stat().st_size > 100:
        return target
    return render(target)


def render_math(latex: str) -> Path:
    latex = latex.strip()
    if not latex:
        raise ValueError("公式为空")

    def draw(target: Path) -> Path:
        script = ("katex.render(" + json.dumps(latex, ensure_ascii=False)
                  + ", document.getElementById('math'), {displayMode:true,throwOnError:false});")
        html = _html(script, 900, 260, '<div id="math"></div>')
        return _capture(html, 900, 260, target.stem)

    return _cached("math", latex, draw)


def parse_expressions(value: str) -> list[str]:
    parts = re.split(r"[;\n]+", value or "")
    result = []
    for part in parts:
        expression = part.strip()
        if not expression:
            continue
        if expression.lower().startswith("y="):
            expression = expression[2:].strip()
        if len(expression) > 140 or not _ALLOWED_EXPR.match(expression):
            raise ValueError(f"不支持的函数表达式：{expression[:80]}")
        if any(word in expression.lower() for word in ("import", "require", "function", "window", "document", "eval")):
            raise ValueError("函数表达式包含不允许的关键字")
        result.append(expression)
    if not result:
        raise ValueError("请给出函数表达式，例如 /plot x^2; sin(x)")
    if len(result) > 4:
        raise ValueError("一次最多绘制 4 条函数")
    return result


def render_plot(expressions: str | list[str], xmin: float = -5, xmax: float = 5,
                ymin: float | None = None, ymax: float | None = None,
                title: str = "") -> Path:
    functions = parse_expressions(";".join(expressions) if isinstance(expressions, list) else expressions)
    if xmax <= xmin:
        raise ValueError("x 轴范围无效")
    data = [{"fn": function, "color": COLORS[index % len(COLORS)]}
            for index, function in enumerate(functions)]
    domain = {"domain": [xmin, xmax]}
    y_axis = {} if ymin is None or ymax is None else {"domain": [ymin, ymax]}
    script = ("functionPlot({target:'#plot',width:760,height:420,grid:true,"
              "xAxis:" + json.dumps(domain) + ",yAxis:" + json.dumps(y_axis)
              + ",data:" + json.dumps(data, ensure_ascii=False) + "});")
    title_html = "<div style='font:600 16px Segoe UI;margin-bottom:6px'>" + (
        title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")) + "</div>" if title else ""
    html = _html(script, 760, 420, '<div id="plot"></div>')
    if title_html:
        html = html.replace('<div id="plot"></div>', title_html + '<div id="plot"></div>')
    payload = json.dumps([functions, xmin, xmax, ymin, ymax, title], ensure_ascii=False)
    return _cached("plot", payload, lambda target: _capture(html, 780, 470, target.stem))


_BLOCK = re.compile(r"```.*?```", re.S)
_MATH = re.compile(r"\$\$(.+?)\$\$|\\\[(.+?)\\\]", re.S)


def render_markdown(text: str, max_equations: int = 12) -> str:
    """Replace display math with rendered PNG markdown; code fences are untouched."""
    if not available() or not text:
        return text
    chunks = []
    last = 0
    for match in _BLOCK.finditer(text):
        chunks.append(("text", text[last:match.start()]))
        chunks.append(("code", match.group(0)))
        last = match.end()
    chunks.append(("text", text[last:]))
    count = 0
    output = []
    for kind, chunk in chunks:
        if kind == "code":
            output.append(chunk)
            continue
        def replace(match: re.Match) -> str:
            nonlocal count
            if count >= max_equations:
                return match.group(0)
            latex = (match.group(1) or match.group(2) or "").strip()
            if not latex:
                return match.group(0)
            try:
                path = render_math(latex)
                count += 1
                return f"\n![公式]({path.as_posix()})\n"
            except Exception:
                return match.group(0)
        output.append(_MATH.sub(replace, chunk))
    return "".join(output)


if __name__ == "__main__":
    print(render_math(r"f(x)=\int_0^x e^{-t^2}\,dt"))
    print(render_plot("x^2/2; sin(x)*2", -5, 5, -3, 7, "plot"))
