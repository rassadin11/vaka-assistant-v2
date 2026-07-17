"""Unit tests for Telegram HTML formatting."""

import pytest

from core.telegram_format import markdown_to_telegram_html


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("**bold**", "<b>bold</b>"),
        ("*italic* and _also italic_", "<i>italic</i> and <i>also italic</i>"),
        ("keep snake_case intact", "keep snake_case intact"),
        ("`<tag>`", "<code>&lt;tag&gt;</code>"),
        ("```\n<block>\n```", "<pre>\n&lt;block&gt;\n</pre>"),
        (
            '[label](https://example.test/"quoted")',
            '<a href="https://example.test/&quot;quoted&quot;">label</a>',
        ),
        ("### Heading", "<b>Heading</b>"),
        ("unmatched ** marker", "unmatched ** marker"),
        ("Привет, «мир» — готово 😊", "Привет, «мир» — готово 😊"),
        ("<script>alert(1)</script>", "&lt;script&gt;alert(1)&lt;/script&gt;"),
    ],
)
def test_markdown_to_telegram_html(source: str, expected: str) -> None:
    assert markdown_to_telegram_html(source) == expected


def test_markdown_to_telegram_html_leaves_non_http_links_literal() -> None:
    assert markdown_to_telegram_html("[file](file:///tmp/example)") == "[file](file:///tmp/example)"
