"""Render notifications and digests.

Rendering happens in the delivery worker, just before sending, from the immutable payload
snapshot taken when the match was found plus the user's *current* language and timezone. A
message that waited in the queue or a flood gate therefore shows the right relative time and
the right language, even if the user changed settings meanwhile.

A payload knows where it came from. Telegram links are *derived* from the chat and message ids,
which is only right for a Telegram source: a Facebook group id fed to the same functions would
produce a working-looking ``t.me`` link to somebody else's chat. So a source other than
Telegram carries its links in the payload and gets no derived link at all.
"""

from __future__ import annotations

import datetime as dt
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from telethon.tl import types as tl

from ddiver_core.i18n import t
from ddiver_core.links import ChatRef, chat_link, message_link
from ddiver_core.richtext import MAX_MESSAGE_UTF16, RichText, Span, window
from ddiver_core.rules import RuleKind
from ddiver_core.timeutil import format_local, relative

TEXT_BUDGET = 3200
"""UTF-16 units left for the message body after the header lines."""

SNIPPET_CHARS = 60

DIGEST_BUDGET = 3500
DIGEST_CHATS = 12
DIGEST_ITEMS_PER_CHAT = 5
DIGEST_RULE_CHARS = 48
DIGEST_SENDER_CHARS = 32
DIGEST_LINE_MAX = DIGEST_RULE_CHARS + DIGEST_SENDER_CHARS + SNIPPET_CHARS + 32
"""Longest one item line can be, bullet and separators included.

The loop adds a line only when this much still fits, which is what makes "the digest is never
longer than Telegram will accept" a property of the code rather than of the inputs.
"""

DIGEST_TAIL_RESERVE = 320
"""Room kept for the two lines appended after the loop, which nothing else checks."""

SOURCE_TELEGRAM = "tg"
"""The only source whose links can be built from ids; any other value gets no derived link."""

_LINK_SCHEMES = frozenset({"https", "http"})
_LINK_MAX_CHARS = 2048
"""Longer than any link a source has reason to send. The link line shows it in full, so a
longer one would crowd the post text out of the message."""
_INVISIBLE = frozenset({"Cc", "Cf"})
"""Control and format characters: line breaks, bidi overrides, zero-width joiners."""


