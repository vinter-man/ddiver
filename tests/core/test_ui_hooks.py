"""What an app may change in the bot without forking the handlers, and that ddiver changes none.

ddiver-fb hides the bots toggle (no Facebook post is sent by a Telegram bot), turns the owner's
"refresh" into a request to the collector, writes its own /status and adds owner commands.
Each hook is off by default, and off means the screens, callbacks and strings ddiver has
always had.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from ddiver_core.bot import menus
from ddiver_core.bot.handlers import MAX_REPLY_CHARS, StatusInfo, UiHooks, build_router
from ddiver_core.bot.router import COMMANDS, Action, Context, Edit, Incoming, Reply, Router, Toast
from ddiver_core.i18n import t
from ddiver_core.links import ChatRef
from ddiver_core.storage import Role, Storage

OWNER = 1
MOD = 2
GROUP = ChatRef(1234567890, "Varna Rentals", None, "group")
OTHER = ChatRef(2345678901, "Robota Sofia", None, "group")


@pytest.fixture
async def store(tmp_path, clock) -> AsyncIterator[Storage]:
    s = Storage(tmp_path / "db.sqlite3", owner_id=OWNER, clock=clock)
    await s.open()
    await s.ensure_user(OWNER)
    await s.update_user(OWNER, lang="en", tz="UTC")
    await s.ensure_user(MOD)
    await s.update_user(MOD, role=Role.ALLOWED, lang="uk", tz="Europe/Kyiv")
    await s.upsert_chat(GROUP)
    await s.upsert_chat(OTHER)
    await s.set_chat_shared(GROUP.id, True)
    yield s
    await s.close()


@dataclass
class Refresher:
    """Stands in for ddiver-fb queueing a refresh_groups command for the collector."""

    calls: list[Context] = field(default_factory=list)

    async def __call__(self, ctx: Context) -> list[Action]:
        self.calls.append(ctx)
        return [Toast("🔄 Asked the collector to re-read the groups.", literal=True)]


async def cb(router: Router, user_id: int, data: str) -> list[Action]:
    return await router.dispatch(Incoming(user_id=user_id, data=data))


async def msg(router: Router, user_id: int, text: str, **kw: Any) -> list[Action]:
    return await router.dispatch(Incoming(user_id=user_id, text=text, **kw))


def callbacks(actions: list[Action]) -> list[str]:
    return [data for a in actions for row in getattr(a, "buttons", None) or [] for _, data in row]


def labels(actions: list[Action]) -> list[str]:
    return [label for a in actions for row in getattr(a, "buttons", None) or [] for label, _ in row]


class TestDefaultsAreDdiver:
    """No hook set: every screen an app could change is what ddiver has always drawn."""

    SCRIPT = (
        ("cb", OWNER, "menu"),
        ("cb", OWNER, "chats:0"),
        ("cb", MOD, "chats:0"),
        ("cb", OWNER, "shared:0"),
        ("cb", OWNER, f"share:{OTHER.id}:1"),
        ("cb", OWNER, f"sub:{GROUP.id}:1:0"),
        ("cb", OWNER, "set:main"),
        ("cb", OWNER, "set:bots"),
        ("cb", OWNER, "set:bots"),
        ("cb", MOD, "set:weekly"),
        ("cb", MOD, "set:backup"),
        ("msg", OWNER, "/status"),
        ("cb", MOD, "status"),
        ("cb", OWNER, "refresh"),
        ("msg", OWNER, "/resume"),
    )

    async def run(self, router: Router) -> list[list[Action]]:
        out = []
        for kind, user, payload in self.SCRIPT:
            send = cb if kind == "cb" else msg
            out.append(await send(router, user, payload))
        return out

    async def test_an_explicit_default_hooks_object_changes_nothing(self, tmp_path, clock):
        """Two identical databases, one router built without hooks and one with UiHooks()."""
        results = []
        for name, hooks in (("a", None), ("b", UiHooks())):
            store = Storage(tmp_path / f"{name}.sqlite3", owner_id=OWNER, clock=clock)
            await store.open()
            try:
                await store.ensure_user(OWNER)
                await store.update_user(OWNER, lang="en", tz="UTC")
                await store.ensure_user(MOD)
                await store.update_user(MOD, role=Role.ALLOWED, lang="uk", tz="UTC")
                await store.upsert_chat(GROUP)
                await store.upsert_chat(OTHER)
                await store.set_chat_shared(GROUP.id, True)
                results.append(await self.run(build_router(store, clock=clock, hooks=hooks)))
            finally:
                await store.close()
        assert results[0] == results[1]

    async def test_no_refresh_button_anywhere_and_no_refresh_callback(self, store, clock):
        router = build_router(store, clock=clock)
        seen: list[str] = []
        for data in ("chats:0", "shared:0", f"sub:{GROUP.id}:1:0"):
            seen += callbacks(await cb(router, OWNER, data))
        assert "refresh" not in seen
        assert await cb(router, OWNER, "refresh") == [Toast("bot.err.unknown_action")]

    async def test_the_bots_toggle_is_there_and_works(self, store, clock):
        router = build_router(store, clock=clock)
        actions = await cb(router, OWNER, "set:bots")
        assert "set:bots" in callbacks(actions)
        assert (await store.get_user(OWNER)).include_bots is True

    async def test_status_is_the_listener_card(self, store, clock):
        async def status() -> StatusInfo:
            return StatusInfo(listener_ok=False)

        router = build_router(store, clock=clock, status=status)
        [reply] = await msg(router, OWNER, "/status")
        assert t("en", "bot.status.listener_degraded") in reply.key_or_text

    async def test_an_unknown_command_is_still_unknown(self, store, clock):
        router = build_router(store, clock=clock)
        assert await msg(router, OWNER, "/resume") == [Reply("bot.err.unknown_command")]


class TestTelegramSettingsHidden:
    @pytest.fixture
    def router(self, store, clock) -> Router:
        return build_router(store, clock=clock, hooks=UiHooks(telegram_settings=False))

    @pytest.mark.parametrize(
        ("user", "lang", "expected"),
        [
            (OWNER, "en", "Language: English · Timezone: UTC · Digest threshold: 20"),
            (
                MOD,
                "uk",
                "Мова: Українська · Часовий пояс: Europe/Kyiv · Поріг дайджесту: 20",
            ),
        ],
    )
    async def test_the_settings_say_nothing_about_bots(self, router, user, lang, expected):
        [edit] = await cb(router, user, "set:main")
        assert isinstance(edit, Edit)
        assert edit.key_or_text == expected
        assert "set:bots" not in callbacks([edit])
        assert labels([edit]) == [
            "bot.settings.language",
            "bot.settings.timezone",
            "bot.settings.digest",
            "bot.settings.weekly_on",
            "bot.settings.backup_on",
            "bot.btn.back",
        ]

    async def test_a_forged_toggle_changes_nothing_and_shows_the_settings(self, router, store):
        actions = await cb(router, OWNER, "set:bots")
        assert (await store.get_user(OWNER)).include_bots is False
        assert [type(a) for a in actions] == [Edit]  # no "now matching bot messages" toast
        assert "set:bots" not in callbacks(actions)

    async def test_the_other_settings_still_work(self, router):
        actions = await cb(router, OWNER, "set:lang")
        assert "lang:uk" in callbacks(actions)


class TestWeeklySettings:
    """The core stores the weekly toggles; the task that reads them is the app's."""

    async def test_ddiver_offers_both_and_they_toggle(self, store, clock):
        router = build_router(store, clock=clock)
        [edit] = await cb(router, MOD, "set:main")
        assert {"set:weekly", "set:backup"} <= set(callbacks([edit]))
        toast, edit = await cb(router, MOD, "set:weekly")
        assert toast == Toast("bot.settings.off_hint")
        assert "bot.settings.weekly_off" in labels([edit])
        await cb(router, MOD, "set:backup")
        user = await store.get_user(MOD)
        assert (user.weekly_report, user.auto_backup) == (False, False)

    async def test_an_app_without_a_weekly_task_hides_them(self, store, clock):
        router = build_router(store, clock=clock, hooks=UiHooks(weekly_settings=False))
        [edit] = await cb(router, OWNER, "set:main")
        assert labels([edit]) == [
            "bot.settings.language",
            "bot.settings.timezone",
            "bot.settings.digest",
            "bot.settings.bots_off",
            "bot.btn.back",
        ]

    @pytest.mark.parametrize("data", ["set:weekly", "set:backup"])
    async def test_a_forged_toggle_changes_nothing(self, store, clock, data):
        router = build_router(store, clock=clock, hooks=UiHooks(weekly_settings=False))
        actions = await cb(router, OWNER, data)
        assert [type(a) for a in actions] == [Edit]  # no toast: nothing was switched
        assert not {"set:weekly", "set:backup"} & set(callbacks(actions))
        user = await store.get_user(OWNER)
        assert (user.weekly_report, user.auto_backup) == (True, True)


