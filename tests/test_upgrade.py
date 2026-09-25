"""The ddiver deployed today is b25b311; its data has to come through this change untouched.

Everything in ``tests/fixtures/b25b311`` was written by b25b311's own code, not reconstructed
(``build.py`` there is the script, run against ``git archive b25b311``): a schema-v3 database
with ``stats_daily``, the /export files it handed out, and how it rendered each outbox row.
These tests open that data with the code in this tree.

* The database. ddiver starts on it and stops, and the file holds exactly what it held:
  ``user_version`` still 3, no ``schema_tracks`` table, every row as it was. This is the one
  step that could lose data, so it is the one tested through ddiver's own startup.
* The outbox. A row b25b311 wrote has none of the new Payload fields; each takes its default,
  and the row renders, as a notification and in a digest, exactly as b25b311 rendered it.
* /export. A file b25b311 produced is read with the default format tag, ``ddiver.config``,
  and imports through the bot as it did.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any

import pytest

from ddiver import runtime
from ddiver.config import Settings
from ddiver_core import portable
from ddiver_core import storage as storage_module
from ddiver_core.bot import menus
from ddiver_core.bot.handlers import build_router
from ddiver_core.bot.router import Incoming, Reply
from ddiver_core.botruntime import notification_buttons_factory
from ddiver_core.delivery import Delivery
from ddiver_core.formatter import DigestEntry, Payload, render_digest, render_notification
from ddiver_core.i18n import t
from ddiver_core.links import ChatRef
from ddiver_core.storage import OutboxItem, Role, Storage
from ddiver_core_testing import FakeBotClient, FakeClock

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(__file__).parent / "fixtures" / "b25b311"
BUILD = DATA / "build.py"
B25B311 = "b25b3110b3c15d6a148f674b2d0c6381f8a980f3"
RENDERED: dict[str, Any] = json.loads((DATA / "rendered.json").read_text(encoding="utf-8"))
NOW = dt.datetime.fromisoformat(RENDERED["now"])
OWNER = 111111111
MOD = 222222222
EXPORTS = {OWNER: DATA / "export-owner.json", MOD: DATA / "export-moderator.json"}
NEW_PAYLOAD_FIELDS = {"source", "url", "chat_url", "sender_url", "chat_private", "late"}
"""What Payload gained with the core extraction; a b25b311 row has none of them."""


def snapshot(path: Path) -> dict[str, Any]:
    """Everything a database holds that an upgrade could change, read without our code."""
    with contextlib.closing(sqlite3.connect(path)) as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        schema = sorted(db.execute("SELECT type, name, tbl_name, sql FROM sqlite_master"))
        tables = [name for kind, name, _, _ in schema if kind == "table"]
        rows = {
            name: sorted(db.execute(f'SELECT * FROM "{name}"').fetchall(), key=repr)  # noqa: S608
            for name in tables
        }
    return {"user_version": version, "schema": schema, "tables": tables, "rows": rows}


def as_dicts(rendered_entities: list[Any]) -> list[dict[str, Any]]:
    return [entity.to_dict() for entity in rendered_entities]


@pytest.fixture
def db_file(tmp_path: Path) -> Path:
    """A copy of b25b311's database where ddiver's settings put it (DATA_DIR/ddiver.sqlite3)."""
    target = tmp_path / "data" / "ddiver.sqlite3"
    target.parent.mkdir()
    shutil.copyfile(DATA / "ddiver.sqlite3", target)
    return target


@pytest.fixture
async def opened(db_file: Path) -> Any:
    store = Storage(db_file, owner_id=OWNER, clock=FakeClock(NOW))
    await store.open()
    yield store
    await store.close()


class NoTelegram:
    """Enough of a Telethon client for ``Application``: handlers register, nothing connects."""

    def on(self, _event: object) -> Any:
        return lambda func: func

    async def disconnect(self) -> None:
        return None


async def outbox(store: Storage) -> list[OutboxItem]:
    cur = await store.db.execute("SELECT user_id, chat_id, msg_id FROM outbox ORDER BY rowid")
    keys = [(int(r[0]), int(r[1]), int(r[2])) for r in await cur.fetchall()]
    items = [await store.get_delivery(*key) for key in keys]
    return [item for item in items if item is not None]


class TestTheDatabase:
    """C1: the only step of the upgrade that could destroy data."""

    def test_the_fixture_is_what_production_holds_today(self, db_file):
        held = snapshot(db_file)
        assert held["user_version"] == 3
        assert held["rows"]["stats_daily"]
        assert {"users", "chats", "rules", "outbox", "stats_daily"} <= set(held["tables"])
        assert "schema_tracks" not in held["tables"]

    def test_this_build_brings_no_core_migration(self):
        """A fourth core migration would change this file on the first start. It may come, but
        not unnoticed: this test, and the PR's backup step, are then the ones to revisit."""
        assert len(storage_module._MIGRATIONS) == 3

    async def test_ddiver_starts_and_stops_on_it_and_changes_nothing(
        self, db_file, monkeypatch, clock
    ):
        env = {
            "API_ID": "1",
            "API_HASH": "0" * 32,
            "BOT_TOKEN": "123456789:AAExampleTokenWithEnoughLength_xyz",
            "OWNER_ID": str(OWNER),
            "DATA_DIR": str(db_file.parent),
        }
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setattr(runtime, "make_user_client", lambda *_a, **_k: NoTelegram())
        monkeypatch.setattr(runtime, "make_bot_client", lambda *_a, **_k: NoTelegram())
        before = snapshot(db_file)

        app = runtime.Application(Settings.load(None), clock=clock)
        assert app.settings.db_path == db_file
        assert app.storage._tracks == {}  # ddiver's tables are the core's; it has no track
        await app.setup()
        try:
            assert await app.storage.schema_version() == 3
            assert await app.storage.track_version("anything") == 0
        finally:
            await app.close()

        after = snapshot(db_file)
        assert after["user_version"] == before["user_version"] == 3
        assert after["schema"] == before["schema"]
        assert "schema_tracks" not in after["tables"]
        assert after["rows"] == before["rows"]

    async def test_the_login_path_opens_it_the_same_way(self, db_file):
        """``ddiver login`` opens the file with a bare Storage to cache the dialogs."""
        before = snapshot(db_file)
        store = Storage(db_file, owner_id=OWNER)
        await store.open()
        await store.close()
        assert snapshot(db_file) == before


