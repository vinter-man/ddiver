"""Rule syntax and compiled matching.

Syntax (fixed by the requirements):

* ``word``     case-insensitive substring
* ``=word``    whole word; lookarounds instead of ``\\b`` so ``=c++`` and ``=#tag`` work
* ``re:expr``  regular expression, IGNORECASE by default (opt out inline with ``(?-i:...)``)

Everything is compiled with the third-party :mod:`regex` module because it supports a
``timeout`` on matching, which is the only practical defence against a user-supplied
catastrophic pattern freezing the event loop.
"""

from __future__ import annotations

import enum
import unicodedata
from dataclasses import dataclass
from typing import Any

import regex

MAX_PATTERN_LEN = 300
MAX_HIGHLIGHTS = 10
RE_PREFIX = "re:"
WORD_PREFIX = "="

# Inputs that make naive catastrophic patterns blow up; used only at validation time.
_PROBE_INPUTS = (
    "a" * 64 + "!",
    "aa " * 32 + "!",
    "x" * 32 + "y" * 32,
    "а" * 64 + "!",  # Cyrillic 'а' for patterns written in Cyrillic
)


MAX_RULES_PER_CHAT = 200
MAX_RULES_PER_USER = 500
"""Caps on how many rules one person may hold.

They live here rather than in the bot UI because there is more than one way in: the rules
screen enforced them and /import did not, so a file could put a moderator far beyond what the
screen would ever have allowed — and every one of those patterns is then compiled on the hot
path for every message in the chat.
"""


class RuleKind(enum.StrEnum):
    INCLUDE = "include"
    EXCLUDE = "exclude"


class MatchMode(enum.StrEnum):
    SUBSTRING = "substring"
    WORD = "word"
    REGEX = "regex"


class RuleError(ValueError):
    """Invalid rule pattern. ``key`` is an i18n key, ``params`` its format arguments."""

    def __init__(self, key: str, **params: Any) -> None:
        super().__init__(key)
        self.key = key
        self.params = params


class RuleTimeout(Exception):  # noqa: N818 - "Timeout" reads better than "TimeoutError" here
    """Matching exceeded the configured time budget."""


@dataclass(frozen=True, slots=True)
class Span:
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class CompiledRule:
    """A parsed and compiled rule. ``pattern`` is the text exactly as the user typed it."""

    pattern: str
    mode: MatchMode
    compiled: regex.Pattern[str]

    def search(self, text: str, timeout: float) -> Span | None:
        """First non-empty match span, or ``None``; raises :class:`RuleTimeout` on timeout.

        Zero-length matches (``re:x*`` and friends) are skipped: Telegram rejects zero-length
        entities, and "matched nothing" is not a useful notification.
        """
        spans = self.search_all(text, timeout, limit=1)
        return spans[0] if spans else None

    def search_all(self, text: str, timeout: float, limit: int = MAX_HIGHLIGHTS) -> list[Span]:
        """Up to ``limit`` non-empty, non-overlapping match spans in order."""
        out: list[Span] = []
        try:
            for m in self.compiled.finditer(text, timeout=timeout):
                if m.end() > m.start():
                    out.append(Span(m.start(), m.end()))
                    if len(out) >= limit:
                        break
        except TimeoutError as exc:
            raise RuleTimeout(self.pattern) from exc
        return out


def parse_pattern(raw: str) -> tuple[MatchMode, str]:
    """Split a raw rule into (mode, payload) without compiling it."""
    text = raw.strip()
    if text.startswith(RE_PREFIX):
        return MatchMode.REGEX, text[len(RE_PREFIX) :].strip()
    if text.startswith(WORD_PREFIX):
        return MatchMode.WORD, text[len(WORD_PREFIX) :].strip()
    return MatchMode.SUBSTRING, text


def clean_text(raw: str) -> tuple[str, int]:
    """NFC-normalize and drop invisible format characters (zero-width spaces/joiners, BOM…).

    Returns the cleaned text and how many characters were removed, so the UI can tell the
    user that a pasted keyword contained invisible junk.
    """
    normalized = unicodedata.normalize("NFC", raw)
    cleaned = "".join(ch for ch in normalized if unicodedata.category(ch) != "Cf")
    return cleaned, len(normalized) - len(cleaned)


def normalize_pattern(raw: str) -> str:
    """Canonical stored form: cleaned, trimmed, prefix kept, inner whitespace preserved."""
    mode, payload = parse_pattern(clean_text(raw)[0])
    if mode is MatchMode.REGEX:
        return f"{RE_PREFIX}{payload}"
    if mode is MatchMode.WORD:
        return f"{WORD_PREFIX}{payload}"
    return payload


