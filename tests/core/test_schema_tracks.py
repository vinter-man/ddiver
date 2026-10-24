"""An app's tables version on a named track, independently of the core's ``user_version``.

ddiver-fb keeps ``fb_groups`` and ``fb_items`` next to the core schema in one file. Either side
may gain migrations without the other: a core upgrade must not renumber or re-run the app's
scripts, an app upgrade must not touch ``PRAGMA user_version``, and ddiver — which registers
no track — must get exactly the schema it always had.
"""

import asyncio
import contextlib
import logging
import sqlite3
import tempfile
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import ddiver_core.storage as storage_module
from ddiver_core.links import ChatRef
from ddiver_core.storage import DatabaseCorruptError, MigrationError, Storage

CORE = len(storage_module._MIGRATIONS)

FB_V1 = """
CREATE TABLE fb_groups (
  chat_id INTEGER PRIMARY KEY REFERENCES chats(id) ON DELETE CASCADE,
  slug TEXT,
  state TEXT NOT NULL DEFAULT 'member'
);
"""
FB_V2 = """
ALTER TABLE fb_groups ADD COLUMN listings INTEGER NOT NULL DEFAULT 0;
CREATE TABLE fb_items (
  group_id INTEGER NOT NULL,
  post_id INTEGER NOT NULL,
  received_at TEXT NOT NULL,
  PRIMARY KEY (group_id, post_id)
);
"""
FB_V3 = "CREATE INDEX fb_items_received ON fb_items(received_at);"
CORE_NEXT = "ALTER TABLE users ADD COLUMN from_the_future INTEGER NOT NULL DEFAULT 0;"

GROUP = ChatRef(1234567890, "Varna Rentals", None, "group")


async def opened(path: Path, **kw) -> Storage:
    store = Storage(path, owner_id=1, **kw)
    await store.open()
    return store


async def tables(store: Storage) -> set[str]:
    cur = await store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    return {row[0] for row in await cur.fetchall()}


async def columns(store: Storage, table: str) -> set[str]:
    cur = await store.db.execute(f"PRAGMA table_info({table})")
    return {row["name"] for row in await cur.fetchall()}


def schema_of(path: Path) -> list[tuple[str, str, str | None]]:
    with contextlib.closing(sqlite3.connect(path)) as db:
        return sorted(db.execute("SELECT type, name, sql FROM sqlite_master").fetchall())


def reference_core_schema(path: Path) -> None:
    """The core schema built the way the code before tracks built it: the migrations alone."""
    db = sqlite3.connect(path, isolation_level=None)
    try:
        for index, script in enumerate(storage_module._MIGRATIONS, start=1):
            db.executescript(f"BEGIN;\n{script}\nPRAGMA user_version={index};\nCOMMIT;")
    finally:
        db.close()


class TestWithoutTracks:
    async def test_a_database_without_tracks_is_exactly_the_core_schema(self, tmp_path):
        """ddiver's file: no ledger table, nothing else new, the same user_version."""
        store = await opened(tmp_path / "ddiver.sqlite3")
        try:
            assert await store.schema_version() == CORE
            assert "schema_tracks" not in await tables(store)
            assert await store.track_version("fb") == 0
        finally:
            await store.close()
        reference_core_schema(tmp_path / "reference.sqlite3")
        assert schema_of(tmp_path / "ddiver.sqlite3") == schema_of(tmp_path / "reference.sqlite3")

    @pytest.mark.parametrize("tracks", [None, {}])
    async def test_no_tracks_and_an_empty_mapping_are_the_same(self, tmp_path, tracks):
        store = await opened(tmp_path / "db.sqlite3", tracks=tracks)
        try:
            assert "schema_tracks" not in await tables(store)
        finally:
            await store.close()


