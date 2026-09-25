"""/export writes the app's format tag and /import accepts nothing else.

ddiver and ddiver-fb share the export layout, but a chat id means a Telegram chat in one and a
Facebook group in the other. A file carried from one bot to the other must be refused, with the
same answer a file that is not an export at all gets.
"""

import datetime as dt
import json
from typing import Any

import pytest

from ddiver_core import portable
from ddiver_core.bot.handlers import build_router
from ddiver_core.bot.router import Action, Incoming, Reply, Router, SendDocument
from ddiver_core.i18n import t
from ddiver_core.links import ChatRef
from ddiver_core.rules import RuleKind
from ddiver_core.storage import ALL_CHATS, Storage
from ddiver_core_testing import T0

FB = "ddiver-fb.config"
OWNER = 1
GROUP = ChatRef(1234567890, "Оренда Варна", None, "group")


def config(tag: object = FB, **extra: object) -> dict[str, object]:
    return {
        "format": tag,
        "version": portable.VERSION,
        "rules": [{"chat_id": 0, "kind": "include", "pattern": "квартира"}],
        **extra,
    }


class TestAppName:
    @pytest.mark.parametrize(
        ("tag", "name"),
        [("ddiver.config", "ddiver"), (FB, "ddiver-fb"), ("x.config", "x")],
    )
    def test_the_tag_names_the_app(self, tag, name):
        assert portable.app_name(tag) == name

    @pytest.mark.parametrize(
        "tag",
        [
            "",
            "ddiver",
            "Ddiver.config",
            "ddiver.config.json",
            "../ddiver.config",
            "ddiver fb.config",
            "-ddiver.config",
            "a" * 33 + ".config",
        ],
    )
    def test_a_tag_of_another_shape_is_a_programming_error(self, tag):
        with pytest.raises(ValueError, match="bad export format tag"):
            portable.app_name(tag)


class TestLoads:
    def test_the_apps_own_file_is_read(self):
        data = portable.loads(json.dumps(config()), format_tag=FB)
        assert data["rules"][0]["pattern"] == "квартира"

    def test_the_default_is_still_ddiver(self):
        assert portable.loads(json.dumps(config(portable.FORMAT)))["format"] == "ddiver.config"

    @pytest.mark.parametrize(
        "document",
        [
            config(portable.FORMAT),  # a ddiver export carried over to ddiver-fb
            config("DDIVER-FB.config"),
            config(None),
            {"rules": []},  # no tag at all
            ["not", "an", "object"],
            {"format": {"nested": FB}},
        ],
    )
    def test_anything_else_gets_the_one_answer_a_stranger_file_gets(self, document):
        with pytest.raises(portable.BadExportError) as caught:
            portable.loads(json.dumps(document), format_tag=FB)
        assert str(caught.value) == "not a ddiver-fb export"

    def test_ddiver_refuses_a_ddiver_fb_file_the_same_way(self):
        with pytest.raises(portable.BadExportError) as caught:
            portable.loads(json.dumps(config(FB)))
        assert str(caught.value) == "not a ddiver export"

    def test_a_newer_version_names_the_app(self):
        raw = json.dumps(config(version=portable.VERSION + 1))
        with pytest.raises(portable.BadExportError, match="made by a newer ddiver-fb"):
            portable.loads(raw, format_tag=FB)

    @pytest.mark.parametrize("as_bytes", [True, False])
    def test_an_oversized_file_names_the_app(self, as_bytes):
        raw = " " * (portable.MAX_BYTES + 1)
        with pytest.raises(portable.BadExportError, match="too large to be a ddiver-fb export"):
            portable.loads(raw.encode() if as_bytes else raw, format_tag=FB)

    def test_a_malformed_tag_is_refused_before_the_document_is_read(self):
        with pytest.raises(ValueError, match="bad export format tag"):
            portable.loads(b"\xff not even utf-8", format_tag="fb")


