"""Export and import a moderator's configuration as JSON.

What is worth carrying between installs is small: which chats you watch, the rules you wrote
and who you ignore. Everything else — the outbox, the cursors, the cached dialog list, the
session — is either recoverable on its own or must not leave the machine at all. A backup of
``user.session`` is a backup of the Telegram *account*, so nothing here ever touches it.

Chats are matched on import by ``@username`` first and by id second. Ids are stable, but a
config moved to another account is useless unless a public chat can be found by name, and the
username is the only thing that survives a different listener.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from dataclasses import dataclass, field
from typing import Any

from ddiver.rules import (
    MAX_IGNORED_PER_USER,
    MAX_RULES_PER_CHAT,
    MAX_RULES_PER_USER,
    RuleError,
    RuleKind,
    compile_rule,
)
from ddiver.storage import (
    ALL_CHATS,
    SQLITE_INT_MAX,
    SQLITE_INT_MIN,
    DuplicateRuleError,
    Storage,
)

FORMAT = "ddiver.config"
VERSION = 1
MAX_BYTES = 1_000_000
"""An export of 500 rules is about 40 KB; a megabyte is a file that is not one of ours."""

MAX_RULE_ENTRIES = MAX_RULES_PER_USER + MAX_RULES_PER_CHAT
"""How many rule entries an import will even look at.