class TestOneTrack:
    async def test_a_fresh_file_gets_the_core_schema_and_the_track(self, tmp_path):
        store = await opened(tmp_path / "fb.sqlite3", tracks={"fb": [FB_V1, FB_V2]})
        try:
            assert await store.schema_version() == CORE
            assert await store.track_version("fb") == 2
            assert {"fb_groups", "fb_items", "schema_tracks"} <= await tables(store)
            assert "listings" in await columns(store, "fb_groups")
        finally:
            await store.close()

    async def test_the_track_runs_after_the_core_so_it_can_reference_core_tables(self, tmp_path):
        store = await opened(tmp_path / "fb.sqlite3", tracks={"fb": [FB_V1]})
        try:
            await store.upsert_chat(GROUP)
            await store.db.execute(
                "INSERT INTO fb_groups (chat_id, slug) VALUES (?, 'varna')", (GROUP.id,)
            )
            with pytest.raises(sqlite3.IntegrityError):
                await store.db.execute("INSERT INTO fb_groups (chat_id) VALUES (42)")
            await store.db.execute("DELETE FROM chats WHERE id=?", (GROUP.id,))
            cur = await store.db.execute("SELECT COUNT(*) FROM fb_groups")
            assert (await cur.fetchone())[0] == 0  # ON DELETE CASCADE from the core table
        finally:
            await store.close()

    async def test_reopening_runs_nothing_twice(self, tmp_path):
        path = tmp_path / "fb.sqlite3"
        store = await opened(path, tracks={"fb": [FB_V1, FB_V2]})
        await store.upsert_chat(GROUP)
        await store.db.execute("INSERT INTO fb_groups (chat_id) VALUES (?)", (GROUP.id,))
        await store.close()

        again = await opened(path, tracks={"fb": [FB_V1, FB_V2]})
        try:
            assert await again.track_version("fb") == 2
            cur = await again.db.execute("SELECT chat_id FROM fb_groups")
            assert [row[0] for row in await cur.fetchall()] == [GROUP.id]
        finally:
            await again.close()

    async def test_an_app_upgrade_moves_only_its_own_track(self, tmp_path):
        path = tmp_path / "fb.sqlite3"
        store = await opened(path, tracks={"fb": [FB_V1]})
        await store.upsert_chat(GROUP)
        await store.db.execute("INSERT INTO fb_groups (chat_id) VALUES (?)", (GROUP.id,))
        await store.close()

        upgraded = await opened(path, tracks={"fb": [FB_V1, FB_V2, FB_V3]})
        try:
            assert await upgraded.schema_version() == CORE
            assert await upgraded.track_version("fb") == 3
            cur = await upgraded.db.execute("SELECT chat_id, listings FROM fb_groups")
            assert [tuple(row) for row in await cur.fetchall()] == [(GROUP.id, 0)]
        finally:
            await upgraded.close()

    async def test_an_empty_track_is_at_version_zero(self, tmp_path):
        store = await opened(tmp_path / "fb.sqlite3", tracks={"fb": []})
        try:
            assert "schema_tracks" in await tables(store)
            assert await store.track_version("fb") == 0
        finally:
            await store.close()


class TestAgainstAnOlderCore:
    async def test_a_file_from_an_older_core_upgrades_the_core_and_keeps_the_track(
        self, tmp_path, monkeypatch
    ):
        path = tmp_path / "fb.sqlite3"
        monkeypatch.setattr(storage_module, "_MIGRATIONS", storage_module._MIGRATIONS[:1])
        old = await opened(path, tracks={"fb": [FB_V1]})
        await old.db.execute(
            "INSERT INTO users (id, role, created_at, updated_at) VALUES (1,'allowed','t','t')"
        )
        await old.upsert_chat(GROUP)
        await old.db.execute("INSERT INTO fb_groups (chat_id) VALUES (?)", (GROUP.id,))
        assert await old.schema_version() == 1
        await old.close()
        monkeypatch.undo()

        current = await opened(path, tracks={"fb": [FB_V1, FB_V2]})
        try:
            assert await current.schema_version() == CORE
            assert await current.track_version("fb") == 2
            user = await current.get_user(1)
            assert user is not None
            assert user.include_bots is False  # the core's v2 ran on the old file
            cur = await current.db.execute("SELECT chat_id FROM fb_groups")
            assert [row[0] for row in await cur.fetchall()] == [GROUP.id]
        finally:
            await current.close()

    async def test_a_ddiver_file_can_take_on_an_app_track(self, tmp_path, monkeypatch):
        """No ledger yet, an old user_version: both catch up, the core first."""
        path = tmp_path / "db.sqlite3"
        monkeypatch.setattr(storage_module, "_MIGRATIONS", storage_module._MIGRATIONS[:1])
        old = await opened(path)
        await old.close()
        monkeypatch.undo()

        current = await opened(path, tracks={"fb": [FB_V1]})
        try:
            assert await current.schema_version() == CORE
            assert await current.track_version("fb") == 1
        finally:
            await current.close()


class TestAgainstANewerCore:
    async def test_a_newer_core_adds_its_migration_and_leaves_the_track_alone(
        self, tmp_path, monkeypatch
    ):
        path = tmp_path / "fb.sqlite3"
        current = await opened(path, tracks={"fb": [FB_V1, FB_V2]})
        await current.close()

        monkeypatch.setattr(storage_module, "_MIGRATIONS", [*storage_module._MIGRATIONS, CORE_NEXT])
        newer = await opened(path, tracks={"fb": [FB_V1, FB_V2]})
        try:
            assert await newer.schema_version() == CORE + 1
            assert await newer.track_version("fb") == 2
            assert "from_the_future" in await columns(newer, "users")
        finally:
            await newer.close()

    async def test_an_older_core_on_a_newer_file_still_runs_the_apps_migrations(
        self, tmp_path, monkeypatch
    ):
        """A rolled-back core must neither lower user_version nor block the app's track."""
        path = tmp_path / "fb.sqlite3"
        monkeypatch.setattr(storage_module, "_MIGRATIONS", [*storage_module._MIGRATIONS, CORE_NEXT])
        newer = await opened(path, tracks={"fb": [FB_V1]})
        await newer.close()
        monkeypatch.undo()

        older = await opened(path, tracks={"fb": [FB_V1, FB_V2]})
        try:
            assert await older.schema_version() == CORE + 1  # untouched, not re-run
            assert await older.track_version("fb") == 2
            assert "from_the_future" in await columns(older, "users")
        finally:
            await older.close()


class TestSeveralTracks:
    async def test_tracks_version_independently_and_run_in_order(self, tmp_path):
        audit_v1 = "CREATE TABLE audit (group_id INTEGER REFERENCES fb_groups(chat_id), note TEXT);"
        path = tmp_path / "fb.sqlite3"
        store = await opened(path, tracks={"fb": [FB_V1], "audit": [audit_v1]})
        await store.close()

        store = await opened(path, tracks={"fb": [FB_V1, FB_V2], "audit": [audit_v1]})
        try:
            assert await store.track_version("fb") == 2
            assert await store.track_version("audit") == 1
            cur = await store.db.execute("SELECT track, version FROM schema_tracks ORDER BY 1")
            assert [tuple(row) for row in await cur.fetchall()] == [("audit", 1), ("fb", 2)]
        finally:
            await store.close()


class TestFailures:
    async def test_a_failing_script_is_rolled_back_whole_and_names_its_track(self, tmp_path):
        path = tmp_path / "fb.sqlite3"
        store = await opened(path, tracks={"fb": [FB_V1]})
        await store.close()

        broken = "CREATE TABLE fb_items (x INTEGER);\nINSERT INTO no_such_table VALUES (1);"
        failing = Storage(path, owner_id=1, tracks={"fb": [FB_V1, broken]})
        with pytest.raises(MigrationError, match="fb v2"):
            await failing.open()
        with pytest.raises(RuntimeError, match="not open"):
            _ = failing.db

        store = await opened(path, tracks={"fb": [FB_V1]})
        try:
            assert await store.track_version("fb") == 1
            assert "fb_items" not in await tables(store)  # the half that succeeded is gone
            assert await store.schema_version() == CORE
        finally:
            await store.close()

    async def test_a_constraint_a_migration_trips_over_is_not_called_corruption(self, tmp_path):
        """IntegrityError is a DatabaseError; unwrapped it would say "restore a backup"."""
        orphan = "INSERT INTO fb_groups (chat_id) VALUES (999);"
        failing = Storage(tmp_path / "fb.sqlite3", tracks={"fb": [FB_V1, orphan]})
        with pytest.raises(MigrationError) as caught:
            await failing.open()
        assert not isinstance(caught.value, DatabaseCorruptError)
        assert isinstance(caught.value.__cause__, sqlite3.IntegrityError)

    async def test_a_track_newer_than_the_build_is_run_anyway_and_logged(self, tmp_path, caplog):
        """A rolled-back deploy: refusing to open would turn the rollback into an outage."""
        path = tmp_path / "fb.sqlite3"
        store = await opened(path, tracks={"fb": [FB_V1, FB_V2]})
        await store.close()

        with caplog.at_level(logging.WARNING, logger="ddiver_core.storage"):
            older = await opened(path, tracks={"fb": [FB_V1]})
        try:
            assert await older.track_version("fb") == 2  # never lowered
            assert "fb_items" in await tables(older)
        finally:
            await older.close()
        assert "migration track fb is at v2, this build knows v1" in caplog.text

    async def test_a_script_that_leaves_its_transaction_open_fails_and_leaves_nothing(
        self, tmp_path
    ):
        """The last line of defence, with the construction checks out of the way.

        An unclosed ``/*`` runs to the end of the input, taking the ledger row and the COMMIT
        with it. Without this check the file opened with the transaction still open, and every
        write after it was lost when the connection closed.
        """
        path = tmp_path / "fb.sqlite3"
        failing = Storage(path, owner_id=1, tracks={"fb": [FB_V1]})
        failing._tracks = {"fb": ("CREATE TABLE fb_x (id INTEGER); /* never closed",)}
        with pytest.raises(MigrationError, match="fb v1 left its transaction open"):
            await failing.open()
        with pytest.raises(RuntimeError, match="not open"):
            _ = failing.db

        store = await opened(path)
        try:
            assert "fb_x" not in await tables(store)
            assert await store.track_version("fb") == 0
            assert await store.schema_version() == CORE
        finally:
            await store.close()


class TestValidation:
    @pytest.mark.parametrize(
        "name", ["", "FB", "1fb", "fb-x", "fb x", "fb'); DROP TABLE users; --", "a" * 33]
    )
    def test_a_name_that_would_need_quoting_is_refused(self, name):
        with pytest.raises(ValueError, match="bad migration track name"):
            Storage(":memory:", tracks={name: [FB_V1]})

    def test_the_longest_allowed_name_is_accepted(self):
        Storage(":memory:", tracks={"a" * 32: [FB_V1]})

    def test_one_string_instead_of_a_list_is_refused(self):
        with pytest.raises(TypeError, match="list of SQL scripts"):
            Storage(":memory:", tracks={"fb": FB_V1})

    def test_something_that_is_not_a_list_is_refused(self):
        with pytest.raises(TypeError, match="list of SQL scripts"):
            Storage(":memory:", tracks={"fb": 3})  # type: ignore[dict-item]

    def test_a_script_that_is_not_text_is_refused(self):
        with pytest.raises(TypeError, match="fb v2 is not SQL text"):
            Storage(":memory:", tracks={"fb": [FB_V1, b"CREATE TABLE x (y)"]})  # type: ignore[list-item]

    @pytest.mark.parametrize(
        "script",
        [
            "PRAGMA user_version=0;",
            "pragma USER_VERSION = 7;",
            "DELETE FROM schema_tracks;",
            "UPDATE Schema_Tracks SET version=0;",
        ],
    )
    def test_a_script_that_touches_the_bookkeeping_is_refused(self, script):
        with pytest.raises(ValueError, match="touches user_version or schema_tracks"):
            Storage(":memory:", tracks={"fb": [FB_V1, script]})

    @pytest.mark.parametrize(
        ("script", "verb"),
        [
            ("CREATE TABLE fb_x (id INTEGER); ROLLBACK;", "ROLLBACK"),
            ("CREATE TABLE fb_x (id INTEGER);\ncommit;", "COMMIT"),
            ("  -- leading comment\n  END TRANSACTION;", "END"),
            ("/* opens its own */ BEGIN IMMEDIATE; CREATE TABLE fb_x (id INTEGER);", "BEGIN"),
            ("SAVEPOINT s1; CREATE TABLE fb_x (id INTEGER); RELEASE s1;", "SAVEPOINT"),
            ("CREATE TABLE fb_x (id INTEGER); release s1", "RELEASE"),
        ],
    )
    def test_a_script_that_controls_the_transaction_is_refused(self, script, verb):
        # The script runs inside the ledger's BEGIN ... COMMIT: a ROLLBACK there would let the
        # ledger row commit alone and record a version whose tables do not exist.
        with pytest.raises(ValueError, match=f"controls the transaction itself \\({verb}\\)"):
            Storage(":memory:", tracks={"fb": [FB_V1, script]})

    @pytest.mark.parametrize(
        "script",
        [
            # BEGIN ... END inside a trigger body is part of CREATE TRIGGER, not a transaction.
            (
                "CREATE TABLE fb_x (id INTEGER);\n"
                "CREATE TRIGGER fb_x_ai AFTER INSERT ON fb_x BEGIN\n"
                "  UPDATE fb_x SET id = id WHERE id = NEW.id;\n"
                "END;"
            ),
            # The words inside literals and comments are data, not statements.
            "CREATE TABLE fb_x (note TEXT DEFAULT 'COMMIT; ROLLBACK;');",
            "-- ROLLBACK; would be bad\nCREATE TABLE fb_x (id INTEGER);",
            "/* BEGIN; */ CREATE TABLE fb_x (id INTEGER);",
        ],
    )
    async def test_triggers_literals_and_comments_are_not_transaction_control(
        self, tmp_path, script
    ):
        store = await opened(tmp_path / "fb.sqlite3", tracks={"fb": [script]})
        try:
            assert await store.track_version("fb") == 1
        finally:
            await store.close()

    @pytest.mark.parametrize(
        "script",
        [
            "CREATE TABLE fb_x (id INTEGER); /* a note left open",
            "CREATE TABLE fb_x (note TEXT DEFAULT 'never closed);",
            (
                "CREATE TABLE fb_x (id INTEGER);\n"
                "CREATE TRIGGER fb_x_ai AFTER INSERT ON fb_x BEGIN\n"
                "  UPDATE fb_x SET id = id WHERE id = NEW.id;"
            ),
        ],
    )
    def test_a_script_that_ends_inside_a_comment_string_or_trigger_is_refused(self, script):
        # The wrapper appends the ledger row and COMMIT; any of these would swallow them.
        with pytest.raises(ValueError, match="fb v2 ends inside a comment, a string or a trigger"):
            Storage(":memory:", tracks={"fb": [FB_V1, script]})

    @pytest.mark.parametrize(
        "script",
        [
            "CREATE TABLE fb_x (id INTEGER); -- a closing note, no line break after it",
            "CREATE TABLE fb_x (id INTEGER)",  # no final semicolon
            "CREATE TABLE fb_x (id INTEGER); /* a closed note */",
            (
                "CREATE TABLE fb_x (id INTEGER);\n"
                "CREATE TRIGGER fb_x_ai AFTER INSERT ON fb_x BEGIN\n"
                "  UPDATE fb_x SET id = id WHERE id = NEW.id;\n"
                "END"
            ),
        ],
    )
    async def test_a_script_that_ends_cleanly_without_a_semicolon_or_newline_runs(
        self, tmp_path, script
    ):
        store = await opened(tmp_path / "fb.sqlite3", tracks={"fb": [script]})
        try:
            assert await store.track_version("fb") == 1
            assert "fb_x" in await tables(store)
            assert not store.db.in_transaction
        finally:
            await store.close()


