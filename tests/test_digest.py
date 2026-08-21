from datetime import date

import digest_bot.digest as digest_module
from digest_bot.digest import (
    EMPTY_VARIANT_WEIGHTS,
    EMPTY_VARIANTS,
    INTRO_VARIANT_WEIGHTS,
    INTRO_VARIANTS,
    PRIMARY_EMPTY_VARIANTS,
    PRIMARY_INTRO_VARIANTS,
    RARE_EMPTY_VARIANTS,
    RARE_INTRO_VARIANTS,
    choose_empty_variant,
    choose_intro_variant,
    choose_outro_variant,
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


def test_missing_date_uses_empty_variant(monkeypatch) -> None:
    variant = PRIMARY_EMPTY_VARIANTS[0]
    monkeypatch.setattr(
        digest_module,
        "choose_empty_variant",
        lambda *, digest_is_today: variant[0 if digest_is_today else 1],
    )
    message = render_message(
        extract_digest(MARKDOWN, date(2026, 8, 18)),
        digest_is_today=False,
    )
    assert variant[1] in message
    assert "18.08.2026" in message
    assert message.endswith(variant[1])


def test_empty_variants_use_primary_and_rare_weights(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def choose(population, *, weights, k):
        captured.update(population=population, weights=weights, k=k)
        return [population[0]]

    monkeypatch.setattr(digest_module.random, "choices", choose)

    assert choose_empty_variant(digest_is_today=True) == PRIMARY_EMPTY_VARIANTS[0][0]
    assert captured == {
        "population": EMPTY_VARIANTS,
        "weights": EMPTY_VARIANT_WEIGHTS,
        "k": 1,
    }
    assert (5,) * len(PRIMARY_EMPTY_VARIANTS) + (1,) * len(
        RARE_EMPTY_VARIANTS
    ) == EMPTY_VARIANT_WEIGHTS


def test_intro_variants_use_primary_and_rare_weights(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def choose(population, *, weights, k):
        captured.update(population=population, weights=weights, k=k)
        return [population[0]]

    monkeypatch.setattr(digest_module.random, "choices", choose)

    assert choose_intro_variant(digest_is_today=False) == PRIMARY_INTRO_VARIANTS[0][1]
    assert captured == {
        "population": INTRO_VARIANTS,
        "weights": INTRO_VARIANT_WEIGHTS,
        "k": 1,
    }
    assert (5,) * len(PRIMARY_INTRO_VARIANTS) + (1,) * len(
        RARE_INTRO_VARIANTS
    ) == INTRO_VARIANT_WEIGHTS


def test_outro_variant_depends_on_digest_size(monkeypatch) -> None:
    captured: list[tuple[object, object, bool]] = []

    def choose(variants, weights, *, digest_is_today):
        captured.append((variants, weights, digest_is_today))
        return variants[0][0 if digest_is_today else 1]

    monkeypatch.setattr(digest_module, "_choose_relative_variant", choose)

    assert choose_outro_variant(item_count=2, digest_is_today=True)
    assert choose_outro_variant(item_count=3, digest_is_today=False)
    assert choose_outro_variant(item_count=6, digest_is_today=True)

    assert captured[0][0] == (
        digest_module.PRIMARY_SMALL_OUTRO_VARIANTS
        + digest_module.RARE_SMALL_OUTRO_VARIANTS
    )
    assert captured[1][0] == (
        digest_module.PRIMARY_REGULAR_OUTRO_VARIANTS
        + digest_module.RARE_REGULAR_OUTRO_VARIANTS
    )
    assert captured[2][0] == (
        digest_module.PRIMARY_LARGE_OUTRO_VARIANTS
        + digest_module.RARE_LARGE_OUTRO_VARIANTS
    )
    assert captured[0][1] == (5, 5, 5, 1, 1)
    assert captured[1][1] == (5, 5, 5, 1, 1, 1)
    assert captured[2][1] == (5, 5, 5, 1, 1)


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


def test_render_message_wraps_intro_and_outro(monkeypatch) -> None:
    monkeypatch.setattr(
        digest_module,
        "choose_outro_variant",
        lambda *, item_count, digest_is_today: "Работа продолжается.",
    )
    message = render_message(
        extract_digest(MARKDOWN, date(2026, 8, 20)),
        digest_is_today=True,
    )
    assert message.startswith("<b>")
    assert "сегодня" in message
    assert "<i>Дайджест за 20.08.2026</i>" in message
    assert message.endswith("<i>Работа продолжается.</i>")
