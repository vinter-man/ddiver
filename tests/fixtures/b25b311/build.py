"""Build the b25b311 fixtures with b25b311's own code: a database, two exports, the renders.

This script never imports the working tree. It is run against a copy of commit b25b311 (the
ddiver that is deployed when the core extraction lands), so what it writes is what that build
writes, not a reconstruction of it:

    mkdir /tmp/b25b311 && git archive b25b311 | tar -x -C /tmp/b25b311
    uv run python tests/fixtures/b25b311/build.py /tmp/b25b311 tests/fixtures/b25b311

It writes

* ``ddiver.sqlite3``: schema v3 (``stats_daily`` included), built by b25b311's ``Storage``
  through its public methods: users in every role, a paused one, chats of every kind, a
  migrated basic group, shared and unavailable chats, rules of every mode (one disabled, with
  hits), ignores, cursors, boots, per-day stats, and outbox rows in every status.
* ``export-owner.json`` and ``export-moderator.json``: what ``/export`` handed out, byte for
  byte (b25b311's ``portable.export_config`` and ``portable.dumps``).
* ``rendered.json``: every outbox row rendered by b25b311's formatter from the row as it reads
  it back, in three languages and time zones, with the buttons it would carry, and each
  user's pending rows as a digest.

tests/test_upgrade.py opens these with the new code. When b25b311 is in the local history it
also runs this script again and checks the committed files are still exactly its output.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import importlib
import json
import sys
from pathlib import Path
from typing import Any

T0 = dt.datetime(2026, 9, 20, 9, 0, tzinfo=dt.UTC)
RENDER_AT = T0 + dt.timedelta(hours=2, minutes=7)
EXPORTED_AT = T0 + dt.timedelta(hours=3)
VIEWS = (("en", "UTC"), ("ru", "Europe/Kyiv"), ("uk", "America/New_York"))
"""Every row is rendered in each of these; the users below read in one each."""
OVERFLOWS = (0, 12)

OWNER = 111111111
MOD = 222222222
NEWCOMER = 333333333
DENIED = 444444444
BLOCKED = 555555555


class Clock:
    """b25b311's Storage takes a clock; this one moves only when the script says so."""

    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> dt.datetime:
        return self.now

    def tick(self, minutes: float) -> None:
        self.now += dt.timedelta(minutes=minutes)


class Legacy:
    """b25b311's modules, imported from the tree given on the command line and nowhere else."""

    def __init__(self, tree: Path) -> None:
        sys.path.insert(0, str(tree))
        self.storage = importlib.import_module("ddiver.storage")
        self.formatter = importlib.import_module("ddiver.formatter")
        self.portable = importlib.import_module("ddiver.portable")
        self.links = importlib.import_module("ddiver.links")
        self.rules = importlib.import_module("ddiver.rules")
        self.menus = importlib.import_module("ddiver.bot.menus")
        modules = (self.storage, self.formatter, self.portable, self.links, self.rules, self.menus)
        for module in modules:
            if not Path(str(module.__file__)).resolve().is_relative_to(tree.resolve()):
                msg = f"{module.__name__} came from {module.__file__}, not from {tree}"
                raise SystemExit(msg)


def entities(rendered: Any) -> list[dict[str, Any]]:
    return [entity.to_dict() for entity in rendered.entities]


async def build(old: Legacy, out: Path) -> None:
    clock = Clock()
    db_path = out / "ddiver.sqlite3"
    storage: Any = old.storage.Storage(db_path, owner_id=OWNER, clock=clock, digest_threshold=20)
    await storage.open()
    try:
        await populate(old, storage, clock)
        await write_exports(old, storage, out)
        await write_renders(old, storage, out)
    finally:
        await storage.close()


