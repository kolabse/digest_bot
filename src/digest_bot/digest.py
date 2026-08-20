from __future__ import annotations

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


# TODO: make templates configurable and support weighted variants.
DEFAULT_INTRO = "Ежедневный дайджест проекта за {date}:"
DEFAULT_OUTRO = "До следующего дайджеста!"
DEFAULT_EMPTY = "Команда отдыхала."


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


def render_message(document: DigestDocument) -> str:
    display_date = date.fromisoformat(document.date).strftime("%d.%m.%Y")
    body = document.body or DEFAULT_EMPTY
    intro = escape(DEFAULT_INTRO.format(date=display_date))
    rendered_body = render_markdown_for_telegram(body)
    outro = escape(DEFAULT_OUTRO)
    return f"<b>{intro}</b>\n\n{rendered_body}\n\n<i>{outro}</i>"


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