def pattern_key(pattern: str) -> str:
    """Uniqueness key: case-folded for substring/word rules (they match case-insensitively
    anyway), verbatim for regexes."""
    mode, payload = parse_pattern(pattern)
    if mode is MatchMode.REGEX:
        return f"{RE_PREFIX}{payload}"
    folded = payload.casefold()
    return f"{WORD_PREFIX}{folded}" if mode is MatchMode.WORD else folded


@dataclass(frozen=True, slots=True)
class PreparedText:
    """Message text ready for matching.

    ``shadow`` is the NFC-normalized text with format characters (zero-width joiners, BOM…)
    removed; ``starts``/``ends`` map each shadow code point back to the half-open range of
    original code points it came from, so a match span can be turned into a highlight span on
    the original text.
    """

    original: str
    shadow: str
    starts: tuple[int, ...]
    ends: tuple[int, ...]

    def to_original(self, span: Span) -> Span:
        if not self.starts or span.end <= span.start:
            return Span(0, 0)
        last = len(self.starts) - 1
        return Span(self.starts[min(span.start, last)], self.ends[min(span.end - 1, last)])


def prepare_text(text: str) -> PreparedText:
    if not text:
        return PreparedText("", "", (), ())
    if unicodedata.is_normalized("NFC", text) and not any(
        unicodedata.category(ch) == "Cf" for ch in text
    ):
        positions = tuple(range(len(text)))
        return PreparedText(text, text, positions, tuple(p + 1 for p in positions))
    shadow: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    unit: list[str] = []
    unit_start = 0
    unit_end = 0

    def flush() -> None:
        if not unit:
            return
        for out in unicodedata.normalize("NFC", "".join(unit)):
            shadow.append(out)
            starts.append(unit_start)
            ends.append(unit_end)
        unit.clear()

    for pos, ch in enumerate(text):
        category = unicodedata.category(ch)
        if category == "Cf":
            continue
        if category[0] == "M" and unit:  # combining mark: stays with the current base character
            unit.append(ch)
            unit_end = pos + 1
            continue
        flush()
        unit_start, unit_end = pos, pos + 1
        unit.append(ch)
    flush()
    return PreparedText(text, "".join(shadow), tuple(starts), tuple(ends))


def compile_rule(raw: str, *, timeout: float) -> CompiledRule:
    """Validate and compile a rule.

    Raises :class:`RuleError` with an i18n key for: empty pattern, pattern too long, invalid
    regex, regex that matches empty text, regex that times out on probe inputs.
    """
    pattern = normalize_pattern(raw)
    mode, payload = parse_pattern(pattern)
    if not payload:
        raise RuleError("rule.err.empty")
    if len(pattern) > MAX_PATTERN_LEN:
        raise RuleError("rule.err.too_long", limit=MAX_PATTERN_LEN)

    if mode is MatchMode.SUBSTRING:
        expr = regex.escape(payload)
    elif mode is MatchMode.WORD:
        expr = rf"(?<!\w){regex.escape(payload)}(?!\w)"
    else:
        expr = payload

    try:
        compiled = regex.compile(expr, regex.IGNORECASE)
    except regex.error as exc:  # invalid user regex
        raise RuleError("rule.err.invalid_regex", detail=str(exc)) from exc

    if mode is MatchMode.REGEX:
        _validate_regex(pattern, compiled, timeout)
    return CompiledRule(pattern=pattern, mode=mode, compiled=compiled)


def _validate_regex(pattern: str, compiled: regex.Pattern[str], timeout: float) -> None:
    """Heuristic guard rails. The real protection against slow patterns is the runtime timeout
    plus auto-disable; this only catches the obvious cases at insert time."""
    try:
        if _matches_everything(compiled, timeout):
            raise RuleError("rule.err.matches_empty")
        for probe in _PROBE_INPUTS:
            compiled.search(probe, timeout=timeout)
    except TimeoutError as exc:
        raise RuleError("rule.err.too_slow", pattern=pattern) from exc


def _matches_everything(compiled: regex.Pattern[str], timeout: float) -> bool:
    """True when the pattern can match empty text (``re:.*``, ``re:^``, ``re:x*``): such a rule
    fires on every message, which is never what the user wants."""
    return compiled.search("", timeout=timeout) is not None


def parse_rule_lines(text: str) -> list[str]:
    """Split multi-line user input into candidate rules; blank lines dropped.

    There is deliberately no comment syntax: ``#промо`` is a hashtag keyword, not a comment.
    """
    return [line.strip() for line in text.splitlines() if line.strip()]
