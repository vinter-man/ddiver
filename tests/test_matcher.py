import pytest
import regex

from ddiver.links import ChatRef
from ddiver.matcher import TIMEOUTS_BEFORE_DISABLE, Matcher
from ddiver.rules import CompiledRule, MatchMode, RuleKind
from ddiver.storage import ALL_CHATS, Role, Storage

CHAT_A = ChatRef(-1001, "Alpha", "alpha", "supergroup")
CHAT_B = ChatRef(-1002, "Beta", None, "supergroup")


@pytest.fixture
async def store(tmp_path):
    s = Storage(tmp_path / "db.sqlite3")
    await s.open()
    await s.ensure_user(1, role=Role.OWNER)
    await s.ensure_user(2, role=Role.ALLOWED)
    await s.upsert_chat(CHAT_A)
    await s.upsert_chat(CHAT_B)
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
    row = await store.get_rule(bad.id)
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
    row = await store.get_rule(slow.id)
    assert row is not None and row.disabled_reason == "timeout"
    # after disabling, the rule is gone from the (rebuilt) rule set
    monkeypatch.undo()
    assert await m.evaluate(CHAT_A.id, "slow", 1) == []
