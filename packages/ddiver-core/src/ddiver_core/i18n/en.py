"""English strings (the reference catalog; other languages must define the same keys)."""

STRINGS: dict[str, str] = {
    # rule validation
    "rule.err.empty": "Empty pattern.",
    "rule.err.repeat_too_big": "A repetition larger than {limit} would use too much memory to compile.",
    "rule.err.unrolls_too_big": (
        "This pattern would expand into more than {limit} pieces while compiling. "
        "Make the repetitions smaller — nested ones multiply."
    ),
    "rule.err.too_long": "Pattern is longer than {limit} characters.",
    "rule.err.invalid_regex": "Invalid regular expression: {detail}",
    "rule.err.matches_empty": "This regular expression matches empty text, so it would match every message.",
    "rule.err.no_phone": (
        "Telegram only shows a phone number for somebody already in your contacts, so a phone rule would match nothing. Use @username or the numeric id."
    ),
    "rule.err.bad_username": (
        "That does not look like a username. A Telegram username is 3–32 characters: letters, digits and underscores."
    ),
    "rule.err.name_too_short": (
        "A name is matched as a fragment, so it needs at least {limit} characters — otherwise it fires on half the chat."
    ),
    "rule.err.bad_sender_id": ("That number is too large to be a Telegram id."),
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
    "notif.forwarded": "↪️ forwarded",
    "notif.private": "🔒 Private chat — the link opens for members only",
    "notif.late": "⏳ Posted {ago}, appeared only now",
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
        "notif.catchup_capped": "⚠️ Older messages in this chat were not checked — the catch-up limit was reached",
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
        "bot.err.not_found": "Not found.",
        "bot.btn.allow": "✅ Allow",
        "bot.btn.deny": "⛔ Deny",
        "bot.btn.revoke": "🚫 Revoke",
        "bot.btn.back": "⬅️ Back",
        "bot.btn.prev": "◀️",
        "bot.btn.next": "▶️",
        "bot.btn.add": "➕ Add",
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
        "bot.cmd.export": "Save your chats and rules to a file",
        "bot.cmd.import": "Load chats and rules from a file",
        "bot.export.caption": (
            "📦 Your configuration: {chats} chats, {rules} rules, {ignored} ignored.\n"
            "Keep the file somewhere else — /import restores it.\n"
            "It contains no Telegram session, and never will."
        ),
        "bot.import.prompt": (
            "<b>Load a configuration</b>\n\n"
            "Send the file /export gave you. A small one can be pasted as text.\n\n"
            "Importing <b>adds</b> rather than replaces: nothing you already have is removed, "
            "and duplicates are skipped.\n\n"
            "Chats are matched by @username first and by id second — so public chats move "
            "between accounts, private ones only where the listener already sees them."
        ),
        "bot.import.need_file": "Waiting for the file or the configuration text. /cancel to stop.",
        "bot.import.bad_file": "Could not read it: {reason}",
        "bot.import.done": "✅ Imported: {chats} chats, {rules} rules, {ignored} ignored.",
        "bot.import.over_limit": (
            "⚠️ {n} rules did not fit. The caps are {chat} per chat and {total} in total."
        ),
        "bot.import.duplicates": "↩️ Skipped {n} duplicate rules.",
        "bot.import.chats_over_limit": "⚠️ Only the first {n} chats in the file were read.",
        "bot.import.ignored_over_limit": ("⚠️ {n} ignored senders did not fit. The cap is {limit}."),
        "bot.import.missing_chats": "⚠️ These chats were not found: {chats}",
        "bot.import.bad_rules": "⚠️ These rules were rejected: {rules}",
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
        "bot.chats.title": ("Chats you are monitoring ({n}). Tapping toggles: ✅ on, ☐ off."),
        "bot.chats.empty": "No chats yet. Use search or pick one from the list.",
        "bot.chats.none_shared": "The owner has not shared any chats with you yet.",
        "bot.chats.subscribed": "✅ Now monitoring {chat}.",
        "bot.chats.unsubscribed": "☐ No longer monitoring {chat}.",
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
            "<b>Rules</b> — one per line.\n\n"
            "<code>taxi</code> — substring, case-insensitive\n"
            "  ✅ “need a TAXI”, “taxidermy”\n"
            "  ❌ “tax”, “taxes”\n\n"
            "<code>=taxi</code> — whole word only\n"
            "  ✅ “taxi!”, “taxi-service”\n"
            "  ❌ “taxidermy”, “taxis”\n\n"
            "<code>from:@vasya</code> — watch the sender, not the text\n"
            "  ✅ anything @vasya posts, even a photo with no caption\n"
            '  ❌ somebody else\'s message that says "vasya"\n'
            "  Also <code>from:123456789</code> (a numeric id, which they cannot change)\n"
            "  and <code>from:John Smith</code> (part of a name, case-insensitive)\n\n"
            "<code>re:tax[iy]</code> — regular expression\n"
            "  ✅ “taxi”, “taxy”\n"
            "  ❌ “tax”, “taxes”\n\n"
            "<code>#promo</code> — a hashtag is an ordinary keyword\n"
            "  ✅ “grab the #promo”\n"
            "  ❌ “promo” without the hash"
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
        "bot.rules.kind.sender": "👤",
        "bot.ignored.title": "Ignored senders",
        "bot.ignored.empty": "Nobody is ignored yet.",
        "bot.ignored.added": "🚫 Ignoring {sender} in {chat}.",
        "bot.ignored.added_global": "🚫 Ignoring {sender} everywhere.",
        "bot.ignored.limit": "Your ignore list is already at its limit of {limit}.",
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
        "bot.settings.weekly_on": "📅 Weekly summary: on",
        "bot.settings.weekly_off": "📅 Weekly summary: off",
        "bot.settings.backup_on": "💾 Weekly backup: on",
        "bot.settings.backup_off": "💾 Weekly backup: off",
        "bot.settings.weekly_hint": "Every seven days, at ten in the morning your time.",
        "bot.settings.backup_hint": "Your chats and rules as a file, with the weekly summary.",
        "bot.settings.off_hint": "Turned off. Nothing else changes.",
        "bot.settings.saved": "Saved.",
        "bot.status.title": "📊 Status",
        "bot.status.listener": "Listener: {state}",
        "bot.status.listener_ok": "connected",
        "bot.status.listener_degraded": "needs re-login — run `ddiver login`",
        "bot.status.uptime": "Uptime: {uptime}",
        "bot.status.chats": "Chats: {monitored} monitored, {unavailable} unavailable",
        "bot.status.session": "Since the last start:",
        "bot.status.counters": (
            "  seen {seen} · matched {matched} · delivered {delivered}\n"
            "  digested {digested} · failed {failed}"
        ),
        "bot.status.lifetime": (
            "Across your rules, all time: {hits} — this one survives a restart"
        ),
        "bot.status.queue": "Queue: {queue} · last update {last_update} ago",
        "bot.test.prompt": (
            "<b>Rule check</b>\n\n"
            "Send any text and I will run it through your rules exactly as if it were a real "
            "message, then show the verdict for every chat you watch.\n\n"
            "This checks a text, it does not search history: nothing is looked up and no time "
            "window applies. The sender and your ignore list are not considered either — text "
            "only.\n\n"
            "You can do it in one go:\n"
            "<code>/test cheap ride, taxi anywhere</code>\n\n"
            "What comes back, per chat:\n"
            "  ✅ matched — which rule, and the fragment it hit\n"
            "  🚫 dropped by one of your exclusions\n"
            "  — no match"
        ),
        "bot.test.match": "✅ {chat} — {rule} ({fragment})",
        "bot.test.excluded": "🚫 {chat} — excluded by {rule}",
        "bot.test.too_slow": "⏱ {chat} — too many rules to check within the time budget.",
        "bot.test.nomatch": "— {chat} — no match",
        "bot.test.sender_rules": "(plus {n} from: rules — those match on who sent a message, so a text test cannot answer for them)",
        "bot.test.no_chats": (
            "⚠️ You are not watching any chat yet, so no rule can notify you. Below is "
            "what your rules for all chats would have done."
        ),
        "bot.test.global_scope": "rules for all chats",
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
            "<b>Rule syntax</b>\n\n"
            "<code>taxi</code> — substring, case-insensitive\n"
            "  ✅ “need a TAXI”, “taxidermy”\n"
            "  ❌ “tax”, “taxes”\n\n"
            "<code>=taxi</code> — whole word only\n"
            "  ✅ “taxi!”, “taxi-service”\n"
            "  ❌ “taxidermy”, “taxis”\n\n"
            "<code>re:tax[iy]</code> — regular expression\n"
            "  ✅ “taxi”, “taxy”\n"
            "  ❌ “tax”, “taxes”\n\n"
            "<code>from:@vasya</code> — watch the sender\n"
            "  ✅ anything they post; also by numeric id or part of a name\n"
            "  ❌ a mention of @vasya in somebody else's text\n\n"
            "<b>Exclusions</b>\n"
            "Same syntax, and they win over keywords.\n\n"
            "<b>Commands</b>\n"
            "/menu · /test · /status · /cancel"
        ),
        "weekly.title": "📅 Your week",
        "weekly.total": "Caught: {n}",
        "weekly.no_baseline": "Nothing to compare with — this is the first full week.",
        "weekly.same": "The same as last week ({n}).",
        "weekly.up": "{pct}% more than last week ({n}).",
        "weekly.down": "{pct}% less than last week ({n}).",
        "weekly.chats": "Busiest chats",
        "weekly.rules": "Rules that fired",
        "weekly.nothing": "Quiet week. If that is a surprise, /status says whether the listener is still logged in.",
        "weekly.backup": "Your configuration, in case you ever need it back: {chats} chats, {rules} rules, {ignored} ignored. /import restores it.",
        "weekly.backup_too_big": "Your configuration: {chats} chats, {rules} rules, {ignored} ignored. Keep the file — but it is larger than /import accepts, so restoring it means trimming the ignore list first.",
        "bot.cancelled": "Cancelled.",
    }
)