async def populate(old: Legacy, storage: Any, clock: Clock) -> None:
    chat_ref = old.links.ChatRef
    role = old.storage.Role
    include, exclude = old.rules.RuleKind.INCLUDE, old.rules.RuleKind.EXCLUDE
    all_chats = old.storage.ALL_CHATS

    await storage.record_boot()
    clock.tick(1)
    await storage.record_boot()

    await storage.ensure_user(OWNER)
    await storage.update_user(
        OWNER, lang="ru", tz="Europe/Kyiv", digest_threshold=3, include_bots=True
    )
    await storage.update_user(OWNER, weekly_sent_at=T0 - dt.timedelta(days=2))
    await storage.ensure_user(MOD, lang="uk")
    await storage.update_user(
        MOD,
        role=role.ALLOWED,
        tz="America/New_York",
        digest_threshold=0,
        weekly_report=False,
        auto_backup=False,
    )
    await storage.ensure_user(NEWCOMER, lang="en")
    await storage.update_user(NEWCOMER, notified_owner_at=clock())
    await storage.ensure_user(DENIED)
    await storage.update_user(DENIED, role=role.DENIED)
    await storage.ensure_user(BLOCKED, lang="en")
    await storage.update_user(BLOCKED, role=role.ALLOWED, tz="UTC")
    clock.tick(1)

    public = chat_ref(-1001111111111, "Оренда Варна", "varna_rent", "supergroup")
    private = chat_ref(-1002222222222, "Закрытый <чат> & co", None, "supergroup")
    forum = chat_ref(-1003333333333, "Robota Sofia 🇧🇬", "robota_sofia", "supergroup", True)
    basic = chat_ref(-4444444444, "Basic group", None, "group")
    channel = chat_ref(-1005555555555, "Новости", "novosti_bg", "channel")
    old_basic = chat_ref(-5555555555, "Old basic group", None, "group")
    migrated = chat_ref(-1006666666666, "Old basic group", None, "supergroup")
    for index, chat in enumerate((public, private, forum, basic, channel, old_basic), start=1):
        await storage.upsert_chat(chat, access_hash=None if chat.kind == "group" else index * 1001)
    await storage.set_chat_shared(public.id, True)
    await storage.set_chat_shared(forum.id, True)
    await storage.set_chat_shared(old_basic.id, True)
    clock.tick(1)

    for chat in (public, private, forum, basic, channel, old_basic):
        await storage.subscribe(OWNER, chat.id)
    for chat in (public, forum, old_basic):
        await storage.subscribe(MOD, chat.id)
    await storage.subscribe(BLOCKED, public.id)
    clock.tick(1)

    flat = await storage.add_rule(OWNER, all_chats, include, "квартира")
    urgent = await storage.add_rule(OWNER, private.id, include, r"re:(?i)срочно\s+продам")
    vasya = await storage.add_rule(OWNER, forum.id, include, "from:@vasya")
    await storage.add_rule(OWNER, private.id, include, "from:123456789")
    await storage.add_rule(OWNER, public.id, exclude, "реклама")
    taxi = await storage.add_rule(OWNER, all_chats, include, "=такси")
    await storage.add_rule(OWNER, old_basic.id, include, "#promo")
    news = await storage.add_rule(OWNER, channel.id, include, "курс")
    casino = await storage.add_rule(MOD, public.id, include, "казино")
    ivan = await storage.add_rule(MOD, all_chats, include, "from:Иван Петров")
    await storage.add_rule(MOD, forum.id, exclude, "from:@spam_bot")
    blocked_rule = await storage.add_rule(BLOCKED, public.id, include, "квартира")
    await storage.disable_rule(taxi.id, "timeout")
    await storage.flush_rule_hits({flat.id: 3, casino.id: 1, urgent.id: 2})
    clock.tick(1)

    await storage.add_ignored(OWNER, public.id, 999000999, "спам")
    await storage.add_ignored(OWNER, all_chats, -1007777777777, None)
    await storage.add_ignored(MOD, forum.id, 888000888, "бот объявлений")
    await storage.add_ignored(OWNER, old_basic.id, 777000777, "moved with the chat")
    clock.tick(1)

    await storage.migrate_chat(old_basic.id, migrated, 6006)
    await storage.set_chat_available(channel.id, False)

    cursor = old.storage.Cursor
    await storage.upsert_cursors(
        [
            cursor(public.id, 112, T0 + dt.timedelta(minutes=50)),
            cursor(private.id, 202, T0 + dt.timedelta(minutes=41)),
            cursor(forum.id, 310, T0 + dt.timedelta(minutes=55)),
        ]
    )

    await storage.record_stats(
        {
            (OWNER, "2026-09-12", public.id, flat.id): 4,
            (OWNER, "2026-09-19", public.id, flat.id): 7,
            (OWNER, "2026-09-20", private.id, urgent.id): 2,
            (MOD, "2026-09-18", public.id, casino.id): 11,
            (MOD, "2026-09-19", forum.id, ivan.id): 1,
        }
    )

    await claim_outbox(
        old,
        storage,
        clock,
        chats={"public": public, "private": private, "forum": forum, "basic": basic},
        channel=channel,
        rules={
            "flat": flat,
            "urgent": urgent,
            "vasya": vasya,
            "news": news,
            "casino": casino,
            "ivan": ivan,
            "blocked": blocked_rule,
        },
    )


