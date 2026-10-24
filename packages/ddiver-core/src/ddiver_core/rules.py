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
import threading
import tracemalloc
import unicodedata
from dataclasses import dataclass
from typing import Any

import regex

MAX_PATTERN_LEN = 300
MAX_HIGHLIGHTS = 10
RE_PREFIX = "re:"
WORD_PREFIX = "="
FROM_PREFIX = "from:"

USERNAME_RE = regex.compile(r"^[A-Za-z0-9_]{3,32}$")

# Inputs that make naive catastrophic patterns blow up; used only at validation time.
_PROBE_INPUTS = (
    "a" * 64 + "!",
    "aa " * 32 + "!",
    "x" * 32 + "y" * 32,
    "а" * 64 + "!",  # Cyrillic 'а' for patterns written in Cyrillic
)


MAX_IGNORED_PER_USER = 2000
"""Cap on the ignore list.

Higher than the rule caps because an ignore is a cheap integer comparison rather than a
compiled pattern, and lower than nothing at all because an import is a way to write rows in
bulk and every row is read back into memory for every chat the user watches.
"""

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
    SENDER = "sender"


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


class SenderField(enum.StrEnum):
    ID = "id"
    USERNAME = "username"
    NAME = "name"


@dataclass(frozen=True, slots=True)
class SenderTarget:
    """Who a ``from:`` rule watches.

    Three shapes, and they behave differently on purpose. An id is exact and cannot be
    changed by its owner, so it is the one to use for somebody who keeps coming back. A
    username is exact but they can change it tomorrow. A name is a substring, because a
    display name is decoration — people pad them with emoji, and "Остап" should still find
    "Остап 🚀".
    """

    field: SenderField
    value: str
    number: int = 0

    def matches(self, sender_id: int | None, username: str | None, display_name: str) -> bool:
        if self.field is SenderField.ID:
            return sender_id == self.number
        if self.field is SenderField.USERNAME:
            return username is not None and username.casefold() == self.value
        return self.value in _fold_name(display_name)


def _fold_name(name: str) -> str:
    return " ".join(clean_text(name)[0].split()).casefold()


@dataclass(frozen=True, slots=True)
class CompiledRule:
    """A parsed and compiled rule. ``pattern`` is the text exactly as the user typed it."""

    pattern: str
    mode: MatchMode
    compiled: regex.Pattern[str] | None = None
    """None for a ``from:`` rule, which is answered by ``target`` and never touches the text."""
    target: SenderTarget | None = None
    cost_bytes: int = 0
    """What compiling this pattern allocated. See :data:`MAX_COMPILED_BYTES`."""

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
        if self.compiled is None:  # a sender rule; the matcher never asks it about text
            return out
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
    if text.lower().startswith(FROM_PREFIX):
        return MatchMode.SENDER, text[len(FROM_PREFIX) :].strip()
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
    if mode is MatchMode.SENDER:
        return f"{FROM_PREFIX}{payload}"
    if mode is MatchMode.WORD:
        return f"{WORD_PREFIX}{payload}"
    return payload


