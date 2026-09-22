"""SQLite persistence (aiosqlite, WAL). One connection, one writer, simple migrations.

Ownership is *computed* (``user_id == OWNER_ID``), never stored, so the owner can never be
locked out and changing ``OWNER_ID`` needs no migration. Every mutating method takes the acting
``user_id`` and filters on it, because callback payloads are client-controlled.
"""

from __future__ import annotations

import datetime as dt
import enum
import json
import logging
import sqlite3
from collections.abc import Awaitable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

from ddiver.links import ChatRef
from ddiver.rules import RuleKind, pattern_key
from ddiver.timeutil import Clock, utcnow

log = logging.getLogger(__name__)

ALL_CHATS = 0
"""``chat_id`` value meaning "every chat this user is subscribed to" (rules, ignores)."""

CHAT_KINDS = ("group", "supergroup", "channel")
PRUNE_BATCH = 5000


class Role(enum.StrEnum):
    ALLOWED = "allowed"
    PENDING = "pending"
    DENIED = "denied"


class DeliveryStatus(enum.StrEnum):
    PENDING = "pending"
    SENT = "sent"
    DIGESTED = "digested"
    FAILED = "failed"


class DatabaseCorruptError(Exception):
    """``PRAGMA quick_check`` failed; the file needs restoring from a backup."""


class DuplicateRuleError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class User:
    id: int
    lang: str | None
    tz: str | None
    role: Role
    digest_threshold: int
    delivery_paused_at: dt.datetime | None
    pause_reason: str | None
    notified_owner_at: dt.datetime | None
    is_owner: bool = False

    @property
    def onboarded(self) -> bool:
        return self.lang is not None and self.tz is not None

    @property
    def active(self) -> bool:
        """May use the bot at all (independently of delivery being paused)."""
        return self.is_owner or self.role is Role.ALLOWED

    @property
    def paused(self) -> bool:
        return self.delivery_paused_at is not None


@dataclass(frozen=True, slots=True)
class ChatRow:
    ref: ChatRef
    available: bool
    shared: bool
    access_hash: int | None

    @property
    def id(self) -> int:
        return self.ref.id


@dataclass(frozen=True, slots=True)
class Rule:
    id: int
    user_id: int
    chat_id: int
    kind: RuleKind
    pattern: str
    disabled_reason: str | None
    hits: int

    @property
    def is_global(self) -> bool:
        return self.chat_id == ALL_CHATS


@dataclass(frozen=True, slots=True)
class IgnoredSender:
    user_id: int
    chat_id: int
    sender_id: int
    note: str | None


@dataclass(frozen=True, slots=True)
class Cursor:
    chat_id: int
    last_msg_id: int
    last_msg_at: dt.datetime


@dataclass(frozen=True, slots=True)
class OutboxItem:
    user_id: int
    chat_id: int
    msg_id: int
    rule_id: int | None
    status: DeliveryStatus
    attempts: int
    payload: dict[str, Any]
    created_at: dt.datetime

    @property
    def key(self) -> tuple[int, int, int]:
        return (self.user_id, self.chat_id, self.msg_id)