async def claim_outbox(
    old: Legacy,
    storage: Any,
    clock: Clock,
    *,
    chats: dict[str, Any],
    channel: Any,
    rules: dict[str, Any],
) -> None:
    """The outbox as the listener leaves it: claimed, then sent, digested or failed."""
    payload_type = old.formatter.Payload
    status = old.storage.DeliveryStatus
    public, private, forum, basic = (chats[k] for k in ("public", "private", "forum", "basic"))

    def payload(
        chat: Any, msg_id: int, rule: Any, text: str, fragment: str | None, **kw: Any
    ) -> Any:
        start = text.index(fragment) if fragment else -1
        base: dict[str, Any] = {
            "chat_id": chat.id,
            "chat_title": chat.title,
            "chat_username": chat.username,
            "chat_kind": chat.kind,
            "msg_id": msg_id,
            "topic_id": None,
            "date": T0 + dt.timedelta(minutes=msg_id % 60),
            "text": text,
            "spans": [(start, start + len(fragment))] if fragment else [],
            "rule_pattern": rule.pattern,
            "rule_kind": rule.kind.value,
            "rule_is_global": rule.chat_id == 0,
            "sender_id": 42,
            "sender_name": "Иван",
            "sender_username": "ivan_petrov",
        }
        return payload_type(**{**base, **kw})

    rows: list[tuple[int, Any, Any]] = [
        (
            OWNER,
            rules["flat"],
            payload(
                public, 101, rules["flat"], "Сдаётся квартира в центре, 2 комнаты 🏠", "квартира"
            ),
        ),
        (
            OWNER,
            rules["urgent"],
            payload(
                private,
                202,
                rules["urgent"],
                "СРОЧНО продам велосипед <почти новый>",
                "СРОЧНО продам",
                sender_id=43,
                sender_name="Ольга",
                sender_username=None,
                is_forwarded=True,
            ),
        ),
        (
            OWNER,
            rules["vasya"],
            payload(
                forum,
                303,
                rules["vasya"],
                "",
                None,
                topic_id=7,
                sender_id=44,
                sender_name="Вася",
                sender_username="vasya",
            ),
        ),
        (
            OWNER,
            rules["flat"],
            payload(
                basic,
                404,
                rules["flat"],
                "Длинное объявление. " * 40 + "Ищу квартиру на месяц.",
                "квартир",
                sender_id=45,
                sender_name="",
                sender_username=None,
                sender_is_deleted=True,
                catchup=True,
                catchup_capped=True,
            ),
        ),
        (
            OWNER,
            rules["flat"],
            payload(public, 108, rules["flat"], "квартира у моря", "квартира", sender_name=""),
        ),
        (
            MOD,
            rules["casino"],
            payload(public, 109, rules["casino"], "🎰🎰 Заходи в казино!", "казино"),
        ),
        (
            MOD,
            rules["ivan"],
            payload(
                forum,
                310,
                rules["ivan"],
                "Всем привет",
                None,
                topic_id=1,
                sender_id=46,
                sender_name="Иван Петров",
                sender_username=None,
            ),
        ),
        (
            MOD,
            rules["casino"],
            payload(public, 111, rules["casino"], "<b>&amp;</b> казино", "казино"),
        ),
        (
            BLOCKED,
            rules["blocked"],
            payload(public, 112, rules["blocked"], "квартира", "квартира"),
        ),
        (
            OWNER,
            rules["news"],
            payload(
                channel,
                505,
                rules["news"],
                "Курс лева не изменился",
                "Курс",
                sender_id=channel.id,
                sender_name="Новости",
                sender_username="novosti_bg",
                sender_is_channel=True,
            ),
        ),
        (
            OWNER,
            rules["flat"],
            payload(
                public,
                106,
                rules["flat"],
                "Администрация: квартира сдана",
                "квартира",
                sender_id=public.id,
                sender_name="Оренда Варна",
                sender_username="varna_rent",
                sender_is_channel=True,
            ),
        ),
        (
            OWNER,
            rules["flat"],
            payload(
                public,
                107,
                rules["flat"],
                "квартира " * 600,
                "квартира",
                sender_id=None,
                sender_name="",
                sender_username=None,
            ),
        ),
    ]
    for user_id, rule, snapshot in rows:
        clock.tick(0.5)
        claimed = await storage.claim_delivery(
            user_id, snapshot.chat_id, snapshot.msg_id, rule.id, snapshot.to_json()
        )
        if claimed is None:
            msg = f"row {(user_id, snapshot.chat_id, snapshot.msg_id)} was claimed twice"
            raise SystemExit(msg)

    clock.tick(1)
    # What the delivery worker writes back: a name it resolved, and the three final states.
    resolved = next(s for _, _, s in rows if s.msg_id == 108)
    await storage.update_payload(
        (OWNER, public.id, 108), {**resolved.to_json(), "sender_name": "Резолвнутый Имя"}
    )
    await storage.set_delivery_status(OWNER, channel.id, 505, status.SENT, attempts=1)
    await storage.mark_digested([(OWNER, public.id, 106)])
    await storage.set_delivery_status(
        OWNER, public.id, 107, status.FAILED, error="MessageTooLongError", attempts=2
    )
    await storage.pause_delivery(BLOCKED, "blocked")