class TestRefreshChats:
    @pytest.fixture
    def refresher(self) -> Refresher:
        return Refresher()

    @pytest.fixture
    def router(self, store, clock, refresher) -> Router:
        return build_router(store, clock=clock, hooks=UiHooks(refresh_chats=refresher))

    async def test_the_owner_sees_the_button_on_every_chat_list(self, router):
        for data in ("chats:0", "shared:0", f"sub:{GROUP.id}:1:0", f"share:{OTHER.id}:1"):
            actions = await cb(router, OWNER, data)
            assert "refresh" in callbacks(actions), data
            assert "bot.btn.refresh_chats" in labels(actions)

    async def test_search_results_carry_it_too(self, router):
        await cb(router, OWNER, "chats:search")
        actions = await msg(router, OWNER, "varna")
        assert "refresh" in callbacks(actions)

    async def test_the_button_sits_above_search_and_back(self, router):
        [edit] = await cb(router, OWNER, "chats:0")
        assert edit.buttons[-2:] == [
            list(menus.REFRESH_ROW),
            [("bot.btn.search", "chats:search"), ("bot.btn.back", "menu")],
        ]

    async def test_an_empty_list_is_where_the_owner_needs_it_most(self, tmp_path, clock):
        store = Storage(tmp_path / "empty.sqlite3", owner_id=OWNER, clock=clock)
        await store.open()
        try:
            await store.ensure_user(OWNER)
            router = build_router(store, clock=clock, hooks=UiHooks(refresh_chats=Refresher()))
            [edit] = await cb(router, OWNER, "chats:0")
            assert edit.key_or_text == "bot.chats.empty"
            assert edit.buttons == [list(menus.REFRESH_ROW), [("bot.btn.back", "menu")]]
        finally:
            await store.close()

    async def test_a_moderator_never_sees_it(self, router):
        for data in ("chats:0", f"sub:{GROUP.id}:1:0"):
            assert "refresh" not in callbacks(await cb(router, MOD, data))

    async def test_pressing_it_calls_the_app_and_answers_with_its_actions(self, router, refresher):
        actions = await cb(router, OWNER, "refresh")
        assert actions == [Toast("🔄 Asked the collector to re-read the groups.", literal=True)]
        [ctx] = refresher.calls
        assert ctx.user_id == OWNER and ctx.is_owner and ctx.lang == "en"

    async def test_a_moderator_cannot_press_it_by_forging_the_callback(self, router, refresher):
        assert await cb(router, MOD, "refresh") == [Toast("bot.err.owner_only", alert=True)]
        assert refresher.calls == []

    async def test_the_moderators_empty_list_is_unchanged(self, tmp_path, clock):
        store = Storage(tmp_path / "mod.sqlite3", owner_id=OWNER, clock=clock)
        await store.open()
        try:
            await store.ensure_user(MOD)
            await store.update_user(MOD, role=Role.ALLOWED)
            router = build_router(store, clock=clock, hooks=UiHooks(refresh_chats=Refresher()))
            assert await cb(router, MOD, "chats:0") == [
                Edit("bot.chats.none_shared", buttons=[[("bot.btn.back", "menu")]])
            ]
        finally:
            await store.close()