def pattern_key(pattern: str) -> str:
    """Uniqueness key: case-folded for substring/word/sender rules (they match
    case-insensitively anyway), verbatim for regexes."""
    mode, payload = parse_pattern(pattern)
    if mode is MatchMode.REGEX:
        return f"{RE_PREFIX}{payload}"
    folded = payload.casefold()
    if mode is MatchMode.SENDER:
        # The `@` stays: `from:@ostap` watches a username and `from:ostap` watches a name
        # fragment, so folding them together made the second one a duplicate of the first and
        # refused it — two different rules about two possibly different people.
        return f"{FROM_PREFIX}{folded}"
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

    if mode is MatchMode.SENDER:
        return CompiledRule(pattern=pattern, mode=mode, target=_sender_target(payload))

    # Before compiling, not after: the length cap bounds the pattern, not what compiling it
    # costs. `re:a{2000000}` is thirteen characters and allocates about half a gigabyte while
    # the module unrolls it — measured: a{200000} is 55 MB — which on a container with a memory
    # limit is an OOM kill delivered by one message.
    if mode is MatchMode.REGEX and _unroll_cost(payload) > MAX_UNROLL:
        raise RuleError("rule.err.unrolls_too_big", limit=MAX_UNROLL)

    if mode is MatchMode.SUBSTRING:
        expr = regex.escape(payload)
    elif mode is MatchMode.WORD:
        expr = rf"(?<!\w){regex.escape(payload)}(?!\w)"
    else:
        expr = payload

    try:
        compiled, cost = _compile_measured(expr)
    except regex.error as exc:  # invalid user regex
        raise RuleError("rule.err.invalid_regex", detail=str(exc)) from exc

    if cost > MAX_COMPILED_BYTES:
        # The module keeps its own cache of compiled patterns, so dropping our reference to a
        # rejected monster does not give the memory back — and rejecting a hundred of them in
        # one import was itself a way to exhaust the container.
        regex.purge()
        raise RuleError("rule.err.unrolls_too_big", limit=MAX_UNROLL)

    if mode is MatchMode.REGEX:
        _validate_regex(pattern, compiled, timeout)
    return CompiledRule(pattern=pattern, mode=mode, compiled=compiled, cost_bytes=cost)


MAX_REPEAT = 1_000
"""Largest single bounded repetition.

``re:a{2000000}`` is thirteen characters and allocates about half a gigabyte while the module
unrolls it — measured: ``a{200000}`` is 55 MB — which on a container with a memory limit is an
OOM kill delivered by one message. The limit is on the count rather than on the time because
the memory is allocated before anything can be timed.
"""

MAX_UNROLL = 5_000
r"""Largest number of atoms a whole pattern may unroll into.

Nesting is what costs. Measured on the same machine: ``(\w{32}){300}`` is 9600 atoms, 2.7 MB
and 29 ms, while ``.{50}такси.{50}`` is 106 atoms, 30 KB and 0.4 ms — both are three bounded
repeats in a row, and only one of them is a problem. The first version of this guard multiplied
every repeat by every other, which rejected ordinary moderation patterns (an e-mail shape, a
word with a gap either side) with a sentence about memory that was untrue of the pattern in
front of the moderator — and, worse, auto-disabled such rules already stored, with that same
reason, the next time their chat was loaded.
"""

MIN_SENDER_NAME = 3
"""Shortest name a ``from:`` rule may watch, because a name is a substring match.

``from:ан`` would fire on half a chat. The limit does not apply to a username or an id: those
are exact, and a two-character username cannot exist anyway.
"""


def _sender_target(payload: str) -> SenderTarget:
    """Read ``@username`` / ``123456789`` / ``Имя Фамилия`` out of a ``from:`` rule."""
    if payload.startswith("+"):
        # Telegram hands out a phone number only for somebody already in your contacts, so a
        # phone rule would quietly match nothing for everyone else. Saying so beats shipping a
        # setting that looks like it works.
        raise RuleError("rule.err.no_phone")
    if payload.startswith("@"):
        name = payload[1:]
        if not USERNAME_RE.match(name):
            raise RuleError("rule.err.bad_username")
        return SenderTarget(SenderField.USERNAME, name.casefold())
    stripped = payload.removeprefix("-")
    if stripped.isascii() and stripped.isdigit():
        number = int(payload)
        # The column is a SQLite signed 64-bit integer, and Python's ints are not. A rule
        # nobody can store is better refused here, where there is somewhere to say why.
        if not -(2**63) <= number < 2**63:
            raise RuleError("rule.err.bad_sender_id")
        return SenderTarget(SenderField.ID, payload, number)
    folded = _fold_name(payload)
    if len(folded) < MIN_SENDER_NAME:
        raise RuleError("rule.err.name_too_short", limit=MIN_SENDER_NAME)
    return SenderTarget(SenderField.NAME, folded)