async def write_exports(old: Legacy, storage: Any, out: Path) -> None:
    for user_id, name in ((OWNER, "owner"), (MOD, "moderator")):
        config = await old.portable.export_config(storage, user_id, now=EXPORTED_AT)
        (out / f"export-{name}.json").write_text(old.portable.dumps(config), encoding="utf-8")


async def write_renders(old: Legacy, storage: Any, out: Path) -> None:
    fmt = old.formatter
    cur = await storage.db.execute("SELECT user_id, chat_id, msg_id FROM outbox ORDER BY rowid")
    keys = [(int(r[0]), int(r[1]), int(r[2])) for r in await cur.fetchall()]
    notifications = []
    for key in keys:
        item = await storage.get_delivery(*key)
        payload = fmt.Payload.from_json(item.payload)
        keyboard = old.menus.notification_buttons(
            item.chat_id, item.msg_id, item.payload.get("sender_id"), item.rule_id
        )
        for lang, tz in VIEWS:
            rendered = fmt.render_notification(payload, lang=lang, tz=tz, now=RENDER_AT)
            notifications.append(
                {
                    "key": list(key),
                    "lang": lang,
                    "tz": tz,
                    "text": rendered.text,
                    "entities": entities(rendered),
                    "buttons": [[list(button) for button in row] for row in keyboard],
                }
            )
    digests = []
    pending = await storage.pending_deliveries()
    for user_id in (OWNER, MOD):
        batch = [item for item in pending if item.user_id == user_id]
        batch_entries = [fmt.DigestEntry(fmt.Payload.from_json(i.payload), i.key) for i in batch]
        for lang, tz in VIEWS:
            for overflow in OVERFLOWS:
                rendered = fmt.render_digest(
                    batch_entries, lang=lang, tz=tz, now=RENDER_AT, overflow=overflow
                )
                digests.append(
                    {
                        "user_id": user_id,
                        "keys": [list(item.key) for item in batch],
                        "lang": lang,
                        "tz": tz,
                        "overflow": overflow,
                        "text": rendered.text,
                        "entities": entities(rendered),
                    }
                )
    document = {
        "built_by": "b25b311",
        "now": RENDER_AT.isoformat(),
        "notifications": notifications,
        "digests": digests,
    }
    text = json.dumps(document, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    (out / "rendered.json").write_text(text, encoding="utf-8")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        sys.stderr.write("usage: build.py <b25b311 source tree> <output directory>\n")
        return 2
    tree, out = Path(argv[0]), Path(argv[1])
    out.mkdir(parents=True, exist_ok=True)
    for stale in out.glob("ddiver.sqlite3*"):
        stale.unlink()
    asyncio.run(build(Legacy(tree), out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
