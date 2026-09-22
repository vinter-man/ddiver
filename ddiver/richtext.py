"""Build message text plus Telethon ``MessageEntity`` objects with correct UTF-16 offsets.

Telegram measures entity offsets and the 4096-character message limit in UTF-16 code units,
so anything outside the BMP (most emoji) counts twice. Building entities ourselves (instead of
HTML) avoids escaping bugs; Telethon sends ``formatting_entities=`` verbatim.

Server-side rules encoded here: trailing whitespace is stripped from messages (an entity that
reaches into it is rejected with ``EntityBoundsInvalid``), zero-length entities are invalid and
at most 100 entities are accepted per message.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Self

from telethon.tl import types as tl

MAX_MESSAGE_UTF16 = 4096
MAX_ENTITIES = 90  # Telegram allows 100; keep headroom for the caller's own additions
ELLIPSIS = "…"


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


@dataclass(frozen=True, slots=True)
class Window:
    text: str
    spans: list[Span]
    cut_before: int  # code points removed before the window
    cut_after: int  # code points removed after the window


def window(text: str, spans: list[Span], limit: int, *, context: int = 160) -> Window:
    """Fit ``text`` into ``limit`` UTF-16 units keeping the first span visible.

    If the whole text fits it is returned untouched. Otherwise a head slice is used when the
    first span starts early enough; else a slice beginning ``context`` code points before the
    first span. Spans are re-based to the returned text and clipped to it.
    """
    if utf16_len(text) <= limit:
        return Window(text, list(spans), 0, 0)
    ordered = sorted((s for s in spans if s.end > s.start), key=lambda s: s.start)
    first_start = ordered[0].start if ordered else 0
    head = truncate_utf16(text, limit)
    if first_start < len(head) - context // 2:
        start = 0
        body = head
    else:
        start = max(0, first_start - context)
        body = truncate_utf16(text[start:], limit)
    end = start + len(body)
    rebased = [
        Span(max(s.start, start) - start, min(s.end, end) - start)
        for s in ordered
        if s.start < end and s.end > start
    ]
    return Window(body, rebased, start, len(text) - end)


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

    def build(self) -> tuple[str, list[tl.TypeMessageEntity]]:
        """Final text and entities: trailing whitespace stripped, entities clipped/dropped to stay
        inside the text, zero-length ones removed, at most :data:`MAX_ENTITIES` kept."""
        text = self.text().rstrip()
        length = utf16_len(text)
        kept: list[tl.TypeMessageEntity] = []
        for ent in self._entities:
            end = min(ent.offset + ent.length, length)
            if ent.offset >= end:
                continue
            clipped = ent if end == ent.offset + ent.length else _with_length(ent, end - ent.offset)
            kept.append(clipped)
            if len(kept) >= MAX_ENTITIES:
                break
        return text, kept


def _with_length(ent: tl.TypeMessageEntity, length: int) -> tl.TypeMessageEntity:
    if isinstance(ent, tl.MessageEntityTextUrl):
        return tl.MessageEntityTextUrl(ent.offset, length, ent.url)
    return type(ent)(ent.offset, length)


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