class TestExportConfig:
    async def test_the_export_carries_the_tag_it_was_asked_for(self, tmp_path, clock):
        store = Storage(tmp_path / "db.sqlite3", owner_id=OWNER, clock=clock)
        await store.open()
        try:
            await store.ensure_user(OWNER)
            await store.add_rule(OWNER, ALL_CHATS, RuleKind.INCLUDE, "квартира")
            exported = await portable.export_config(store, OWNER, now=T0, format_tag=FB)
            assert exported["format"] == FB
            back = portable.loads(portable.dumps(exported), format_tag=FB)
            assert back["rules"] == [{"chat_id": 0, "kind": "include", "pattern": "квартира"}]
        finally:
            await store.close()

    async def test_a_malformed_tag_fails_before_the_database_is_read(self):
        class Untouchable:
            def __getattr__(self, name: str) -> object:
                raise AssertionError(f"storage.{name} was used")

        with pytest.raises(ValueError, match="bad export format tag"):
            await portable.export_config(Untouchable(), OWNER, now=T0, format_tag="fb")


@pytest.fixture
async def store(tmp_path, clock):
    s = Storage(tmp_path / "db.sqlite3", owner_id=OWNER, clock=clock)
    await s.open()
    await s.ensure_user(OWNER)
    await s.update_user(OWNER, lang="en", tz="UTC")
    await s.upsert_chat(GROUP)
    yield s
    await s.close()


async def say(router: Router, text: str | None = None, **kw: Any) -> list[Action]:
    return await router.dispatch(Incoming(user_id=OWNER, text=text, **kw))


class TestRouter:
    async def test_export_names_the_file_after_the_app_and_writes_its_tag(self, store, clock):
        router = build_router(store, clock=clock, export_format=FB)
        await store.add_rule(OWNER, ALL_CHATS, RuleKind.INCLUDE, "квартира")
        clock.now = dt.datetime(2026, 10, 1, 9, 30, tzinfo=dt.UTC)
        [doc] = await say(router, "/export")
        assert isinstance(doc, SendDocument)
        assert doc.filename == "ddiver-fb-2026-10-01.json"
        assert json.loads(doc.content)["format"] == FB

    async def test_ddiver_keeps_its_file_name(self, store, clock):
        [doc] = await say(build_router(store, clock=clock), "/export")
        assert doc.filename == f"ddiver-{T0:%Y-%m-%d}.json"
        assert json.loads(doc.content)["format"] == "ddiver.config"

    async def test_a_ddiver_export_is_refused_by_the_fb_bot_and_nothing_lands(self, store, clock):
        ddiver = build_router(store, clock=clock)
        await store.add_rule(OWNER, ALL_CHATS, RuleKind.INCLUDE, "такси")
        [doc] = await say(ddiver, "/export")
        for rule in await store.list_rules(OWNER):
            await store.delete_rule(rule.id, OWNER)

        fb = build_router(store, clock=clock, export_format=FB)
        await say(fb, "/import")
        [reply] = await say(fb, document=doc.content, document_name=doc.filename)
        assert isinstance(reply, Reply)
        assert reply.key_or_text == t("en", "bot.import.bad_file", reason="not a ddiver-fb export")
        assert await store.list_rules(OWNER) == []

    async def test_an_fb_export_is_refused_by_ddiver(self, store, clock):
        ddiver = build_router(store, clock=clock)
        await say(ddiver, "/import")
        [reply] = await say(ddiver, json.dumps(config(FB)))
        assert "not a ddiver export" in reply.key_or_text
        assert await store.list_rules(OWNER) == []

    async def test_the_fb_bot_reads_back_its_own_export(self, store, clock):
        fb = build_router(store, clock=clock, export_format=FB)
        await store.subscribe(OWNER, GROUP.id)
        await store.add_rule(OWNER, GROUP.id, RuleKind.INCLUDE, "квартира")
        [doc] = await say(fb, "/export")
        for rule in await store.list_rules(OWNER):
            await store.delete_rule(rule.id, OWNER)

        await say(fb, "/import")
        await say(fb, document=doc.content, document_name=doc.filename)
        assert [(r.chat_id, r.pattern) for r in await store.list_rules(OWNER)] == [
            (GROUP.id, "квартира")
        ]

    async def test_a_malformed_tag_fails_when_the_router_is_built(self, store):
        with pytest.raises(ValueError, match="bad export format tag"):
            build_router(store, export_format="ddiver-fb")
