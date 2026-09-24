import asyncio

import pytest
import regex

import ddiver.matcher as matcher_module
from ddiver.links import ChatRef
from ddiver.matcher import TIMEOUTS_BEFORE_DISABLE, Matcher
from ddiver.rules import CompiledRule, MatchMode, RuleKind
from ddiver.storage import ALL_CHATS, Role, Storage

CHAT_A = ChatRef(-1001, "Alpha", "alpha", "supergroup")
CHAT_B = ChatRef(-1002, "Beta", None, "supergroup")


@pytest.fixture
async def store(tmp_path):
    s = Storage(tmp_path / "db.sqlite3", owner_id=1)
    await s.open()
    await s.ensure_user(1)
    await s.ensure_user(2)
    await s.update_user(2, role=Role.ALLOWED)
    await s.upsert_chat(CHAT_A)
    await s.upsert_chat(CHAT_B)
    await s.set_chat_shared(CHAT_A.id, True)  # moderators only see chats the owner shared
    await s.set_chat_shared(CHAT_B.id, True)
    await s.subscribe(1, CHAT_A.id)
    await s.subscribe(2, CHAT_A.id)
    await s.subscribe(1, CHAT_B.id)
    yield s
    await s.close()


@pytest.fixture
async def matcher(store):
    m = Matcher(store, timeout=0.2)
    await m.reload()
    return m


async def test_monitored_set_and_fast_path(store, matcher):
    assert matcher.monitored == {CHAT_A.id, CHAT_B.id}
    assert matcher.is_monitored(CHAT_A.id) and not matcher.is_monitored(-5)
    assert await matcher.evaluate(-5, "казино", 10) == []
    assert await matcher.evaluate(CHAT_A.id, "", 10) == []
    assert matcher.stats_evaluated == 0


async def test_one_match_per_user_chat_specific_before_global(store, matcher):
    g = await store.add_rule(1, ALL_CHATS, RuleKind.INCLUDE, "казино")
    c = await store.add_rule(1, CHAT_A.id, RuleKind.INCLUDE, "=казино")
    await store.add_rule(2, CHAT_A.id, RuleKind.INCLUDE, "re:каз\\w+")
    await matcher.invalidate()
    matches = await matcher.evaluate(CHAT_A.id, "Заходи в КАЗИНО", 10)
    assert {(m.user_id, m.rule.id) for m in matches} == {(1, c.id), (2, matches[1].rule.id)}
    assert "Заходи в КАЗИНО"[matches[0].span.start : matches[0].span.end] == "КАЗИНО"
    # in chat B only user 1 is subscribed and only the global rule applies
    matches_b = await matcher.evaluate(CHAT_B.id, "казино!", 10)
    assert [(m.user_id, m.rule.id) for m in matches_b] == [(1, g.id)]
    assert matcher.stats_matched == 2


async def test_exclusion_beats_inclusion(store, matcher):
    await store.add_rule(1, CHAT_A.id, RuleKind.INCLUDE, "казино")
    await store.add_rule(1, ALL_CHATS, RuleKind.EXCLUDE, "re:тест")
    await matcher.invalidate()
    assert await matcher.evaluate(CHAT_A.id, "казино тест", 10) == []
    assert len(await matcher.evaluate(CHAT_A.id, "казино", 10)) == 1


async def test_ignored_sender_per_chat_and_global(store, matcher):
    await store.add_rule(1, ALL_CHATS, RuleKind.INCLUDE, "спам")
    await store.add_ignored(1, CHAT_A.id, 500)
    await store.add_ignored(1, ALL_CHATS, 600)
    await matcher.invalidate()
    assert await matcher.evaluate(CHAT_A.id, "спам", 500) == []
    assert await matcher.evaluate(CHAT_B.id, "спам", 500) != []  # chat-scoped ignore
    assert await matcher.evaluate(CHAT_A.id, "спам", 600) == []
    assert await matcher.evaluate(CHAT_B.id, "спам", 600) == []
    assert await matcher.evaluate(CHAT_A.id, "спам", None) != []  # unknown sender is not ignored


async def test_invalidate_single_chat_refreshes_only_that_chat(store, matcher):
    await store.add_rule(1, ALL_CHATS, RuleKind.INCLUDE, "one")
    await matcher.invalidate()
    assert await matcher.evaluate(CHAT_A.id, "one", 1) != []
    assert await matcher.evaluate(CHAT_B.id, "one", 1) != []
    await store.add_rule(1, ALL_CHATS, RuleKind.INCLUDE, "two")
    await matcher.invalidate(CHAT_A.id)
    assert await matcher.evaluate(CHAT_A.id, "two", 1) != []
    assert await matcher.evaluate(CHAT_B.id, "two", 1) == []  # still cached without 'two'


