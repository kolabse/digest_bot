from __future__ import annotations

import random
import re
from datetime import date
from html import escape
from urllib.parse import urlparse

from .models import DigestDocument

DATE_HEADING = re.compile(r"(?m)^## \[(\d{4}-\d{2}-\d{2})\]\s*$")
BULLET = re.compile(r"(?m)^-\s+\S")


def extract_digest(markdown: str, target_date: date) -> DigestDocument:
    wanted = target_date.isoformat()
    matches = list(DATE_HEADING.finditer(markdown))
    for index, match in enumerate(matches):
        if match.group(1) != wanted:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        body = markdown[match.end() : end].strip()
        if not body or not BULLET.search(body):
            body = None
        return DigestDocument(date=wanted, body=body)
    return DigestDocument(date=wanted, body=None)


# TODO: make templates configurable.
DEFAULT_EMPTY_INTRO = "Ежедневный дайджест проекта:"
PRIMARY_EMPTY_VARIANTS = (
    (
        "Сегодня изменений в проекте не было. "
        "Команда отдыхала и набиралась сил для новых свершений.",
        "Вчера изменений в проекте не было. "
        "Команда отдыхала и набиралась сил для новых свершений.",
    ),
    (
        "Сегодня в проекте спокойно: никаких изменений. "
        "Команда отдыхала и копила силы для новых подвигов.",
        "Вчера в проекте было спокойно: никаких изменений. "
        "Команда отдыхала и копила силы для новых подвигов.",
    ),
    (
        "Сегодня без изменений — команда устроила небольшую передышку "
        "и готовится вернуться с новыми силами.",
        "Вчера обошлось без изменений — команда устроила небольшую передышку "
        "и готовится вернуться с новыми силами.",
    ),
    (
        "Сегодня новых изменений нет. "
        "Команда отдыхала — великие подвиги требуют хорошей подготовки.",
        "Вчера новых изменений не было. "
        "Команда отдыхала — великие подвиги требуют хорошей подготовки.",
    ),
    (
        "Сегодня без обновлений: герои отдыхали, восстанавливали силы "
        "и готовили снаряжение к новым задачам.",
        "Вчера обошлось без обновлений: герои отдыхали, восстанавливали силы "
        "и готовили снаряжение к новым задачам.",
    ),
)
RARE_EMPTY_VARIANTS = (
    (
        "За сегодня новых изменений нет. "
        "Команда взяла небольшую паузу перед следующими задачами.",
        "За вчера новых изменений нет. "
        "Команда взяла небольшую паузу перед следующими задачами.",
    ),
    (
        "Сегодня проект остался без обновлений. "
        "Команда восстанавливала силы и готовилась к новым достижениям.",
        "Вчера проект остался без обновлений. "
        "Команда восстанавливала силы и готовилась к новым достижениям.",
    ),
    (
        "Новостей по проекту сегодня нет. "
        "Похоже, команда решила немного передохнуть и зарядиться энергией.",
        "Новостей по проекту за вчера нет. "
        "Похоже, команда решила немного передохнуть и зарядиться энергией.",
    ),
    (
        "День прошёл без обновлений. Команда переводила дух перед новыми приключениями.",
        "Вчерашний день прошёл без обновлений. "
        "Команда переводила дух перед новыми приключениями.",
    ),
    (
        "В проекте сегодня затишье. Команда заряжалась энергией для будущих побед.",
        "Вчера в проекте было затишье. Команда заряжалась энергией для будущих побед.",
    ),
    (
        "Сегодня проект взял выходной. "
        "Команда набиралась сил, а новые подвиги немного подождут.",
        "Вчера проект взял выходной. "
        "Команда набиралась сил, а новые подвиги немного подождут.",
    ),
    (
        "Изменений сегодня не обнаружено. "
        "Вероятно, команда копила ману для следующего рывка.",
        "Изменений за вчера не обнаружено. "
        "Вероятно, команда копила ману для следующего рывка.",
    ),
)
EMPTY_VARIANTS = PRIMARY_EMPTY_VARIANTS + RARE_EMPTY_VARIANTS
EMPTY_VARIANT_WEIGHTS = (5,) * len(PRIMARY_EMPTY_VARIANTS) + (1,) * len(
    RARE_EMPTY_VARIANTS
)
PRIMARY_INTRO_VARIANTS = (
    (
        "Хорошие новости! Вот какие изменения появились в проекте сегодня:",
        "Хорошие новости! Вот какие изменения появились в проекте вчера:",
    ),
    (
        "Есть новости из проекта! Вот что изменилось сегодня:",
        "Есть новости из проекта! Вот что изменилось вчера:",
    ),
    (
        "Проект движется вперёд. Рассказываем, что изменилось сегодня:",
        "Проект движется вперёд. Рассказываем, что изменилось вчера:",
    ),
    (
        "Собрали главное о том, что изменилось в проекте сегодня:",
        "Собрали главное о том, что изменилось в проекте вчера:",
    ),
    (
        "Ещё один шаг вперёд! Вот что изменилось в проекте сегодня:",
        "Ещё один шаг вперёд! Вот что изменилось в проекте вчера:",
    ),
)
RARE_INTRO_VARIANTS = (
    (
        "Команда снова совершила несколько подвигов. Вот результаты за сегодня:",
        "Команда снова совершила несколько подвигов. Вот результаты за вчера:",
    ),
    (
        "Свежий дайджест готов. Рассказываем об изменениях за сегодняшний день:",
        "Свежий дайджест готов. Рассказываем об изменениях за вчерашний день:",
    ),
    (
        "Сегодняшний день проходит продуктивно. Вот какие изменения появились в проекте:",
        "Вчерашний день прошёл продуктивно. Вот какие изменения появились в проекте:",
    ),
    (
        "В проекте есть обновления! Делимся результатами сегодняшнего дня:",
        "В проекте есть обновления! Делимся результатами вчерашнего дня:",
    ),
)
INTRO_VARIANTS = PRIMARY_INTRO_VARIANTS + RARE_INTRO_VARIANTS
INTRO_VARIANT_WEIGHTS = (5,) * len(PRIMARY_INTRO_VARIANTS) + (1,) * len(
    RARE_INTRO_VARIANTS
)
SMALL_DIGEST_MAX_ITEMS = 2
LARGE_DIGEST_MIN_ITEMS = 6
PRIMARY_SMALL_OUTRO_VARIANTS = (
    (
        "Пусть сегодня изменений немного, за каждым из них стоит большая работа команды.",
        "Пусть вчера изменений было немного, за каждым из них стоит большая работа команды.",
    ),
    (
        "Изменений немного, однако за этими строками скрывается гораздо больше проделанной работы.",
        (
            "Изменений было немного, однако за этими строками скрывается "
            "гораздо больше проделанной работы."
        ),
    ),
    (
        "Сегодня список короткий, но даже небольшие шаги двигают проект вперёд.",
        "Вчера список получился коротким, но даже небольшие шаги двигают проект вперёд.",
    ),
)
RARE_SMALL_OUTRO_VARIANTS = (
    (
        "Список получился небольшим, но каждое изменение — результат труда и внимания команды.",
        "Список получился небольшим, но каждое изменение — результат труда и внимания команды.",
    ),
    (
        "За каждой короткой строкой этого дайджеста стоят время, идеи и работа команды.",
        "За каждой короткой строкой этого дайджеста стоят время, идеи и работа команды.",
    ),
)
PRIMARY_REGULAR_OUTRO_VARIANTS = (
    (
        (
            "На сегодня это всё, но работа продолжается. Возможно, прямо сейчас "
            "команда занимается тем, чего ждёте именно вы."
        ),
        (
            "На этом всё о вчерашних изменениях, но работа продолжается. "
            "Возможно, прямо сейчас команда занимается тем, чего ждёте именно вы."
        ),
    ),
    (
        "Дайджест подошёл к концу, а работа над проектом продолжается.",
        "Дайджест подошёл к концу, а работа над проектом продолжается.",
    ),
    (
        (
            "Сегодня рассказываем об этих изменениях. "
            "Возможно, следующее будет именно тем, которого вы ждёте."
        ),
        (
            "Вчера мы рассказали об этих изменениях. "
            "Возможно, следующее будет именно тем, которого вы ждёте."
        ),
    ),
)
RARE_REGULAR_OUTRO_VARIANTS = (
    (
        (
            "Вот таким получился сегодняшний список, но это далеко не всё — "
            "новые изменения уже в работе."
        ),
        (
            "Вот таким получился вчерашний список, но это далеко не всё — "
            "новые изменения уже в работе."
        ),
    ),
    (
        "На этом сегодняшние новости заканчиваются, но новые идеи и задачи уже ждут своего часа.",
        "На этом вчерашние новости заканчиваются, но новые идеи и задачи уже ждут своего часа.",
    ),
    (
        "Это все изменения на сегодня. Продолжение обязательно последует.",
        "Это все изменения за вчера. Продолжение обязательно последует.",
    ),
)
PRIMARY_LARGE_OUTRO_VARIANTS = (
    (
        "Список получился внушительным. Нашлось ли в нём то, чего ждали именно вы?",
        "Список получился внушительным. Нашлось ли в нём то, чего ждали именно вы?",
    ),
    (
        (
            "Большой список — результат большой работы. "
            "Надеемся, среди изменений есть то, чего вы давно ждали."
        ),
        (
            "Большой список — результат большой работы. "
            "Надеемся, среди изменений есть то, чего вы давно ждали."
        ),
    ),
    (
        (
            "Дайджест получился насыщенным. "
            "Возможно, одно из этих изменений решает именно вашу задачу."
        ),
        (
            "Дайджест получился насыщенным. "
            "Возможно, одно из этих изменений решает именно вашу задачу."
        ),
    ),
)
RARE_LARGE_OUTRO_VARIANTS = (
    (
        "Сегодня изменений немало. Какое из них оказалось для вас самым ожидаемым?",
        "Вчера изменений было немало. Какое из них оказалось для вас самым ожидаемым?",
    ),
    (
        "Изменений много, и каждое приближает проект к тому, каким вы хотите его видеть.",
        "Изменений много, и каждое приближает проект к тому, каким вы хотите его видеть.",
    ),
)


