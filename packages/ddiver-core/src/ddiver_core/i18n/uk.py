"""Ukrainian strings."""

STRINGS: dict[str, str] = {
    "rule.err.empty": "Порожній шаблон.",
    "rule.err.repeat_too_big": "Повтор більший за {limit} — такий шаблон з'їсть пам'ять під час компіляції.",
    "rule.err.unrolls_too_big": (
        "Такий шаблон під час компіляції розгорнеться більше ніж у {limit} шматків. "
        "Зменш повтори — вкладені перемножуються."
    ),
    "rule.err.too_long": "Шаблон довший за {limit} символів.",
    "rule.err.invalid_regex": "Некоректний регулярний вираз: {detail}",
    "rule.err.matches_empty": "Цей регулярний вираз збігається з порожнім текстом, тобто спрацює на кожне повідомлення.",
    "rule.err.no_phone": (
        "Телефон Telegram показує лише для тих, хто вже у ваших контактах, тож таке правило не впіймає нічого. Використайте @юзернейм або числовий id."
    ),
    "rule.err.bad_username": (
        "Це не схоже на юзернейм. У Telegram він 3–32 символи: літери, цифри й підкреслення."
    ),
    "rule.err.name_too_short": (
        "Ім'я шукається як частина рядка, тому потрібно щонайменше {limit} символи — інакше спрацює на пів чату."
    ),
    "rule.err.bad_sender_id": ("Це число завелике для id у Telegram."),
    "rule.err.too_slow": "Цей регулярний вираз надто повільний (катастрофічний перебір): {pattern}",
    "notif.chat": "📡 Чат",
    "notif.sender": "👤 Відправник",
    "notif.link": "🔗 Повідомлення",
    "notif.time": "🕒",
    "notif.rule": "🔑 Правило",
    "notif.rule_scope.chat": "правило чату",
    "notif.rule_scope.global": "правило для всіх чатів",
    "notif.missed": "⏪ пропущено, поки бот був офлайн",
    "notif.sender.deleted": "Видалений акаунт",
    "notif.sender.unknown": "Невідомий відправник",
    "notif.sender.by_id": "Користувач {id}",
    "notif.sender.anonymous": "Анонімний адмін",
    "notif.truncated": "… [+{n} символів]",
    "notif.btn.ignore": "🚫 Ігнорувати відправника",
    "notif.btn.drop_rule": "🗑 Прибрати це правило",
    "notif.forwarded": "↪️ переслано",
    "notif.private": "🔒 Закритий чат — посилання відкриється лише учасникам",
    "notif.late": "⏳ Опубліковано {ago}, з'явилося лише зараз",
}

STRINGS.update(
    {
        "digest.title": "📦 {n} збігів, поки ви були зайняті",
        "digest.item": "{rule} · {sender} · {snippet}",
        "digest.more_chats": "… і ще {n} чатів ({m} збігів)",
        "digest.more_items": "… і ще {n}",
        "digest.overflow": "⚠️ {n} збігів відкинуто: черга була переповнена",
        "notif.catchup_capped": "⚠️ Старіші повідомлення в цьому чаті не перевірені — спрацював ліміт наздоганяння",
    }
)