async def test_unsubscribe_removes_chat_from_monitored(store, matcher):
    await store.unsubscribe(1, CHAT_B.id)
    await matcher.invalidate(CHAT_B.id)
    assert not matcher.is_monitored(CHAT_B.id)


async def test_invalid_stored_rule_is_disabled_with_hook(store):
    disabled = []

    async def hook(rule, reason):
        disabled.append((rule.id, reason))

    bad = await store.add_rule(1, CHAT_A.id, RuleKind.INCLUDE, "re:(unclosed")
    good = await store.add_rule(1, CHAT_A.id, RuleKind.INCLUDE, "ok")
    m = Matcher(store, timeout=0.2, on_rule_disabled=hook)
    await m.reload()
    matches = await m.evaluate(CHAT_A.id, "ok (unclosed", 1)
    assert [x.rule.id for x in matches] == [good.id]
    assert disabled == [(bad.id, "invalid:rule.err.invalid_regex")]
    row = await store.get_rule(bad.id, 1)
    assert row is not None and row.disabled_reason == "invalid:rule.err.invalid_regex"


async def test_timeout_counts_and_disables_after_threshold(store, monkeypatch):
    disabled = []

    async def hook(rule, reason):
        disabled.append((rule.id, reason))

    slow = await store.add_rule(1, CHAT_A.id, RuleKind.INCLUDE, "re:slow")
    m = Matcher(store, timeout=0.02, on_rule_disabled=hook)
    await m.reload()
    # Replace compilation with a genuinely catastrophic pattern that validation would reject.
    monkeypatch.setattr(
        "ddiver.matcher.compile_rule",
        lambda raw, timeout: CompiledRule(raw, MatchMode.REGEX, regex.compile("((a|aa)+)+b")),
    )
    for i in range(TIMEOUTS_BEFORE_DISABLE):
        assert await m.evaluate(CHAT_A.id, "a" * 40, 1) == []
        assert m.stats_timeouts == i + 1
    assert disabled == [(slow.id, "timeout")]
    row = await store.get_rule(slow.id, 1)
    assert row is not None and row.disabled_reason == "timeout"
    # after disabling, the rule is gone from the (rebuilt) rule set
    monkeypatch.undo()
    assert await m.evaluate(CHAT_A.id, "slow", 1) == []


