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
from dataclasses import dataclass
from typing import Any

import regex

MAX_PATTERN_LEN = 300
RE_PREFIX = "re:"
WORD_PREFIX = "="

# Inputs that make naive catastrophic patterns blow up; used only at validation time.
_PROBE_INPUTS = (
    "a" * 64 + "!",
    "aa " * 32 + "!",
    "x" * 32 + "y" * 32,
    "а" * 64 + "!",  # Cyrillic 'а' for patterns written in Cyrillic
)


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
        """Return the first match span or ``None``; raise :class:`RuleTimeout` on timeout."""
        try:
            m = self.compiled.search(text, timeout=timeout)
        except TimeoutError as exc:
            raise RuleTimeout(self.pattern) from exc
        if m is None:
            return None
        return Span(m.start(), m.end())


def parse_pattern(raw: str) -> tuple[MatchMode, str]:
    """Split a raw rule into (mode, payload) without compiling it."""
    text = raw.strip()
    if text.startswith(RE_PREFIX):
        return MatchMode.REGEX, text[len(RE_PREFIX) :].strip()
    if text.startswith(WORD_PREFIX):
        return MatchMode.WORD, text[len(WORD_PREFIX) :].strip()
    return MatchMode.SUBSTRING, text


def normalize_pattern(raw: str) -> str:
    """Canonical stored form: trimmed, prefix kept, inner whitespace preserved."""
    mode, payload = parse_pattern(raw)
    if mode is MatchMode.REGEX:
        return f"{RE_PREFIX}{payload}"
    if mode is MatchMode.WORD:
        return f"{WORD_PREFIX}{payload}"
    return payload


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
    try:
        if compiled.search("", timeout=timeout) is not None:
            raise RuleError("rule.err.matches_empty")
        for probe in _PROBE_INPUTS:
            compiled.search(probe, timeout=timeout)
    except TimeoutError as exc:
        raise RuleError("rule.err.too_slow", pattern=pattern) from exc


def parse_rule_lines(text: str) -> list[str]:
    """Split multi-line user input into candidate rules; blank lines and ``#`` comments dropped."""
    out: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        out.append(stripped)
    return out