@dataclass(frozen=True, slots=True)
class Payload:
    """Everything needed to render one notification, captured at match time.

    Stored as JSON in the outbox, so it must stay plain-data and backwards compatible.
    """

    chat_id: int
    chat_title: str
    chat_username: str | None
    chat_kind: str
    msg_id: int
    topic_id: int | None
    date: dt.datetime
    text: str
    spans: list[tuple[int, int]]
    rule_pattern: str
    rule_kind: str
    rule_is_global: bool
    sender_id: int | None
    sender_name: str
    sender_username: str | None
    sender_is_channel: bool = False
    sender_is_deleted: bool = False
    is_forwarded: bool = False
    catchup: bool = False
    catchup_capped: bool = False
    source: str = SOURCE_TELEGRAM
    url: str | None = None
    """Link to the message itself. Preferred over a derived one when set."""
    chat_url: str | None = None
    sender_url: str | None = None
    """Link to the sender's profile. A Telegram notification points the name at the message
    instead (that works on every client), so ddiver never sets it."""
    chat_private: bool = False
    """The chat's link opens for members only; the notification says so."""
    late: bool = False
    """The source saw the message long after it was posted (a post approved late)."""

    @property
    def chat_ref(self) -> ChatRef:
        return ChatRef(
            id=self.chat_id,
            title=self.chat_title,
            username=self.chat_username,
            kind=self.chat_kind,
        )

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "chat_id": self.chat_id,
            "chat_title": self.chat_title,
            "chat_username": self.chat_username,
            "chat_kind": self.chat_kind,
            "msg_id": self.msg_id,
            "topic_id": self.topic_id,
            "date": self.date.astimezone(dt.UTC).isoformat(),
            "text": self.text,
            "spans": [list(s) for s in self.spans],
            "rule_pattern": self.rule_pattern,
            "rule_kind": self.rule_kind,
            "rule_is_global": self.rule_is_global,
            "sender_id": self.sender_id,
            "sender_name": self.sender_name,
            "sender_username": self.sender_username,
            "sender_is_channel": self.sender_is_channel,
            "sender_is_deleted": self.sender_is_deleted,
            "is_forwarded": self.is_forwarded,
            "catchup": self.catchup,
            "catchup_capped": self.catchup_capped,
        }
        # The newer fields are written only when they say something. A Telegram row then stays
        # byte-for-byte what it was, and a build from before these fields can still read every
        # row a newer one wrote (it ignores keys it does not know) if a deploy is rolled back.
        if self.source != SOURCE_TELEGRAM:
            data["source"] = self.source
        if self.url is not None:
            data["url"] = self.url
        if self.chat_url is not None:
            data["chat_url"] = self.chat_url
        if self.sender_url is not None:
            data["sender_url"] = self.sender_url
        if self.chat_private:
            data["chat_private"] = True
        if self.late:
            data["late"] = True
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Payload:
        moment = dt.datetime.fromisoformat(data["date"])
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=dt.UTC)
        return cls(
            chat_id=data["chat_id"],
            chat_title=data["chat_title"],
            chat_username=data.get("chat_username"),
            chat_kind=data.get("chat_kind", "supergroup"),
            msg_id=data["msg_id"],
            topic_id=data.get("topic_id"),
            date=moment,
            text=data.get("text", ""),
            spans=[(int(a), int(b)) for a, b in data.get("spans", [])],
            rule_pattern=data.get("rule_pattern", ""),
            rule_kind=data.get("rule_kind", RuleKind.INCLUDE.value),
            rule_is_global=bool(data.get("rule_is_global", False)),
            sender_id=data.get("sender_id"),
            sender_name=data.get("sender_name", ""),
            sender_username=data.get("sender_username"),
            sender_is_channel=bool(data.get("sender_is_channel", False)),
            sender_is_deleted=bool(data.get("sender_is_deleted", False)),
            is_forwarded=bool(data.get("is_forwarded", False)),
            catchup=bool(data.get("catchup", False)),
            # `catchup_skipped` is the old name, still on outbox rows written before
            # the upgrade that replaced a fabricated count with the fact.
            catchup_capped=bool(data.get("catchup_capped") or data.get("catchup_skipped")),
            source=str(data.get("source") or SOURCE_TELEGRAM),
            url=data.get("url"),
            chat_url=data.get("chat_url"),
            sender_url=data.get("sender_url"),
            chat_private=bool(data.get("chat_private", False)),
            late=bool(data.get("late", False)),
        )

    @property
    def is_telegram(self) -> bool:
        return self.source == SOURCE_TELEGRAM

    def link_to_message(self, chat: ChatRef | None = None) -> str | None:
        """The payload's own link, or one derived from the ids for a Telegram message.

        ``chat`` is the chat to derive from when it is not this payload's own snapshot: a
        digest links every item of a chat through the first one's username.
        """
        if self.url is not None:
            return _safe_url(self.url)
        if self.is_telegram:
            return message_link(chat or self.chat_ref, self.msg_id, self.topic_id)
        return None

    def link_to_chat(self) -> str | None:
        """Link to the chat itself; ``None`` when there is none (callers fall back)."""
        if self.chat_url is not None:
            return _safe_url(self.chat_url)
        if self.is_telegram:
            return chat_link(self.chat_ref)
        return None


def _safe_url(url: object) -> str | None:
    """A link the notification may carry, or ``None``.

    The payload's links are data from a source, and they end up as clickable text in a private
    chat. Anything that is not a plain web address (``javascript:``, ``tg:``, a bare path, a
    non-string from a damaged row) is dropped rather than linked.

    So is an address with whitespace or an invisible character in it. ``urlsplit`` quietly
    removes tabs and line breaks while it parses, but the string it was given is what would
    be shown: a line break would add a line of the source's choosing to the notification's
    header, and a bidi override would make the link line read as another address.
    """
    if not isinstance(url, str) or len(url) > _LINK_MAX_CHARS:
        return None
    if any(ch.isspace() or unicodedata.category(ch) in _INVISIBLE for ch in url):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme.lower() not in _LINK_SCHEMES or not parts.netloc:
        return None
    return url


@dataclass(frozen=True, slots=True)
class Rendered:
    text: str
    entities: list[tl.TypeMessageEntity] = field(default_factory=list)