class TestTheOutbox:
    """C2: a row b25b311 wrote is read with defaults and renders exactly as it did."""

    async def test_every_field_a_b25b311_row_lacks_takes_its_default(self, opened):
        items = await outbox(opened)
        assert len(items) == len({tuple(n["key"]) for n in RENDERED["notifications"]})
        defaults = {f.name: f.default for f in dataclasses.fields(Payload)}
        for item in items:
            absent = set(defaults) - set(item.payload)
            assert absent == NEW_PAYLOAD_FIELDS, item.key
            payload = Payload.from_json(item.payload)
            assert {name: getattr(payload, name) for name in absent} == {
                name: defaults[name] for name in absent
            }
            assert payload.is_telegram
            # Written back (a resolved sender name does that), the row gains no key either.
            assert json.loads(json.dumps(payload.to_json())) == item.payload

    async def test_each_row_renders_as_b25b311_rendered_it(self, opened):
        items = {item.key: item for item in await outbox(opened)}
        checked = 0
        for view in RENDERED["notifications"]:
            item = items[tuple(view["key"])]
            rendered = render_notification(
                Payload.from_json(item.payload), lang=view["lang"], tz=view["tz"], now=NOW
            )
            where = (view["key"], view["lang"])
            assert rendered.text == view["text"], where
            assert as_dicts(rendered.entities) == view["entities"], where
            keyboard = menus.notification_buttons(
                item.chat_id, item.msg_id, item.payload.get("sender_id"), item.rule_id
            )
            assert [[list(b) for b in row] for row in keyboard] == view["buttons"], where
            checked += 1
        assert checked == 36

    async def test_each_backlog_digests_as_b25b311_digested_it(self, opened):
        pending = await opened.pending_deliveries()
        checked = 0
        for view in RENDERED["digests"]:
            batch = [item for item in pending if item.user_id == view["user_id"]]
            assert [list(item.key) for item in batch] == view["keys"]
            rendered = render_digest(
                [DigestEntry(Payload.from_json(i.payload), i.key) for i in batch],
                lang=view["lang"],
                tz=view["tz"],
                now=NOW,
                overflow=view["overflow"],
            )
            where = (view["user_id"], view["lang"], view["overflow"])
            assert rendered.text == view["text"], where
            assert as_dicts(rendered.entities) == view["entities"], where
            checked += 1
        assert checked == 12

    async def test_delivery_sends_a_moderators_backlog_as_b25b311_rendered_it(self, opened):
        """End to end for the moderator, who never gets a digest: row → queue → message."""
        mod = await opened.get_user(MOD)
        assert mod is not None
        assert (mod.lang, mod.tz, mod.digest_threshold) == ("uk", "America/New_York", 0)
        expected = [
            view
            for view in RENDERED["notifications"]
            if view["key"][0] == MOD and view["lang"] == "uk"
        ]
        bot = FakeBotClient()
        moment = [0.0]

        async def no_wait(seconds: float) -> None:
            moment[0] += seconds

        delivery = Delivery(
            bot,
            opened,
            clock=FakeClock(NOW),
            monotonic=lambda: moment[0],
            sleep=no_wait,
            buttons=notification_buttons_factory(),
        )
        await delivery.replay_pending(quiet=True)
        assert await delivery.wait_idle()
        sent = [message for message in bot.sent if message.user_id == MOD]
        assert [m.text for m in sent] == [view["text"] for view in expected]
        assert [as_dicts(m.entities) for m in sent] == [view["entities"] for view in expected]
        for message, view in zip(sent, expected, strict=True):
            drawn = [[[b.text, b.type.data.decode()] for b in row] for row in message.buttons]
            assert drawn == [[[t("uk", key), data] for key, data in row] for row in view["buttons"]]
            assert message.link_preview is False
        await delivery.drain(timeout=1)