class TestStatusText:
    async def test_the_apps_text_replaces_the_listener_card(self, store, clock):
        asked: list[tuple[int, str, bool]] = []

        async def status() -> StatusInfo:
            raise AssertionError("the listener card must not be built")

        async def fb_status(ctx: Context) -> str:
            asked.append((ctx.user_id, ctx.lang, ctx.is_owner))
            return f"Collector: OK ({ctx.lang})"

        router = build_router(
            store, clock=clock, status=status, hooks=UiHooks(status_text=fb_status)
        )
        assert await msg(router, OWNER, "/status") == [Reply("Collector: OK (en)", literal=True)]
        assert await cb(router, MOD, "status") == [
            Edit("Collector: OK (uk)", literal=True, buttons=[[("bot.btn.back", "menu")]])
        ]
        assert asked == [(OWNER, "en", True), (MOD, "uk", False)]

    async def test_an_overlong_text_is_clamped_to_what_telegram_accepts(self, store, clock):
        async def chatty(ctx: Context) -> str:
            return "x" * 10_000

        router = build_router(store, clock=clock, hooks=UiHooks(status_text=chatty))
        [reply] = await msg(router, OWNER, "/status")
        assert len(reply.key_or_text) == MAX_REPLY_CHARS
        assert reply.key_or_text.endswith("…")