def sender_label(payload: Payload, lang: str) -> str:
    if payload.sender_is_deleted:
        return t(lang, "notif.sender.deleted")
    if payload.sender_name:
        return payload.sender_name
    if payload.sender_is_channel:
        return t(lang, "notif.sender.anonymous")
    if payload.sender_id is not None:
        # We know who it was, just not their name (a basic group's update carries no entity).
        return t(lang, "notif.sender.by_id", id=payload.sender_id)
    return t(lang, "notif.sender.unknown")


def render_notification(payload: Payload, *, lang: str, tz: str, now: dt.datetime) -> Rendered:
    link = payload.link_to_message()
    rt = RichText()

    # chat line
    rt.add(t(lang, "notif.chat")).add(" ")
    rt.add(payload.chat_title or str(payload.chat_id), url=payload.link_to_chat() or link)
    rt.newline()
    if payload.chat_private:
        rt.add(t(lang, "notif.private")).newline()

    # sender line: the name points at the profile when the source gave one, otherwise at the
    # message (works on every client); the username at the profile, and the numeric id is
    # copyable code.
    profile = _safe_url(payload.sender_url)
    rt.add(t(lang, "notif.sender")).add(" ")
    rt.add(sender_label(payload, lang), url=profile or link)
    if payload.sender_username and payload.is_telegram:
        rt.add(" · ")
        rt.add(f"@{payload.sender_username}", url=f"https://t.me/{payload.sender_username}")
    if payload.sender_id is not None:
        rt.add(" · ").add(str(payload.sender_id), code=True)
    if payload.is_forwarded:
        rt.add(" · ").add(t(lang, "notif.forwarded"))
    rt.newline()

    if link:
        rt.add(t(lang, "notif.link")).add(" ").add(link, url=link).newline()

    rt.add(t(lang, "notif.time")).add(" ")
    rt.add(format_local(payload.date, tz))
    rt.add(" · ").add(relative(payload.date, now, lang))
    if payload.catchup:
        rt.add(" · ").add(t(lang, "notif.missed"))
    rt.newline()
    if payload.late:
        rt.add(t(lang, "notif.late", ago=relative(payload.date, now, lang))).newline()

    scope = "notif.rule_scope.global" if payload.rule_is_global else "notif.rule_scope.chat"
    rt.add(t(lang, "notif.rule")).add(" ")
    rt.add(payload.rule_pattern, code=True).add(" (").add(t(lang, scope)).add(")")
    if payload.catchup_capped:
        rt.newline().add(t(lang, "notif.catchup_capped"))
    rt.newline(2)

    spans = [Span(a, b) for a, b in payload.spans]
    budget = min(TEXT_BUDGET, MAX_MESSAGE_UTF16 - rt.utf16_length - 64)
    view = window(payload.text, spans, budget)
    if view.cut_before:
        rt.add("…")
    rt.add_with_bold_spans(view.text, view.spans)
    if view.cut_after:
        rt.add("…").newline().add(t(lang, "notif.truncated", n=view.cut_after))

    text, entities = rt.build()
    return Rendered(text, entities)


@dataclass(frozen=True, slots=True)
class DigestEntry:
    payload: Payload
    key: tuple[int, int, int]


def render_digest(
    entries: list[DigestEntry],
    *,
    lang: str,
    tz: str,
    now: dt.datetime,
    overflow: int = 0,
) -> Rendered:
    """One message summarising many matches, grouped by chat, newest chats first by count."""
    del tz, now  # only absolute grouping is shown; per-item times stay in the linked messages
    by_chat: dict[int, list[Payload]] = {}
    for entry in entries:
        by_chat.setdefault(entry.payload.chat_id, []).append(entry.payload)
    ordered = sorted(by_chat.items(), key=lambda kv: (-len(kv[1]), kv[1][0].chat_title))

    rt = RichText().add(t(lang, "digest.title", n=len(entries)), bold=True).newline(2)
    shown_chats = 0
    for chat_id, payloads in ordered:
        if shown_chats >= DIGEST_CHATS or rt.utf16_length > DIGEST_BUDGET:
            break
        first = payloads[0]
        chat = first.chat_ref
        head_link = first.link_to_chat() or first.link_to_message()
        rt.add("📡 ")
        rt.add(first.chat_title or str(chat_id), bold=True, url=head_link)
        if first.chat_private:
            rt.add(" 🔒")
        rt.add(f" — {len(payloads)}").newline()
        shown_items = 0
        for payload in payloads[:DIGEST_ITEMS_PER_CHAT]:
            if rt.utf16_length + DIGEST_LINE_MAX + DIGEST_TAIL_RESERVE > MAX_MESSAGE_UTF16:
                # The budget was checked once per chat, before the chat's whole block went in,
                # and the two trailing lines were never checked at all. A digest could leave
                # here at ~4700 units, Telegram answered MESSAGE_TOO_LONG — which is not a
                # rendering error, so there was no plain-text retry — and the entire batch was
                # written `failed`, which nothing re-reads. Now every line is asked.
                break
            link = payload.link_to_message(chat)
            line = _digest_line(payload, lang)
            rt.add("  • ")
            rt.add(line, url=link) if link else rt.add(line)
            rt.newline()
            shown_items += 1
        if len(payloads) > shown_items:
            rt.add("  ").add(t(lang, "digest.more_items", n=len(payloads) - shown_items)).newline()
        shown_chats += 1

    if shown_chats < len(ordered):
        remaining = ordered[shown_chats:]
        rt.newline().add(
            t(
                lang,
                "digest.more_chats",
                n=len(remaining),
                m=sum(len(p) for _, p in remaining),
            )
        )
    if overflow:
        rt.newline().add(t(lang, "digest.overflow", n=overflow))

    text, entities = rt.build()
    return Rendered(text, entities)


