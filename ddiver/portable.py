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

import datetime as dt
import json
from dataclasses import dataclass, field
from typing import Any

from ddiver.rules import RuleError, RuleKind, compile_rule
from ddiver.storage import ALL_CHATS, DuplicateRuleError, Storage

FORMAT = "ddiver.config"
VERSION = 1
MAX_BYTES = 1_000_000
"""An export of 500 rules is about 40 KB; a megabyte is a file that is not one of ours."""


class BadExportError(Exception):
    """The document is not a ddiver export, or is one we cannot read."""


@dataclass(slots=True)
class ImportReport:
    chats_subscribed: int = 0
    chats_missing: list[str] = field(default_factory=list)
    rules_added: int = 0
    rules_duplicate: int = 0
    rules_invalid: list[str] = field(default_factory=list)
    ignored_added: int = 0

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
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = f"not valid JSON: {exc.msg}"
        raise BadExportError(msg) from exc
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        msg = "not a ddiver export"
        raise BadExportError(msg)
    if int(data.get("version", 0)) > VERSION:
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
    resolved = await _import_chats(storage, user_id, data, report)
    await _import_rules(
        storage, user_id, data, report, resolved, regex_timeout=regex_timeout, is_owner=is_owner
    )
    await _import_ignored(storage, user_id, data, report, resolved)
    return report


async def _import_chats(
    storage: Storage, user_id: int, data: dict[str, Any], report: ImportReport
) -> dict[int, int]:
    """Returns the exported chat id → local chat id map the other two passes need."""
    resolved: dict[int, int] = {}
    for entry in data.get("chats", []) or []:
        wanted_id = _int(entry.get("id"))
        username = (entry.get("username") or "").lstrip("@") or None
        chat = await _find_chat(storage, wanted_id, username)
        if chat is None:
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
    for entry in data.get("rules", []) or []:
        pattern = str(entry.get("pattern") or "")
        chat_id = _int(entry.get("chat_id"))
        if chat_id != ALL_CHATS:
            chat_id = resolved.get(chat_id, chat_id)
            reason = await _chat_problem(storage, user_id, chat_id, is_owner=is_owner)
            if reason is not None:
                report.rules_invalid.append(f"{pattern}: {reason}")
                continue
        try:
            compiled = compile_rule(pattern, timeout=regex_timeout)
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


async def _import_ignored(
    storage: Storage,
    user_id: int,
    data: dict[str, Any],
    report: ImportReport,
    resolved: dict[int, int],
) -> None:
    for entry in data.get("ignored", []) or []:
        sender_id = _int(entry.get("sender_id"))
        if not sender_id:
            continue
        chat_id = _int(entry.get("chat_id"))
        if chat_id != ALL_CHATS:
            chat_id = resolved.get(chat_id, chat_id)
            if await storage.get_chat(chat_id) is None:
                continue
        await storage.add_ignored(user_id, chat_id, sender_id, entry.get("note"))
        report.ignored_added += 1


async def _chat_problem(
    storage: Storage, user_id: int, chat_id: int, *, is_owner: bool
) -> str | None:
    """Why this user may not write a rule for this chat, or None when they may."""
    if await storage.get_chat(chat_id) is None:
        return "chat not found"
    if not is_owner and not await storage.is_subscribed(user_id, chat_id):
        return "chat not available to you"
    return None


async def _find_chat(storage: Storage, chat_id: int, username: str | None) -> Any:
    """Username first: it is what survives the config moving to another account."""
    if username:
        wanted = username.casefold()
        for chat in await storage.list_chats():
            if chat.ref.username and chat.ref.username.casefold() == wanted:
                return chat
    return await storage.get_chat(chat_id) if chat_id else None


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