class TestOffloadAndBudget:
    async def test_a_big_rule_set_is_matched_off_the_event_loop(self, store, matcher, monkeypatch):
        """A hostile moderator with hundreds of slow rules must not freeze everything.

        Asserted on the mechanism rather than on timing: a wall-clock test here passed even
        with the offload removed, because awaiting the ticker drove it to completion anyway.
        """
        import ddiver.matcher as matcher_module

        offloaded = []
        real_to_thread = asyncio.to_thread

        async def spy(func, *args):
            offloaded.append(func.__name__)
            return await real_to_thread(func, *args)

        monkeypatch.setattr(matcher_module.asyncio, "to_thread", spy)

        await store.add_rule(1, CHAT_A.id, RuleKind.INCLUDE, "needle")
        await matcher.invalidate()
        await matcher.evaluate(CHAT_A.id, "ничего похожего", 1)
        # Compiling always goes to a thread — it validates each pattern, which is several
        # timed searches — but matching one rule does not: the hop would cost more than it
        # saves.
        assert offloaded == ["_compile_all"]

        # A fixed count, not OFFLOAD_ABOVE_RULES + n: deriving it from the constant makes the
        # test follow the constant wherever it goes, so raising the threshold would not fail.
        assert matcher_module.OFFLOAD_ABOVE_RULES < 30, "the count below must clear the threshold"
        for i in range(30):
            await store.add_rule(1, CHAT_A.id, RuleKind.INCLUDE, f"needle{i}")
        await matcher.invalidate()
        await matcher.evaluate(CHAT_A.id, "ничего похожего " * 200, 1)
        assert offloaded[-1] == "_evaluate_sync"

    async def test_compiling_a_rule_set_never_runs_on_the_event_loop(self, store, matcher):
        """The first message into a chat used to stall everything while its rules compiled."""
        import ddiver.matcher as matcher_module

        seen = []
        real = asyncio.to_thread

        async def spy(func, *args, **kwargs):
            seen.append(func.__name__)
            return await real(func, *args, **kwargs)

        for i in range(40):
            await store.add_rule(1, CHAT_A.id, RuleKind.INCLUDE, f"re:слово{i}\\d+")
        await matcher.invalidate()
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(matcher_module.asyncio, "to_thread", spy)
        try:
            await matcher.evaluate(CHAT_A.id, "ничего", 1)
        finally:
            monkeypatch.undo()
        assert "_compile_all" in seen

    async def test_an_expensive_rule_set_is_bounded_rather_than_fatal(
        self, store, matcher, monkeypatch, caplog
    ):
        """Two hundred rules are allowed per chat, and each may be under the per-rule cap.

        They still add up to more memory than the container has, and every cached chat holds
        its own set — so the set itself needs a ceiling. Past it the rules are skipped, never
        disabled: a rule its owner did not ask to remove must not disappear because somebody
        else's patterns were expensive that minute.
        """
        monkeypatch.setattr(matcher_module, "MAX_RULESET_BYTES", 8 * 1024)
        for i in range(12):
            await store.add_rule(1, CHAT_A.id, RuleKind.INCLUDE, f"такси{i}")
        await matcher.invalidate()
        await matcher.evaluate(CHAT_A.id, "ничего", 1)

        assert matcher.stats_rules_skipped > 0
        assert "rules were not compiled" in caplog.text
        # skipped, not disabled: every rule is still there for the next load
        rules = await store.rules_for_chat(CHAT_A.id)
        assert all(r.disabled_reason is None for r in rules)

    async def test_budget_stops_a_pathological_rule_set(self, store, monkeypatch):
        m = Matcher(store, timeout=0.02)
        await m.reload()
        for i in range(30):
            await store.add_rule(1, CHAT_A.id, RuleKind.INCLUDE, f"re:slow{i}")
        monkeypatch.setattr(
            "ddiver.matcher.compile_rule",
            lambda raw, timeout: CompiledRule(raw, MatchMode.REGEX, regex.compile("((a|aa)+)+b")),
        )
        monkeypatch.setattr("ddiver.matcher.MESSAGE_BUDGET", 0.05)
        await m.invalidate()
        assert await m.evaluate(CHAT_A.id, "a" * 40, 1) == []
        assert m.stats_budget_exceeded >= 1

    async def test_small_rule_sets_stay_on_the_loop(self, store, matcher):
        await store.add_rule(1, CHAT_A.id, RuleKind.INCLUDE, "казино")
        await matcher.invalidate()
        assert len(await matcher.evaluate(CHAT_A.id, "казино", 1)) == 1


class TestCacheGeneration:
    """A rule edited while its chat is being compiled must not be lost."""

    async def test_an_edit_during_a_load_is_not_overwritten(self, store, matcher):
        """`_load` awaits twice — on storage and on the compile thread — and the bot runs on
        the same loop, so a moderator's tap lands in the middle of it. Caching the older read
        would hide that rule until the next invalidation, which for a quiet chat is days.
        """
        import ddiver.matcher as matcher_module

        await store.add_rule(1, CHAT_A.id, RuleKind.INCLUDE, "первое")
        await matcher.invalidate()

        landed = asyncio.Event()
        real = asyncio.to_thread

        async def interleave(func, *args, **kwargs):
            result = await real(func, *args, **kwargs)
            if func.__name__ == "_compile_all" and not landed.is_set():
                landed.set()
                # exactly what the bot does when somebody adds a rule
                await store.add_rule(1, CHAT_A.id, RuleKind.INCLUDE, "второе")
                await matcher.invalidate(CHAT_A.id)
            return result

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(matcher_module.asyncio, "to_thread", interleave)
        try:
            await matcher.evaluate(CHAT_A.id, "первое", 5)
        finally:
            monkeypatch.undo()

        assert landed.is_set(), "the interleaving never happened; the test proves nothing"
        assert await matcher.evaluate(CHAT_A.id, "второе", 5), "the new rule was lost"

    async def test_an_uninterrupted_load_is_still_cached(self, store, matcher):
        """The guard must not turn every evaluation into a reload."""
        await store.add_rule(1, CHAT_A.id, RuleKind.INCLUDE, "такси")
        await matcher.invalidate()
        loads = 0
        real_load = matcher._load

        async def counting(chat_id):
            nonlocal loads
            loads += 1
            return await real_load(chat_id)

        matcher._load = counting
        await matcher.evaluate(CHAT_A.id, "такси", 5)
        await matcher.evaluate(CHAT_A.id, "такси", 5)
        await matcher.evaluate(CHAT_A.id, "такси", 5)
        assert loads == 1
