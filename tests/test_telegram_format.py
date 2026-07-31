"""Unit tests for Telegram HTML formatting."""

# ruff: noqa: RUF001

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


def test_table_is_rendered_as_aligned_monospace_block() -> None:
    source = (
        "| Категория | Сумма | Доля |\n"
        "|-----------|------:|------|\n"
        "| Еда | 12 000 | 40% |\n"
        "| Транспорт | 3 500 | 12% |"
    )
    expected = (
        "<pre>Категория   Сумма  Доля\n"
        "───────────────────────\n"
        "Еда        12 000   40%\n"
        "Транспорт   3 500   12%</pre>"
    )
    assert markdown_to_telegram_html(source) == expected


def test_table_without_outer_pipes_is_rendered() -> None:
    source = "Ключ | Значение\n--- | ---\nтема | спорт"
    expected = "<pre>Ключ  Значение\n──────────────\nтема  спорт</pre>"
    assert markdown_to_telegram_html(source) == expected


def test_numeric_column_is_right_aligned_without_explicit_alignment() -> None:
    source = "| Товар | Цена |\n| --- | --- |\n| Хлеб | 55 |\n| Молоко | 1 200 |"
    expected = "<pre>Товар    Цена\n─────────────\nХлеб       55\nМолоко  1 200</pre>"
    assert markdown_to_telegram_html(source) == expected


def test_table_cells_lose_inline_markdown_markers() -> None:
    source = (
        "| Что | Где |\n"
        "| --- | --- |\n"
        "| **отчёт** | [сайт](https://example.test/a) |\n"
        "| `код` | _тут_ |"
    )
    rendered = markdown_to_telegram_html(source)
    assert rendered == ("<pre>Что    Где\n───────────\nотчёт  сайт\nкод    тут</pre>")
    assert "<b>" not in rendered
    assert "<code>" not in rendered
    assert "<a href" not in rendered


def test_table_escapes_html_and_measures_visible_width() -> None:
    source = "| Ввод | Итог |\n| --- | --- |\n| <script> | R&D |\n| x | y |"
    expected = "<pre>Ввод      Итог\n──────────────\n&lt;script&gt;  R&amp;D\nx         y</pre>"
    assert markdown_to_telegram_html(source) == expected


def test_ragged_table_rows_keep_every_cell() -> None:
    source = "| A | B |\n| --- | --- |\n| одна |\n| x | y | хвост |"
    rendered = markdown_to_telegram_html(source)
    assert rendered == ("<pre>A     B\n─────────────\nодна\nx     y хвост</pre>")


def test_table_without_delimiter_row_stays_literal() -> None:
    source = "| Категория | Сумма |\n| Еда | 12 000 |"
    assert markdown_to_telegram_html(source) == source


def test_table_inside_fenced_code_is_left_untouched() -> None:
    source = "```\n| a | b |\n|---|---|\n| 1 | 2 |\n```"
    assert markdown_to_telegram_html(source) == "<pre>\n| a | b |\n|---|---|\n| 1 | 2 |\n</pre>"


def test_text_around_table_is_preserved() -> None:
    source = "Итоги **июля**:\n\n| A | B |\n| --- | --- |\n| 1 | 2 |\n\nВсё."
    expected = "Итоги <b>июля</b>:\n\n<pre>A  B\n────\n1  2</pre>\n\nВсё."
    assert markdown_to_telegram_html(source) == expected


def test_escaped_pipe_stays_inside_cell() -> None:
    source = "| Выражение | Смысл |\n| --- | --- |\n| a \\| b | или |"
    assert markdown_to_telegram_html(source) == (
        "<pre>Выражение  Смысл\n────────────────\na | b      или</pre>"
    )
