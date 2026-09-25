"""Keyboards and list pagination, expressed as plain data so handlers stay testable.

A button is ``(label_key_or_text, callback_data)``; the adapter turns it into a Telethon
``Button.inline``. Labels are i18n keys unless they are pre-rendered (chat titles, patterns),
in which case the handler passes the literal text via :func:`literal`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ddiver.i18n import LANG_NAMES, LANGS
from ddiver.storage import ALL_CHATS, ChatRow, IgnoredSender, Rule

CHATS_PER_PAGE = 8
RULES_PER_PAGE = 10
PATTERN_PREVIEW = 60
MAX_CALLBACK_BYTES = 64

QUICK_TIMEZONES = ("Europe/Kyiv", "Europe/Warsaw", "Europe/Berlin", "Europe/London", "UTC")

Button = tuple[str, str]
Keyboard = list[list[Button]]


def literal(text: str) -> str:
    """Mark a button label as already-rendered text rather than an i18n key."""
    return f"\x00{text}"


def is_literal(label: str) -> bool:
    return label.startswith("\x00")


def unwrap(label: str) -> str:
    return label[1:] if is_literal(label) else label


def check_callback(data: str) -> str:
    """Telegram rejects callback data over 64 bytes; fail loudly in tests rather than at runtime."""
    if len(data.encode()) > MAX_CALLBACK_BYTES:
        msg = f"callback data too long: {data!r}"
        raise ValueError(msg)
    return data


@dataclass(frozen=True, slots=True)
class Page:
    items: Sequence[Any]
    index: int
    total: int

    @property
    def has_prev(self) -> bool:
        return self.index > 0

    @property
    def has_next(self) -> bool:
        return self.index + 1 < self.total


def paginate(items: Sequence[Any], index: int, per_page: int) -> Page:
    total = max(1, -(-len(items) // per_page))
    index = max(0, min(index, total - 1))
    start = index * per_page
    return Page(items[start : start + per_page], index, total)


def nav_row(prefix: str, page: Page, *, suffix: str = "") -> list[Button]:
    row: list[Button] = []
    if page.has_prev:
        row.append(("bot.btn.prev", check_callback(f"{prefix}:{page.index - 1}{suffix}")))
    if page.has_next:
        row.append(("bot.btn.next", check_callback(f"{prefix}:{page.index + 1}{suffix}")))
    return row


def main_menu(*, is_owner: bool) -> Keyboard:
    rows: Keyboard = [
        [("bot.menu.chats", "chats:0"), ("bot.menu.rules", "rules:pick")],
        [("bot.menu.ignored", "ign:0"), ("bot.menu.settings", "set:main")],
        [("bot.menu.status", "status"), ("bot.menu.help", "help")],
    ]
    if is_owner:
        rows.append([("bot.menu.shared", "shared:0"), ("bot.menu.users", "users")])
    return rows


def language_menu() -> Keyboard:
    return [[(literal(LANG_NAMES[lang]), f"lang:{lang}")] for lang in LANGS]


def timezone_menu() -> Keyboard:
    rows = [[(literal(tz), check_callback(f"tz:{tz}"))] for tz in QUICK_TIMEZONES]
    rows.append([("bot.btn.cancel", "menu")])
    return rows


def chat_label(chat: ChatRow, *, subscribed: bool) -> str:
    mark = "✅" if subscribed else "☐"
    warn = "" if chat.available else " ⚠️"
    return literal(f"{mark} {chat.ref.title}{warn}")


def chats_menu(chats: Sequence[ChatRow], subscribed: set[int], page_index: int) -> Keyboard:
    # Watched chats come first: with a few hundred cached dialogs, the alphabetical position of
    # a chat you subscribed to is no help at all when you want to drop it again.
    ordered = sorted(chats, key=lambda chat: chat.id not in subscribed)
    page = paginate(ordered, page_index, CHATS_PER_PAGE)
    rows: Keyboard = []
    for chat in page.items:
        target = 0 if chat.id in subscribed else 1
        rows.append(
            [
                (
                    chat_label(chat, subscribed=chat.id in subscribed),
                    check_callback(f"sub:{chat.id}:{target}:{page.index}"),
                )
            ]
        )
    nav = nav_row("chats", page)
    if nav:
        rows.append(nav)
    rows.append([("bot.btn.search", "chats:search"), ("bot.btn.back", "menu")])
    return rows


def shared_menu(chats: Sequence[ChatRow], page_index: int) -> Keyboard:
    page = paginate(chats, page_index, CHATS_PER_PAGE)
    rows: Keyboard = []
    for chat in page.items:
        mark = "🗂" if chat.shared else "🔒"
        target = 0 if chat.shared else 1
        rows.append([(literal(f"{mark} {chat.ref.title}"), f"share:{chat.id}:{target}")])
    nav = nav_row("shared", page)
    if nav:
        rows.append(nav)
    rows.append([("bot.btn.back", "menu")])
    return rows


def rules_chat_menu(chats: Sequence[ChatRow], page_index: int) -> Keyboard:
    page = paginate(chats, page_index, CHATS_PER_PAGE)
    rows: Keyboard = [[("bot.btn.all_chats", f"rules:{ALL_CHATS}:include:0")]]
    for chat in page.items:
        rows.append([(literal(chat.ref.title), f"rules:{chat.id}:include:0")])
    nav = nav_row("rpick", page)
    if nav:
        rows.append(nav)
    rows.append([("bot.btn.back", "menu")])
    return rows


def rule_preview(rule: Rule, icon: str) -> str:
    pattern = rule.pattern
    if len(pattern) > PATTERN_PREVIEW:
        pattern = pattern[: PATTERN_PREVIEW - 1] + "…"
    disabled = "⏸ " if rule.disabled_reason else ""
    return literal(f"❌ {disabled}{icon} {pattern}")


def rules_menu(
    rules: Sequence[Rule],
    icons: Sequence[str],
    chat_id: int,
    kind: str,
    page_index: int,
) -> Keyboard:
    page = paginate(list(zip(rules, icons, strict=True)), page_index, RULES_PER_PAGE)
    rows: Keyboard = [
        [(rule_preview(rule, icon), check_callback(f"rdel:{rule.id}"))] for rule, icon in page.items
    ]
    nav = nav_row("rules", page, suffix=f":{chat_id}:{kind}")
    if nav:
        # the paging callback carries the chat and kind so the handler stays stateless
        nav = [
            (label, check_callback(f"rules:{chat_id}:{kind}:{data.split(':')[1]}"))
            for label, data in nav
        ]
        rows.append(nav)
    other = "exclude" if kind == "include" else "include"
    other_label = "bot.btn.exclude" if kind == "include" else "bot.btn.include"
    rows.append(
        [
            ("bot.btn.add", check_callback(f"radd:{chat_id}:{kind}")),
            (other_label, check_callback(f"rules:{chat_id}:{other}:0")),
        ]
    )
    rows.append([("bot.btn.back", "rules:pick")])
    return rows


def ignored_menu(entries: Sequence[IgnoredSender], page_index: int) -> Keyboard:
    page = paginate(entries, page_index, RULES_PER_PAGE)
    rows: Keyboard = []
    for entry in page.items:
        scope = "🌐" if entry.chat_id == ALL_CHATS else "📡"
        label = literal(f"❌ {scope} {entry.note or entry.sender_id}")
        rows.append([(label, check_callback(f"igdel:{entry.chat_id}:{entry.sender_id}"))])
    nav = nav_row("ign", page)
    if nav:
        rows.append(nav)
    rows.append([("bot.btn.back", "menu")])
    return rows


def settings_menu(
    *, include_bots: bool = False, weekly_report: bool = True, auto_backup: bool = True
) -> Keyboard:
    bots = "bot.settings.bots_on" if include_bots else "bot.settings.bots_off"
    weekly = "bot.settings.weekly_on" if weekly_report else "bot.settings.weekly_off"
    backup = "bot.settings.backup_on" if auto_backup else "bot.settings.backup_off"
    return [
        [("bot.settings.language", "set:lang"), ("bot.settings.timezone", "set:tz")],
        [("bot.settings.digest", "set:digest")],
        [(bots, "set:bots")],
        [(weekly, "set:weekly"), (backup, "set:backup")],
        [("bot.btn.back", "menu")],
    ]


def notification_buttons(
    chat_id: int, msg_id: int, sender_id: int | None, rule_id: int | None = None
) -> Keyboard:
    """Buttons under a notification. No nonce: the handler reads the user's own outbox row."""
    del msg_id  # kept in the signature: the callbacks are built from the row, not the message
    row: list[Button] = []
    if sender_id is not None:
        row.append(("notif.btn.ignore", check_callback(f"ig:{chat_id}:{sender_id}")))
    if rule_id is not None:
        row.append(("notif.btn.drop_rule", check_callback(f"rdrop:{rule_id}")))
    return [row] if row else []


def users_menu(
    pending: Sequence[int], allowed: Sequence[int], denied: Sequence[int] = ()
) -> Keyboard:
    rows: Keyboard = [
        [
            ("bot.btn.allow", check_callback(f"allow:{user_id}")),
            ("bot.btn.deny", check_callback(f"deny:{user_id}")),
        ]
        for user_id in pending
    ]
    rows.extend([("bot.btn.revoke", check_callback(f"revoke:{user_id}"))] for user_id in allowed)
    # Deny must not be a one-way door: the owner can change their mind.
    rows.extend(
        [(literal(f"✅ {user_id}"), check_callback(f"allow:{user_id}"))] for user_id in denied
    )
    rows.append([("bot.btn.back", "menu")])
    return rows
