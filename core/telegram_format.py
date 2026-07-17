"""Convert a small, safe Markdown subset into Telegram HTML.

Supported constructs are fenced code blocks, inline code, bold, italic, HTTP(S)
links, and level-one through level-three ATX headings.  The converter is
intentionally regex-based rather than a complete Markdown implementation.
"""

from __future__ import annotations

import html
import re

_FENCED_CODE_RE = re.compile(r"```([\s\S]*?)```")
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_BOLD_RE = re.compile(r"\*\*([^*\n]+?)\*\*")
_ASTERISK_ITALIC_RE = re.compile(r"(?<!\*)\*([^*\n]+?)\*(?!\*)")
_UNDERSCORE_ITALIC_RE = re.compile(r"(?<![\w_])_([^_\n]+?)_(?![\w_])")
_HEADING_RE = re.compile(r"(?m)^#{1,3}[ \t]+(.+?)[ \t]*$")


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