WEEKLY_ROWS = 5
WEEKLY_LABEL = 40
"""How much of a chat title or a rule fits on one line of the report."""


def render_weekly(
    *,
    lang: str,
    total: int,
    previous: int,
    chats: Sequence[tuple[int, str, int]],
    rules: Sequence[tuple[str, int]],
) -> Rendered:
    """The week in one message: how much was caught, where, and by which rules.

    The comparison with the week before is the part that earns the message — a number on its
    own says nothing, and "quiet week" is exactly when somebody should check whether the
    listener is still logged in rather than assume the chats went quiet.
    """
    rt = RichText().add(t(lang, "weekly.title"), bold=True).newline(2)
    rt.add(t(lang, "weekly.total", n=total)).newline()
    rt.add(_change_line(lang, previous, total)).newline()

    if chats:
        rt.newline().add(t(lang, "weekly.chats"), bold=True).newline()
        for _chat_id, title, n in chats[:WEEKLY_ROWS]:
            rt.add(f"  • {_cut(title or '—', WEEKLY_LABEL)} — {n}").newline()
    if rules:
        rt.newline().add(t(lang, "weekly.rules"), bold=True).newline()
        for pattern, n in rules[:WEEKLY_ROWS]:
            rt.add("  • ")
            rt.add(_cut(pattern, WEEKLY_LABEL), code=True)
            rt.add(f" — {n}").newline()
    if total == 0:
        rt.newline().add(t(lang, "weekly.nothing"))

    text, entities = rt.build()
    return Rendered(text, entities)


def _change_line(lang: str, previous: int, total: int) -> str:
    """How this week compares. A percentage of nothing is not a fact, so that case says so."""
    if previous == 0:
        return t(lang, "weekly.no_baseline")
    delta = round((total - previous) / previous * 100)
    if delta == 0:
        return t(lang, "weekly.same", n=previous)
    key = "weekly.up" if delta > 0 else "weekly.down"
    return t(lang, key, pct=abs(delta), n=previous)


def _digest_line(payload: Payload, lang: str) -> str:
    """One item line, bounded.

    Everything interpolated here is attacker-shaped: the rule is the moderator's own and may
    be 300 characters, the sender name is whatever somebody called themselves. Cutting them to
    a preview is what lets `render_digest` know, before it adds a line, that the message will
    still fit.
    """
    return t(
        lang,
        "digest.item",
        rule=_cut(payload.rule_pattern, DIGEST_RULE_CHARS),
        sender=_cut(sender_label(payload, lang) or "?", DIGEST_SENDER_CHARS),
        snippet=_snippet(payload),
    )


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def _snippet(payload: Payload) -> str:
    text = " ".join(payload.text.split())
    if not text:
        return "—"
    start = payload.spans[0][0] if payload.spans else 0
    begin = max(0, start - SNIPPET_CHARS // 3)
    cut = text[begin : begin + SNIPPET_CHARS]
    prefix = "…" if begin else ""
    suffix = "…" if begin + SNIPPET_CHARS < len(text) else ""
    return f"{prefix}{cut}{suffix}"