_MIGRATIONS: list[str] = [
    # v1
    """
    CREATE TABLE users (
      id INTEGER PRIMARY KEY,
      lang TEXT,
      tz TEXT,
      role TEXT NOT NULL DEFAULT 'pending',
      digest_threshold INTEGER NOT NULL DEFAULT 20,
      delivery_paused_at TEXT,
      pause_reason TEXT,
      notified_owner_at TEXT,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE TABLE chats (
      id INTEGER PRIMARY KEY,
      title TEXT NOT NULL,
      username TEXT,
      kind TEXT NOT NULL CHECK (kind IN ('group','supergroup','channel')),
      is_forum INTEGER NOT NULL DEFAULT 0,
      access_hash INTEGER,
      available INTEGER NOT NULL DEFAULT 1,
      shared INTEGER NOT NULL DEFAULT 0,
      shared_at TEXT,
      updated_at TEXT NOT NULL
    );
    CREATE TABLE subscriptions (
      user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
      created_at TEXT NOT NULL,
      PRIMARY KEY (user_id, chat_id)
    );
    CREATE INDEX subscriptions_chat ON subscriptions(chat_id);
    CREATE TABLE rules (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      chat_id INTEGER NOT NULL DEFAULT 0,
      kind TEXT NOT NULL CHECK (kind IN ('include','exclude')),
      pattern TEXT NOT NULL,
      pattern_key TEXT NOT NULL,
      disabled_reason TEXT,
      hits INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL,
      UNIQUE (user_id, chat_id, kind, pattern_key)
    );
    CREATE TABLE ignored_senders (
      user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      chat_id INTEGER NOT NULL DEFAULT 0,
      sender_id INTEGER NOT NULL,
      note TEXT,
      created_at TEXT NOT NULL,
      PRIMARY KEY (user_id, chat_id, sender_id)
    );
    CREATE TABLE chat_cursor (
      chat_id INTEGER PRIMARY KEY,
      last_msg_id INTEGER NOT NULL,
      last_msg_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE TABLE outbox (
      user_id INTEGER NOT NULL,
      chat_id INTEGER NOT NULL,
      msg_id INTEGER NOT NULL,
      rule_id INTEGER,
      status TEXT NOT NULL CHECK (status IN ('pending','sent','digested','failed')),
      attempts INTEGER NOT NULL DEFAULT 0,
      last_error TEXT,
      payload TEXT NOT NULL,
      created_at TEXT NOT NULL,
      sent_at TEXT,
      PRIMARY KEY (user_id, chat_id, msg_id)
    );
    CREATE INDEX outbox_pending ON outbox(status, created_at);
    CREATE TABLE boots (started_at TEXT NOT NULL);
    """,
]


def _iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.UTC).isoformat(timespec="seconds")


def _parse(text: str | None) -> dt.datetime | None:
    if not text:
        return None
    moment = dt.datetime.fromisoformat(text)
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.UTC)


