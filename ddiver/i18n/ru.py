"""Russian strings."""

STRINGS: dict[str, str] = {
    "rule.err.empty": "Пустой шаблон.",
    "rule.err.too_long": "Шаблон длиннее {limit} символов.",
    "rule.err.invalid_regex": "Некорректное регулярное выражение: {detail}",
    "rule.err.matches_empty": "Это регулярное выражение совпадает с пустым текстом, т.е. сработает на каждое сообщение.",
    "rule.err.too_slow": "Это регулярное выражение слишком медленное (катастрофический перебор): {pattern}",
    "notif.chat": "📡 Чат",
    "notif.sender": "👤 Отправитель",
    "notif.link": "🔗 Сообщение",
    "notif.time": "🕒",
    "notif.rule": "🔑 Правило",
    "notif.rule_scope.chat": "правило чата",
    "notif.rule_scope.global": "правило для всех чатов",
    "notif.missed": "⏪ пропущено, пока бот был офлайн",
    "notif.sender.deleted": "Удалённый аккаунт",
    "notif.sender.unknown": "Неизвестный отправитель",
    "notif.sender.anonymous": "Анонимный админ",
    "notif.truncated": "… [+{n} символов]",
    "notif.btn.ignore": "🚫 Игнорировать отправителя",
    "notif.btn.exclude": "➖ Добавить исключение",
    "notif.btn.profile": "👤 Профиль",
    "notif.forwarded": "↪️ переслано",
}

STRINGS.update(
    {
        "digest.title": "📦 {n} совпадений, пока вы были заняты",
        "digest.item": "{rule} · {sender} · {snippet}",
        "digest.more_chats": "… и ещё {n} чатов ({m} совпадений)",
        "digest.more_items": "… и ещё {n}",
        "digest.overflow": "⚠️ {n} совпадений отброшено: очередь была переполнена",
        "notif.catchup_capped": "⚠️ {n} более старых сообщений не проверено",
    }
)