def _choose_relative_variant(
    variants: tuple[tuple[str, str], ...],
    weights: tuple[int, ...],
    *,
    digest_is_today: bool,
) -> str:
    selected = random.choices(variants, weights=weights, k=1)[0]
    return selected[0 if digest_is_today else 1]


def choose_empty_variant(*, digest_is_today: bool) -> str:
    return _choose_relative_variant(
        EMPTY_VARIANTS,
        EMPTY_VARIANT_WEIGHTS,
        digest_is_today=digest_is_today,
    )


def choose_intro_variant(*, digest_is_today: bool) -> str:
    return _choose_relative_variant(
        INTRO_VARIANTS,
        INTRO_VARIANT_WEIGHTS,
        digest_is_today=digest_is_today,
    )


def choose_outro_variant(*, item_count: int, digest_is_today: bool) -> str:
    if item_count <= SMALL_DIGEST_MAX_ITEMS:
        primary = PRIMARY_SMALL_OUTRO_VARIANTS
        rare = RARE_SMALL_OUTRO_VARIANTS
    elif item_count >= LARGE_DIGEST_MIN_ITEMS:
        primary = PRIMARY_LARGE_OUTRO_VARIANTS
        rare = RARE_LARGE_OUTRO_VARIANTS
    else:
        primary = PRIMARY_REGULAR_OUTRO_VARIANTS
        rare = RARE_REGULAR_OUTRO_VARIANTS
    variants = primary + rare
    weights = (5,) * len(primary) + (1,) * len(rare)
    return _choose_relative_variant(
        variants,
        weights,
        digest_is_today=digest_is_today,
    )


