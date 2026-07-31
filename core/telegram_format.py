"""Convert a small, safe Markdown subset into Telegram HTML.

Supported constructs are fenced code blocks, inline code, bold, italic, HTTP(S)
links, level-one through level-three ATX headings, and pipe tables.  The
converter is intentionally regex-based rather than a complete Markdown
implementation.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable

_FENCED_CODE_RE = re.compile(r"```([\s\S]*?)```")
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_BOLD_RE = re.compile(r"\*\*([^*\n]+?)\*\*")
_ASTERISK_ITALIC_RE = re.compile(r"(?<!\*)\*([^*\n]+?)\*(?!\*)")
_UNDERSCORE_ITALIC_RE = re.compile(r"(?<![\w_])_([^_\n]+?)_(?![\w_])")
_HEADING_RE = re.compile(r"(?m)^#{1,3}[ \t]+(.+?)[ \t]*$")

_UNESCAPED_PIPE_RE = re.compile(r"(?<!\\)\|")
_TABLE_ALIGNMENT_CELL_RE = re.compile(r"^:?-+:?$")
_NUMERIC_CELL_RE = re.compile(r"^[+-]?\d[\d\s.,]*[%₽$€]?$")
_WHITESPACE_RE = re.compile(r"\s+")

_TABLE_COLUMN_GAP = "  "
_TABLE_RULE_CHAR = "─"

_ALIGN_LEFT = "left"
_ALIGN_RIGHT = "right"
_ALIGN_CENTER = "center"
_ALIGN_AUTO = "auto"


def markdown_to_telegram_html(text: str) -> str:
    """Return Telegram-compatible HTML for the supported Markdown subset.

    All input is escaped before markup is added. Malformed or unmatched Markdown
    remains literal text.
    """

    escaped = html.escape(text, quote=False)
    protected: list[str] = []
    token_prefix = _token_prefix(escaped)

    def protect(value: str) -> str:
        token = f"{token_prefix}{len(protected)}\x00"
        protected.append(value)
        return token

    escaped = _FENCED_CODE_RE.sub(lambda match: protect(f"<pre>{match.group(1)}</pre>"), escaped)
    escaped = _convert_tables(escaped, protect)
    escaped = _INLINE_CODE_RE.sub(lambda match: protect(f"<code>{match.group(1)}</code>"), escaped)

    def replace_link(match: re.Match[str]) -> str:
        label, url = match.groups()
        safe_url = url.replace('"', "&quot;").replace("'", "&#x27;")
        return protect(f'<a href="{safe_url}">{label}</a>')

    escaped = _LINK_RE.sub(replace_link, escaped)
    escaped = _BOLD_RE.sub(r"<b>\1</b>", escaped)
    escaped = _ASTERISK_ITALIC_RE.sub(r"<i>\1</i>", escaped)
    escaped = _UNDERSCORE_ITALIC_RE.sub(r"<i>\1</i>", escaped)
    escaped = _HEADING_RE.sub(r"<b>\1</b>", escaped)

    for index, value in enumerate(protected):
        escaped = escaped.replace(f"{token_prefix}{index}\x00", value)
    return escaped


def _token_prefix(text: str) -> str:
    """Return a placeholder prefix not already contained in *text*."""

    prefix = "\x00telegram-format-"
    while prefix in text:
        prefix = f"\x00{prefix}"
    return prefix


def _convert_tables(text: str, protect: Callable[[str], str]) -> str:
    """Replace Markdown pipe tables with protected monospace ``<pre>`` blocks.

    Telegram renders no table markup, so an aligned monospace block is the only
    layout that keeps columns readable.  Lines that do not form a well-formed
    table are returned untouched.
    """

    lines = text.split("\n")
    converted: list[str] = []
    index = 0
    while index < len(lines):
        table = _match_table(lines, index)
        if table is None:
            converted.append(lines[index])
            index += 1
            continue
        rendered, consumed = table
        converted.append(protect(rendered))
        index += consumed
    return "\n".join(converted)


def _match_table(lines: list[str], start: int) -> tuple[str, int] | None:
    """Return the rendered table and the line count consumed, or ``None``."""

    if start + 1 >= len(lines) or "|" not in lines[start]:
        return None

    header = _split_row(lines[start])
    if header == []:
        return None

    alignments = _parse_alignment_row(lines[start + 1], len(header))
    if alignments is None:
        return None

    body: list[list[str]] = []
    index = start + 2
    while index < len(lines):
        line = lines[index]
        if line.strip() == "" or "|" not in line:
            break
        body.append(_normalise_row(_split_row(line), len(header)))
        index += 1

    return _render_table(header, alignments, body), index - start


def _split_row(line: str) -> list[str]:
    """Split one table line into cells, honouring escaped pipes."""

    stripped = line.strip()
    cells = _UNESCAPED_PIPE_RE.split(stripped)
    if cells != [] and stripped.startswith("|"):
        cells = cells[1:]
    if cells != [] and stripped.endswith("|") and not stripped.endswith("\\|"):
        cells = cells[:-1]
    return [_plain_cell(cell) for cell in cells]


def _plain_cell(cell: str) -> str:
    """Strip inline Markdown from a cell: ``<pre>`` cannot carry nested tags."""

    text = _LINK_RE.sub(r"\1", cell)
    text = _INLINE_CODE_RE.sub(r"\1", text)
    text = _BOLD_RE.sub(r"\1", text)
    text = _ASTERISK_ITALIC_RE.sub(r"\1", text)
    text = _UNDERSCORE_ITALIC_RE.sub(r"\1", text)
    text = text.replace("\\|", "|")
    return _WHITESPACE_RE.sub(" ", text).strip()


def _parse_alignment_row(line: str, columns: int) -> list[str] | None:
    """Return per-column alignments for a Markdown delimiter row."""

    if "|" not in line or "-" not in line:
        return None
    cells = _split_row(line)
    if len(cells) != columns:
        return None

    alignments: list[str] = []
    for cell in cells:
        if not _TABLE_ALIGNMENT_CELL_RE.match(cell):
            return None
        if cell.startswith(":") and cell.endswith(":"):
            alignments.append(_ALIGN_CENTER)
        elif cell.endswith(":"):
            alignments.append(_ALIGN_RIGHT)
        elif cell.startswith(":"):
            alignments.append(_ALIGN_LEFT)
        else:
            alignments.append(_ALIGN_AUTO)
    return alignments


def _normalise_row(cells: list[str], columns: int) -> list[str]:
    """Pad short rows and fold surplus cells into the last column."""

    if len(cells) < columns:
        return cells + [""] * (columns - len(cells))
    if len(cells) > columns:
        head = cells[: columns - 1]
        tail = " ".join(cell for cell in cells[columns - 1 :] if cell != "")
        return [*head, tail]
    return cells


def _render_table(header: list[str], alignments: list[str], body: list[list[str]]) -> str:
    """Lay the table out as a fixed-width monospace block."""

    columns = len(header)
    resolved = [
        _resolve_alignment(alignment, [row[index] for row in body])
        for index, alignment in enumerate(alignments)
    ]
    widths = [
        max([_display_width(header[index])] + [_display_width(row[index]) for row in body])
        for index in range(columns)
    ]

    rows = [_render_row(header, widths, resolved)]
    total_width = sum(widths) + len(_TABLE_COLUMN_GAP) * (columns - 1)
    rows.append(_TABLE_RULE_CHAR * total_width)
    rows.extend(_render_row(row, widths, resolved) for row in body)
    return "<pre>{}</pre>".format("\n".join(rows))


def _resolve_alignment(alignment: str, cells: list[str]) -> str:
    """Right-align auto columns whose body cells all read as numbers."""

    if alignment != _ALIGN_AUTO:
        return alignment
    filled = [cell for cell in cells if cell != ""]
    if filled != [] and all(_NUMERIC_CELL_RE.match(cell) for cell in filled):
        return _ALIGN_RIGHT
    return _ALIGN_LEFT


def _render_row(cells: list[str], widths: list[int], alignments: list[str]) -> str:
    """Render one padded row, trimming the trailing padding."""

    padded = [
        _pad_cell(cell, width, alignment)
        for cell, width, alignment in zip(cells, widths, alignments, strict=True)
    ]
    return _TABLE_COLUMN_GAP.join(padded).rstrip()


def _pad_cell(cell: str, width: int, alignment: str) -> str:
    """Pad an HTML-escaped cell to *width* visible characters."""

    padding = width - _display_width(cell)
    if padding <= 0:
        return cell
    if alignment == _ALIGN_RIGHT:
        return " " * padding + cell
    if alignment == _ALIGN_CENTER:
        left = padding // 2
        return " " * left + cell + " " * (padding - left)
    return cell + " " * padding


def _display_width(cell: str) -> int:
    """Return the on-screen length of a cell whose HTML is already escaped."""

    return len(html.unescape(cell))