class TestTheExport:
    """C3: an /export b25b311 handed out is still an export this build reads and imports."""

    @pytest.mark.parametrize("user_id", sorted(EXPORTS))
    def test_it_is_read_with_the_default_tag(self, user_id):
        raw = EXPORTS[user_id].read_bytes()
        assert portable.FORMAT == "ddiver.config"
        data = portable.loads(raw)
        assert data == json.loads(raw)
        assert data["format"] == "ddiver.config"

    @pytest.mark.parametrize("user_id", sorted(EXPORTS))
    async def test_it_imports_through_the_bot_and_exports_back_the_same(
        self, tmp_path, db_file, user_id
    ):
        """A reinstall: a fresh file that knows the dialogs, then /import with the old file."""
        store = Storage(tmp_path / "fresh.sqlite3", owner_id=OWNER, clock=FakeClock(NOW))
        await store.open()
        try:
            await self._know_the_dialogs_of(db_file, store)
            await store.ensure_user(OWNER)
            await store.ensure_user(MOD)
            await store.update_user(MOD, role=Role.ALLOWED, lang="uk")
            raw = EXPORTS[user_id].read_bytes()
            original = json.loads(raw)
            router = build_router(store, clock=FakeClock(NOW))

            await router.dispatch(Incoming(user_id=user_id, text="/import"))
            [reply] = await router.dispatch(
                Incoming(user_id=user_id, document=raw, document_name="ddiver-2026-09-20.json")
            )

            lang = "en" if user_id == OWNER else "uk"
            assert reply == Reply(
                t(
                    lang,
                    "bot.import.done",
                    chats=sum(1 for chat in original["chats"] if chat["subscribed"]),
                    rules=len(original["rules"]),
                    ignored=len(original["ignored"]),
                ),
                literal=True,
            )
            again = await portable.export_config(
                store, user_id, now=dt.datetime.fromisoformat(original["exported_at"])
            )
            assert portable.dumps(again) == raw.decode("utf-8")
        finally:
            await store.close()

    @staticmethod
    async def _know_the_dialogs_of(source: Path, store: Storage) -> None:
        """What ``ddiver login`` caches on a new box: the chats, not anybody's configuration."""
        with contextlib.closing(sqlite3.connect(source)) as db:
            rows = db.execute(
                "SELECT id, title, username, kind, is_forum, shared, available FROM chats"
            ).fetchall()
        for chat_id, title, username, kind, is_forum, shared, available in rows:
            await store.upsert_chat(ChatRef(chat_id, title, username, kind, bool(is_forum)))
            if shared:
                await store.set_chat_shared(chat_id, True)
            if not available:
                await store.set_chat_available(chat_id, False)