def _closing_marker(text: str, marker: str, start: int) -> int:
    return text.find(marker, start + len(marker))


def render_markdown_inline(text: str) -> str:
    output: list[str] = []
    index = 0
    markers = (("**", "b"), ("__", "b"), ("~~", "s"), ("*", "i"), ("_", "i"))
    while index < len(text):
        if text[index] == "\\" and index + 1 < len(text):
            output.append(escape(text[index + 1]))
            index += 2
            continue
        if text[index] == "`":
            end = text.find("`", index + 1)
            if end != -1:
                output.append(f"<code>{escape(text[index + 1 : end])}</code>")
                index = end + 1
                continue
        if text[index] == "[":
            label_end = text.find("](", index + 1)
            if label_end != -1:
                url_end = text.find(")", label_end + 2)
                if url_end != -1:
                    url = text[label_end + 2 : url_end]
                    if urlparse(url).scheme in {"http", "https"}:
                        label = render_markdown_inline(text[index + 1 : label_end])
                        output.append(f'<a href="{escape(url, quote=True)}">{label}</a>')
                        index = url_end + 1
                        continue
        matched = False
        for marker, tag in markers:
            if not text.startswith(marker, index):
                continue
            end = _closing_marker(text, marker, index)
            if end == -1 or end == index + len(marker):
                continue
            inner = render_markdown_inline(text[index + len(marker) : end])
            output.append(f"<{tag}>{inner}</{tag}>")
            index = end + len(marker)
            matched = True
            break
        if matched:
            continue
        output.append(escape(text[index]))
        index += 1
    return "".join(output)