class TestAppCommands:
    @pytest.fixture
    def router(self, store, clock) -> Router:
        router = build_router(store, clock=clock)

        @router.command("resume")
        async def resume(ctx: Context) -> list[Action]:
            return [Reply(f"resume {ctx.incoming.args or '-'}", literal=True)]

        return router

    async def test_a_registered_command_is_dispatched(self, router):
        assert await msg(router, OWNER, "/resume b-17") == [Reply("resume b-17", literal=True)]

    @pytest.mark.parametrize("text", ["/RESUME", "/resume@ddiver_fb_bot", "/Resume@X now"])
    async def test_it_is_matched_like_the_built_in_ones(self, router, text):
        [reply] = await msg(router, OWNER, text)
        assert reply.key_or_text.startswith("resume")

    async def test_it_ends_pending_input_like_any_command(self, router):
        await msg(router, OWNER, "/import")
        assert await msg(router, OWNER, "/resume") == [Reply("resume -", literal=True)]
        # and the next message is not read as a file for /import any more
        assert await msg(router, OWNER, "hello") == [
            Reply("bot.err.not_waiting", buttons=menus.main_menu(is_owner=True))
        ]

    async def test_the_built_in_commands_are_untouched(self, router):
        assert await msg(router, OWNER, "/cancel") == [Reply("bot.cancelled")]
        assert "resume" not in COMMANDS

    async def test_other_unknown_commands_stay_unknown(self, router):
        assert await msg(router, OWNER, "/pause") == [Reply("bot.err.unknown_command")]

    async def test_an_open_command_is_open_to_moderators_too(self, router):
        assert await msg(router, MOD, "/resume") == [Reply("resume -", literal=True)]

    async def test_an_owner_only_command_refuses_a_moderator_before_the_handler(self, store, clock):
        router = build_router(store, clock=clock)
        entered: list[int] = []

        @router.command("lease", owner_only=True)
        async def lease(ctx: Context) -> list[Action]:
            entered.append(ctx.user_id)
            return [Reply("moved", literal=True)]

        assert await msg(router, MOD, "/lease home-2") == [Reply("bot.err.owner_only")]
        assert entered == []
        assert await msg(router, OWNER, "/lease home-2") == [Reply("moved", literal=True)]
        assert entered == [OWNER]

    async def test_a_refused_command_still_ends_pending_input(self, store, clock):
        router = build_router(store, clock=clock)

        @router.command("lease", owner_only=True)
        async def lease(ctx: Context) -> list[Action]:
            return []

        await msg(router, MOD, "/import")
        assert await msg(router, MOD, "/lease") == [Reply("bot.err.owner_only")]
        assert router.get_pending(MOD) is None

    async def test_a_bare_slash_is_not_a_command(self, router):
        assert Incoming(user_id=OWNER, text="/").command_name is None
        assert Incoming(user_id=OWNER, text=None).command_name is None
        assert Incoming(user_id=OWNER, text="/Resume@bot x").command_name == "resume"
