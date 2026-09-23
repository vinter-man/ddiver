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
    "notif.sender.by_id": "Пользователь {id}",
    "notif.sender.anonymous": "Анонимный админ",
    "notif.truncated": "… [+{n} символов]",
    "notif.btn.ignore": "🚫 Игнорировать отправителя",
    "notif.btn.drop_rule": "🗑 Убрать это правило",
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


STRINGS.update(
    {
        "bot.gate.pending": "⏳ Запрос на доступ ждёт подтверждения владельца.",
        "bot.gate.card": "👤 Пользователь {user_id} просит доступ к ddiver.",
        "bot.gate.denied": "⛔ Доступ запрещён.",
        "bot.gate.allowed": "✅ Доступ открыт. Отправьте /start для настройки.",
        "bot.err.unknown_action": "Эта кнопка больше не работает.",
        "bot.err.owner_only": "Это может только владелец.",
        "bot.err.unknown_command": "Неизвестная команда. Используйте /menu.",
        "bot.err.not_waiting": "Я не жду ввода — используйте /menu.",
        "bot.err.expired": "Действие устарело. Используйте меню.",
        "bot.err.not_found": "Не найдено.",
        "bot.btn.allow": "✅ Разрешить",
        "bot.btn.deny": "⛔ Отказать",
        "bot.btn.revoke": "🚫 Отозвать",
        "bot.btn.back": "⬅️ Назад",
        "bot.btn.prev": "◀️",
        "bot.btn.next": "▶️",
        "bot.btn.add": "➕ Добавить",
        "bot.btn.done": "✅ Готово",
        "bot.btn.drop_yes": "🗑 Да, убрать",
        "bot.btn.cancel": "✖️ Отмена",
        "bot.btn.all_chats": "🌐 Все чаты",
        "bot.btn.search": "🔍 Поиск",
        "bot.btn.include": "🔑 Ключевые слова",
        "bot.btn.exclude": "➖ Исключения",
        "bot.cmd.start": "Начать и открыть меню",
        "bot.cmd.menu": "Главное меню",
        "bot.cmd.test": "Проверить текст на ваших правилах",
        "bot.cmd.status": "Что сейчас происходит",
        "bot.cmd.help": "Синтаксис правил и что умеет бот",
        "bot.cmd.cancel": "Отменить текущий ввод",
        "bot.menu.title": "Что делаем?",
        "bot.menu.chats": "📡 Чаты",
        "bot.menu.rules": "🔑 Правила",
        "bot.menu.ignored": "🚫 Игнор",
        "bot.menu.settings": "⚙️ Настройки",
        "bot.menu.status": "📊 Статус",
        "bot.menu.help": "❓ Помощь",
        "bot.menu.users": "👥 Пользователи",
        "bot.menu.shared": "🗂 Общие чаты",
        "bot.start.language": "Выберите язык общения.",
        "bot.start.timezone": "Теперь часовой пояс — выберите кнопкой или напишите, например Europe/Kyiv.",
        "bot.start.tz_confirm": "Сейчас в {tz}: {clock}. Верно?",
        "bot.start.tz_unknown": "Не знаю такой часовой пояс. Попробуйте название города, например Киев.",
        "bot.start.tz_candidates": "Возможно, вы имели в виду:",
        "bot.start.tz_fixed_offset": "⚠️ У фиксированного смещения нет перехода на летнее время.",
        "bot.start.done": "Готово. Теперь можно добавить чаты.",
        "bot.chats.title": (
            "Чаты под наблюдением ({n}). Нажатие переключает: ✅ — слежу, ☐ — нет."
        ),
        "bot.chats.empty": "Пока ничего. Выберите чат из списка или через поиск.",
        "bot.chats.none_shared": "Владелец пока не поделился ни одним чатом.",
        "bot.chats.subscribed": "✅ Слежу за {chat}.",
        "bot.chats.unsubscribed": "☐ Больше не слежу за {chat}.",
        "bot.chats.unavailable": "⚠️ {chat} — нет доступа",
        "bot.chats.search_prompt": "Пришлите часть названия чата или @username.",
        "bot.chats.search_empty": "Ничего не найдено.",
        "bot.chats.lost_access": "⚠️ Слушатель потерял доступ к {chat}; наблюдение приостановлено.",
        "bot.chats.regained_access": "✅ Доступ к {chat} восстановлен.",
        "bot.chats.migrated": "ℹ️ {chat} стал супергруппой; правила перенесены.",
        "bot.rules.pick_chat": "Для какого чата правила?",
        "bot.rules.title": "Правила для {chat}",
        "bot.rules.title_global": "Правила для всех чатов",
        "bot.rules.empty": "Правил пока нет.",
        "bot.rules.prompt": (
            "<b>Правила</b> — по одному в строке.\n\n"
            "<code>такси</code> — подстрока, без учёта регистра\n"
            "  ✅ «нужно ТАКСИ», «мототакси»\n"
            "  ❌ «такса», «taxi»\n\n"
            "<code>=такси</code> — только целое слово\n"
            "  ✅ «такси!», «такси-сервис»\n"
            "  ❌ «мототакси», «таксист»\n\n"
            "<code>re:такс[иі]</code> — регулярное выражение\n"
            "  ✅ «такси», «таксі»\n"
            "  ❌ «такса», «taxi»\n\n"
            "<code>#промо</code> — хештег это обычное ключевое слово\n"
            "  ✅ «лови #промо»\n"
            "  ❌ «промо» без решётки"
        ),
        "bot.rules.report": "Добавлено {added} · дубликатов {duplicates} · ошибок {invalid}",
        "bot.rules.line_invalid": "строка {line}: {reason}",
        "bot.rules.line_duplicate": "строка {line}: уже есть",
        "bot.rules.deleted": "Правило удалено.",
        "bot.rules.limit_chat": "Для этого чата уже {limit} правил.",
        "bot.rules.limit_user": "У вас уже {limit} правил всего.",
        "bot.rules.too_many_lines": "Не больше {limit} строк за раз.",
        "bot.rules.disabled": "⚠️ Правило {pattern} отключено автоматически: {reason}",
        "bot.rules.kind.substring": "🔤",
        "bot.rules.kind.word": "🔠",
        "bot.rules.kind.regex": "🧩",
        "bot.ignored.title": "Игнорируемые отправители",
        "bot.ignored.empty": "Список пуст.",
        "bot.ignored.added": "🚫 Игнорирую {sender} в {chat}.",
        "bot.ignored.added_global": "🚫 Игнорирую {sender} везде.",
        "bot.ignored.removed": "Отправитель больше не игнорируется.",
        "bot.ignored.anonymous": "анонимных админов",
        "bot.ignored.all_chats_btn": "🚫 И во всех чатах",
        "bot.notif.drop_confirm": (
            "Убрать правило «{pattern}» ({scope})?\nУведомления по нему больше приходить не будут."
        ),
        "bot.notif.drop_scope_chat": "только для чата {chat}",
        "bot.notif.drop_done": "🗑 Правило «{pattern}» убрано.",
        "bot.notif.drop_gone": "Этого правила уже нет.",
        "bot.settings.title": (
            "Язык: {lang} · Часовой пояс: {tz} · Порог дайджеста: {digest}\nСообщения ботов: {bots}"
        ),
        "bot.settings.language": "🌐 Язык",
        "bot.settings.timezone": "🕒 Часовой пояс",
        "bot.settings.digest": "📦 Порог дайджеста",
        "bot.settings.digest_prompt": (
            "Сколько совпадений в очереди собирать в один дайджест? "
            "Сейчас {n}. (0 — никогда, иначе 2–500)"
        ),
        "bot.settings.digest_set": "Порог дайджеста теперь {n}.",
        "bot.settings.bots_yes": "учитываю",
        "bot.settings.bots_no": "пропускаю",
        "bot.settings.bots_on": "🤖 Сообщения ботов: учитываю",
        "bot.settings.bots_off": "🤖 Сообщения ботов: пропускаю",
        "bot.settings.bots_on_hint": "🤖 Теперь правила проверяются и на сообщениях ботов.",
        "bot.settings.bots_off_hint": "🤖 Сообщения ботов снова пропускаются.",
        "bot.settings.saved": "Сохранено.",
        "bot.status.title": "📊 Статус",
        "bot.status.listener": "Слушатель: {state}",
        "bot.status.listener_ok": "подключён",
        "bot.status.listener_degraded": "нужен повторный вход — выполните `ddiver login`",
        "bot.status.uptime": "Аптайм: {uptime}",
        "bot.status.chats": "Чатов: {monitored} под наблюдением, {unavailable} без доступа",
        "bot.status.session": "С момента запуска:",
        "bot.status.counters": (
            "  просмотрено {seen} · совпало {matched} · отправлено {delivered}\n"
            "  в дайджестах {digested} · ошибок {failed}"
        ),
        "bot.status.lifetime": (
            "Всего по вашим правилам: {hits} — этот счётчик переживает перезапуск"
        ),
        "bot.status.queue": "Очередь: {queue} · последнее обновление {last_update} назад",
        "bot.test.prompt": "Пришлите текст для проверки.",
        "bot.test.match": "✅ {chat} — {rule} ({fragment})",
        "bot.test.excluded": "🚫 {chat} — исключено правилом {rule}",
        "bot.test.nomatch": "— {chat} — нет совпадений",
        "bot.test.no_chats": "У вас нет чатов; проверяю только глобальные правила.",
        "bot.users.title": (
            "👥 Пользователи\n\n"
            "Каждый, кто напишет боту /start, попадает сюда как «ожидает» и до вашего "
            "решения не получает ничего. Допущенный модератор выбирает чаты только из "
            "открытых вами и ведёт свои правила, игнор-лист, язык и часовой пояс.\n"
        ),
        "bot.users.pending": "⏳ ожидает: {user_id}",
        "bot.users.allowed": "✅ {user_id} — чатов: {chats}, правил: {rules}",
        "bot.users.denied": "⛔ {user_id}",
        "bot.users.allowed_toast": "Пользователь {user_id} допущен.",
        "bot.users.denied_toast": "Пользователю {user_id} отказано.",
        "bot.users.revoked_toast": "Доступ для {user_id} отозван.",
        "bot.shared.hint": (
            "🗂 Общие чаты\n\n"
            "Telegram слушает только ваш аккаунт, поэтому другие модераторы видят не все "
            "ваши чаты, а лишь те, что вы открыли здесь.\n\n"
            "🗂 — открыт: модератор может добавить его себе\n"
            "🔒 — только ваш\n\n"
            "Нажатие переключает. Когда закрываете чат, он исчезает из подписок всех "
            "остальных — на ваши подписки это не влияет."
        ),
        "bot.shared.on": "🗂 {chat} теперь доступен модераторам.",
        "bot.shared.off": "{chat} закрыт; удалено подписок: {n}.",
        "bot.shared.unshared_notice": "ℹ️ Владелец закрыл доступ к {chat}; чат убран из ваших.",
        "bot.help": (
            "Я слежу за выбранными чатами и пишу вам, когда что-то совпадает "
            "с вашими правилами.\n\n"
            "<b>Синтаксис правил</b>\n\n"
            "<code>такси</code> — подстрока, без учёта регистра\n"
            "  ✅ «нужно ТАКСИ», «мототакси»\n"
            "  ❌ «такса», «taxi»\n\n"
            "<code>=такси</code> — только целое слово\n"
            "  ✅ «такси!», «такси-сервис»\n"
            "  ❌ «мототакси», «таксист»\n\n"
            "<code>re:такс[иі]</code> — регулярное выражение\n"
            "  ✅ «такси», «таксі»\n"
            "  ❌ «такса», «taxi»\n\n"
            "<b>Исключения</b>\n"
            "Тот же синтаксис, и они важнее ключевых слов.\n\n"
            "<b>Команды</b>\n"
            "/menu · /test · /status · /cancel"
        ),
        "bot.cancelled": "Отменено.",
    }
)
