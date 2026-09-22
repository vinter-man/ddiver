"""SQLite persistence (aiosqlite, WAL). One connection, one writer, simple migrations."""

from __future__ import annotations

import datetime as dt
import enum
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

from ddiver.links import ChatRef
from ddiver.rules import RuleKind
from ddiver.timeutil import Clock, utcnow

ALL_CHATS = 0
"""``chat_id`` value meaning "every chat this user is subscribed to" (rules, ignores)."""


class Role(enum.StrEnum):
    OWNER = "owner"
    ALLOWED = "allowed"
    PENDING = "pending"
    DENIED = "denied"

    @property
    def can_use(self) -> bool:
        return self in (Role.OWNER, Role.ALLOWED)


class DeliveryStatus(enum.StrEnum):
    QUEUED = "queued"
    SENT = "sent"
    DIGESTED = "digested"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class User:
    id: int
    lang: str
    tz: str
    role: Role
    digest_threshold: int
    paused: bool
    onboarded: bool


@dataclass(frozen=True, slots=True)
class ChatRow:
    ref: ChatRef
    available: bool

    @property
    def id(self) -> int:
        return self.ref.id


@dataclass(frozen=True, slots=True)
class Rule:
    id: int
    user_id: int
    chat_id: int  # ALL_CHATS or a chat id
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
    chat_id: int  # ALL_CHATS or a chat id
    sender_id: int
    note: str | None


@dataclass(frozen=True, slots=True)
class Cursor:
    chat_id: int
    last_msg_id: int
    last_msg_at: dt.datetime


class DuplicateRuleError(Exception):
    pass


_MIGRATIONS: list[str] = [
    # v1
    """
    CREATE TABLE users (
      id INTEGER PRIMARY KEY,
      lang TEXT NOT NULL DEFAULT 'en',
      tz TEXT NOT NULL DEFAULT 'UTC',
      role TEXT NOT NULL DEFAULT 'pending',
      digest_threshold INTEGER NOT NULL DEFAULT 20,
      paused INTEGER NOT NULL DEFAULT 0,
      onboarded INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE TABLE chats (
      id INTEGER PRIMARY KEY,
      title TEXT NOT NULL,
      username TEXT,
      kind TEXT NOT NULL,
      is_forum INTEGER NOT NULL DEFAULT 0,
      available INTEGER NOT NULL DEFAULT 1,
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
      kind TEXT NOT NULL,
      pattern TEXT NOT NULL,
      disabled_reason TEXT,
      hits INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL,
      UNIQUE (user_id, chat_id, kind, pattern)
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
    CREATE TABLE deliveries (
      user_id INTEGER NOT NULL,
      chat_id INTEGER NOT NULL,
      msg_id INTEGER NOT NULL,
      rule_id INTEGER,
      status TEXT NOT NULL,
      created_at TEXT NOT NULL,
      PRIMARY KEY (user_id, chat_id, msg_id)
    );
    CREATE INDEX deliveries_created ON deliveries(created_at);
    """,
]


def _iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.UTC).isoformat(timespec="seconds")


def _parse(text: str) -> dt.datetime:
    moment = dt.datetime.fromisoformat(text)
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.UTC)