class Storage:
    def __init__(self, path: Path | str, *, owner_id: int = 0, clock: Clock = utcnow) -> None:
        self._path = str(path)
        self._owner_id = owner_id
        self._clock = clock
        self._db: aiosqlite.Connection | None = None

    # -- lifecycle -------------------------------------------------------------------------

    async def open(self) -> None:
        if self._path != ":memory:" and not self._path.startswith("file:"):
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(self._path, isolation_level=None, uri=True)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute("PRAGMA busy_timeout=5000")
        self._db = db
        try:
            await self._quick_check()
            await self._migrate()
        except Exception:
            self._db = None
            await db.close()
            raise

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            msg = "Storage is not open"
            raise RuntimeError(msg)
        return self._db

    async def _quick_check(self) -> None:
        cur = await self.db.execute("PRAGMA quick_check")
        row = await cur.fetchone()
        result = str(row[0]) if row else "unknown"
        if result != "ok":
            raise DatabaseCorruptError(result)

    async def _migrate(self) -> None:
        version = await self.schema_version()
        for index, script in enumerate(_MIGRATIONS[version:], start=version + 1):
            # executescript() commits any open transaction first, so the transaction has to
            # live inside the script itself for the migration to be atomic.
            await self.db.executescript(f"BEGIN;\n{script}\nPRAGMA user_version={index};\nCOMMIT;")

    async def schema_version(self) -> int:
        cur = await self.db.execute("PRAGMA user_version")
        row = await cur.fetchone()
        return int(row[0]) if row else 0

    def _now(self) -> str:
        return _iso(self._clock())

    def is_owner(self, user_id: int) -> bool:
        return self._owner_id != 0 and user_id == self._owner_id

    # -- users -----------------------------------------------------------------------------

    def _user(self, row: aiosqlite.Row) -> User:
        return User(
            id=row["id"],
            lang=row["lang"],
            tz=row["tz"],
            role=Role(row["role"]),
            digest_threshold=row["digest_threshold"],
            delivery_paused_at=_parse(row["delivery_paused_at"]),
            pause_reason=row["pause_reason"],
            notified_owner_at=_parse(row["notified_owner_at"]),
            is_owner=self.is_owner(row["id"]),
        )

    async def get_user(self, user_id: int) -> User | None:
        cur = await self.db.execute("SELECT * FROM users WHERE id=?", (user_id,))
        row = await cur.fetchone()
        return self._user(row) if row else None

    async def ensure_user(
        self, user_id: int, *, lang: str | None = None, digest_threshold: int = 20
    ) -> User:
        """Create the user row if missing and return it. Existing rows are untouched."""
        now = self._now()
        role = Role.ALLOWED if self.is_owner(user_id) else Role.PENDING
        await self.db.execute(
            """INSERT OR IGNORE INTO users
                 (id, lang, role, digest_threshold, created_at, updated_at)
               VALUES (?,?,?,?,?,?)""",
            (user_id, lang, role.value, digest_threshold, now, now),
        )
        user = await self.get_user(user_id)
        if user is None:  # pragma: no cover - impossible after the insert
            raise KeyError(user_id)
        return user

    async def update_user(self, user_id: int, **fields: Any) -> User:
        allowed = {
            "lang",
            "tz",
            "role",
            "digest_threshold",
            "delivery_paused_at",
            "pause_reason",
            "notified_owner_at",
        }
        unknown = set(fields) - allowed
        if unknown:
            msg = f"unknown user fields: {sorted(unknown)}"
            raise ValueError(msg)
        if not fields:
            user = await self.get_user(user_id)
            if user is None:
                raise KeyError(user_id)
            return user
        values: dict[str, Any] = {}
        for key, value in fields.items():
            if isinstance(value, enum.Enum):
                values[key] = value.value
            elif isinstance(value, dt.datetime):
                values[key] = _iso(value)
            else:
                values[key] = value
        assignments = ", ".join(f"{k}=?" for k in values)
        await self.db.execute(
            f"UPDATE users SET {assignments}, updated_at=? WHERE id=?",  # noqa: S608 - keys checked
            (*values.values(), self._now(), user_id),
        )
        user = await self.get_user(user_id)
        if user is None:
            raise KeyError(user_id)
        return user

    async def pause_delivery(self, user_id: int, reason: str) -> None:
        await self.update_user(user_id, delivery_paused_at=self._clock(), pause_reason=reason)

    async def resume_delivery(self, user_id: int) -> bool:
        """Clear a delivery pause; ``True`` when there was one (any update from the user does)."""
        cur = await self.db.execute(
            """UPDATE users SET delivery_paused_at=NULL, pause_reason=NULL, updated_at=?
               WHERE id=? AND delivery_paused_at IS NOT NULL""",
            (self._now(), user_id),
        )
        return cur.rowcount == 1

    async def list_users(self, *, roles: set[Role] | None = None) -> list[User]:
        if roles:
            marks = ",".join("?" * len(roles))
            cur = await self.db.execute(
                f"SELECT * FROM users WHERE role IN ({marks}) ORDER BY id",  # noqa: S608
                tuple(r.value for r in roles),
            )
        else:
            cur = await self.db.execute("SELECT * FROM users ORDER BY id")
        return [self._user(r) for r in await cur.fetchall()]

    async def active_user_ids(self) -> set[int]:
        cur = await self.db.execute("SELECT id, role FROM users")
        return {
            int(r["id"])
            for r in await cur.fetchall()
            if self.is_owner(r["id"]) or r["role"] == Role.ALLOWED.value
        }

    # -- chats -----------------------------------------------------------------------------

    @staticmethod
    def _chat(row: aiosqlite.Row) -> ChatRow:
        ref = ChatRef(
            id=row["id"],
            title=row["title"],
            username=row["username"],
            kind=row["kind"],
            is_forum=bool(row["is_forum"]),
        )
        return ChatRow(
            ref=ref,
            available=bool(row["available"]),
            shared=bool(row["shared"]),
            access_hash=row["access_hash"],
        )

    async def upsert_chat(self, ref: ChatRef, *, access_hash: int | None = None) -> None:
        """Insert or refresh chat metadata. ``available`` and ``shared`` are never reset here."""
        await self.db.execute(
            """INSERT INTO chats (id, title, username, kind, is_forum, access_hash, updated_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET title=excluded.title, username=excluded.username,
                 kind=excluded.kind, is_forum=excluded.is_forum,
                 access_hash=COALESCE(excluded.access_hash, chats.access_hash),
                 updated_at=excluded.updated_at""",
            (
                ref.id,
                ref.title,
                ref.username,
                ref.kind,
                int(ref.is_forum),
                access_hash,
                self._now(),
            ),
        )

    async def get_chat(self, chat_id: int) -> ChatRow | None:
        cur = await self.db.execute("SELECT * FROM chats WHERE id=?", (chat_id,))
        row = await cur.fetchone()
        return self._chat(row) if row else None

    async def list_chats(self, *, shared_only: bool = False) -> list[ChatRow]:
        sql = "SELECT * FROM chats"
        if shared_only:
            sql += " WHERE shared=1"
        sql += " ORDER BY title COLLATE NOCASE"
        cur = await self.db.execute(sql)
        return [self._chat(r) for r in await cur.fetchall()]

    async def set_chat_available(self, chat_id: int, available: bool) -> bool:
        """``True`` when the flag actually changed, so callers notify only on transitions."""
        cur = await self.db.execute(
            "UPDATE chats SET available=?, updated_at=? WHERE id=? AND available<>?",
            (int(available), self._now(), chat_id, int(available)),
        )
        return cur.rowcount == 1

    async def set_chat_shared(self, chat_id: int, shared: bool) -> bool:
        cur = await self.db.execute(
            "UPDATE chats SET shared=?, shared_at=?, updated_at=? WHERE id=? AND shared<>?",
            (int(shared), self._now() if shared else None, self._now(), chat_id, int(shared)),
        )
        return cur.rowcount == 1

    async def migrate_chat(self, old_id: int, new_ref: ChatRef, access_hash: int | None) -> None:
        """Move subscriptions, rules and ignores from a basic group to its supergroup."""
        await self.upsert_chat(new_ref, access_hash=access_hash)
        now = self._now()
        await self.db.execute(
            """INSERT OR IGNORE INTO subscriptions (user_id, chat_id, created_at)
               SELECT user_id, ?, ? FROM subscriptions WHERE chat_id=?""",
            (new_ref.id, now, old_id),
        )
        await self.db.execute("DELETE FROM subscriptions WHERE chat_id=?", (old_id,))
        await self.db.execute("UPDATE rules SET chat_id=? WHERE chat_id=?", (new_ref.id, old_id))
        await self.db.execute(
            "UPDATE ignored_senders SET chat_id=? WHERE chat_id=?", (new_ref.id, old_id)
        )
        await self.db.execute("DELETE FROM chat_cursor WHERE chat_id=?", (old_id,))
        await self.db.execute(
            "UPDATE chats SET available=0, updated_at=? WHERE id=?", (now, old_id)
        )

    # -- subscriptions ---------------------------------------------------------------------

    async def subscribe(self, user_id: int, chat_id: int) -> bool:
        """Subscribe, but only to a known chat of a group-like kind (callback data is untrusted)."""
        chat = await self.get_chat(chat_id)
        if chat is None or chat.ref.kind not in CHAT_KINDS:
            return False
        cur = await self.db.execute(
            "INSERT OR IGNORE INTO subscriptions (user_id, chat_id, created_at) VALUES (?,?,?)",
            (user_id, chat_id, self._now()),
        )
        return cur.rowcount == 1

    async def unsubscribe(self, user_id: int, chat_id: int) -> bool:
        cur = await self.db.execute(
            "DELETE FROM subscriptions WHERE user_id=? AND chat_id=?", (user_id, chat_id)
        )
        return cur.rowcount == 1

    async def unsubscribe_all(self, chat_id: int, *, except_user: int | None = None) -> list[int]:
        """Drop every subscription for a chat (used when the owner unshares it)."""
        sql = "SELECT user_id FROM subscriptions WHERE chat_id=?"
        params: list[Any] = [chat_id]
        if except_user is not None:
            sql += " AND user_id<>?"
            params.append(except_user)
        cur = await self.db.execute(sql, params)
        affected = [int(r["user_id"]) for r in await cur.fetchall()]
        if affected:
            marks = ",".join("?" * len(affected))
            await self.db.execute(
                f"DELETE FROM subscriptions WHERE chat_id=? AND user_id IN ({marks})",  # noqa: S608
                (chat_id, *affected),
            )
        return affected

    async def is_subscribed(self, user_id: int, chat_id: int) -> bool:
        cur = await self.db.execute(
            "SELECT 1 FROM subscriptions WHERE user_id=? AND chat_id=?", (user_id, chat_id)
        )
        return await cur.fetchone() is not None

    async def list_subscriptions(self, user_id: int) -> list[ChatRow]:
        cur = await self.db.execute(
            """SELECT c.* FROM chats c JOIN subscriptions s ON s.chat_id=c.id
               WHERE s.user_id=? ORDER BY c.title COLLATE NOCASE""",
            (user_id,),
        )
        return [self._chat(r) for r in await cur.fetchall()]

    async def subscribers_of(self, chat_id: int, *, active_only: bool = True) -> list[int]:
        cur = await self.db.execute(
            "SELECT user_id FROM subscriptions WHERE chat_id=? ORDER BY user_id", (chat_id,)
        )
        ids = [int(r["user_id"]) for r in await cur.fetchall()]
        if not active_only:
            return ids
        active = await self.active_user_ids()
        return [uid for uid in ids if uid in active]

    async def monitored_chat_ids(self) -> set[int]:
        """Chats with at least one active, unpaused subscriber, that we still have access to."""
        active = await self.active_user_ids()
        if not active:
            return set()
        marks = ",".join("?" * len(active))
        cur = await self.db.execute(
            f"""SELECT DISTINCT s.chat_id FROM subscriptions s
                JOIN users u ON u.id=s.user_id
                JOIN chats c ON c.id=s.chat_id
                WHERE s.user_id IN ({marks}) AND u.delivery_paused_at IS NULL
                  AND c.available=1 AND c.kind IN ('group','supergroup','channel')""",  # noqa: S608
            tuple(sorted(active)),
        )
        return {int(r["chat_id"]) for r in await cur.fetchall()}

    # -- rules -----------------------------------------------------------------------------

    @staticmethod
    def _rule(row: aiosqlite.Row) -> Rule:
        return Rule(
            id=row["id"],
            user_id=row["user_id"],
            chat_id=row["chat_id"],
            kind=RuleKind(row["kind"]),
            pattern=row["pattern"],
            disabled_reason=row["disabled_reason"],
            hits=row["hits"],
        )

    async def add_rule(self, user_id: int, chat_id: int, kind: RuleKind, pattern: str) -> Rule:
        try:
            cur = await self.db.execute(
                """INSERT INTO rules (user_id, chat_id, kind, pattern, pattern_key, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (user_id, chat_id, kind.value, pattern, pattern_key(pattern), self._now()),
            )
        except aiosqlite.IntegrityError as exc:
            raise DuplicateRuleError(pattern) from exc
        rule = await self.get_rule(int(cur.lastrowid or 0), user_id)
        if rule is None:  # pragma: no cover - impossible after the insert
            raise KeyError(pattern)
        return rule

    async def get_rule(self, rule_id: int, user_id: int) -> Rule | None:
        cur = await self.db.execute(
            "SELECT * FROM rules WHERE id=? AND user_id=?", (rule_id, user_id)
        )
        row = await cur.fetchone()
        return self._rule(row) if row else None

    async def delete_rule(self, rule_id: int, user_id: int) -> bool:
        cur = await self.db.execute(
            "DELETE FROM rules WHERE id=? AND user_id=?", (rule_id, user_id)
        )
        return cur.rowcount == 1

    async def count_rules(self, user_id: int, chat_id: int | None = None) -> int:
        sql = "SELECT COUNT(*) AS n FROM rules WHERE user_id=?"
        params: list[Any] = [user_id]
        if chat_id is not None:
            sql += " AND chat_id=?"
            params.append(chat_id)
        cur = await self.db.execute(sql, params)
        row = await cur.fetchone()
        return int(row["n"]) if row else 0

    async def list_rules(
        self, user_id: int, chat_id: int | None = None, kind: RuleKind | None = None
    ) -> list[Rule]:
        sql = "SELECT * FROM rules WHERE user_id=?"
        params: list[Any] = [user_id]
        if chat_id is not None:
            sql += " AND chat_id=?"
            params.append(chat_id)
        if kind is not None:
            sql += " AND kind=?"
            params.append(kind.value)
        sql += " ORDER BY (chat_id=0) DESC, chat_id, kind DESC, id"
        cur = await self.db.execute(sql, params)
        return [self._rule(r) for r in await cur.fetchall()]

    async def rules_for_chat(self, chat_id: int) -> list[Rule]:
        """Enabled rules applying to ``chat_id``: chat-specific ones plus the global rules of
        every active, unpaused subscriber. Chat-specific rules come first."""
        active = await self.active_user_ids()
        if not active:
            return []
        marks = ",".join("?" * len(active))
        cur = await self.db.execute(
            f"""SELECT r.* FROM rules r
                JOIN users u ON u.id=r.user_id
                JOIN subscriptions s ON s.user_id=r.user_id AND s.chat_id=?
                WHERE (r.chat_id=? OR r.chat_id=0) AND r.disabled_reason IS NULL
                  AND r.user_id IN ({marks}) AND u.delivery_paused_at IS NULL
                ORDER BY r.user_id, (r.chat_id=0), r.kind, r.id""",  # noqa: S608
            (chat_id, chat_id, *sorted(active)),
        )
        return [self._rule(r) for r in await cur.fetchall()]

    async def disable_rule(self, rule_id: int, reason: str) -> None:
        await self.db.execute("UPDATE rules SET disabled_reason=? WHERE id=?", (reason, rule_id))

    async def enable_rule(self, rule_id: int, user_id: int) -> bool:
        cur = await self.db.execute(
            "UPDATE rules SET disabled_reason=NULL WHERE id=? AND user_id=?", (rule_id, user_id)
        )
        return cur.rowcount == 1

    async def flush_rule_hits(self, hits: dict[int, int]) -> None:
        if not hits:
            return
        await self.db.executemany(
            "UPDATE rules SET hits=hits+? WHERE id=?",
            [(count, rule_id) for rule_id, count in hits.items()],
        )

    # -- ignored senders -------------------------------------------------------------------

    async def add_ignored(
        self, user_id: int, chat_id: int, sender_id: int, note: str | None = None
    ) -> bool:
        cur = await self.db.execute(
            """INSERT OR IGNORE INTO ignored_senders
                 (user_id, chat_id, sender_id, note, created_at)
               VALUES (?,?,?,?,?)""",
            (user_id, chat_id, sender_id, note, self._now()),
        )
        return cur.rowcount == 1

    async def remove_ignored(self, user_id: int, chat_id: int, sender_id: int) -> bool:
        cur = await self.db.execute(
            "DELETE FROM ignored_senders WHERE user_id=? AND chat_id=? AND sender_id=?",
            (user_id, chat_id, sender_id),
        )
        return cur.rowcount == 1

    async def list_ignored(self, user_id: int, chat_id: int | None = None) -> list[IgnoredSender]:
        sql = "SELECT * FROM ignored_senders WHERE user_id=?"
        params: list[Any] = [user_id]
        if chat_id is not None:
            sql += " AND chat_id=?"
            params.append(chat_id)
        sql += " ORDER BY (chat_id=0) DESC, chat_id, sender_id"
        cur = await self.db.execute(sql, params)
        return [
            IgnoredSender(r["user_id"], r["chat_id"], r["sender_id"], r["note"])
            for r in await cur.fetchall()
        ]

    async def ignored_for_chat(self, chat_id: int) -> dict[int, set[int]]:
        """``{user_id: {sender_id, …}}`` combining chat-specific and global ignores."""
        cur = await self.db.execute(
            "SELECT user_id, sender_id FROM ignored_senders WHERE chat_id=? OR chat_id=0",
            (chat_id,),
        )
        out: dict[int, set[int]] = {}
        for r in await cur.fetchall():
            out.setdefault(int(r["user_id"]), set()).add(int(r["sender_id"]))
        return out

    # -- cursors ---------------------------------------------------------------------------

    async def get_cursors(self) -> dict[int, Cursor]:
        cur = await self.db.execute("SELECT * FROM chat_cursor")
        out: dict[int, Cursor] = {}
        for r in await cur.fetchall():
            moment = _parse(r["last_msg_at"])
            if moment is None:  # pragma: no cover - column is NOT NULL
                continue
            out[int(r["chat_id"])] = Cursor(int(r["chat_id"]), int(r["last_msg_id"]), moment)
        return out

    async def upsert_cursors(self, cursors: list[Cursor]) -> None:
        """Advance cursors; a stored id is never moved backwards."""
        if not cursors:
            return
        now = self._now()
        await self.db.executemany(
            """INSERT INTO chat_cursor (chat_id, last_msg_id, last_msg_at, updated_at)
               VALUES (?,?,?,?)
               ON CONFLICT(chat_id) DO UPDATE SET
                 last_msg_id=excluded.last_msg_id, last_msg_at=excluded.last_msg_at,
                 updated_at=excluded.updated_at
               WHERE excluded.last_msg_id > chat_cursor.last_msg_id""",
            [(c.chat_id, c.last_msg_id, _iso(c.last_msg_at), now) for c in cursors],
        )

    async def delete_cursor(self, chat_id: int) -> None:
        await self.db.execute("DELETE FROM chat_cursor WHERE chat_id=?", (chat_id,))

    # -- outbox ----------------------------------------------------------------------------

    @staticmethod
    def _outbox(row: aiosqlite.Row) -> OutboxItem:
        created = _parse(row["created_at"])
        return OutboxItem(
            user_id=row["user_id"],
            chat_id=row["chat_id"],
            msg_id=row["msg_id"],
            rule_id=row["rule_id"],
            status=DeliveryStatus(row["status"]),
            attempts=row["attempts"],
            payload=json.loads(row["payload"]),
            created_at=created or dt.datetime.fromtimestamp(0, dt.UTC),
        )

    async def claim_delivery(
        self,
        user_id: int,
        chat_id: int,
        msg_id: int,
        rule_id: int | None,
        payload: dict[str, Any],
    ) -> OutboxItem | None:
        """Record intent to deliver. ``None`` when this (user, chat, msg) was already claimed —
        the dedupe between live updates, catch-up and restarts."""
        cur = await self.db.execute(
            """INSERT OR IGNORE INTO outbox
                 (user_id, chat_id, msg_id, rule_id, status, payload, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (
                user_id,
                chat_id,
                msg_id,
                rule_id,
                DeliveryStatus.PENDING.value,
                json.dumps(payload, ensure_ascii=False),
                self._now(),
            ),
        )
        if cur.rowcount != 1:
            return None
        return await self.get_delivery(user_id, chat_id, msg_id)

    async def get_delivery(self, user_id: int, chat_id: int, msg_id: int) -> OutboxItem | None:
        cur = await self.db.execute(
            "SELECT * FROM outbox WHERE user_id=? AND chat_id=? AND msg_id=?",
            (user_id, chat_id, msg_id),
        )
        row = await cur.fetchone()
        return self._outbox(row) if row else None

    async def pending_deliveries(self, limit: int = 5000) -> list[OutboxItem]:
        cur = await self.db.execute(
            "SELECT * FROM outbox WHERE status='pending' ORDER BY created_at, msg_id LIMIT ?",
            (limit,),
        )
        return [self._outbox(r) for r in await cur.fetchall()]

    async def set_delivery_status(
        self,
        user_id: int,
        chat_id: int,
        msg_id: int,
        status: DeliveryStatus,
        *,
        error: str | None = None,
        attempts: int | None = None,
    ) -> None:
        sent_at = self._now() if status in (DeliveryStatus.SENT, DeliveryStatus.DIGESTED) else None
        await self.db.execute(
            """UPDATE outbox SET status=?, last_error=?, sent_at=?,
                 attempts=COALESCE(?, attempts)
               WHERE user_id=? AND chat_id=? AND msg_id=?""",
            (status.value, error, sent_at, attempts, user_id, chat_id, msg_id),
        )

    async def mark_digested(self, keys: list[tuple[int, int, int]]) -> None:
        if not keys:
            return
        now = self._now()
        await self.db.executemany(
            """UPDATE outbox SET status='digested', sent_at=?
               WHERE user_id=? AND chat_id=? AND msg_id=?""",
            [(now, *key) for key in keys],
        )

    async def release_delivery(self, user_id: int, chat_id: int, msg_id: int) -> None:
        await self.db.execute(
            "DELETE FROM outbox WHERE user_id=? AND chat_id=? AND msg_id=?",
            (user_id, chat_id, msg_id),
        )

    async def delivery_counts(self, since: dt.datetime) -> dict[str, int]:
        cur = await self.db.execute(
            "SELECT status, COUNT(*) AS n FROM outbox WHERE created_at>=? GROUP BY status",
            (_iso(since),),
        )
        return {str(r["status"]): int(r["n"]) for r in await cur.fetchall()}

    async def prune_outbox(self, older_than: dt.timedelta = dt.timedelta(days=7)) -> int:
        cutoff = _iso(self._clock() - older_than)
        removed = 0
        while True:
            cur = await self.db.execute(
                """DELETE FROM outbox WHERE rowid IN
                     (SELECT rowid FROM outbox WHERE created_at<? AND status<>'pending' LIMIT ?)""",
                (cutoff, PRUNE_BATCH),
            )
            if cur.rowcount <= 0:
                return removed
            removed += cur.rowcount

    # -- boots -----------------------------------------------------------------------------

    async def record_boot(self) -> int:
        """Record this start and return how many boots happened in the last 10 minutes."""
        now = self._clock()
        await self.db.execute("INSERT INTO boots (started_at) VALUES (?)", (_iso(now),))
        cur = await self.db.execute(
            "SELECT COUNT(*) AS n FROM boots WHERE started_at>=?",
            (_iso(now - dt.timedelta(minutes=10)),),
        )
        row = await cur.fetchone()
        await self.db.execute(
            "DELETE FROM boots WHERE started_at<?", (_iso(now - dt.timedelta(days=1)),)
        )
        return int(row["n"]) if row else 1


async def safe_write(coro: Awaitable[Any], what: str) -> bool:
    """Run a storage coroutine, logging (not raising) transient SQLite errors.

    Used by the listener's cursor flush and the outbox writer: a locked database must never
    take down a message handler.
    """
    try:
        await coro
    except sqlite3.OperationalError:
        log.warning("sqlite busy during %s; will retry on the next tick", what)
        return False
    return True
