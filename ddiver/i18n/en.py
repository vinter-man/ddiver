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
    "notif.sender.anonymous": "Anonymous admin",
    "notif.truncated": "… [+{n} more characters]",
    "notif.btn.ignore": "🚫 Ignore sender",
    "notif.btn.exclude": "➖ Add exclusion",
    "notif.btn.profile": "👤 Profile",
    "notif.forwarded": "↪️ forwarded",
}