The caps in :mod:`ddiver.rules` bound what gets *stored*; this bounds the *work*. A rejected
pattern never counts towards the stored caps, so those caps alone never end the loop, and a
megabyte of JSON holds around 26,000 patterns. Validating one costs well under a millisecond
in the cases measured — but the ceiling is ``regex_timeout`` per pattern, and choosing a
pattern that reaches it is the attacker's side of the bargain. An export of a completely full
account is 500 entries, so this bound is never met by a real file.
"""


class BadExportError(Exception):
    """The document is not a ddiver export, or is one we cannot read."""


@dataclass(slots=True)
class ImportReport:
    chats_subscribed: int = 0
    chats_missing: list[str] = field(default_factory=list)
    rules_added: int = 0
    rules_duplicate: int = 0
    rules_invalid: list[str] = field(default_factory=list)
    rules_over_limit: int = 0
    ignored_added: int = 0
    ignored_over_limit: int = 0

    @property
    def anything_happened(self) -> bool:
        return bool(self.chats_subscribed or self.rules_added or self.ignored_added)


async def export_config(storage: Storage, user_id: int, *, now: dt.datetime) -> dict[str, Any]:
    """Everything this user would have to retype after a reinstall, and nothing else."""
    chats = await storage.list_subscriptions(user_id)
    by_id = {chat.id: chat for chat in chats}
    for rule in await storage.list_rules(user_id):
        if rule.chat_id != ALL_CHATS and rule.chat_id not in by_id:
            found = await storage.get_chat(rule.chat_id)
            if found is not None:
                by_id[found.id] = found

    return {
        "format": FORMAT,
        "version": VERSION,
        "exported_at": now.isoformat(),
        "chats": [
            {
                "id": chat.id,
                "username": chat.ref.username,
                "title": chat.ref.title,
                "subscribed": chat.id in {c.id for c in chats},
            }
            for chat in by_id.values()
        ],
        "rules": [
            {
                "chat_id": rule.chat_id,
                "kind": rule.kind.value,
                "pattern": rule.pattern,
            }
            for rule in await storage.list_rules(user_id)
        ],
        "ignored": [
            {"chat_id": entry.chat_id, "sender_id": entry.sender_id, "note": entry.note}
            for entry in await storage.list_ignored(user_id)
        ],
    }


def dumps(config: dict[str, Any]) -> str:
    return json.dumps(config, ensure_ascii=False, indent=2)


def loads(raw: bytes | str) -> dict[str, Any]:
    """Parse and sanity-check an export, with messages a person can act on."""
    if isinstance(raw, bytes):
        if len(raw) > MAX_BYTES:
            msg = "file is too large to be a ddiver export"
            raise BadExportError(msg)
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            msg = "file is not UTF-8 text"
            raise BadExportError(msg) from exc
    if len(raw) > MAX_BYTES:
        msg = "file is too large to be a ddiver export"
        raise BadExportError(msg)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = f"not valid JSON: {exc.msg}"
        raise BadExportError(msg) from exc
    except RecursionError as exc:
        # CPython's scanner recurses per nesting level, and a megabyte of "[" is well within
        # the size limit. RecursionError is not a JSONDecodeError, so it escaped this
        # function entirely and reached the user as a traceback.
        msg = "nested too deeply to read"
        raise BadExportError(msg) from exc
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        msg = "not a ddiver export"
        raise BadExportError(msg)
    if _int(data.get("version")) > VERSION:
        msg = "made by a newer ddiver; upgrade before importing"
        raise BadExportError(msg)
    return data


async def import_config(
    storage: Storage,
    user_id: int,
    data: dict[str, Any],
    *,
    regex_timeout: float,
    is_owner: bool,
) -> ImportReport:
    """Apply an export additively: nothing already configured is removed.

    Additive on purpose. An import that replaced the configuration would turn one mistaken tap
    into the loss of every rule, and merging is what somebody restoring a backup wants anyway.
    Authorisation is unchanged: subscribing still goes through ``Storage.subscribe``, so a
    moderator cannot reach a chat the owner has not shared, whatever the file claims.
    """
    report = ImportReport()
    resolved = await _import_chats(storage, user_id, data, report, is_owner=is_owner)
    await _import_rules(
        storage, user_id, data, report, resolved, regex_timeout=regex_timeout, is_owner=is_owner
    )
    await _import_ignored(storage, user_id, data, report, resolved, is_owner=is_owner)
    return report


async def _import_chats(
    storage: Storage,
    user_id: int,
    data: dict[str, Any],
    report: ImportReport,
    *,
    is_owner: bool,
) -> dict[int, int]:
    """Returns the exported chat id → local chat id map the other two passes need."""
    resolved: dict[int, int] = {}
    for entry in data.get("chats") or []:
        if not isinstance(entry, dict):
            continue
        wanted_id = _int(entry.get("id"))
        username = _text(entry.get("username")).lstrip("@") or None
        chat = await _find_chat(storage, user_id, wanted_id, username, is_owner=is_owner)
        if chat is None:
            # Echo back what they sent, never what we know: naming the chat we found instead
            # would turn this list into the same oracle the single error message closed.
            report.chats_missing.append(str(entry.get("title") or username or wanted_id))
            continue
        resolved[wanted_id] = chat.id
        if entry.get("subscribed") and await storage.subscribe(user_id, chat.id):
            report.chats_subscribed += 1
    return resolved


async def _import_rules(
    storage: Storage,
    user_id: int,
    data: dict[str, Any],
    report: ImportReport,
    resolved: dict[int, int],
    *,
    regex_timeout: float,
    is_owner: bool,
) -> None:
    """Add the rules a file asks for, under the same caps the rules screen applies.

    Two reasons the caps matter more here than there. A file can carry thousands of patterns
    in one message, and every accepted pattern is compiled again on the hot path for every
    message in that chat — so an import is the cheapest way to make matching slow for
    everybody in a shared chat, not just for the person who did it.
    """
    per_chat: dict[int, int] = {}
    total = await storage.count_rules(user_id)
    examined = 0
    for entry in data.get("rules") or []:
        examined += 1
        if examined > MAX_RULE_ENTRIES:
            # Counted, not compiled: past here the file cannot be one of ours, and every
            # further pattern would be paid for at validation time whether or not it is kept.
            report.rules_over_limit += 1
            continue
        if not isinstance(entry, dict):
            continue
        pattern = _text(entry.get("pattern"))
        chat_id = _int(entry.get("chat_id"))
        if chat_id != ALL_CHATS:
            chat_id = resolved.get(chat_id, chat_id)
            reason = await _chat_problem(storage, user_id, chat_id, is_owner=is_owner)
            if reason is not None:
                report.rules_invalid.append(f"{pattern}: {reason}")
                continue
        if total >= MAX_RULES_PER_USER:
            report.rules_over_limit += 1
            continue
        if chat_id not in per_chat:
            per_chat[chat_id] = await storage.count_rules(user_id, chat_id)
        if per_chat[chat_id] >= MAX_RULES_PER_CHAT:
            report.rules_over_limit += 1
            continue
        try:
            # Compiling is the expensive half and it is pure: a file full of slow patterns
            # must not hold the event loop while every other moderator waits.
            compiled = await asyncio.to_thread(compile_rule, pattern, timeout=regex_timeout)
        except RuleError:
            report.rules_invalid.append(pattern)
            continue
        kind = RuleKind.EXCLUDE if entry.get("kind") == "exclude" else RuleKind.INCLUDE
        try:
            await storage.add_rule(user_id, chat_id, kind, compiled.pattern)
        except DuplicateRuleError:
            report.rules_duplicate += 1
        else:
            report.rules_added += 1
            per_chat[chat_id] += 1
            total += 1


async def _import_ignored(
    storage: Storage,
    user_id: int,
    data: dict[str, Any],
    report: ImportReport,
    resolved: dict[int, int],
    *,
    is_owner: bool,
) -> None:
    held = await storage.count_ignored(user_id)
    for entry in data.get("ignored") or []:
        if not isinstance(entry, dict):
            continue
        if held >= MAX_IGNORED_PER_USER:
            report.ignored_over_limit += 1
            continue
        sender_id = _int(entry.get("sender_id"))
        if not sender_id:
            continue
        chat_id = _int(entry.get("chat_id"))
        if chat_id != ALL_CHATS:
            chat_id = resolved.get(chat_id, chat_id)
            if await _visible_chat(storage, user_id, chat_id, is_owner=is_owner) is None:
                continue
        note = entry.get("note")
        await storage.add_ignored(
            user_id, chat_id, sender_id, str(note) if note is not None else None
        )
        report.ignored_added += 1
        held += 1


async def _chat_problem(
    storage: Storage, user_id: int, chat_id: int, *, is_owner: bool
) -> str | None:
    """Why this user may not write a rule for this chat, or None when they may.

    One answer for both "no such chat" and "not yours", deliberately. Two different answers
    let a moderator upload a file full of guesses and read the owner's chat list back out of
    the report, one guess at a time.
    """
    if await _visible_chat(storage, user_id, chat_id, is_owner=is_owner) is None:
        return "chat not found or not available to you"
    return None


async def _visible_chat(storage: Storage, user_id: int, chat_id: int, *, is_owner: bool) -> Any:
    """A chat this user is allowed to know exists: shared, subscribed, or theirs as owner.

    The same test the bot UI applies, because an import must not be a way around the UI. The
    owner sees everything the listener knows; a moderator sees a chat only once it has been
    shared with them, or while they are still subscribed to one that has since been unshared.
    """
    if not chat_id:
        return None
    chat = await storage.get_chat(chat_id)
    if chat is None:
        return None
    if is_owner:
        return chat
    if await storage.is_subscribed(user_id, chat_id):
        return chat
    return chat if chat.shared else None


async def _find_chat(
    storage: Storage, user_id: int, chat_id: int, username: str | None, *, is_owner: bool
) -> Any:
    """Username first: it is what survives the config moving to another account.

    Both lookups are filtered to what this user may see. An unfiltered scan here would answer
    "does the owner have a chat called @x" for any @x a moderator cared to try.
    """
    if username:
        wanted = username.casefold()
        for chat in await storage.list_chats(shared_only=not is_owner):
            if chat.ref.username and chat.ref.username.casefold() == wanted:
                return chat
        # A moderator may still hold a subscription to a chat that has since been unshared.
        if not is_owner:
            for chat in await storage.list_subscriptions(user_id):
                if chat.ref.username and chat.ref.username.casefold() == wanted:
                    return chat
        return None
    return await _visible_chat(storage, user_id, chat_id, is_owner=is_owner)


def _text(value: Any) -> str:
    """Everything in the file is attacker-controlled JSON; a username may be a number."""
    return value if isinstance(value, str) else ""


def _int(value: Any) -> int:
    """Whatever the file said, as an integer SQLite can hold, or 0.

    ``int()`` raises OverflowError on a non-finite float, and json produces those from
    ``1e999``, ``Infinity`` and ``NaN`` — all of which a hostile export can contain.
    """
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return parsed if SQLITE_INT_MIN <= parsed <= SQLITE_INT_MAX else 0