def render_markdown_for_telegram(markdown: str) -> str:
    rendered: list[str] = []
    code_lines: list[str] | None = None
    for line in markdown.splitlines():
        if line.strip().startswith("```"):
            if code_lines is None:
                code_lines = []
            else:
                rendered.append(f"<pre>{escape(chr(10).join(code_lines))}</pre>")
                code_lines = None
            continue
        if code_lines is not None:
            code_lines.append(line)
            continue
        heading = re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", line)
        if heading:
            rendered.append(f"<b>{render_markdown_inline(heading.group(1))}</b>")
            continue
        bullet = re.match(r"^\s*[-+*]\s+(.+)$", line)
        if bullet:
            rendered.append(f"• {render_markdown_inline(bullet.group(1))}")
            continue
        numbered = re.match(r"^\s*(\d+)[.)]\s+(.+)$", line)
        if numbered:
            rendered.append(
                f"{numbered.group(1)}. {render_markdown_inline(numbered.group(2))}"
            )
            continue
        quote = re.match(r"^>\s?(.*)$", line)
        if quote:
            rendered.append(f"<blockquote>{render_markdown_inline(quote.group(1))}</blockquote>")
            continue
        if re.match(r"^\s*([-*_])(?:\s*\1){2,}\s*$", line):
            rendered.append("────────")
            continue
        rendered.append(render_markdown_inline(line))
    if code_lines is not None:
        rendered.append(f"<pre>{escape(chr(10).join(code_lines))}</pre>")
    return "\n".join(rendered).strip()


def render_message(document: DigestDocument, *, digest_is_today: bool) -> str:
    display_date = date.fromisoformat(document.date).strftime("%d.%m.%Y")
    if document.body:
        body = document.body
        intro = choose_intro_variant(digest_is_today=digest_is_today)
        outro = choose_outro_variant(
            item_count=len(BULLET.findall(body)),
            digest_is_today=digest_is_today,
        )
    else:
        body = choose_empty_variant(digest_is_today=digest_is_today)
        intro = DEFAULT_EMPTY_INTRO
        outro = None
    rendered_intro = escape(intro)
    date_line = escape(f"Дайджест за {display_date}")
    rendered_body = render_markdown_for_telegram(body)
    message = (
        f"<b>{rendered_intro}</b>\n"
        f"<i>{date_line}</i>\n\n"
        f"{rendered_body}"
    )
    if outro:
        message += f"\n\n<i>{escape(outro)}</i>"
    return message


def split_message(message: str, limit: int = 4000) -> list[str]:
    if len(message) <= limit:
        return [message]
    chunks: list[str] = []
    remaining = message
    while len(remaining) > limit:
        boundary = remaining.rfind("\n", 0, limit + 1)
        if boundary < limit // 2:
            boundary = limit
        chunks.append(remaining[:boundary].rstrip())
        remaining = remaining[boundary:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks
