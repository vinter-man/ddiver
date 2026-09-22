"""Build message text plus Telethon ``MessageEntity`` objects with correct UTF-16 offsets.

Telegram measures entity offsets and the 4096-character message limit in UTF-16 code units,
so anything outside the BMP (most emoji) counts twice. Building entities ourselves (instead of
HTML) avoids escaping bugs and lets us emit ``tg://user?id=`` links as plain text-URL
entities, which Telethon's HTML parser would otherwise convert or drop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Self

from telethon.tl import types as tl

MAX_MESSAGE_UTF16 = 4096


def utf16_len(text: str) -> int:
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)


def truncate_utf16(text: str, limit: int) -> str:
    """Cut ``text`` so that its UTF-16 length is ``<= limit`` without splitting a code point."""
    if limit <= 0:
        return ""
    units = 0
    for i, ch in enumerate(text):
        units += 2 if ord(ch) > 0xFFFF else 1
        if units > limit:
            return text[:i]
    return text


@dataclass(frozen=True, slots=True)
class Span:
    start: int  # code-point offsets within the text they were computed for
    end: int


@dataclass(slots=True)
class RichText:
    """Append-only builder. Offsets are tracked in UTF-16 units as parts are added."""

    _parts: list[str] = field(default_factory=list)
    _entities: list[tl.TypeMessageEntity] = field(default_factory=list)
    _units: int = 0

    @property
    def utf16_length(self) -> int:
        return self._units

    def add(
        self,
        text: str,
        *,
        bold: bool = False,
        code: bool = False,
        url: str | None = None,
    ) -> Self:
        if not text:
            return self
        length = utf16_len(text)
        if url:
            self._entities.append(tl.MessageEntityTextUrl(self._units, length, url))
        if bold:
            self._entities.append(tl.MessageEntityBold(self._units, length))
        if code:
            self._entities.append(tl.MessageEntityCode(self._units, length))
        self._parts.append(text)
        self._units += length
        return self

    def add_with_bold_spans(self, text: str, spans: list[Span]) -> Self:
        """Add ``text`` making the given code-point spans bold; spans may overlap or be unsorted."""
        merged = _merge_spans(spans, len(text))
        cursor = 0
        for span in merged:
            self.add(text[cursor : span.start])
            self.add(text[span.start : span.end], bold=True)
            cursor = span.end
        self.add(text[cursor:])
        return self

    def newline(self, count: int = 1) -> Self:
        return self.add("\n" * count)

    def text(self) -> str:
        return "".join(self._parts)

    def entities(self) -> list[tl.TypeMessageEntity]:
        return list(self._entities)


def _merge_spans(spans: list[Span], text_len: int) -> list[Span]:
    clipped = sorted(
        (Span(max(0, s.start), min(text_len, s.end)) for s in spans if s.end > s.start),
        key=lambda s: s.start,
    )
    out: list[Span] = []
    for span in clipped:
        if span.end <= span.start:
            continue
        if out and span.start <= out[-1].end:
            out[-1] = Span(out[-1].start, max(out[-1].end, span.end))
        else:
            out.append(span)
    return out
