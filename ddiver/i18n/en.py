"""English strings (the reference catalog; other languages must define the same keys)."""

STRINGS: dict[str, str] = {
    # rule validation
    "rule.err.empty": "Empty pattern.",
    "rule.err.too_long": "Pattern is longer than {limit} characters.",
    "rule.err.invalid_regex": "Invalid regular expression: {detail}",
    "rule.err.matches_empty": "This regular expression matches empty text, so it would match every message.",
    "rule.err.too_slow": "This regular expression is too slow (catastrophic backtracking): {pattern}",
    # notification
    "notif.chat": "📡 Chat",
    "notif.sender": "👤 Sender",
    "notif.link": "🔗 Message",
    "notif.time": "🕒",
    "notif.rule": "🔑 Rule",
    "notif.rule_scope.chat": "chat rule",
    "notif.rule_scope.global": "rule for all chats",
    "notif.missed": "⏪ missed while offline",
    "notif.sender.deleted": "Deleted account",
    "notif.sender.unknown": "Unknown sender",
    "notif.sender.by_id": "User {id}",
    "notif.sender.anonymous": "Anonymous admin",
    "notif.truncated": "… [+{n} more characters]",
    "notif.btn.ignore": "🚫 Ignore sender",
    "notif.btn.drop_rule": "🗑 Remove this rule",
    "notif.btn.profile": "👤 Profile",
    "notif.forwarded": "↪️ forwarded",
}

STRINGS.update(
    {
        # digest
        "digest.title": "📦 {n} matches while you were busy",
        "digest.item": "{rule} · {sender} · {snippet}",
        "digest.more_chats": "… and {n} more chats ({m} matches)",
        "digest.more_items": "… and {n} more",
        "digest.overflow": "⚠️ {n} matches were dropped because the queue was full",
        # catch-up
        "notif.catchup_capped": "⚠️ {n} older messages were not checked",
    }
)


