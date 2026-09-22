"""Per-chat rule sets for all subscribed users, with lazy compilation and invalidation."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from ddiver.rules import CompiledRule, RuleError, RuleKind, RuleTimeout, Span, compile_rule
from ddiver.storage import Rule, Storage

log = logging.getLogger(__name__)

TIMEOUTS_BEFORE_DISABLE = 3
OFFLOAD_ABOVE_RULES = 20
"""Above this many rules for one chat, matching moves off the event loop.

The ``regex`` module releases the GIL while matching, so a worker thread keeps the listener,
the bot UI and the heartbeat responsive even when somebody installs hundreds of patterns.
"""

MESSAGE_BUDGET = 0.5
"""Seconds of matching per message, after which the remaining rules are skipped."""
DISABLED_TIMEOUT = "timeout"
DISABLED_INVALID = "invalid"

RuleDisabledHook = Callable[[Rule, str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Match:
    user_id: int
    rule: Rule
    spans: list[Span]

    @property
    def span(self) -> Span:
        return self.spans[0]


@dataclass(slots=True)
class _UserRules:
    user_id: int
    includes: list[tuple[Rule, CompiledRule]] = field(default_factory=list)
    excludes: list[tuple[Rule, CompiledRule]] = field(default_factory=list)
    ignored: set[int] = field(default_factory=set)


@dataclass(slots=True)
class _ChatRuleSet:
    chat_id: int
    users: list[_UserRules]

    @property
    def rule_count(self) -> int:
        return sum(len(u.includes) + len(u.excludes) for u in self.users)


class Matcher:
    """Evaluates messages against every subscribed user's rules for a chat.

    The monitored-chat set and the compiled rule sets are cached; the bot UI calls
    :meth:`invalidate` after any change to subscriptions, rules or ignore lists.
    """

    def __init__(
        self,
        storage: Storage,
        *,
        timeout: float,
        on_rule_disabled: RuleDisabledHook | None = None,
    ) -> None:
        self._storage = storage
        self._timeout = timeout
        self._on_rule_disabled = on_rule_disabled
        self._monitored: set[int] = set()
        self._cache: dict[int, _ChatRuleSet] = {}
        self._timeouts: dict[int, int] = {}
        self._hits: Counter[int] = Counter()
        self.stats_evaluated = 0
        self.stats_matched = 0
        self.stats_timeouts = 0
        self.stats_budget_exceeded = 0

    def note_hit(self, rule_id: int) -> None:
        """Count a delivered match; flushed to storage together with the cursors."""
        self._hits[rule_id] += 1

    async def flush_hits(self) -> None:
        if not self._hits:
            return
        hits, self._hits = dict(self._hits), Counter()
        await self._storage.flush_rule_hits(hits)

    # -- monitored set ---------------------------------------------------------------------

    @property
    def monitored(self) -> frozenset[int]:
        return frozenset(self._monitored)

    def is_monitored(self, chat_id: int) -> bool:
        return chat_id in self._monitored

    async def reload(self) -> None:
        """Reload the monitored set from storage and drop every cached rule set."""
        self._monitored = await self._storage.monitored_chat_ids()
        self._cache.clear()

    async def invalidate(self, chat_id: int | None = None) -> None:
        """Drop cached rules for one chat (or all) and refresh the monitored set."""
        if chat_id is None:
            self._cache.clear()
        else:
            self._cache.pop(chat_id, None)
        self._monitored = await self._storage.monitored_chat_ids()

    # -- evaluation ------------------------------------------------------------------------

    async def evaluate(self, chat_id: int, text: str, sender_id: int | None) -> list[Match]:
        """Return at most one :class:`Match` per subscribed user for ``text`` in ``chat_id``.

        Per user: ignored sender → no match; any exclusion hit → no match; otherwise the first
        include rule (chat-specific before global) that matches. A rule that times out counts
        as "no match" and is disabled after :data:`TIMEOUTS_BEFORE_DISABLE` timeouts.
        """
        if not text or chat_id not in self._monitored:
            return []
        self.stats_evaluated += 1
        ruleset = await self._get(chat_id)
        if ruleset.rule_count > OFFLOAD_ABOVE_RULES:
            matches, timed_out = await asyncio.to_thread(
                self._evaluate_sync, ruleset, text, sender_id
            )
        else:
            matches, timed_out = self._evaluate_sync(ruleset, text, sender_id)
        for rule in timed_out:
            await self._note_timeout(rule)
        if matches:
            self.stats_matched += 1
        return matches

    def _evaluate_sync(
        self, ruleset: _ChatRuleSet, text: str, sender_id: int | None
    ) -> tuple[list[Match], list[Rule]]:
        """The pure matching work, safe to run in a worker thread."""
        deadline = time.monotonic() + MESSAGE_BUDGET
        matches: list[Match] = []
        timed_out: list[Rule] = []
        for user in ruleset.users:
            if sender_id is not None and sender_id in user.ignored:
                continue
            if self._first_hit(user.excludes, text, deadline, timed_out) is not None:
                continue
            hit = self._first_hit(user.includes, text, deadline, timed_out)
            if hit is not None:
                rule, spans = hit
                matches.append(Match(user.user_id, rule, spans))
        return matches, timed_out

    def _first_hit(
        self,
        rules: list[tuple[Rule, CompiledRule]],
        text: str,
        deadline: float,
        timed_out: list[Rule],
    ) -> tuple[Rule, list[Span]] | None:
        for rule, compiled in rules:
            if time.monotonic() > deadline:
                self.stats_budget_exceeded += 1
                log.warning("matching budget exhausted; skipping the remaining rules")
                return None
            try:
                spans = compiled.search_all(text, self._timeout)
            except RuleTimeout:
                timed_out.append(rule)
                continue
            if spans:
                return rule, spans
        return None

    async def _note_timeout(self, rule: Rule) -> None:
        self.stats_timeouts += 1
        count = self._timeouts.get(rule.id, 0) + 1
        self._timeouts[rule.id] = count
        log.warning("rule %s timed out (%d/%d)", rule.id, count, TIMEOUTS_BEFORE_DISABLE)
        if count >= TIMEOUTS_BEFORE_DISABLE:
            await self._disable(rule, DISABLED_TIMEOUT)

    async def _disable(self, rule: Rule, reason: str) -> None:
        await self._storage.disable_rule(rule.id, reason)
        self._timeouts.pop(rule.id, None)
        self._cache.clear()  # the rule may be global, so every chat could be affected
        log.warning("rule %s (%r) disabled: %s", rule.id, rule.pattern, reason)
        if self._on_rule_disabled is not None:
            await self._on_rule_disabled(rule, reason)

    # -- loading ---------------------------------------------------------------------------

    async def _get(self, chat_id: int) -> _ChatRuleSet:
        cached = self._cache.get(chat_id)
        if cached is not None:
            return cached
        ruleset = await self._load(chat_id)
        self._cache[chat_id] = ruleset
        return ruleset

    async def _load(self, chat_id: int) -> _ChatRuleSet:
        rules = await self._storage.rules_for_chat(chat_id)
        ignored = await self._storage.ignored_for_chat(chat_id)
        by_user: dict[int, _UserRules] = {}
        for rule in rules:  # ordered by user, chat-specific before global
            bucket = by_user.setdefault(rule.user_id, _UserRules(rule.user_id))
            try:
                compiled = compile_rule(rule.pattern, timeout=self._timeout)
            except RuleError as exc:
                await self._disable(rule, f"{DISABLED_INVALID}:{exc.key}")
                continue
            target = bucket.includes if rule.kind is RuleKind.INCLUDE else bucket.excludes
            target.append((rule, compiled))
        for user_id, senders in ignored.items():
            if user_id in by_user:
                by_user[user_id].ignored = set(senders)
        return _ChatRuleSet(chat_id, list(by_user.values()))