class Storage:
    def __init__(self, path: Path | str, clock: Clock = utcnow) -> None:
        self._path = str(path)
        self._clock = clock
        self._db: aiosqlite.Connection | None = None

    # -- lifecycle -------------------------------------------------------------------------

    async def open(self) -> None:
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(self._path, isolation_level=None)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute("PRAGMA busy_timeout=5000")
        self._db = db
        try:
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

    async def _migrate(self) -> None:
        cur = await self.db.execute("PRAGMA user_version")
        row = await cur.fetchone()
        version = int(row[0]) if row else 0
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

    # -- users -----------------------------------------------------------------------------

    @staticmethod
    def _user(row: aiosqlite.Row) -> User:
        return User(
            id=row["id"],
            lang=row["lang"],
            tz=row["tz"],
            role=Role(row["role"]),
            digest_threshold=row["digest_threshold"],
            paused=bool(row["paused"]),
            onboarded=bool(row["onboarded"]),
        )

    async def get_user(self, user_id: int) -> User | None:
        cur = await self.db.execute("SELECT * FROM users WHERE id=?", (user_id,))
        row = await cur.fetchone()
        return self._user(row) if row else None

    async def ensure_user(self, user_id: int, *, role: Role = Role.PENDING) -> User:
        """Create the user if missing (with ``role``) and return it. Existing rows are kept."""
        now = self._now()
        await self.db.execute(
            "INSERT OR IGNORE INTO users (id, role, created_at, updated_at) VALUES (?,?,?,?)",
            (user_id, role.value, now, now),
        )
        user = await self.get_user(user_id)
        assert user is not None  # noqa: S101 - just inserted
        return user

    async def update_user(self, user_id: int, **fields: Any) -> User:
        allowed = {"lang", "tz", "role", "digest_threshold", "paused", "onboarded"}
        unknown = set(fields) - allowed
        if unknown:
            msg = f"unknown user fields: {sorted(unknown)}"
            raise ValueError(msg)
        if not fields:
            user = await self.get_user(user_id)
            if user is None:
                raise KeyError(user_id)
            return user
        values = {k: (v.value if isinstance(v, enum.Enum) else v) for k, v in fields.items()}
        assignments = ", ".join(f"{k}=?" for k in values)
        await self.db.execute(
            f"UPDATE users SET {assignments}, updated_at=? WHERE id=?",  # noqa: S608 - keys validated
            (*values.values(), self._now(), user_id),
        )
        user = await self.get_user(user_id)
        if user is None:
            raise KeyError(user_id)
        return user

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
        return ChatRow(ref=ref, available=bool(row["available"]))

    async def upsert_chat(self, ref: ChatRef, *, available: bool = True) -> None:
        await self.db.execute(
            """INSERT INTO chats (id, title, username, kind, is_forum, available, updated_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET title=excluded.title, username=excluded.username,
                 kind=excluded.kind, is_forum=excluded.is_forum, available=excluded.available,
                 updated_at=excluded.updated_at""",
            (
                ref.id,
                ref.title,
                ref.username,
                ref.kind,
                int(ref.is_forum),
                int(available),
                self._now(),
            ),
        )

    async def get_chat(self, chat_id: int) -> ChatRow | None:
        cur = await self.db.execute("SELECT * FROM chats WHERE id=?", (chat_id,))
        row = await cur.fetchone()
        return self._chat(row) if row else None

    async def set_chat_available(self, chat_id: int, available: bool) -> None:
        await self.db.execute(
            "UPDATE chats SET available=?, updated_at=? WHERE id=?",
            (int(available), self._now(), chat_id),
        )

    # -- subscriptions ---------------------------------------------------------------------

    async def subscribe(self, user_id: int, chat_id: int) -> bool:
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

    async def subscribers_of(self, chat_id: int) -> list[int]:
        cur = await self.db.execute(
            "SELECT user_id FROM subscriptions WHERE chat_id=? ORDER BY user_id", (chat_id,)
        )
        return [int(r["user_id"]) for r in await cur.fetchall()]

    async def monitored_chat_ids(self) -> set[int]:
        """Chats with at least one subscriber whose role allows usage and who is not paused."""
        cur = await self.db.execute(
            """SELECT DISTINCT s.chat_id FROM subscriptions s JOIN users u ON u.id=s.user_id
               WHERE u.role IN ('owner','allowed') AND u.paused=0"""
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
                "INSERT INTO rules (user_id, chat_id, kind, pattern, created_at)"
                " VALUES (?,?,?,?,?)",
                (user_id, chat_id, kind.value, pattern, self._now()),
            )
        except aiosqlite.IntegrityError as exc:
            raise DuplicateRuleError(pattern) from exc
        rule = await self.get_rule(int(cur.lastrowid or 0))
        assert rule is not None  # noqa: S101 - just inserted
        return rule

    async def get_rule(self, rule_id: int) -> Rule | None:
        cur = await self.db.execute("SELECT * FROM rules WHERE id=?", (rule_id,))
        row = await cur.fetchone()
        return self._rule(row) if row else None

    async def delete_rule(self, rule_id: int, user_id: int) -> bool:
        """Delete only if the rule belongs to ``user_id`` (callback data is untrusted)."""
        cur = await self.db.execute(
            "DELETE FROM rules WHERE id=? AND user_id=?", (rule_id, user_id)
        )
        return cur.rowcount == 1

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
        """Enabled rules that apply to ``chat_id``: chat-specific ones plus the global rules of
        every user subscribed to that chat. Only usable, unpaused users are considered."""
        cur = await self.db.execute(
            """SELECT r.* FROM rules r
               JOIN users u ON u.id=r.user_id
               JOIN subscriptions s ON s.user_id=r.user_id AND s.chat_id=?
               WHERE (r.chat_id=? OR r.chat_id=0) AND r.disabled_reason IS NULL
                 AND u.role IN ('owner','allowed') AND u.paused=0
               ORDER BY r.user_id, (r.chat_id=0), r.kind, r.id""",
            (chat_id, chat_id),
        )
        return [self._rule(r) for r in await cur.fetchall()]

    async def disable_rule(self, rule_id: int, reason: str) -> None:
        await self.db.execute("UPDATE rules SET disabled_reason=? WHERE id=?", (reason, rule_id))

    async def bump_rule_hits(self, rule_id: int, by: int = 1) -> None:
        await self.db.execute("UPDATE rules SET hits=hits+? WHERE id=?", (by, rule_id))

    # -- ignored senders -------------------------------------------------------------------

    async def add_ignored(
        self, user_id: int, chat_id: int, sender_id: int, note: str | None = None
    ) -> bool:
        cur = await self.db.execute(
            """INSERT OR IGNORE INTO ignored_senders (user_id, chat_id, sender_id, note, created_at)
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
        return {
            int(r["chat_id"]): Cursor(
                int(r["chat_id"]), int(r["last_msg_id"]), _parse(r["last_msg_at"])
            )
            for r in await cur.fetchall()
        }

    async def upsert_cursors(self, cursors: list[Cursor]) -> None:
        """Advance cursors; a stored id is never moved backwards."""
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

    # -- deliveries ------------------------------------------------------------------------

    async def claim_delivery(
        self, user_id: int, chat_id: int, msg_id: int, rule_id: int | None
    ) -> bool:
        """Atomically record intent to deliver; ``False`` if this (user, chat, msg) was already
        claimed — the dedupe between live updates, catch-up and restarts."""
        cur = await self.db.execute(
            """INSERT OR IGNORE INTO deliveries
                 (user_id, chat_id, msg_id, rule_id, status, created_at)
               VALUES (?,?,?,?,?,?)""",
            (user_id, chat_id, msg_id, rule_id, DeliveryStatus.QUEUED.value, self._now()),
        )
        return cur.rowcount == 1

    async def set_delivery_status(
        self, user_id: int, chat_id: int, msg_id: int, status: DeliveryStatus
    ) -> None:
        await self.db.execute(
            "UPDATE deliveries SET status=? WHERE user_id=? AND chat_id=? AND msg_id=?",
            (status.value, user_id, chat_id, msg_id),
        )

    async def release_delivery(self, user_id: int, chat_id: int, msg_id: int) -> None:
        """Forget a claim so the item can be re-queued (used when the queue is dropped)."""
        await self.db.execute(
            "DELETE FROM deliveries WHERE user_id=? AND chat_id=? AND msg_id=?",
            (user_id, chat_id, msg_id),
        )

    async def delivery_counts(self, since: dt.datetime) -> dict[str, int]:
        cur = await self.db.execute(
            "SELECT status, COUNT(*) AS n FROM deliveries WHERE created_at>=? GROUP BY status",
            (_iso(since),),
        )
        return {str(r["status"]): int(r["n"]) for r in await cur.fetchall()}

    async def prune_deliveries(self, older_than: dt.timedelta = dt.timedelta(days=7)) -> int:
        cutoff = _iso(self._clock() - older_than)
        cur = await self.db.execute("DELETE FROM deliveries WHERE created_at<?", (cutoff,))
        return int(cur.rowcount)
