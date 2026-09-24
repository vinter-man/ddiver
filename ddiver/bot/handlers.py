"""All bot UI flows, registered on a :class:`~ddiver.bot.router.Router`.

Every handler returns actions; nothing here talks to Telethon. Authorisation is already done
by the router, but each storage call still carries the acting ``user_id`` so a forged callback
payload can only ever touch the sender's own rows.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from ddiver import portable
from ddiver.bot import menus
from ddiver.bot.router import Action, Context, Edit, Reply, Router, Send, SendDocument, Toast
from ddiver.i18n import LANG_NAMES, LANGS, t
from ddiver.matcher import Matcher
from ddiver.rules import (
    MAX_RULES_PER_CHAT,
    MAX_RULES_PER_USER,
    MatchMode,
    PreparedText,
    RuleError,
    RuleKind,
    RuleTimeout,
    compile_rule,
    parse_pattern,
    parse_rule_lines,
    prepare_text,
)
from ddiver.storage import (
    ALL_CHATS,
    ChatRow,
    DuplicateRuleError,
    Role,
    Rule,
    Storage,
    User,
)
from ddiver.timeutil import Clock, format_clock, relative, resolve_tz, utcnow

log = logging.getLogger(__name__)

MAX_LINES_PER_MESSAGE = 50
DIGEST_MIN = 2
DIGEST_MAX = 500
# a rule pattern quoted back inside a confirmation line; long regexes must not push the
# question itself out of view
DROP_CONFIRM_PREVIEW = 30
MAX_REPLY_CHARS = 3800  # Telegram's limit is 4096; leave room for the header lines

KIND_ICONS = {
    MatchMode.SUBSTRING: "bot.rules.kind.substring",
    MatchMode.WORD: "bot.rules.kind.word",
    MatchMode.REGEX: "bot.rules.kind.regex",
}


@dataclass(slots=True)
class StatusInfo:
    """What ``/status`` shows; filled in by the app so handlers stay pure."""

    listener_ok: bool = True
    uptime: str = "—"
    monitored: int = 0
    unavailable: int = 0
    seen: int = 0
    matched: int = 0
    delivered: int = 0
    digested: int = 0
    failed: int = 0
    queue: int = 0
    last_update_at: dt.datetime | None = None
    """Left unrendered: /status is written in the reader's language, not the server's."""


StatusProvider = Callable[[], Awaitable[StatusInfo]]
Invalidator = Callable[[int | None], Awaitable[None]]


def build_router(
    storage: Storage,
    *,
    matcher: Matcher | None = None,
    status: StatusProvider | None = None,
    regex_timeout: float = 0.2,
    clock: Clock = utcnow,
) -> Router:
    async def invalidate(chat_id: int | None = None) -> None:
        if matcher is not None:
            await matcher.invalidate(chat_id)

    router = Router(storage, clock=clock, on_user_state_changed=invalidate)

    # -- menus -----------------------------------------------------------------------------

    def menu_actions(ctx: Context, *, edit: bool = False) -> list[Action]:
        buttons = menus.main_menu(is_owner=ctx.is_owner)
        action = Edit if edit else Reply
        return [action("bot.menu.title", buttons=buttons)]

    @router.command("start")
    async def start(ctx: Context) -> list[Action]:
        if ctx.user.lang is None:
            return [Reply("bot.start.language", buttons=menus.language_menu())]
        if ctx.user.tz is None:
            # the prompt invites free text, so be ready to receive it
            router.set_pending(ctx.user_id, "timezone")
            return [Reply("bot.start.timezone", buttons=menus.timezone_menu())]
        return menu_actions(ctx)

    @router.command("menu")
    async def menu(ctx: Context) -> list[Action]:
        return menu_actions(ctx)

    @router.callback("menu")
    async def menu_cb(ctx: Context) -> list[Action]:
        return menu_actions(ctx, edit=True)

    @router.command("help")
    async def help_cmd(_ctx: Context) -> list[Action]:
        return [Reply("bot.help", html=True)]

    @router.callback("help")
    async def help_cb(_ctx: Context) -> list[Action]:
        return [Edit("bot.help", buttons=[[("bot.btn.back", "menu")]], html=True)]

    @router.command("cancel")
    async def cancel(ctx: Context) -> list[Action]:
        router.clear_pending(ctx.user_id)
        return [Reply("bot.cancelled")]

    # -- onboarding ------------------------------------------------------------------------

    @router.callback("lang")
    async def set_language(ctx: Context) -> list[Action]:
        lang = _parts(ctx.incoming.data, 2)[1]
        if lang not in LANGS:
            return [Toast("bot.err.unknown_action")]
        user = await storage.update_user(ctx.user_id, lang=lang)
        if user.tz is None:
            router.set_pending(ctx.user_id, "timezone")
            return [Edit("bot.start.timezone", buttons=menus.timezone_menu())]
        return [Toast("bot.settings.saved"), *menu_actions(ctx, edit=True)]

    @router.callback("tz")
    async def set_timezone(ctx: Context) -> list[Action]:
        name = _parts(ctx.incoming.data, 2)[1]
        resolution = resolve_tz(name)
        if resolution.exact is None:
            return [Toast("bot.start.tz_unknown")]
        return await _apply_timezone(ctx, resolution.exact, fixed=resolution.fixed_offset)

    @router.pending("timezone")
    async def timezone_input(ctx: Context) -> list[Action]:
        resolution = resolve_tz(ctx.incoming.text or "")
        if resolution.exact:
            router.clear_pending(ctx.user_id)
            return await _apply_timezone(ctx, resolution.exact, fixed=resolution.fixed_offset)
        if resolution.candidates:
            buttons = [[(menus.literal(tz), f"tz:{tz}")] for tz in resolution.candidates]
            return [Reply("bot.start.tz_candidates", buttons=buttons)]
        return [Reply("bot.start.tz_unknown")]

    async def _apply_timezone(ctx: Context, tz: str, *, fixed: bool) -> list[Action]:
        user = await storage.update_user(ctx.user_id, tz=tz)
        lang = user.lang or ctx.lang
        actions: list[Action] = [
            Reply(
                t(lang, "bot.start.tz_confirm", tz=tz, clock=format_clock(clock(), tz)),
                literal=True,
            )
        ]
        if fixed:
            actions.append(Reply("bot.start.tz_fixed_offset"))
        actions.extend(menu_actions(ctx))
        return actions

    # -- chats -----------------------------------------------------------------------------

    async def visible_chats(user: User) -> list[ChatRow]:
        """The owner sees every known chat; moderators only the shared ones."""
        return await storage.list_chats(shared_only=not user.is_owner)

    async def accessible_chat(ctx: Context, chat_id: int) -> ChatRow | None:
        """The one place that decides whether a user may act on a chat id.

        Callback payloads are attacker-controlled, so a moderator who guesses the id of the
        owner's private chat must get the same answer as for an id that does not exist.
        """
        if chat_id == ALL_CHATS:
            return None
        chat = await storage.get_chat(chat_id)
        if chat is None:
            return None
        if ctx.is_owner:
            return chat
        if await storage.is_subscribed(ctx.user_id, chat_id):
            # The subscription is the grant: it could only have been created while the chat was
            # shared, and taking it away must never lock the user out of managing it.
            return chat
        if not chat.shared:
            return None
        return chat

    @router.callback("chats")
    async def chats(ctx: Context) -> list[Action]:
        arg = _parts(ctx.incoming.data or "chats:0", 2)[1]
        if arg == "search":
            router.set_pending(ctx.user_id, "chat_search")
            return [Reply("bot.chats.search_prompt")]
        page_index = _int(arg)
        rows = await visible_chats(ctx.user)
        if not rows:
            key = "bot.chats.empty" if ctx.is_owner else "bot.chats.none_shared"
            return [Edit(key, buttons=[[("bot.btn.back", "menu")]])]
        subscribed = {c.id for c in await storage.list_subscriptions(ctx.user_id)}
        return [
            Edit(
                "bot.chats.title",
                {"n": len(subscribed)},
                buttons=menus.chats_menu(rows, subscribed, page_index),
            )
        ]

    @router.pending("chat_search")
    async def chat_search(ctx: Context) -> list[Action]:
        needle = (ctx.incoming.text or "").strip().lstrip("@").casefold()
        rows = [
            c
            for c in await visible_chats(ctx.user)
            if needle in c.ref.title.casefold() or needle in (c.ref.username or "").casefold()
        ]
        if not rows:
            return [Reply("bot.chats.search_empty")]  # stay in search mode so they can retype
        router.clear_pending(ctx.user_id)
        subscribed = {c.id for c in await storage.list_subscriptions(ctx.user_id)}
        return [
            Reply(
                "bot.chats.title",
                {"n": len(subscribed)},
                buttons=menus.chats_menu(rows, subscribed, 0),
            )
        ]

    @router.callback("sub")
    async def subscribe(ctx: Context) -> list[Action]:
        parts = _parts(ctx.incoming.data, 4)
        _, raw_chat, raw_state = parts[:3]
        # The page travels in the callback, so dropping a chat does not throw the moderator
        # back to the first page of a few hundred. Keyboards sent before this omit it.
        page_index = _int(parts[3]) if len(parts) > 3 else 0
        chat_id, wanted = _int(raw_chat), raw_state == "1"
        chat = await accessible_chat(ctx, chat_id)
        if chat is None:
            return [Toast("bot.err.not_found")]
        if wanted:
            # storage.subscribe re-checks sharing itself, atomically
            await storage.subscribe(ctx.user_id, chat_id)
        else:
            await storage.unsubscribe(ctx.user_id, chat_id)
        await invalidate(chat_id)
        rows = await visible_chats(ctx.user)
        subscribed = {c.id for c in await storage.list_subscriptions(ctx.user_id)}
        key = "bot.chats.subscribed" if wanted else "bot.chats.unsubscribed"
        return [
            Toast(t(ctx.lang, key, chat=chat.ref.title), literal=True),
            Edit(
                "bot.chats.title",
                {"n": len(subscribed)},
                buttons=menus.chats_menu(rows, subscribed, page_index),
            ),
        ]

    # -- shared chats (owner only) ---------------------------------------------------------

    @router.callback("shared", owner_only=True)
    async def shared(ctx: Context) -> list[Action]:
        page_index = _int(_parts(ctx.incoming.data or "shared:0", 2)[1])
        rows = await storage.list_chats()
        return [
            Edit("bot.shared.hint", buttons=menus.shared_menu(rows, page_index)),
        ]

    @router.callback("share", owner_only=True)
    async def share(ctx: Context) -> list[Action]:
        _, raw_chat, raw_state = _parts(ctx.incoming.data, 3)
        chat_id, wanted = _int(raw_chat), raw_state == "1"
        chat = await storage.get_chat(chat_id)
        if chat is None:
            return [Toast("bot.err.not_found")]
        await storage.set_chat_shared(chat_id, wanted)
        actions: list[Action] = []
        if wanted:
            actions.append(Toast(t(ctx.lang, "bot.shared.on", chat=chat.ref.title), literal=True))
        else:
            dropped = await storage.unsubscribe_all(chat_id, except_user=ctx.user_id)
            await invalidate(chat_id)
            actions.append(
                Toast(
                    t(ctx.lang, "bot.shared.off", chat=chat.ref.title, n=len(dropped)), literal=True
                )
            )
            for user_id in dropped:
                other = await storage.get_user(user_id)
                lang = (other.lang if other else None) or ctx.lang
                actions.append(
                    Send(
                        user_id,
                        t(lang, "bot.shared.unshared_notice", chat=chat.ref.title),
                        literal=True,
                    )
                )
        rows = await storage.list_chats()
        actions.append(Edit("bot.shared.hint", buttons=menus.shared_menu(rows, 0)))
        return actions

    # -- users (owner only) ----------------------------------------------------------------

    @router.callback("users", owner_only=True)
    async def users(ctx: Context) -> list[Action]:
        return await _users_view(ctx)

    async def _users_view(ctx: Context) -> list[Action]:
        everyone = await storage.list_users()
        pending = [u.id for u in everyone if u.role is Role.PENDING and not u.is_owner]
        allowed = [u.id for u in everyone if u.role is Role.ALLOWED and not u.is_owner]
        denied = [u.id for u in everyone if u.role is Role.DENIED]
        lines = [ctx.t("bot.users.title")]
        lines.extend(ctx.t("bot.users.pending", user_id=uid) for uid in pending)
        for user_id in allowed:
            chats = len(await storage.list_subscriptions(user_id))
            rules = await storage.count_rules(user_id)
            lines.append(ctx.t("bot.users.allowed", user_id=user_id, chats=chats, rules=rules))
        lines.extend(ctx.t("bot.users.denied", user_id=uid) for uid in denied)
        return [
            Edit(
                _clamp("\n".join(lines)),
                literal=True,
                buttons=menus.users_menu(pending, allowed, denied),
            )
        ]

    @router.callback("allow", owner_only=True)
    async def allow(ctx: Context) -> list[Action]:
        target = _int(_parts(ctx.incoming.data, 2)[1])
        if await storage.get_user(target) is None:
            return [Toast("bot.err.not_found")]
        user = await storage.update_user(target, role=Role.ALLOWED, notified_owner_at=None)
        await invalidate(None)
        return [
            Toast(t(ctx.lang, "bot.users.allowed_toast", user_id=target), literal=True),
            Send(target, t(user.lang or ctx.lang, "bot.gate.allowed"), literal=True),
            *await _users_view(ctx),
        ]

    @router.callback("deny", owner_only=True)
    async def deny(ctx: Context) -> list[Action]:
        target = _int(_parts(ctx.incoming.data, 2)[1])
        if await storage.get_user(target) is None:
            return [Toast("bot.err.not_found")]
        user = await storage.update_user(target, role=Role.DENIED)
        await invalidate(None)
        return [
            Toast(t(ctx.lang, "bot.users.denied_toast", user_id=target), literal=True),
            Send(target, t(user.lang or ctx.lang, "bot.gate.denied"), literal=True),
            *await _users_view(ctx),
        ]

    @router.callback("revoke", owner_only=True)
    async def revoke(ctx: Context) -> list[Action]:
        target = _int(_parts(ctx.incoming.data, 2)[1])
        if target == ctx.user_id:
            return [Toast("bot.err.owner_only", alert=True)]
        if await storage.get_user(target) is None:
            return [Toast("bot.err.not_found")]
        await storage.update_user(target, role=Role.DENIED)
        await invalidate(None)
        return [
            Toast(t(ctx.lang, "bot.users.revoked_toast", user_id=target), literal=True),
            *await _users_view(ctx),
        ]

    # -- rules -----------------------------------------------------------------------------

    def icon_for(pattern: str, lang: str) -> str:
        mode, _ = parse_pattern(pattern)
        return t(lang, KIND_ICONS[mode])

    async def rules_view(ctx: Context, chat_id: int, kind: RuleKind, page: int) -> list[Action]:
        if chat_id == ALL_CHATS:
            title = ctx.t("bot.rules.title_global")
        else:
            chat = await accessible_chat(ctx, chat_id)
            if chat is None:
                return [Toast("bot.err.not_found")]
            title = ctx.t("bot.rules.title", chat=chat.ref.title)
        rules = await storage.list_rules(ctx.user_id, chat_id, kind)
        icons = [icon_for(r.pattern, ctx.lang) for r in rules]
        body = title if rules else f"{title}\n{ctx.t('bot.rules.empty')}"
        return [
            Edit(
                body,
                literal=True,
                buttons=menus.rules_menu(rules, icons, chat_id, kind.value, page),
            )
        ]

    @router.callback("rules")
    async def rules(ctx: Context) -> list[Action]:
        parts = _parts(ctx.incoming.data or "rules:pick", 4)
        if parts[1] == "pick":
            chats = await storage.list_subscriptions(ctx.user_id)
            return [Edit("bot.rules.pick_chat", buttons=menus.rules_chat_menu(chats, 0))]
        chat_id = _int(parts[1])
        # RuleKind() raises on anything that is not "include" or "exclude", and the payload is
        # whatever the client sent. Padding cannot help here: the field is present and wrong.
        kind = RuleKind.EXCLUDE if parts[2] == RuleKind.EXCLUDE.value else RuleKind.INCLUDE
        page = _int(parts[3])
        return await rules_view(ctx, chat_id, kind, page)

    @router.callback("rpick")
    async def rules_pick_page(ctx: Context) -> list[Action]:
        page = _int(_parts(ctx.incoming.data or "rpick:0", 2)[1])
        chats = await storage.list_subscriptions(ctx.user_id)
        return [Edit("bot.rules.pick_chat", buttons=menus.rules_chat_menu(chats, page))]

    @router.callback("radd")
    async def rules_add_prompt(ctx: Context) -> list[Action]:
        _, raw_chat, raw_kind = _parts(ctx.incoming.data, 3)
        chat_id = _int(raw_chat)
        if chat_id != ALL_CHATS and await accessible_chat(ctx, chat_id) is None:
            return [Toast("bot.err.not_found")]
        router.set_pending(ctx.user_id, "rules_add", {"chat_id": chat_id, "kind": raw_kind})
        return [Reply("bot.rules.prompt", html=True)]

    @router.pending("rules_add")
    async def rules_add(ctx: Context) -> list[Action]:
        payload = router.pending_payload(ctx.user_id)
        chat_id = int(payload.get("chat_id", ALL_CHATS))
        kind = RuleKind(payload.get("kind", RuleKind.INCLUDE.value))
        router.clear_pending(ctx.user_id)
        lines = parse_rule_lines(ctx.incoming.text or "")
        if len(lines) > MAX_LINES_PER_MESSAGE:
            return [Reply("bot.rules.too_many_lines", {"limit": MAX_LINES_PER_MESSAGE})]
        report = await _add_rules(ctx, chat_id, kind, lines)
        return [Reply(_clamp(report), literal=True), *await rules_view(ctx, chat_id, kind, 0)]

    async def _add_rules(ctx: Context, chat_id: int, kind: RuleKind, lines: Sequence[str]) -> str:
        added = duplicates = 0
        problems: list[str] = []
        total_user = await storage.count_rules(ctx.user_id)
        total_chat = await storage.count_rules(ctx.user_id, chat_id)
        for number, line in enumerate(lines, start=1):
            if total_chat >= MAX_RULES_PER_CHAT:
                problems.append(ctx.t("bot.rules.limit_chat", limit=MAX_RULES_PER_CHAT))
                break
            if total_user >= MAX_RULES_PER_USER:
                problems.append(ctx.t("bot.rules.limit_user", limit=MAX_RULES_PER_USER))
                break
            try:
                compiled = await asyncio.to_thread(compile_rule, line, timeout=regex_timeout)
            except RuleError as exc:
                problems.append(
                    ctx.t(
                        "bot.rules.line_invalid",
                        line=number,
                        reason=t(ctx.lang, exc.key, **exc.params),
                    )
                )
                continue
            try:
                await storage.add_rule(ctx.user_id, chat_id, kind, compiled.pattern)
            except DuplicateRuleError:
                duplicates += 1
                problems.append(ctx.t("bot.rules.line_duplicate", line=number))
                continue
            added += 1
            total_user += 1
            total_chat += 1
        if added:
            await invalidate(None if chat_id == ALL_CHATS else chat_id)
        head = ctx.t(
            "bot.rules.report",
            added=added,
            duplicates=duplicates,
            invalid=len(problems) - duplicates,
        )
        return "\n".join([head, *problems])

    @router.callback("rdel")
    async def rules_delete(ctx: Context) -> list[Action]:
        rule_id = _int(_parts(ctx.incoming.data, 2)[1])
        rule = await storage.get_rule(rule_id, ctx.user_id)
        if rule is None:
            return [Toast("bot.err.not_found")]
        await storage.delete_rule(rule_id, ctx.user_id)
        await invalidate(None if rule.is_global else rule.chat_id)
        return [
            Toast("bot.rules.deleted"),
            *await rules_view(ctx, rule.chat_id, rule.kind, 0),
        ]

    # -- ignored senders -------------------------------------------------------------------

    @router.callback("ign")
    async def ignored(ctx: Context) -> list[Action]:
        page = _int(_parts(ctx.incoming.data or "ign:0", 2)[1])
        entries = await storage.list_ignored(ctx.user_id)
        body = ctx.t("bot.ignored.title") if entries else ctx.t("bot.ignored.empty")
        return [Edit(body, literal=True, buttons=menus.ignored_menu(entries, page))]

    @router.callback("igdel")
    async def ignored_delete(ctx: Context) -> list[Action]:
        _, raw_chat, raw_sender = _parts(ctx.incoming.data, 3)
        await storage.remove_ignored(ctx.user_id, _int(raw_chat), _int(raw_sender))
        await invalidate(None)
        entries = await storage.list_ignored(ctx.user_id)
        body = ctx.t("bot.ignored.title") if entries else ctx.t("bot.ignored.empty")
        return [
            Toast("bot.ignored.removed"),
            Edit(body, literal=True, buttons=menus.ignored_menu(entries, 0)),
        ]

    @router.callback("ig")
    async def ignore_sender(ctx: Context) -> list[Action]:
        _, raw_chat, raw_sender = _parts(ctx.incoming.data, 3)
        chat_id, sender_id = _int(raw_chat), _int(raw_sender)
        chat = await accessible_chat(ctx, chat_id)
        if chat is None:
            return [Toast("bot.err.not_found")]
        anonymous = sender_id == chat_id
        note = ctx.t("bot.ignored.anonymous") if anonymous else str(sender_id)
        await storage.add_ignored(ctx.user_id, chat_id, sender_id, note)
        await invalidate(chat_id)
        return [
            Toast(
                t(
                    ctx.lang,
                    "bot.ignored.added",
                    sender=note,
                    chat=chat.ref.title,
                ),
                literal=True,
            ),
            Reply(
                "bot.ignored.title",
                buttons=[[("bot.ignored.all_chats_btn", f"igall:{sender_id}")]],
            ),
        ]

    @router.callback("igall")
    async def ignore_sender_globally(ctx: Context) -> list[Action]:
        sender_id = _int(_parts(ctx.incoming.data, 2)[1])
        await storage.add_ignored(ctx.user_id, ALL_CHATS, sender_id, str(sender_id))
        await invalidate(None)
        return [
            Toast(t(ctx.lang, "bot.ignored.added_global", sender=sender_id), literal=True),
        ]

    # -- notification: drop the rule that fired ---------------------------------------------

    @router.callback("rdrop")
    async def drop_rule_prompt(ctx: Context) -> list[Action]:
        """Ask before removing: one tap must not silently kill a rule on every chat."""
        rule_id = _int(_parts(ctx.incoming.data, 2)[1])
        rule = await storage.get_rule(rule_id, ctx.user_id)
        if rule is None:
            return [Toast("bot.notif.drop_gone", alert=True)]
        scope = await _rule_scope(ctx, rule)
        body = t(
            ctx.lang, "bot.notif.drop_confirm", pattern=_preview_pattern(rule.pattern), scope=scope
        )
        return [
            Reply(
                body,
                literal=True,
                buttons=[
                    [
                        ("bot.btn.drop_yes", menus.check_callback(f"rdropok:{rule_id}")),
                        ("bot.btn.cancel", "menu"),
                    ]
                ],
            )
        ]

    @router.callback("rdropok")
    async def drop_rule(ctx: Context) -> list[Action]:
        rule_id = _int(_parts(ctx.incoming.data, 2)[1])
        rule = await storage.get_rule(rule_id, ctx.user_id)
        if rule is None or not await storage.delete_rule(rule_id, ctx.user_id):
            return [Toast("bot.notif.drop_gone", alert=True)]
        await invalidate(None if rule.chat_id == ALL_CHATS else rule.chat_id)
        return [
            Toast(
                t(ctx.lang, "bot.notif.drop_done", pattern=_preview_pattern(rule.pattern)),
                literal=True,
            ),
            Edit(
                t(ctx.lang, "bot.notif.drop_done", pattern=_preview_pattern(rule.pattern)),
                literal=True,
            ),
        ]

    async def _rule_scope(ctx: Context, rule: Rule) -> str:
        if rule.chat_id == ALL_CHATS:
            return t(ctx.lang, "notif.rule_scope.global")
        chat = await storage.get_chat(rule.chat_id)
        title = chat.ref.title if chat else str(rule.chat_id)
        return t(ctx.lang, "bot.notif.drop_scope_chat", chat=title)

    # -- settings --------------------------------------------------------------------------

    @router.callback("set")
    async def settings(ctx: Context) -> list[Action]:
        what = _parts(ctx.incoming.data or "set:main", 2)[1]
        if what == "lang":
            return [Edit("bot.start.language", buttons=menus.language_menu())]
        if what == "tz":
            router.set_pending(ctx.user_id, "timezone")
            return [Edit("bot.start.timezone", buttons=menus.timezone_menu())]
        if what == "digest":
            router.set_pending(ctx.user_id, "digest")
            return [Reply("bot.settings.digest_prompt", {"n": ctx.user.digest_threshold})]
        user = ctx.user
        toast: list[Action] = []
        if what == "bots":
            user = await storage.update_user(ctx.user_id, include_bots=not user.include_bots)
            await invalidate(None)  # the preference lives in the matcher's per-chat cache
            toast = [
                Toast(
                    "bot.settings.bots_on_hint"
                    if user.include_bots
                    else "bot.settings.bots_off_hint"
                )
            ]
        body = t(
            ctx.lang,
            "bot.settings.title",
            lang=LANG_NAMES.get(ctx.lang, ctx.lang),
            tz=user.tz or "UTC",
            digest=user.digest_threshold,
            bots=t(
                ctx.lang, "bot.settings.bots_yes" if user.include_bots else "bot.settings.bots_no"
            ),
        )
        return [
            *toast,
            Edit(body, literal=True, buttons=menus.settings_menu(include_bots=user.include_bots)),
        ]

    @router.pending("digest")
    async def digest_input(ctx: Context) -> list[Action]:
        raw = (ctx.incoming.text or "").strip()
        current = {"n": ctx.user.digest_threshold}
        if not raw.isdigit():
            return [Reply("bot.settings.digest_prompt", current)]
        value = int(raw)
        if value != 0 and not (DIGEST_MIN <= value <= DIGEST_MAX):
            return [Reply("bot.settings.digest_prompt", current)]
        router.clear_pending(ctx.user_id)
        await storage.update_user(ctx.user_id, digest_threshold=value)
        return [Reply("bot.settings.digest_set", {"n": value})]

    # -- status and test -------------------------------------------------------------------

    @router.command("status")
    async def status_cmd(ctx: Context) -> list[Action]:
        return [Reply(await _status_text(ctx), literal=True)]

    @router.callback("status")
    async def status_cb(ctx: Context) -> list[Action]:
        return [Edit(await _status_text(ctx), literal=True, buttons=[[("bot.btn.back", "menu")]])]

    async def _status_text(ctx: Context) -> str:
        info = await status() if status is not None else StatusInfo()
        state = ctx.t(
            "bot.status.listener_ok" if info.listener_ok else "bot.status.listener_degraded"
        )
        return "\n".join(
            [
                ctx.t("bot.status.title"),
                "",
                ctx.t("bot.status.listener", state=state),
                ctx.t("bot.status.uptime", uptime=info.uptime),
                ctx.t("bot.status.chats", monitored=info.monitored, unavailable=info.unavailable),
                "",
                ctx.t("bot.status.session"),
                ctx.t(
                    "bot.status.counters",
                    seen=info.seen,
                    matched=info.matched,
                    delivered=info.delivered,
                    digested=info.digested,
                    failed=info.failed,
                ),
                ctx.t(
                    "bot.status.queue",
                    queue=info.queue,
                    last_update=(
                        relative(info.last_update_at, clock(), ctx.lang)
                        if info.last_update_at is not None
                        else "—"
                    ),
                ),
                "",
                ctx.t("bot.status.lifetime", hits=await storage.total_rule_hits(ctx.user_id)),
            ]
        )

    # -- config export / import --------------------------------------------------------------

    @router.command("export")
    async def export_cmd(ctx: Context) -> list[Action]:
        """Hand back the part of the configuration that is worth keeping: chats and rules."""
        config = await portable.export_config(storage, ctx.user_id, now=clock())
        body = portable.dumps(config).encode()
        stamp = clock().strftime("%Y-%m-%d")
        return [
            SendDocument(
                filename=f"ddiver-{stamp}.json",
                content=body,
                caption_key="bot.export.caption",
                params={
                    "chats": len(config["chats"]),
                    "rules": len(config["rules"]),
                    "ignored": len(config["ignored"]),
                },
            )
        ]

    @router.command("import")
    async def import_cmd(ctx: Context) -> list[Action]:
        router.set_pending(ctx.user_id, "import")
        return [Reply("bot.import.prompt", html=True)]

    @router.pending("import")
    async def import_input(ctx: Context) -> list[Action]:
        raw: bytes | str | None = ctx.incoming.document
        if raw is None and ctx.incoming.text and not ctx.incoming.looks_like_command:
            raw = ctx.incoming.text  # a small export pasted rather than attached
        if raw is None:
            return [Reply("bot.import.need_file")]
        router.clear_pending(ctx.user_id)
        try:
            data = portable.loads(raw)
        except portable.BadExportError as exc:
            return [Reply(t(ctx.lang, "bot.import.bad_file", reason=str(exc)), literal=True)]
        report = await portable.import_config(
            storage,
            ctx.user_id,
            data,
            regex_timeout=regex_timeout,
            is_owner=ctx.is_owner,
        )
        await invalidate(None)
        lines = [
            ctx.t(
                "bot.import.done",
                chats=report.chats_subscribed,
                rules=report.rules_added,
                ignored=report.ignored_added,
            )
        ]
        if report.rules_duplicate:
            lines.append(ctx.t("bot.import.duplicates", n=report.rules_duplicate))
        if report.rules_over_limit:
            lines.append(
                ctx.t(
                    "bot.import.over_limit",
                    n=report.rules_over_limit,
                    chat=MAX_RULES_PER_CHAT,
                    total=MAX_RULES_PER_USER,
                )
            )
        if report.chats_missing:
            lines.append(ctx.t("bot.import.missing_chats", chats=", ".join(report.chats_missing)))
        if report.rules_invalid:
            lines.append(ctx.t("bot.import.bad_rules", rules=", ".join(report.rules_invalid)))
        return [Reply(_clamp("\n".join(lines)), literal=True)]

    @router.command("test")
    async def test_cmd(ctx: Context) -> list[Action]:
        if not ctx.incoming.args:
            router.set_pending(ctx.user_id, "test")
            return [Reply("bot.test.prompt", html=True)]
        return [Reply(await _test_text(ctx, ctx.incoming.args), literal=True)]

    @router.pending("test")
    async def test_input(ctx: Context) -> list[Action]:
        router.clear_pending(ctx.user_id)
        return [Reply(await _test_text(ctx, ctx.incoming.text or ""), literal=True)]

    async def _test_text(ctx: Context, text: str) -> str:
        return _clamp(await _test_lines(ctx, text))

    async def _test_lines(ctx: Context, text: str) -> str:
        prepared = prepare_text(text)
        chats = await storage.list_subscriptions(ctx.user_id)
        lines: list[str] = []
        if not chats:
            # Nothing can ever arrive without a watched chat, so say that plainly — but still
            # run the global rules, since testing them before adding a chat is a fair thing
            # to want.
            lines.append(ctx.t("bot.test.no_chats"))
            global_rules = await storage.list_rules(ctx.user_id, ALL_CHATS)
            if global_rules:
                lines.append(
                    await asyncio.to_thread(
                        _evaluate_preview,
                        prepared,
                        global_rules,
                        ctx.lang,
                        ctx.t("bot.test.global_scope"),
                        regex_timeout,
                    )
                )
        for chat in chats:
            rules = await storage.rules_for_chat(chat.id)
            mine = [r for r in rules if r.user_id == ctx.user_id]
            verdict = await asyncio.to_thread(
                _evaluate_preview, prepared, mine, ctx.lang, chat.ref.title, regex_timeout
            )
            lines.append(verdict)
        return "\n".join(lines) if lines else ctx.t("bot.test.no_chats")

    @router.fallback
    async def fallback(ctx: Context) -> list[Action]:
        return [Reply("bot.err.not_waiting", buttons=menus.main_menu(is_owner=ctx.is_owner))]

    return router


def _evaluate_preview(
    prepared: PreparedText,
    rules: Sequence[Rule],
    lang: str,
    chat_title: str,
    timeout: float = 0.2,
) -> str:
    """Mirror the production order: exclusions first, then chat rules before global ones.

    Runs in a worker thread: it compiles and matches user-supplied patterns, which is exactly
    the work that must never happen on the event loop.
    """
    excludes = [r for r in rules if r.kind is RuleKind.EXCLUDE]
    includes = [r for r in rules if r.kind is RuleKind.INCLUDE]
    for rule in excludes:
        try:
            compiled = compile_rule(rule.pattern, timeout=timeout)
        except (RuleError, RuleTimeout):
            continue
        try:
            if compiled.search(prepared.shadow, timeout):
                return t(lang, "bot.test.excluded", chat=chat_title, rule=rule.pattern)
        except RuleTimeout:
            continue
    for rule in includes:
        try:
            compiled = compile_rule(rule.pattern, timeout=timeout)
        except (RuleError, RuleTimeout):
            continue
        try:
            span = compiled.search(prepared.shadow, timeout)
        except RuleTimeout:
            continue
        if span:
            original = prepared.to_original(span)
            fragment = prepared.original[original.start : original.end]
            return t(lang, "bot.test.match", chat=chat_title, rule=rule.pattern, fragment=fragment)
    return t(lang, "bot.test.nomatch", chat=chat_title)


def _preview_pattern(pattern: str) -> str:
    return (
        pattern
        if len(pattern) <= DROP_CONFIRM_PREVIEW
        else pattern[: DROP_CONFIRM_PREVIEW - 1] + "…"
    )


def _clamp(text: str, limit: int = MAX_REPLY_CHARS) -> str:
    """Telegram rejects anything over 4096; a long rule list must not lose the whole reply."""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _parts(data: str | None, count: int) -> list[str]:
    """Split a callback payload into exactly ``count`` fields, padding with empty strings.

    Callback data is client-controlled: the button we drew is not necessarily the button that
    comes back. Unpacking it directly turned any truncated payload into an IndexError or a
    ValueError inside the handler, which logged a traceback and answered nothing — the button
    just span. Padding sends every one of those down the same path an unknown id already
    takes: ``_int("")`` is 0, which no chat or rule matches, and the handler replies "not
    found" like it would for any other stale button.
    """
    fields = (data or "").split(":")
    return (fields + [""] * count)[:count]


def _int(raw: str) -> int:
    try:
        return int(raw)
    except ValueError:
        return 0