STRINGS.update(
    {
        "bot.gate.pending": "⏳ Your access request is waiting for the owner's approval.",
        "bot.gate.card": "👤 User {user_id} wants access to ddiver.",
        "bot.gate.denied": "⛔ Access denied.",
        "bot.gate.allowed": "✅ You now have access. Send /start to set things up.",
        "bot.err.unknown_action": "This button no longer works.",
        "bot.err.owner_only": "Only the owner can do that.",
        "bot.err.unknown_command": "Unknown command. Use /menu.",
        "bot.err.not_waiting": "I am not waiting for input — use /menu.",
        "bot.err.expired": "This action expired. Use the menu instead.",
        "bot.err.not_found": "Not found.",
        "bot.btn.allow": "✅ Allow",
        "bot.btn.deny": "⛔ Deny",
        "bot.btn.revoke": "🚫 Revoke",
        "bot.btn.back": "⬅️ Back",
        "bot.btn.prev": "◀️",
        "bot.btn.next": "▶️",
        "bot.btn.add": "➕ Add",
        "bot.btn.done": "✅ Done",
        "bot.btn.drop_yes": "🗑 Yes, remove",
        "bot.btn.cancel": "✖️ Cancel",
        "bot.btn.all_chats": "🌐 All chats",
        "bot.btn.search": "🔍 Search",
        "bot.btn.include": "🔑 Keywords",
        "bot.btn.exclude": "➖ Exclusions",
        "bot.cmd.start": "Start and open the menu",
        "bot.cmd.menu": "Main menu",
        "bot.cmd.test": "Try a text against your rules",
        "bot.cmd.status": "What is going on right now",
        "bot.cmd.help": "Rule syntax and what the bot does",
        "bot.cmd.cancel": "Cancel the current input",
        "bot.menu.title": "What would you like to do?",
        "bot.menu.chats": "📡 Chats",
        "bot.menu.rules": "🔑 Rules",
        "bot.menu.ignored": "🚫 Ignored",
        "bot.menu.settings": "⚙️ Settings",
        "bot.menu.status": "📊 Status",
        "bot.menu.help": "❓ Help",
        "bot.menu.users": "👥 Users",
        "bot.menu.shared": "🗂 Shared chats",
        "bot.start.language": "Choose the language you want me to speak.",
        "bot.start.timezone": "Now pick your timezone, or type an IANA name like Europe/Kyiv.",
        "bot.start.tz_confirm": "Now in {tz}: {clock}. Is that right?",
        "bot.start.tz_unknown": "I do not know that timezone. Try a city name, e.g. Kyiv.",
        "bot.start.tz_candidates": "Did you mean one of these?",
        "bot.start.tz_fixed_offset": "⚠️ A fixed offset has no daylight saving time.",
        "bot.start.done": "All set. You can add chats now.",
        "bot.chats.title": ("Chats you are monitoring ({n}). Tapping toggles: ✅ on, ☐ off."),
        "bot.chats.empty": "No chats yet. Use search or pick one from the list.",
        "bot.chats.none_shared": "The owner has not shared any chats with you yet.",
        "bot.chats.subscribed": "✅ Now monitoring {chat}.",
        "bot.chats.unsubscribed": "☐ No longer monitoring {chat}.",
        "bot.chats.unavailable": "⚠️ {chat} — the listener lost access",
        "bot.chats.search_prompt": "Send part of the chat title or its @username.",
        "bot.chats.search_empty": "Nothing matched.",
        "bot.chats.lost_access": "⚠️ The listener lost access to {chat}; monitoring is paused for it.",
        "bot.chats.regained_access": "✅ The listener can read {chat} again.",
        "bot.chats.migrated": "ℹ️ {chat} became a supergroup; your rules moved with it.",
        "bot.rules.pick_chat": "Which chat are these rules for?",
        "bot.rules.title": "Rules for {chat}",
        "bot.rules.title_global": "Rules for all chats",
        "bot.rules.empty": "No rules yet.",
        "bot.rules.prompt": (
            "Send one rule per line:\n"
            "taxi — substring, case-insensitive "
            "(catches “need a TAXI”, “taxidermy”; misses “tax”, “taxes”)\n"
            "=taxi — whole word only "
            "(catches “taxi!”, “taxi-service”; misses “taxidermy”, “taxis”)\n"
            "re:tax[iy] — regular expression "
            "(catches “taxi”, “taxy”; misses “tax”, “taxes”)\n"
            "#promo — a hashtag is an ordinary keyword "
            "(catches “grab the #promo”; misses “promo” without the hash)"
        ),
        "bot.rules.report": "Added {added} · duplicates {duplicates} · invalid {invalid}",
        "bot.rules.line_invalid": "line {line}: {reason}",
        "bot.rules.line_duplicate": "line {line}: already there",
        "bot.rules.deleted": "Rule deleted.",
        "bot.rules.limit_chat": "You already have {limit} rules for this chat.",
        "bot.rules.limit_user": "You already have {limit} rules in total.",
        "bot.rules.too_many_lines": "Send at most {limit} lines at a time.",
        "bot.rules.disabled": "⚠️ Rule {pattern} was disabled automatically: {reason}",
        "bot.rules.kind.substring": "🔤",
        "bot.rules.kind.word": "🔠",
        "bot.rules.kind.regex": "🧩",
        "bot.ignored.title": "Ignored senders",
        "bot.ignored.empty": "Nobody is ignored yet.",
        "bot.ignored.added": "🚫 Ignoring {sender} in {chat}.",
        "bot.ignored.added_global": "🚫 Ignoring {sender} everywhere.",
        "bot.ignored.removed": "Sender is no longer ignored.",
        "bot.ignored.anonymous": "anonymous admins",
        "bot.ignored.all_chats_btn": "🚫 In all chats too",
        "bot.notif.drop_confirm": (
            "Remove the rule “{pattern}” ({scope})?\nIt will stop notifying you altogether."
        ),
        "bot.notif.drop_scope_chat": "this chat only: {chat}",
        "bot.notif.drop_done": "🗑 Rule “{pattern}” removed.",
        "bot.notif.drop_gone": "That rule is already gone.",
        "bot.settings.title": (
            "Language: {lang} · Timezone: {tz} · Digest threshold: {digest}\n"
            "Messages from bots: {bots}"
        ),
        "bot.settings.language": "🌐 Language",
        "bot.settings.timezone": "🕒 Timezone",
        "bot.settings.digest": "📦 Digest threshold",
        "bot.settings.digest_prompt": (
            "How many queued matches before I collapse them into one digest? "
            "Currently {n}. (0 = never, otherwise 2–500)"
        ),
        "bot.settings.digest_set": "Digest threshold is now {n}.",
        "bot.settings.bots_yes": "matched",
        "bot.settings.bots_no": "skipped",
        "bot.settings.bots_on": "🤖 Messages from bots: matched",
        "bot.settings.bots_off": "🤖 Messages from bots: skipped",
        "bot.settings.bots_on_hint": "🤖 Your rules now run on bot messages too.",
        "bot.settings.bots_off_hint": "🤖 Bot messages are skipped again.",
        "bot.settings.saved": "Saved.",
        "bot.status.title": "📊 Status",
        "bot.status.listener": "Listener: {state}",
        "bot.status.listener_ok": "connected",
        "bot.status.listener_degraded": "needs re-login — run `ddiver login`",
        "bot.status.uptime": "Uptime: {uptime}",
        "bot.status.chats": "Chats: {monitored} monitored, {unavailable} unavailable",
        "bot.status.counters": "Today: {seen} seen · {matched} matched · {delivered} delivered · {digested} digested · {failed} failed",
        "bot.status.queue": "Queue: {queue} · last update {last_update} ago",
        "bot.test.prompt": "Send the text you want to test.",
        "bot.test.match": "✅ {chat} — {rule} ({fragment})",
        "bot.test.excluded": "🚫 {chat} — excluded by {rule}",
        "bot.test.nomatch": "— {chat} — no match",
        "bot.test.no_chats": "You have no chats yet; testing global rules only.",
        "bot.users.title": (
            "👥 Users\n\n"
            "Anyone who sends /start to the bot lands here as “waiting” and gets nothing "
            "until you decide. An allowed moderator picks chats only from the ones you "
            "shared, and keeps their own rules, ignore list, language and timezone.\n"
        ),
        "bot.users.pending": "⏳ pending: {user_id}",
        "bot.users.allowed": "✅ {user_id} — {chats} chats, {rules} rules",
        "bot.users.denied": "⛔ {user_id}",
        "bot.users.allowed_toast": "User {user_id} allowed.",
        "bot.users.denied_toast": "User {user_id} denied.",
        "bot.users.revoked_toast": "Access revoked for {user_id}.",
        "bot.shared.hint": (
            "🗂 Shared chats\n\n"
            "Only your account listens to Telegram, so other moderators do not see all "
            "your chats — only the ones you open here.\n\n"
            "🗂 — open: a moderator can add it to their own list\n"
            "🔒 — yours only\n\n"
            "Tapping toggles it. Closing a chat removes it from everyone else's "
            "subscriptions; yours are untouched."
        ),
        "bot.shared.on": "🗂 {chat} is now shared.",
        "bot.shared.off": "{chat} is no longer shared; {n} subscriptions were removed.",
        "bot.shared.unshared_notice": "ℹ️ The owner stopped sharing {chat}; it was removed from your chats.",
        "bot.help": (
            "I watch the chats you pick and message you when something matches "
            "your rules.\n\n"
            "Rule syntax:\n"
            "taxi — substring, case-insensitive "
            "(catches “need a TAXI”, “taxidermy”; misses “tax”, “taxes”)\n"
            "=taxi — whole word only "
            "(catches “taxi!”, “taxi-service”; misses “taxidermy”, “taxis”)\n"
            "re:tax[iy] — regular expression "
            "(catches “taxi”, “taxy”; misses “tax”, “taxes”)\n\n"
            "Exclusions use the same syntax and win over keywords.\n"
            "Commands: /menu /test /status /cancel"
        ),
        "bot.cancelled": "Cancelled.",
    }
)
