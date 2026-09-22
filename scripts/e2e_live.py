"""End-to-end check against real Telegram.

It sets up a unique keyword, asks you to post a message containing it from your *other*
account, and then verifies the notification the bot delivers — reading it back through the
listener session, because the owner and the listener are the same account.

    uv run python scripts/e2e_live.py --chat "part of the chat title"

The marker has to come from another account on purpose: Telegram never sends a session its own
outgoing messages as updates, so a message posted from this very session could only ever be
found by catch-up, which would not test the live path.

The temporary rule is removed afterwards unless --keep is given.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import secrets
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from telethon.tl import functions
from telethon.tl import types as tl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ddiver.clients import make_user_client
from ddiver.config import Settings
from ddiver.links import ChatRef
from ddiver.rules import RuleKind
from ddiver.storage import ALL_CHATS, Storage

DELIVERY_TIMEOUT = 90.0
POLL_INTERVAL = 2.0


@dataclass
class Report:
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append((name, ok, detail))
        mark = "✓" if ok else "✗"
        print(f"  {mark} {name}{f' — {detail}' if detail else ''}")

    @property
    def failed(self) -> int:
        return sum(1 for _, ok, _ in self.checks if not ok)


async def find_chat(client: Any, needle: str) -> Any:
    """Locate a group/channel dialog by a fragment of its title or its @username."""
    wanted = needle.strip().lstrip("@").casefold()
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        if isinstance(entity, tl.User):
            continue
        title = (getattr(entity, "title", "") or "").casefold()
        username = (getattr(entity, "username", "") or "").casefold()
        if wanted in title or (username and wanted == username):
            return entity
    return None


async def wait_for_notification(client: Any, bot_id: int, marker: str) -> Any:
    """Poll the DM with the bot until the notification for ``marker`` shows up."""
    deadline = asyncio.get_running_loop().time() + DELIVERY_TIMEOUT
    while asyncio.get_running_loop().time() < deadline:
        async for message in client.iter_messages(bot_id, limit=15):
            if message.message and marker in message.message:
                return message
        await asyncio.sleep(POLL_INTERVAL)
    return None


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chat", required=True, help="part of the test chat's title, or @username")
    parser.add_argument("--keep", action="store_true", help="keep the marker messages and rules")
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args()

    settings = Settings.load(args.env_file)
    report = Report()
    marker = f"ddiver-e2e-{secrets.token_hex(3)}"
    keyword = marker  # unique, so nothing else in the chat can match it

    client = make_user_client(settings)
    storage = Storage(settings.db_path, owner_id=settings.owner_id)
    await client.connect()
    await storage.open()
    created_rule = None
    try:
        if not await client.is_user_authorized():
            print("The user session is not authorized. Run `ddiver login` first.")
            return 2
        me = await client.get_me()
        print(f"Account: {me.first_name} (id {me.id})")

        entity = await find_chat(client, args.chat)
        if entity is None:
            print(f"No group or channel matching {args.chat!r} in this account's dialogs.")
            return 2
        ref = ChatRef.from_entity(entity)
        print(f"Chat: {ref.title} ({ref.id}, {ref.kind})\nMarker: {marker}\n")

        # Arrange: make sure ddiver watches this chat for our unique keyword.
        await storage.upsert_chat(ref, access_hash=getattr(entity, "access_hash", None))
        await storage.ensure_user(settings.owner_id)
        await storage.set_chat_shared(ref.id, True)
        await storage.subscribe(settings.owner_id, ref.id)
        created_rule = await storage.add_rule(
            settings.owner_id, ALL_CHATS, RuleKind.INCLUDE, keyword
        )
        print("Subscription and rule are in place.\n")
        print("Now, in this order:")
        print("  1. restart ddiver if it is already running, so it sees the new rule")
        print("  2. from your OTHER account, post this message in the chat:\n")
        print(f"       {marker} тестовое сообщение\n")
        print("Telegram never delivers a session its own messages as updates, which is why the")
        print("marker has to come from the second account.\n")
        await asyncio.to_thread(input, "Press Enter once you have posted it... ")

        print("\nChecks:")
        notification = await wait_for_notification(client, settings.bot_id, marker)
        report.add("notification delivered", notification is not None)
        if notification is None:
            print("\nThe bot did not deliver anything. Check `ddiver` logs and /status.")
            return 1

        text = notification.message
        report.add("chat title present", ref.title in text, ref.title)
        report.add("message text present", "тестовое сообщение" in text)
        report.add("rule shown", keyword in text)

        # Find the message the notification is about, to check the link it built.
        origin = None
        async for candidate in client.iter_messages(entity, limit=20):
            if candidate.message and marker in candidate.message:
                origin = candidate
                break
        report.add("marker found in the chat", origin is not None)
        if origin is not None:
            expected_link = (
                f"https://t.me/{ref.username}/{origin.id}"
                if ref.username
                else f"https://t.me/c/{ref.bare_id}/{origin.id}"
            )
            report.add("message link correct", expected_link in text, expected_link)
            report.add(
                "sender identified",
                str(origin.sender_id) in text,
                f"sender {origin.sender_id}",
            )
            report.add("not attributed to me", str(me.id) not in text.split("🔑")[0])
        entities = notification.entities or []
        report.add("formatting entities present", bool(entities), f"{len(entities)} entities")
        bold = [e for e in entities if isinstance(e, tl.MessageEntityBold)]
        report.add("match highlighted", bool(bold))
        buttons = notification.reply_markup
        report.add("action buttons attached", buttons is not None)

        # The ignore button must work when pressed from the real client.
        if buttons is not None:
            data = None
            for row in buttons.rows:
                for button in row.buttons:
                    raw = getattr(button, "data", None)
                    if raw and raw.startswith(b"ig:"):
                        data = raw
            if data is not None:
                answer = await client(
                    functions.messages.GetBotCallbackAnswerRequest(
                        peer=settings.bot_id, msg_id=notification.id, data=data
                    )
                )
                report.add("ignore button answered", True, (answer.message or "")[:60])
                ignored = await storage.list_ignored(settings.owner_id)
                report.add("ignore stored", any(i.sender_id == me.id for i in ignored))
                for entry in ignored:
                    if entry.sender_id == me.id:
                        await storage.remove_ignored(
                            settings.owner_id, entry.chat_id, entry.sender_id
                        )
            else:
                report.add("ignore button present", False, "no ig: button found")

        print(f"\n{len(report.checks) - report.failed}/{len(report.checks)} checks passed")
        return 1 if report.failed else 0
    finally:
        if not args.keep:
            if created_rule is not None:
                with contextlib.suppress(Exception):
                    await storage.delete_rule(created_rule.id, settings.owner_id)
            print("Removed the temporary rule (the marker message is yours to delete).")
        await storage.close()
        await client.disconnect()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
