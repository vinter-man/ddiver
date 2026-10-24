"""End-to-end check against real Telegram.

It arranges a unique keyword, tells you what to post from your *other* account, and then
watches ddiver's own database for the delivery — asserting on the notification exactly as it
was rendered and sent.

    # terminal 1
    uv run ddiver run
    # terminal 2
    uv run python scripts/e2e_live.py --chat "part of the chat title"

Two details shape this script:

* the marker has to come from another account, because Telegram never delivers a session its
  own outgoing messages as updates — a message posted from the listener session itself could
  only ever be found by catch-up, which would not exercise the live path;
* it never opens the user session, because ddiver is holding it: two processes on one session
  file is exactly what makes Telegram invalidate it.

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

from telethon.tl import types as tl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ddiver.config import Settings
from ddiver_core.formatter import Payload, render_notification
from ddiver_core.links import message_link
from ddiver_core.richtext import utf16_len
from ddiver_core.rules import RuleKind
from ddiver_core.storage import ALL_CHATS, ChatRow, DeliveryStatus, Storage
from ddiver_core.timeutil import utcnow

DELIVERY_TIMEOUT = 120.0
POLL_INTERVAL = 2.0


@dataclass
class Report:
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append((name, ok, detail))
        print(f"  {'✓' if ok else '✗'} {name}{f' — {detail}' if detail else ''}")

    @property
    def failed(self) -> int:
        return sum(1 for _, ok, _ in self.checks if not ok)


async def find_chat(storage: Storage, needle: str) -> ChatRow | None:
    """Find the chat among the ones `ddiver login` cached, by title or @username."""
    wanted = needle.strip().lstrip("@").casefold()
    matches = [
        chat
        for chat in await storage.list_chats()
        if wanted in chat.ref.title.casefold()
        or (chat.ref.username and wanted == chat.ref.username.casefold())
    ]
    if len(matches) > 1:
        print("Several chats match; be more specific:")
        for chat in matches[:10]:
            print(f"  {chat.ref.title} ({chat.id})")
        return None
    return matches[0] if matches else None


async def wait_for_delivery(
    storage: Storage, chat_id: int, marker: str, owner_id: int
) -> Any | None:
    """Watch the outbox for a notification about our marker."""
    deadline = asyncio.get_running_loop().time() + DELIVERY_TIMEOUT
    while asyncio.get_running_loop().time() < deadline:
        cur = await storage.db.execute(
            "SELECT * FROM outbox WHERE user_id=? AND chat_id=? ORDER BY msg_id DESC LIMIT 20",
            (owner_id, chat_id),
        )
        for row in await cur.fetchall():
            item = storage._outbox(row)  # noqa: SLF001 - reads ddiver's own database
            if marker in str(item.payload.get("text", "")):
                if item.status is DeliveryStatus.PENDING:
                    continue  # still queued; wait for the send to finish
                return item
        await asyncio.sleep(POLL_INTERVAL)
    return None


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chat", required=True, help="part of the test chat's title, or @username")
    parser.add_argument("--keep", action="store_true", help="keep the temporary rule")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--marker", help="reuse the keyword from an earlier run")
    parser.add_argument(
        "--setup-only", action="store_true", help="only arrange the rule, then exit"
    )
    args = parser.parse_args()

    settings = Settings.load(args.env_file)
    report = Report()
    marker = args.marker or f"ddiver-e2e-{secrets.token_hex(3)}"

    storage = Storage(settings.db_path, owner_id=settings.owner_id)
    await storage.open()
    created_rule = None
    try:
        chat = await find_chat(storage, args.chat)
        if chat is None:
            print(f"No chat matching {args.chat!r}. Run `ddiver login` to refresh the chat list.")
            return 2
        print(f"Chat: {chat.ref.title} ({chat.id}, {chat.ref.kind})")

        await storage.ensure_user(settings.owner_id)
        await storage.set_chat_shared(chat.id, True)
        await storage.subscribe(settings.owner_id, chat.id)
        if args.marker is None:
            created_rule = await storage.add_rule(
                settings.owner_id, ALL_CHATS, RuleKind.INCLUDE, marker
            )
        print(f"Marker keyword: {marker}\n")

        if args.setup_only:
            created_rule = None  # keep it: the second run needs it
            print("Rule is in place. Start (or restart) ddiver, post the marker from your other")
            print(f"account, then run this again with --marker {marker}")
            return 0

        print("Now, from your OTHER account, post this in the chat:\n")
        print(f"    {marker} test message\n")
        print("ddiver must be running, and must have been started after this rule was added.")
        print(f"Waiting up to {DELIVERY_TIMEOUT:.0f}s for the notification…\n")

        item = await wait_for_delivery(storage, chat.id, marker, settings.owner_id)
        print("Checks:")
        report.add("notification delivered", item is not None)
        if item is None:
            print("\nNothing arrived. Check the ddiver log and /status in the bot.")
            return 1

        report.add(
            "delivery succeeded",
            item.status in (DeliveryStatus.SENT, DeliveryStatus.DIGESTED),
            item.status.value,
        )
        payload = Payload.from_json(item.payload)
        report.add("chat title captured", payload.chat_title == chat.ref.title, payload.chat_title)
        report.add("message text captured", marker in payload.text)
        report.add("rule recorded", payload.rule_pattern == marker)
        report.add("match highlighted", bool(payload.spans), f"spans={payload.spans}")
        report.add(
            "sender is the other account",
            payload.sender_id is not None and payload.sender_id != settings.owner_id,
            f"sender {payload.sender_id} ({payload.sender_name})",
        )
        report.add("not marked as catch-up", payload.catchup is False)

        expected = message_link(chat.ref, payload.msg_id, payload.topic_id)
        rendered = render_notification(payload, lang="ru", tz="Europe/Kyiv", now=utcnow())
        if chat.ref.is_basic_group:
            # Telegram has no message links in basic groups; the notification must not invent one
            report.add(
                "no fake link for a basic group", expected is None and "t.me/" not in rendered.text
            )
        else:
            report.add(
                "message link built", bool(expected) and expected in rendered.text, str(expected)
            )
        report.add(
            "formatting entities produced", bool(rendered.entities), f"{len(rendered.entities)}"
        )
        report.add(
            "bold highlight present",
            any(isinstance(e, tl.MessageEntityBold) for e in rendered.entities),
        )
        report.add("fits Telegram's limit", utf16_len(rendered.text) <= 4096)
        if chat.ref.is_basic_group:
            print("\nNote: this is a basic group, so there are no message links. Run the test in")
            print("a supergroup too if you want the link checks covered.")

        print("\n--- what the bot sent you ---")
        print(rendered.text)
        print("--- end ---")
        print("\nCompare that with the message in your bot chat: the links should open the")
        print("message and the sender, and the keyword should be bold.")
        print(f"\n{len(report.checks) - report.failed}/{len(report.checks)} checks passed")
        return 1 if report.failed else 0
    finally:
        if created_rule is not None and not args.keep:
            with contextlib.suppress(Exception):
                await storage.delete_rule(created_rule.id, settings.owner_id)
            print("Removed the temporary rule.")
        await storage.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
