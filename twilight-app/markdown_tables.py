"""Markdown / HTML table rendering for the conversation Text widgets.

Tk's Text widget cannot lay out a real multi-column table, so tables are
rendered as a small embedded Frame of Labels.  This keeps CJK and proportional
text readable while matching the rest of the lavender UI.
"""

from __future__ import annotations

import re
import unicodedata
from html.parser import HTMLParser

import tkinter as tk
from tkinter import font as tkfont

import theme


_MD_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$")


class _HTMLTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._in_cell = False

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"}:
            if self._row is None:
                self._row = []
            self._cell = []
            self._in_cell = True
        elif tag == "br" and self._in_cell and self._cell is not None:
            self._cell.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"td", "th"}:
            if self._row is not None:
                self._row.append("".join(self._cell or []).strip())
            self._cell = None
            self._in_cell = False
        elif tag == "tr":
            if self._row:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._in_cell and self._cell is not None:
            self._cell.append(data)


def _plain_cell(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text or "")
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = text.replace("**", "").replace("__", "").replace("`", "")
    text = text.replace("~~", "")
    # Bare emphasis markers are only removed at word boundaries so file names
    # such as "final_v2.md" stay intact.
    text = re.sub(r"(?<!\w)\*(?!\s)(.+?)(?<!\s)\*(?!\w)", r"\1", text)
    text = re.sub(r"(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def _split_md_row(line: str) -> list[str]:
    row = line.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|"):
        row = row[:-1]
    return [_plain_cell(part.replace("\\|", "|")) for part in re.split(r"(?<!\\)\|", row)]


def _separator_aligns(line: str) -> list[str] | None:
    if not _MD_SEPARATOR_RE.match(line.strip()):
        return None
    aligns: list[str] = []
    for cell in _split_md_row(line):
        cell = cell.strip()
        left = cell.startswith(":")
        right = cell.endswith(":")
        if left and right:
            aligns.append("center")
        elif right:
            aligns.append("e")
        elif left:
            aligns.append("w")
        else:
            aligns.append("w")
    return aligns


def _display_width(text: str) -> int:
    width = 0
    for char in text:
        if char == "\n":
            continue
        if unicodedata.east_asian_width(char) in {"W", "F"}:
            width += 2
        else:
            width += 1
    return width


def _normalise_rows(rows: list[list[str]]) -> list[list[str]]:
    cols = max((len(row) for row in rows), default=0)
    if cols == 0:
        return []
    result: list[list[str]] = []
    for row in rows:
        cells = [cell.strip() for cell in row[:cols]]
        cells.extend([""] * (cols - len(cells)))
        result.append(cells)
    return result


def _insert_table(
    widget: tk.Text,
    rows: list[list[str]],
    aligns: list[str],
    table_tag: str,
    base_tag: str,
) -> None:
    rows = _normalise_rows(rows)
    if not rows:
        return
    cols = len(rows[0])

    font_spec = widget.tag_cget(table_tag, "font") or "TkDefaultFont"
    try:
        body_font = tkfont.Font(font=font_spec)
    except tk.TclError:
        body_font = tkfont.Font(family=theme.CJK_FAMILY, size=9)
    try:
        header_font = tkfont.Font(font=body_font)
        header_font.configure(weight="bold")
    except tk.TclError:
        header_font = body_font

    # A reasonable pixel width for the pane.  Labels wrap instead of making a
    # horizontal scrollbar, and the column minima are scaled to fit.
    try:
        pane_width = widget.winfo_width()
    except tk.TclError:
        pane_width = 0
    available = max(240, min(900, pane_width - 28))
    weights: list[int] = []
    for col in range(cols):
        longest = max((_display_width(row[col]) for row in rows), default=4)
        weights.append(max(4, min(48, longest)))
    total_weight = max(1, sum(weights))
    min_col = 54
    extra = max(0, available - min_col * cols)
    col_widths = [
        max(min_col, int(extra * weight / total_weight) + min_col)
        for weight in weights
    ]
    scale = available / max(available, sum(col_widths))
    col_widths = [max(42, int(width * scale)) for width in col_widths]

    table = tk.Frame(
        widget,
        bg=theme.WHITE,
        highlightthickness=1,
        highlightbackground=theme.LINE,
        bd=0,
    )
    table._header_font = header_font  # type: ignore[attr-defined]
    table._body_font = body_font  # type: ignore[attr-defined]

    for row_index, row in enumerate(rows):
        is_header = row_index == 0
        for col_index, cell in enumerate(row):
            anchor = aligns[col_index] if col_index < len(aligns) else "w"
            label = tk.Label(
                table,
                text=cell or " ",
                font=header_font if is_header else body_font,
                fg=theme.ACCENT if is_header else theme.BLACK,
                bg=theme.BUTTON if is_header else theme.WHITE,
                anchor=anchor,
                justify={"w": "left", "e": "right", "center": "center"}.get(anchor, "left"),
                wraplength=max(44, col_widths[col_index] - 12),
                padx=5,
                pady=4,
                highlightthickness=1,
                highlightbackground=theme.LINE,
                bd=0,
            )
            label.grid(row=row_index, column=col_index, sticky="nsew")
            table.grid_columnconfigure(col_index, weight=1, minsize=col_widths[col_index])
        table.grid_rowconfigure(row_index, weight=0)

    if widget.index("end-1c") != "1.0":
        widget.insert("end-1c", "\n", base_tag)
    widget.window_create("end-1c", window=table, padx=4, pady=4, align="top")
    widget.insert("end-1c", "\n", base_tag)


def try_insert_table(
    widget: tk.Text,
    lines: list[str],
    start: int,
    base_tag: str,
    table_tag: str,
) -> int | None:
    """Consume and render a Markdown or HTML table at *start*.

    Returns the index of the first unconsumed line, or None if this line does
    not begin a table.
    """
    line = lines[start]
    # HTML table: consume through the closing </table> tag.
    if "<table" in line.lower():
        block: list[str] = []
        end = start
        while end < len(lines):
            block.append(lines[end])
            if "</table>" in lines[end].lower():
                end += 1
                break
            end += 1
        parser = _HTMLTableParser()
        try:
            parser.feed("\n".join(block))
            parser.close()
        except Exception:  # noqa: BLE001
            return None
        if not parser.rows:
            return None
        _insert_table(widget, parser.rows, ["w"] * len(parser.rows[0]), table_tag, base_tag)
        return end

    # Markdown pipe table: header + separator + optional body rows.
    if "|" not in line or start + 1 >= len(lines):
        return None
    aligns = _separator_aligns(lines[start + 1])
    if aligns is None:
        return None
    rows: list[list[str]] = [_split_md_row(line)]
    end = start + 2
    while end < len(lines):
        candidate = lines[end]
        if not candidate.strip() or "|" not in candidate:
            break
        if candidate.lstrip().startswith("```"):
            break
        rows.append(_split_md_row(candidate))
        end += 1
    _insert_table(widget, rows, aligns, table_tag, base_tag)
    return end