STRINGS.update(
    {
        "bot.gate.pending": "⏳ Запит на доступ чекає підтвердження власника.",
        "bot.gate.card": "👤 Користувач {user_id} просить доступ до ddiver.",
        "bot.gate.denied": "⛔ Доступ заборонено.",
        "bot.gate.allowed": "✅ Доступ відкрито. Надішліть /start, щоб налаштувати.",
        "bot.err.unknown_action": "Ця кнопка більше не працює.",
        "bot.err.owner_only": "Це може лише власник.",
        "bot.err.unknown_command": "Невідома команда. Скористайтеся /menu.",
        "bot.err.not_waiting": "Я не чекаю на введення — скористайтеся /menu.",
        "bot.err.not_found": "Не знайдено.",
        "bot.btn.allow": "✅ Дозволити",
        "bot.btn.deny": "⛔ Відмовити",
        "bot.btn.revoke": "🚫 Відкликати",
        "bot.btn.back": "⬅️ Назад",
        "bot.btn.prev": "◀️",
        "bot.btn.next": "▶️",
        "bot.btn.add": "➕ Додати",
        "bot.btn.drop_yes": "🗑 Так, прибрати",
        "bot.btn.cancel": "✖️ Скасувати",
        "bot.btn.all_chats": "🌐 Усі чати",
        "bot.btn.search": "🔍 Пошук",
        "bot.btn.include": "🔑 Ключові слова",
        "bot.btn.exclude": "➖ Винятки",
        "bot.cmd.start": "Почати й відкрити меню",
        "bot.cmd.menu": "Головне меню",
        "bot.cmd.test": "Перевірити текст на ваших правилах",
        "bot.cmd.status": "Що зараз відбувається",
        "bot.cmd.help": "Синтаксис правил і що вміє бот",
        "bot.cmd.cancel": "Скасувати поточне введення",
        "bot.cmd.export": "Вивантажити чати й правила у файл",
        "bot.cmd.import": "Завантажити чати й правила з файлу",
        "bot.export.caption": (
            "📦 Ваша конфігурація: чатів {chats}, правил {rules}, в ігнорі {ignored}.\n"
            "Тримайте файл десь іще — відновитися можна командою /import.\n"
            "Сесії Telegram тут немає й бути не може."
        ),
        "bot.import.prompt": (
            "<b>Завантаження конфігурації</b>\n\n"
            "Надішліть файл, який дав /export. Маленький можна вставити текстом.\n\n"
            "Завантаження <b>додає</b>, а не заміняє: нічого з уже налаштованого не зникне, "
            "дублікати просто пропустяться.\n\n"
            "Чати шукаються спочатку за @іменем, потім за id — тому публічні переїжджають між "
            "акаунтами, а приватні лише там, де слухач їх уже бачить."
        ),
        "bot.import.need_file": "Чекаю на файл або текст конфігурації. Скасувати — /cancel.",
        "bot.import.bad_file": "Не вдалося прочитати: {reason}",
        "bot.import.done": "✅ Завантажено: чатів {chats}, правил {rules}, в ігнорі {ignored}.",
        "bot.import.over_limit": (
            "⚠️ Не вмістилося правил: {n}. Ліміти — {chat} на чат і {total} загалом."
        ),
        "bot.import.duplicates": "↩️ Пропущено дублікатів правил: {n}.",
        "bot.import.chats_over_limit": "⚠️ Прочитав лише перші {n} чатів із файлу.",
        "bot.import.ignored_over_limit": (
            "⚠️ Не вмістилося ігнорованих відправників: {n}. Ліміт — {limit}."
        ),
        "bot.import.missing_chats": "⚠️ Не знайшов ці чати: {chats}",
        "bot.import.bad_rules": "⚠️ Не прийнято правила: {rules}",
        "bot.menu.title": "Що робимо?",
        "bot.menu.chats": "📡 Чати",
        "bot.menu.rules": "🔑 Правила",
        "bot.menu.ignored": "🚫 Ігнор",
        "bot.menu.settings": "⚙️ Налаштування",
        "bot.menu.status": "📊 Статус",
        "bot.menu.help": "❓ Довідка",
        "bot.menu.users": "👥 Користувачі",
        "bot.menu.shared": "🗂 Спільні чати",
        "bot.start.language": "Оберіть мову спілкування.",
        "bot.start.timezone": "Тепер часовий пояс — оберіть кнопкою або напишіть, напр. Europe/Kyiv.",
        "bot.start.tz_confirm": "Зараз у {tz}: {clock}. Правильно?",
        "bot.start.tz_unknown": "Не знаю такого поясу. Спробуйте назву міста, напр. Київ.",
        "bot.start.tz_candidates": "Можливо, ви мали на увазі:",
        "bot.start.tz_fixed_offset": "⚠️ Фіксований зсув не має переходу на літній час.",
        "bot.chats.title": ("Чати під наглядом ({n}). Дотик перемикає: ✅ — стежу, ☐ — ні."),
        "bot.chats.empty": "Поки нічого. Оберіть чат зі списку або через пошук.",
        "bot.chats.none_shared": "Власник поки не поділився жодним чатом.",
        "bot.chats.subscribed": "✅ Стежу за {chat}.",
        "bot.chats.unsubscribed": "☐ Більше не стежу за {chat}.",
        "bot.chats.search_prompt": "Надішліть частину назви чату або @username.",
        "bot.chats.search_empty": "Нічого не знайдено.",
        "bot.chats.lost_access": "⚠️ Слухач втратив доступ до {chat}; нагляд призупинено.",
        "bot.chats.regained_access": "✅ Доступ до {chat} відновлено.",
        "bot.chats.migrated": "ℹ️ {chat} став супергрупою; правила перенесено.",
        "bot.rules.pick_chat": "Для якого чату правила?",
        "bot.rules.title": "Правила для {chat}",
        "bot.rules.title_global": "Правила для всіх чатів",
        "bot.rules.empty": "Правил ще немає.",
        "bot.rules.prompt": (
            "<b>Правила</b> — по одному в рядку.\n\n"
            "<code>таксі</code> — підрядок, без урахування регістру\n"
            "  ✅ «потрібне ТАКСІ», «мототаксі»\n"
            "  ❌ «такса», «taxi»\n\n"
            "<code>=таксі</code> — лише ціле слово\n"
            "  ✅ «таксі!», «таксі-сервіс»\n"
            "  ❌ «мототаксі», «таксист»\n\n"
            "<code>from:@vasya</code> — стежити за відправником, а не за текстом\n"
            "  ✅ будь-яке повідомлення від @vasya, навіть картинка без підпису\n"
            "  ❌ чуже повідомлення зі словом «vasya»\n"
            "  Ще можна <code>from:123456789</code> (числовий id, його не змінити)\n"
            "  і <code>from:Вася Пупкін</code> (частина імені, без урахування регістру)\n\n"
            "<code>re:такс[иі]</code> — регулярний вираз\n"
            "  ✅ «таксі», «такси»\n"
            "  ❌ «такса», «taxi»\n\n"
            "<code>#промо</code> — хештег це звичайне ключове слово\n"
            "  ✅ «лови #промо»\n"
            "  ❌ «промо» без решітки"
        ),
        "bot.rules.report": "Додано {added} · дублікатів {duplicates} · помилок {invalid}",
        "bot.rules.line_invalid": "рядок {line}: {reason}",
        "bot.rules.line_duplicate": "рядок {line}: вже є",
        "bot.rules.deleted": "Правило видалено.",
        "bot.rules.limit_chat": "Для цього чату вже {limit} правил.",
        "bot.rules.limit_user": "У вас уже {limit} правил загалом.",
        "bot.rules.too_many_lines": "Не більше {limit} рядків за раз.",
        "bot.rules.disabled": "⚠️ Правило {pattern} вимкнено автоматично: {reason}",
        "bot.rules.kind.substring": "🔤",
        "bot.rules.kind.word": "🔠",
        "bot.rules.kind.regex": "🧩",
        "bot.rules.kind.sender": "👤",
        "bot.ignored.title": "Ігноровані відправники",
        "bot.ignored.empty": "Список порожній.",
        "bot.ignored.added": "🚫 Ігнорую {sender} у {chat}.",
        "bot.ignored.added_global": "🚫 Ігнорую {sender} всюди.",
        "bot.ignored.limit": "Список ігнору вже на межі: {limit}.",
        "bot.ignored.removed": "Відправник більше не ігнорується.",
        "bot.ignored.anonymous": "анонімних адмінів",
        "bot.ignored.all_chats_btn": "🚫 І в усіх чатах",
        "bot.notif.drop_confirm": (
            "Прибрати правило «{pattern}» ({scope})?\nСповіщення за ним більше не надходитимуть."
        ),
        "bot.notif.drop_scope_chat": "лише для чату {chat}",
        "bot.notif.drop_done": "🗑 Правило «{pattern}» прибрано.",
        "bot.notif.drop_gone": "Цього правила вже немає.",
        "bot.settings.title": (
            "Мова: {lang} · Часовий пояс: {tz} · Поріг дайджесту: {digest}\n"
            "Повідомлення ботів: {bots}"
        ),
        "bot.settings.language": "🌐 Мова",
        "bot.settings.timezone": "🕒 Часовий пояс",
        "bot.settings.digest": "📦 Поріг дайджесту",
        "bot.settings.digest_prompt": (
            "Скільки збігів у черзі збирати в один дайджест? Зараз {n}. (0 — ніколи, інакше 2–500)"
        ),
        "bot.settings.digest_set": "Поріг дайджесту тепер {n}.",
        "bot.settings.bots_yes": "враховую",
        "bot.settings.bots_no": "пропускаю",
        "bot.settings.bots_on": "🤖 Повідомлення ботів: враховую",
        "bot.settings.bots_off": "🤖 Повідомлення ботів: пропускаю",
        "bot.settings.bots_on_hint": "🤖 Тепер правила перевіряються і на повідомленнях ботів.",
        "bot.settings.bots_off_hint": "🤖 Повідомлення ботів знову пропускаються.",
        "bot.settings.weekly_on": "📅 Підсумки тижня: увімк",
        "bot.settings.weekly_off": "📅 Підсумки тижня: вимк",
        "bot.settings.backup_on": "💾 Бекап раз на тиждень: увімк",
        "bot.settings.backup_off": "💾 Бекап раз на тиждень: вимк",
        "bot.settings.weekly_hint": "Раз на сім днів, о десятій ранку за вашим часом.",
        "bot.settings.backup_hint": "Чати й правила файлом, разом із підсумками тижня.",
        "bot.settings.off_hint": "Вимкнено. Більше нічого не змінюється.",
        "bot.settings.saved": "Збережено.",
        "bot.status.title": "📊 Статус",
        "bot.status.listener": "Слухач: {state}",
        "bot.status.listener_ok": "підключений",
        "bot.status.listener_degraded": "потрібен повторний вхід — виконайте `ddiver login`",
        "bot.status.uptime": "Аптайм: {uptime}",
        "bot.status.chats": "Чатів: {monitored} під наглядом, {unavailable} без доступу",
        "bot.status.session": "Від моменту запуску:",
        "bot.status.counters": (
            "  переглянуто {seen} · збіглося {matched} · надіслано {delivered}\n"
            "  у дайджестах {digested} · помилок {failed}"
        ),
        "bot.status.lifetime": (
            "Усього за вашими правилами: {hits} — цей лічильник переживає перезапуск"
        ),
        "bot.status.queue": "Черга: {queue} · останнє оновлення {last_update} тому",
        "bot.test.prompt": (
            "<b>Перевірка правил</b>\n\n"
            "Надішліть будь-який текст — я проганяю його через ваші правила так само, як "
            "справжнє повідомлення, і покажу результат по кожному відстежуваному чату.\n\n"
            "Це перевірка тексту, а не пошук в історії: нікуди не лізу й період часу ні до "
            "чого. Відправник та ігнор-лист теж не враховуються — лише текст.\n\n"
            "Можна одразу однією командою:\n"
            "<code>/test дешево підвезу, таксі недорого</code>\n\n"
            "Що буде у відповіді по кожному чату:\n"
            "  ✅ спрацювало — яке правило і який шматок тексту\n"
            "  🚫 відсіяно вашим винятком\n"
            "  — збігів немає"
        ),
        "bot.test.match": "✅ {chat} — {rule} ({fragment})",
        "bot.test.excluded": "🚫 {chat} — виключено правилом {rule}",
        "bot.test.too_slow": "⏱ {chat} — правил забагато, перевірка не вклалася в час.",
        "bot.test.nomatch": "— {chat} — немає збігів",
        "bot.test.sender_rules": "(ще {n} правил from: — вони дивляться на відправника, текстом їх не перевірити)",
        "bot.test.no_chats": (
            "⚠️ Ви поки не стежите за жодним чатом, тож сповіщень не буде за жодним "
            "правилом. Нижче — що спрацювало б на правилах для всіх чатів."
        ),
        "bot.test.global_scope": "правила для всіх чатів",
        "bot.users.title": (
            "👥 Користувачі\n\n"
            "Кожен, хто напише боту /start, потрапляє сюди як «очікує» і до вашого "
            "рішення не отримує нічого. Допущений модератор обирає чати лише з "
            "відкритих вами й веде свої правила, ігнор-лист, мову та часовий пояс.\n"
        ),
        "bot.users.pending": "⏳ очікує: {user_id}",
        "bot.users.allowed": "✅ {user_id} — чатів: {chats}, правил: {rules}",
        "bot.users.denied": "⛔ {user_id}",
        "bot.users.allowed_toast": "Користувача {user_id} допущено.",
        "bot.users.denied_toast": "Користувачу {user_id} відмовлено.",
        "bot.users.revoked_toast": "Доступ для {user_id} відкликано.",
        "bot.shared.hint": (
            "🗂 Спільні чати\n\n"
            "Telegram слухає лише ваш акаунт, тому інші модератори бачать не всі ваші "
            "чати, а тільки ті, що ви відкрили тут.\n\n"
            "🗂 — відкритий: модератор може додати його собі\n"
            "🔒 — лише ваш\n\n"
            "Дотик перемикає. Коли ви закриваєте чат, він зникає з підписок усіх інших — "
            "на ваші підписки це не впливає."
        ),
        "bot.shared.on": "🗂 {chat} тепер доступний модераторам.",
        "bot.shared.off": "{chat} закрито; видалено підписок: {n}.",
        "bot.shared.unshared_notice": "ℹ️ Власник закрив доступ до {chat}; чат прибрано з ваших.",
        "bot.help": (
            "Я стежу за обраними чатами й пишу вам, коли щось збігається "
            "з вашими правилами.\n\n"
            "<b>Синтаксис правил</b>\n\n"
            "<code>таксі</code> — підрядок, без урахування регістру\n"
            "  ✅ «потрібне ТАКСІ», «мототаксі»\n"
            "  ❌ «такса», «taxi»\n\n"
            "<code>=таксі</code> — лише ціле слово\n"
            "  ✅ «таксі!», «таксі-сервіс»\n"
            "  ❌ «мототаксі», «таксист»\n\n"
            "<code>re:такс[иі]</code> — регулярний вираз\n"
            "  ✅ «таксі», «такси»\n"
            "  ❌ «такса», «taxi»\n\n"
            "<code>from:@vasya</code> — стежити за відправником\n"
            "  ✅ будь-яке його повідомлення; також за id або частиною імені\n"
            "  ❌ згадка @vasya в чужому тексті\n\n"
            "<b>Винятки</b>\n"
            "Той самий синтаксис, і вони важливіші за ключові слова.\n\n"
            "<b>Команди</b>\n"
            "/menu · /test · /status · /cancel"
        ),
        "weekly.title": "📅 Ваш тиждень",
        "weekly.total": "Спіймано: {n}",
        "weekly.no_baseline": "Порівнювати нема з чим — це перший повний тиждень.",
        "weekly.same": "Стільки ж, скільки минулого тижня ({n}).",
        "weekly.up": "На {pct}% більше, ніж минулого тижня ({n}).",
        "weekly.down": "На {pct}% менше, ніж минулого тижня ({n}).",
        "weekly.chats": "Найактивніші чати",
        "weekly.rules": "Що спрацьовувало",
        "weekly.nothing": "Тихий тиждень. Якщо це несподівано — /status покаже, чи не розлогінився слухач.",
        "weekly.backup": "Ваша конфігурація на випадок перевстановлення: чатів {chats}, правил {rules}, в ігнорі {ignored}. Відновити — /import.",
        "weekly.backup_too_big": "Ваша конфігурація: чатів {chats}, правил {rules}, в ігнорі {ignored}. Файл збережіть, але він більший, ніж приймає /import — щоб відновити, спершу доведеться скоротити ігнор-лист.",
        "bot.cancelled": "Скасовано.",
    }
)
