from datetime import date

from digest_bot.digest import (
    DEFAULT_EMPTY,
    extract_digest,
    render_markdown_for_telegram,
    render_message,
    split_message,
)

MARKDOWN = """# Дайджест проекта

## [2026-08-20]

### Доработки

- Добавлена отправка дайджеста.

## [2026-08-19]

### Исправления

- Исправлена настройка.
"""


def test_extracts_only_requested_section() -> None:
    document = extract_digest(MARKDOWN, date(2026, 8, 20))
    assert "Добавлена отправка" in (document.body or "")
    assert "Исправлена настройка" not in (document.body or "")


def test_missing_date_uses_empty_variant() -> None:
    message = render_message(extract_digest(MARKDOWN, date(2026, 8, 18)))
    assert DEFAULT_EMPTY in message
    assert "18.08.2026" in message


def test_heading_without_entries_is_empty() -> None:
    markdown = "# Дайджест проекта\n\n## [2026-08-20]\n\n### Доработки\n"
    document = extract_digest(markdown, date(2026, 8, 20))
    assert document.body is None


def test_splits_long_message_without_data_loss() -> None:
    message = "a" * 4500
    chunks = split_message(message, limit=4000)
    assert list(map(len, chunks)) == [4000, 500]
    assert "".join(chunks) == message


def test_translates_markdown_to_telegram_html() -> None:
    markdown = """### Улучшения

- Добавлен **жирный** и *курсивный* текст с `кодом`.
- Доступна [документация](https://example.com/docs?a=1&b=2).

> Важное замечание

```python
print("<safe>")
```
"""
    rendered = render_markdown_for_telegram(markdown)

    assert "<b>Улучшения</b>" in rendered
    assert "• Добавлен <b>жирный</b> и <i>курсивный</i>" in rendered
    assert "<code>кодом</code>" in rendered
    assert '<a href="https://example.com/docs?a=1&amp;b=2">документация</a>' in rendered
    assert "<blockquote>Важное замечание</blockquote>" in rendered
    assert "<pre>print(&quot;&lt;safe&gt;&quot;)</pre>" in rendered


def test_escapes_raw_html() -> None:
    assert render_markdown_for_telegram("<script>alert('&')</script>") == (
        "&lt;script&gt;alert(&#x27;&amp;&#x27;)&lt;/script&gt;"
    )


def test_render_message_wraps_intro_and_outro() -> None:
    message = render_message(extract_digest(MARKDOWN, date(2026, 8, 20)))
    assert message.startswith("<b>Ежедневный дайджест")
    assert message.endswith("<i>До следующего дайджеста!</i>")