def _b25b311_is_reachable() -> bool:
    git = shutil.which("git")
    if git is None or not (ROOT / ".git").exists():
        return False
    found = subprocess.run(  # noqa: S603 - git with fixed arguments
        [git, "-C", str(ROOT), "cat-file", "-e", f"{B25B311}^{{commit}}"],
        capture_output=True,
        check=False,
    )
    return found.returncode == 0


def _need_b25b311(*, reachable: bool, in_ci: bool) -> None:
    """Skip where b25b311 cannot be had: a `git archive` of this tree, a shallow clone. Never
    in CI, where a skip would leave every test above checking committed files that nothing
    ties to b25b311 any more; the check job fetches the whole history for this."""
    if reachable:
        return
    if in_ci:
        pytest.fail("b25b311 is not in this clone; the check job must check out with fetch-depth 0")
    pytest.skip("b25b311 is not in this clone")


def test_the_fixtures_are_still_exactly_what_b25b311_writes(tmp_path):
    """Rebuild everything from b25b311's own source and compare with what is committed."""
    _need_b25b311(reachable=_b25b311_is_reachable(), in_ci=bool(os.environ.get("CI")))
    git = shutil.which("git")
    assert git is not None
    archive = subprocess.run(  # noqa: S603 - git with fixed arguments
        [git, "-C", str(ROOT), "archive", B25B311], capture_output=True, check=True
    ).stdout
    source = tmp_path / "b25b311"
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(source, filter="data")
    out = tmp_path / "out"
    subprocess.run(  # noqa: S603 - our own interpreter running our own script
        [sys.executable, str(BUILD), str(source), str(out)], check=True, timeout=120
    )
    for name in ("rendered.json", "export-owner.json", "export-moderator.json"):
        assert (out / name).read_bytes() == (DATA / name).read_bytes(), name
    assert snapshot(out / "ddiver.sqlite3") == snapshot(DATA / "ddiver.sqlite3")


class TestWhereB25b311IsMissing:
    """The test above once skipped in every CI run, because checkout fetched one commit."""

    def test_it_runs_wherever_b25b311_is_there(self):
        _need_b25b311(reachable=True, in_ci=True)
        _need_b25b311(reachable=True, in_ci=False)

    def test_ci_fails_rather_than_skips(self):
        with pytest.raises(pytest.fail.Exception, match="fetch-depth 0"):
            _need_b25b311(reachable=False, in_ci=True)

    def test_a_copy_without_the_history_skips(self):
        with pytest.raises(pytest.skip.Exception):
            _need_b25b311(reachable=False, in_ci=False)