MAX_COMPILED_BYTES = 512 * 1024
r"""Largest compiled program one rule may hold.

:data:`MAX_UNROLL` counts atoms, and an atom is not a fixed size — which is the hole this
closes. Measured on the same machine: ``.{999}`` is 999 atoms and 119 KB, while
``[\p{L}\p{N}\p{P}\p{S}\p{M}\p{Z}\p{C}]{1000}`` repeated five times is 4993 atoms — under
the cap — and **4.6 MB**. A hundred of those is a 512 MB container, and since rules live in
SQLite the next start compiles them again before it can disable anything, so it was a crash
loop a moderator could install with one /import and nobody could clear without opening the
database by hand.

The counting guard stays because it runs before the allocation; this one runs after, and is
the one that cannot be walked past with a construct nobody thought of.
"""

_MEASURE = threading.Lock()


def _compile_measured(expr: str) -> tuple[regex.Pattern[str], int]:
    """Compile, and report what the compiled program allocated.

    tracemalloc is global state, so this is serialised — compiling happens when a rule is
    added and when a chat is first matched against, never per message, and the measurement
    costs about 0.1 ms. A second compile of the same expression is answered from the module's
    own cache and measures ~0, which is correct: the memory is already counted.
    """
    with _MEASURE:
        tracing = tracemalloc.is_tracing()
        if not tracing:
            tracemalloc.start()
        try:
            before = tracemalloc.get_traced_memory()[0]
            compiled = regex.compile(expr, regex.IGNORECASE)
            after = tracemalloc.get_traced_memory()[0]
        finally:
            if not tracing:
                tracemalloc.stop()
    return compiled, max(0, after - before)


_REPEAT = regex.compile(r"\{(\d+)(?:,(\d*))?\}")


def _skip_class(payload: str, start: int) -> int:
    """Index just past the ``[...]`` beginning at ``start``; the whole class is one atom."""
    i = start + 1
    if i < len(payload) and payload[i] == "^":
        i += 1
    if i < len(payload) and payload[i] == "]":  # a leading ] is a literal, not the end
        i += 1
    while i < len(payload):
        if payload[i] == "\\":
            i += 2
            continue
        if payload[i] == "]":
            return i + 1
        i += 1
    return i


def _unroll_cost(payload: str) -> int:
    """How many atoms this pattern expands into, counted the way the compiler expands them.

    Concatenation adds, a bounded repeat multiplies the one atom in front of it, and a group
    carries its own total into the repeat that follows it — so ``(a{1000}){1000}`` costs what
    ``a{1000000}`` costs and ``a{1000}b{1000}`` costs two thousand.

    Raises :class:`RuleError` for a single count past :data:`MAX_REPEAT`, which is what stops
    ``a{2000000}`` before this can finish adding anything up.
    """
    totals = [0]  # atoms accumulated at each open group level
    lasts = [0]  # the most recent atom at that level, for the repeat that may follow it
    i = 0
    while i < len(payload):
        ch = payload[i]
        if ch == "\\":  # an escape is one atom, and its second character is never syntax
            i += 2
        elif ch == "[":
            i = _skip_class(payload, i)
        elif ch == "(":
            totals.append(0)
            lasts.append(0)
            i += 1
            continue
        elif ch == ")" and len(totals) > 1:
            group = totals.pop()
            lasts.pop()
            totals[-1] += group
            lasts[-1] = group
            i += 1
            continue
        elif ch == "{" and (match := _REPEAT.match(payload, i)) is not None:
            low, high = match.group(1), match.group(2)
            count = int(high) if high else int(low)
            if count > MAX_REPEAT:
                raise RuleError("rule.err.repeat_too_big", limit=MAX_REPEAT)
            # The atom is already counted once, so this adds the copies it did not have.
            totals[-1] += lasts[-1] * (count - 1)
            lasts[-1] *= count
            i = match.end()
            continue
        else:
            i += 1
        totals[-1] += 1
        lasts[-1] = 1
    # An unclosed group leaves its level on the stack; regex.compile rejects it a moment later.
    return sum(totals)


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