FRAGMENTS = [
    "CREATE TABLE IF NOT EXISTS fb_p (id INTEGER);",
    "INSERT INTO fb_p VALUES (1);",
    "SELECT 'a;b'",
    "SELECT 1",
    ";",
    "\n",
    " -- a note",
    "/* a note */",
    "/* open",
    "'",
    "CREATE TRIGGER IF NOT EXISTS fb_p_ai AFTER INSERT ON fb_p BEGIN SELECT 1;",
    " END",
]


@settings(max_examples=200, deadline=None)
@given(st.lists(st.sampled_from(FRAGMENTS), min_size=1, max_size=6).map("".join))
def test_a_script_either_is_refused_or_ends_with_its_ledger_row_committed(script):
    """The construction check reads a script the way SQLite will run it inside the wrapper.

    Whatever it lets through either migrates, with the ledger row committed and no transaction
    left open, or fails as a whole; the runtime guard behind it never has to step in.
    """

    async def run(path: Path) -> None:
        try:
            store = Storage(path, owner_id=1, tracks={"fb": [script]})
        except ValueError:
            return
        try:
            await store.open()
        except MigrationError as exc:
            assert "left its transaction open" not in str(exc)
            store = await opened(path)
            try:
                assert await store.track_version("fb") == 0
                assert "fb_p" not in await tables(store)
            finally:
                await store.close()
            return
        try:
            assert not store.db.in_transaction
            assert await store.track_version("fb") == 1
            await store.ensure_user(2)
        finally:
            await store.close()
        store = await opened(path)
        try:
            cur = await store.db.execute("SELECT count(*) FROM users WHERE id = 2")
            assert (await cur.fetchone())[0] == 1  # a write after the migration was kept
        finally:
            await store.close()

    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(run(Path(tmp) / "fb.sqlite3"))


SCRIPTS = [FB_V1, FB_V2, FB_V3]


@settings(max_examples=25, deadline=None)
@given(st.lists(st.integers(min_value=0, max_value=len(SCRIPTS)), min_size=1, max_size=5))
def test_any_sequence_of_upgrades_ends_where_one_upgrade_does(steps):
    """Deploying 1, then 3, then 2 (a rollback), then 3 leaves the same file as deploying 3."""
    steps = [*steps, len(SCRIPTS)]

    async def run(path: Path, sequence: list[int]) -> None:
        for known in sequence:
            store = await opened(path, tracks={"fb": SCRIPTS[:known]})
            await store.close()

    with tempfile.TemporaryDirectory() as tmp:
        stepped, direct = Path(tmp) / "stepped.sqlite3", Path(tmp) / "direct.sqlite3"
        asyncio.run(run(stepped, steps))
        asyncio.run(run(direct, [len(SCRIPTS)]))
        assert schema_of(stepped) == schema_of(direct)
        with contextlib.closing(sqlite3.connect(stepped)) as db:
            assert db.execute("SELECT version FROM schema_tracks").fetchall() == [(len(SCRIPTS),)]
