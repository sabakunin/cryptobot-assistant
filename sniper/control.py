import time
import uuid

from .accounts import AccountMissing, remove_account, select_account
from .engine import CAPABILITY_TEXT, active_buttons, audit, button, configured, notice
from .parser import ParseError, money, parse_offer
from .security import safe_link


def accounts_menu(state, runtime):
    accounts = state.get("accounts", {})
    rows, lines = (
        [],
        ["👥 Аккаунты", "", "Каждый аккаунт ищет отдельно. Выбор здесь меняет только панель управления."],
    )
    for account_id, account in accounts.items():
        selected = state.get("selected_account") == account_id
        data = state if selected else account["profile"]
        status_text = (
            "открыта сделка"
            if data["active"]
            else ("поиск включён" if data["settings"]["running"] else "пауза")
        )
        lines.append(f"\n{'✓ ' if selected else ''}{account['label']} — {status_text}")
        rows.append(
            [button(("✓ " if selected else "") + account["label"], callback=f"account:select:{account_id}")]
        )
    if runtime.get("connect_url"):
        rows.append([{"text": "＋ Добавить аккаунт · QR", "web_app": {"url": runtime["connect_url"]}}])
    else:
        rows.append([button("＋ Добавить аккаунт", callback="account:setup")])
    if accounts:
        rows.append([button("⏸ Остановить поиск на всех", callback="stopall")])
        selected = state.get("selected_account")
        if selected:
            rows.append([button("Удалить выбранный аккаунт…", callback=f"account:remove:{selected}")])
    return notice("\n".join(lines), rows)


def menu(state, runtime=None):
    s = state["settings"]
    ready = configured(s)
    status = (
        "⏸ Открыта сделка"
        if state["active"]
        else (
            "🟢 Поиск включён"
            if s["running"]
            else ("⏸ Поиск на паузе" if ready else "⚙️ Для запуска укажи курс")
        )
    )
    rate = f"≤ {s['auto_max']} ₽ / USDT" if s["auto_max"] else "не задан"
    minimum = f"от {s['min_order_rub']} ₽" if s.get("min_order_rub") else "без минимума"
    listener = (runtime or {}).get("listener", "off")
    connection = {
        "userbot": "подключён",
        "passive": "чтение сообщений",
        "off": "отключён",
        "connecting": "подключается",
        "flood_wait": "пауза Telegram",
        "disconnected": "переподключение",
        "authorization_required": "нужен вход",
    }.get(listener, listener)
    text = (
        f"CryptoBot Assistant · Автохав\nUSDT / RUB · СБП\n\n{status}\n"
        f"Курс: {rate}\nСумма покупки: {minimum}\n\nCryptoBot: {connection}\n"
        "Первая страница · одна активная сделка на аккаунт"
    )
    account = state.get("accounts", {}).get(state.get("selected_account"))
    if account:
        text += f"\nАккаунт: {account['label']}"
    if state["active"]:
        primary = button("📦 Открыть активную сделку", callback="deal")
    elif s["running"]:
        primary = button("⏸ Приостановить поиск", callback="stop")
    elif not ready and not state["active"]:
        primary = button(
            "✏️ Задать максимальный курс",
            callback="set:auto_max",
        )
    else:
        primary = button("▶️ Запустить поиск", callback="run")
    rows = [
        [primary],
        [
            button("Курс: " + (s["auto_max"] or "задать"), callback="set:auto_max"),
            button("Мин. сумма: " + (s.get("min_order_rub") or "нет"), callback="set:min_order_rub"),
        ],
        [button("📦 Сделка", callback="deal"), button("👥 Аккаунты", callback="accounts")],
        [button("↻ Обновить", callback="menu"), button("Как пользоваться", callback="help")],
    ]
    if state["active"] and s["running"]:
        rows.append([button("⏸ Остановить дальнейший поиск", callback="stop")])
    if (runtime or {}).get("is_admin"):
        rows.append([button("🔐 Доступ пользователей", callback="access:list:0")])
    return notice(text, rows)


def rates(state):
    s = state["settings"]
    text = (
        f"⚙️ Настройки Автохава\n\nКурс ≤ {s['auto_max'] or '—'} ₽ / USDT.\n"
        f"Минимальная сумма покупки: {s.get('min_order_rub') or 'не задана'} ₽.\n"
        "Покупается весь доступный максимум. Минимум необязателен и проверяется перед созданием сделки."
    )
    rows = [
        [
            button("Изменить курс", callback="set:auto_max"),
            button("Минимальная сумма", callback="set:min_order_rub"),
        ]
    ]
    if configured(s) and not s["running"] and not state["active"]:
        text += "\n\nКурс сохранён. Теперь можно запустить поиск."
        rows.insert(0, [button("▶️ Запустить поиск", callback="run")])
    return notice(text, rows)


def deal(state):
    active = state["active"]
    if not active:
        return notice(
            "📦 Сделка\n\nВ помощнике нет активной сделки.",
            [
                [button("Проверить в CryptoBot", callback="reconcile")],
                [button("Открыть CryptoBot ↗", url="https://t.me/CryptoBot")],
            ],
        )
    labels = {
        "REQUESTED": "Ждём продавца",
        "ACCEPTED": "Продавец принял сделку",
        "PAID": "Отмечена оплата",
        "PREPARING": "Подготовка",
        "ATTEMPTING": "Запрос отправлен",
        "CANCELLING": "Отмена выполняется",
        "UNKNOWN": "Нужна проверка в CryptoBot",
    }
    state_text = (
        "Отмена выполняется"
        if active.get("cancel_requested")
        else labels.get(active.get("stage"), "Поиск приостановлен")
    )
    return notice(
        f"📦 Сделка #{active.get('deal_id') or '—'}\n\n{state_text}\n"
        f"{active.get('volume', '—')} USDT · {active.get('rubles', '—')} RUB",
        active_buttons(active),
    )


def panel_response(state, message, *, home=False):
    revision = state.get("ui_revision", 0) + 1
    state["ui_revision"] = revision
    rows = [list(row) for row in message.get("buttons", [])]
    if not home and not any(item.get("callback_data") == "menu" for row in rows for item in row):
        rows.append([button("‹ Главное меню", callback="menu")])
    account_id = state.get("selected_account")
    scope = f"acct:{account_id}:" if account_id else ""
    buttons = [
        [
            {
                **item,
                "callback_data": f"ui:{revision}:"
                + ("" if item["callback_data"].startswith("access:") else scope)
                + item["callback_data"],
            }
            if "callback_data" in item
            else dict(item)
            for item in row
        ]
        for row in rows
    ]
    return {
        **message,
        "buttons": buttons,
        "kind": "panel",
        "_open": state.get("panel_open_epoch"),
        "_revision": revision,
    }


def confirmation_action(action):
    if action.startswith("acct:"):
        action = action.split(":", 2)[-1]
    return action.startswith(("armed:", "cancelok:", "resolved:", "closed:", "account:delete:", "access:"))


def callback_hint(state, callback):
    data = callback.get("data", "")
    if data.startswith("ui:"):
        parts = data.split(":", 2)
        if len(parts) == 3:
            _, revision, action = parts
            if revision != str(state.get("ui_revision", 0)) and confirmation_action(action):
                return "Состояние изменилось. Открываю актуальное управление."
    return None


def status(state, runtime):
    stats = state["stats"]
    return notice(
        "📊 Состояние\n"
        f"Прочитано карточек: {stats['observed']}\nСовпадений: {stats['eligible']}\n"
        f"Отфильтровано: {stats['filtered']}\n"
        f"Ошибок разбора: {stats['parse_errors']}\n"
        f"Поиск: {'включён' if state['settings']['running'] else 'на паузе'}\n"
        f"Активная сделка: {'да' if state['active'] else 'нет'}"
    )


def history(state):
    items = list(state["offers"].values())[-10:][::-1]
    text = "📋 Последние карточки\n" + (
        "\n".join(f"#{o['id']} · {o['price']} ₽ · {o['volume']} USDT" for o in items)
        if items
        else "Пока пусто."
    )
    return notice(text)


def from_message(message):
    text = message.get("text") or message.get("caption") or ""
    links = [
        e.get("url")
        for e in message.get("entities", []) + message.get("caption_entities", [])
        if e.get("type") == "text_link"
    ]
    link = next((url for url in links if safe_link(url) != "https://t.me/CryptoBot"), None)
    return parse_offer(text, link=link, source="manual")


def handle(state, update, runtime):
    callback = update.get("callback_query")
    if callback:
        action = callback.get("data", "")
        fresh_ui = action.startswith("ui:")
        if action.startswith("ui:"):
            parts = action.split(":", 2)
            if len(parts) != 3:
                return []
            _, revision, action = parts
            plain_action = action.split(":", 2)[-1] if action.startswith("acct:") else action
            if (
                revision != str(state.get("ui_revision", 0))
                and plain_action == "run"
                and state.get("arm")
                and state["arm"]["expires"] > time.time()
            ):
                return []
            if revision != str(state.get("ui_revision", 0)) and confirmation_action(action):
                return [panel_response(state, menu(state, runtime), home=True)]
            message_id = callback.get("message", {}).get("message_id")
            binding = state.get("control_panel", {})
            if message_id and (not binding.get("message_id") or binding.get("send_attempt")):
                state["control_panel"] = {
                    "message_id": message_id,
                    "open_epoch": state.get("panel_open_epoch"),
                }
        if action.startswith("acct:"):
            parts = action.split(":", 2)
            if len(parts) != 3:
                return []
            _, account_id, action = parts
            try:
                select_account(state, account_id)
            except AccountMissing:
                return [panel_response(state, notice("Этот аккаунт уже удалён. Открой «Аккаунты»."))]
            runtime = {
                **runtime,
                "listener": runtime.get("accounts", {}).get(account_id, runtime.get("listener", "off")),
            }
        if not fresh_ui and action.startswith(
            ("armed:", "cancelok:", "resolved:", "closed:", "account:delete:")
        ):
            # Confirmations from an obsolete UI must be reviewed in the new panel.
            return [panel_response(state, notice("Эта кнопка устарела. Открой нужное действие заново."))]
        update = {**update, "callback_query": {**callback, "data": action}}
    message = update.get("message") or update.get("edited_message") or {}
    command_text = (message.get("text") or "").strip().split(maxsplit=1)
    if command_text and command_text[0].split("@", 1)[0] == "/start":
        # A requested entry point must be visible below the owner's message.
        # Burst commands share one opening; ordinary button navigation still edits it.
        if time.time() - state.get("panel_open_at", 0) >= 5:
            state["panel_open_epoch"] = uuid.uuid4().hex
            state["panel_open_at"] = time.time()
    result = _handle(state, update, runtime)
    if result:
        panel = panel_response(state, result[0], home=result[0]["text"].startswith("CryptoBot Assistant"))
        resume_command = (message.get("text") or "").split("@", 1)[0] == "/resume"
        if state["reconcile_requested"] and ((callback and action in {"run", "reconcile"}) or resume_command):
            state["reconcile_panel_revision"] = state["ui_revision"]
        return [panel, *result[1:]]
    return result


def _handle(state, update, runtime):
    callback = update.get("callback_query")
    if callback:
        return handle_action(state, callback.get("data", ""), runtime)
    message = update.get("message") or update.get("edited_message") or {}
    text = (message.get("text") or message.get("caption") or "").strip()
    command = text.split(maxsplit=1)[0].split("@")[0] if text.startswith("/") else ""
    if command:
        state["pending"] = None
        commands = {
            "/start": "menu",
            "/menu": "menu",
            "/settings": "rates",
            "/status": "stats",
            "/stop": "stop",
            "/resume": "run",
            "/history": "history",
            "/accounts": "accounts",
            "/stopall": "stopall",
        }
        if command in commands:
            return handle_action(state, commands[command], runtime)
        if command == "/help":
            return handle_action(state, "help", runtime)
        if command == "/inspect":
            state["inspect_requested"] = True
            return [notice("Запрошен текущий экран CryptoBot (без callback-данных и секретов).")]
        if command == "/resolve":
            nonce = uuid.uuid4().hex[:12]
            state["resolve"] = {"nonce": nonce, "expires": time.time() + 600}
            return [
                notice(
                    "Открой «Мои сделки» в CryptoBot и проверь, что активных сделок действительно нет. "
                    "Подтверждение ниже сбросит локальную блокировку. Оно ничего не отменяет в CryptoBot.",
                    [[button("Проверил: активных сделок нет", callback=f"resolved:{nonce}")]],
                )
            ]
        if command == "/trade":
            import re

            args = text.split(maxsplit=1)
            ref = args[1].strip().upper().lstrip("#").replace("Д", "D") if len(args) > 1 else ""
            if not re.fullmatch(r"D\d{3,}", ref) or not state["active"] or not state["active"].get("managed"):
                return [notice("Для привязки неопределённой операции: /trade D123456 (ID из CryptoBot).")]
            state["active"]["deal_id"] = ref
            state["reconcile_requested"] = True
            audit(state, "manual_trade_binding", deal_id=ref)
            return [notice("ID привязан по твоему указанию. Запрошена проверка статуса.")]
        if command == "/diagnostics":
            # No source text, counterparty, URLs, auth data, account/user ids or session strings.
            return [
                notice(
                    f"Диагностика {uuid.uuid4().hex[:8]}\nВерсия: 0.4.0\n"
                    f"Источник: {runtime.get('listener', 'off')}\n"
                    f"Режим: {state['settings']['mode']}\n"
                    f"Работает: {state['settings']['running']}\n"
                    f"Счётчики: {state['stats']}"
                )
            ]
        return [notice("Неизвестная команда. /start — меню, /help — инструкция.")]
    pending = state["pending"]
    if pending:
        if pending["expires"] < time.time():
            state["pending"] = None
            return [notice("Время ввода истекло. Открой /settings ещё раз.")]
        try:
            field = pending["field"]
            s = state["settings"]
            if field not in {"auto_max", "min_order_rub"}:
                state["pending"] = None
                return [rates(state)]
            if field == "min_order_rub" and text in {"0", "0.0", "0,0"}:
                s[field] = None
                state["notified"].clear()
                state["pending"] = None
                audit(state, "setting_changed", field=field)
                return [rates(state)]
            value = money(text)
            s[field] = str(value)
            state["notified"].clear()
            state["pending"] = None
            audit(state, "setting_changed", field=field)
            return [rates(state)]
        except ParseError as error:
            return [
                notice(str(error) + ". Введи значение ещё раз.", [[button("Отмена ввода", callback="menu")]])
            ]
    return [menu(state, runtime)]


def handle_action(state, action, runtime):
    s = state["settings"]
    if action == "accounts":
        state["pending"] = None
        return [accounts_menu(state, runtime)]
    if action == "account:setup":
        return [
            notice(
                "Для входа внутри Telegram укажи HTTPS-адрес хостинга в PUBLIC_BASE_URL и перезапусти приложение. Затем открой /accounts. Пароль 2FA сообщением не отправляй."
            )
        ]
    if action.startswith("account:select:"):
        try:
            select_account(state, action.removeprefix("account:select:"))
        except AccountMissing:
            return [accounts_menu(state, runtime)]
        runtime = {
            **runtime,
            "listener": runtime.get("accounts", {}).get(state["selected_account"], "connecting"),
        }
        return [menu(state, runtime)]
    if action.startswith(("account:remove:", "account:delete:")):
        account_id = action.split(":", 2)[2]
        account = state.get("accounts", {}).get(account_id)
        if not account:
            return [accounts_menu(state, runtime)]
        data = state if state.get("selected_account") == account_id else account["profile"]
        if data["active"] or data["settings"]["running"]:
            return [notice("Сначала останови поиск этого аккаунта и заверши его активную сделку.")]
        if action.startswith("account:remove:"):
            return [
                notice(
                    f"Удалить {account['label']} из помощника?\nНастройки и история этого аккаунта будут удалены. Сессию Telegram можно отозвать в разделе «Устройства».",
                    [
                        [button("Удалить аккаунт", callback=f"account:delete:{account_id}")],
                        [button("Сохранить", callback="accounts")],
                    ],
                )
            ]
        remove_account(state, account_id)
        return [accounts_menu(state, runtime)]
    if action == "stopall":
        for data in [state, *(a["profile"] for a in state.get("accounts", {}).values())]:
            data["settings"]["running"] = False
            data["arm"] = None
            data["arm_after_reconcile"] = False
        return [accounts_menu(state, runtime)]
    if action in {"run", "menu"} and runtime.get("accounts_required") and not state.get("selected_account"):
        return [accounts_menu(state, runtime)]
    # Callbacks and pending inputs are durable; stale buttons cannot close a different deal.
    if action.startswith("set:"):
        field = action.removeprefix("set:")
        if field not in {"auto_max", "min_order_rub"}:
            return [rates(state)]
        state["pending"] = {"field": field, "expires": time.time() + 600}
        prompt = (
            "Пришли максимальный курс покупки в ₽ за 1 USDT.\nНапример: 89.87"
            if field == "auto_max"
            else "Пришли минимальную сумму покупки в рублях.\nНапример: 5000\n\n0 — отключить минимум. "
            "Если доступный максимум меньше этой суммы, объявление будет пропущено."
        )
        rows = [[button("Отмена ввода", callback="menu")]]
        if field == "min_order_rub":
            rows.insert(0, [button("Без минимума", callback="minimum:off")])
        return [notice("✏️ Настройка Автохава\n\n" + prompt, rows)]
    state["pending"] = None
    if action == "minimum:off":
        s["min_order_rub"] = None
        state["notified"].clear()
        audit(state, "setting_changed", field="min_order_rub")
        return [rates(state)]
    if action.startswith("mode:"):
        if s["mode"] != "auto":
            s.update(mode="auto", running=False)
            state["arm"] = None
            state["arm_after_reconcile"] = False
        if not configured(s):
            return handle_action(state, "set:auto_max", runtime)
        return [menu(state, runtime)]
    elif action == "run":
        if s["running"]:
            return [menu(state, runtime)]
        if state["active"]:
            return [
                notice(
                    "Есть активная отметка сделки. Новые совпадения приостановлены.",
                    active_buttons(state["active"]),
                )
            ]
        if not configured(s):
            return handle_action(
                state, "set:auto_max" if s["mode"] == "auto" else "set:notify_range", runtime
            )
        if s["mode"] == "auto":
            if not runtime.get("live_enabled"):
                return [notice(CAPABILITY_TEXT)]
            if not state["account_checked"]:
                state["reconcile_requested"] = True
                state["arm_after_reconcile"] = True
                return [
                    notice(
                        "Проверяю активные сделки в CryptoBot…\nПосле проверки здесь появится подтверждение запуска."
                    )
                ]
            arm = state.get("arm")
            if not arm or arm["expires"] < time.time():
                arm = {"nonce": uuid.uuid4().hex[:12], "expires": time.time() + 600}
                state["arm"] = arm
            nonce = arm["nonce"]
            return [
                notice(
                    f"Включить РЕАЛЬНОЕ создание сделок по курсу ≤ {s['auto_max']} ₽/USDT?\n"
                    f"Минимальная сумма: {s.get('min_order_rub') or 'не задана'} ₽.\n"
                    "Сумма — весь доступный максимум. Одновременно одна сделка. "
                    "Условия продавца не блокируют покупку; ты получишь их в уведомлении.",
                    [
                        [button("Подтвердить запуск автопокупки", callback=f"armed:{nonce}")],
                        [button("Вернуться без запуска", callback="menu")],
                    ],
                )
            ]
        s["running"] = True
        audit(state, "started")
    elif action == "stop":
        s["running"] = False
        state["arm"] = None
        state["arm_after_reconcile"] = False
        audit(state, "emergency_stop")
    elif action == "stats":
        return [status(state, runtime)]
    elif action == "history":
        return [history(state)]
    elif action == "rates":
        return [rates(state)]
    elif action == "deal":
        return [deal(state)]
    elif action == "help":
        return [
            notice(
                "Как пользоваться\n\n1. Задай максимальный курс.\n"
                "2. При желании задай минимальную сумму покупки в рублях.\n"
                "3. Запусти Автохав и подтверди создание сделок.\n\n"
                "После подтверждённой отмены поиск продолжится автоматически.\n"
                "Меню и кнопки доступны без повторного /start.\n"
                "«Сделка» показывает текущий статус и доступные действия.\n"
                "«Приостановить поиск» останавливает новые попытки; открытая сделка сохраняется.\n\n"
                "Для связи и диагностики: /inspect и /diagnostics."
            )
        ]
    elif action == "capabilities":
        return [notice(CAPABILITY_TEXT)]
    elif action == "reconcile":
        if state["reconcile_requested"]:
            return [notice("Проверка уже запрошена. Дождись результата.")]
        state["reconcile_requested"] = True
        return [notice("Запрошена проверка состояния в CryptoBot.")]
    elif action.startswith("armed:"):
        arm = state.get("arm")
        if (
            not arm
            or arm["expires"] < time.time()
            or arm["nonce"] != action.removeprefix("armed:")
            or not runtime.get("live_enabled")
            or state["active"]
            or s["mode"] != "auto"
            or not state["account_checked"]
            or not configured(s)
        ):
            return [notice("Подтверждение устарело. Нажми «Запустить» заново.")]
        state["arm"] = None
        s["running"] = True
        audit(state, "real_execution_armed")
    elif action.startswith("resolved:"):
        resolution = state.get("resolve")
        if (
            not resolution
            or resolution["expires"] < time.time()
            or resolution["nonce"] != action.removeprefix("resolved:")
        ):
            return [notice("Подтверждение устарело. /resolve — новая сверка.")]
        active = state["active"]
        if active and active.get("offer_id"):
            state["ignored"] = list(dict.fromkeys(state["ignored"] + [active["offer_id"]]))[-2000:]
        state.update(active=None, account_checked=True, resolve=None)
        s["running"] = False
        audit(state, "human_verified_no_active_trades")
    elif action.startswith("cancel:"):
        active = state["active"]
        if (
            not active
            or active["id"] != action.removeprefix("cancel:")
            or not active.get("managed")
            or active.get("stage") not in {"REQUESTED", "ACCEPTED"}
        ):
            return [notice("Состояние сделки изменилось; сначала проверь статус.")]
        return [
            notice(
                "Отменить эту сделку в CryptoBot? Подтверждай только если оплата не была отправлена.",
                [
                    [button("Да, отменить сделку", callback=f"cancelok:{active['id']}")],
                    [button("Сохранить сделку", callback="deal")],
                ],
            )
        ]
    elif action.startswith("cancelok:"):
        active = state["active"]
        if (
            not active
            or active["id"] != action.removeprefix("cancelok:")
            or not active.get("managed")
            or not runtime.get("live_enabled")
            or active.get("stage") not in {"REQUESTED", "ACCEPTED"}
        ):
            return [notice("Отмена сейчас недоступна. Проверь сделку в CryptoBot.")]
        if active.get("cancel_requested"):
            return [notice("Отмена уже запрошена. Дождись результата.")]
        active["cancel_requested"] = True
        audit(state, "cancel_requested", deal_id=active.get("deal_id"))
        return [notice("Отмена запрошена. Пауза сохранится до подтверждённого результата.")]
    elif action.startswith("active:"):
        offer = state["offers"].get(action.removeprefix("active:"))
        if state["active"]:
            return [notice("Уже есть активная отметка.", active_buttons(state["active"]))]
        if not offer:
            return [notice("Карточка устарела. Пришли её заново.")]
        state["active"] = {
            "id": uuid.uuid4().hex[:12],
            "offer_id": offer["id"],
            "paper": False,
            "link": offer["link"],
            "time": time.time(),
        }
        audit(state, "manually_marked_active", offer_id=offer["id"])
        return [
            notice(
                "⏸ Поиск совпадений приостановлен по твоей отметке.\n"
                "Для отмены открой сделку в CryptoBot. Помощник её не отменяет.",
                active_buttons(state["active"]),
            )
        ]
    elif action.startswith("close:"):
        active = state["active"]
        if not active or active["id"] != action.removeprefix("close:"):
            return [notice("Эта кнопка устарела. /start — актуальное состояние.")]
        if active.get("managed"):
            return [notice("У этой сделки статус проверяется через CryptoBot. /resolve — ручная сверка.")]
        text = (
            "Завершить условную сделку?"
            if active["paper"]
            else "Подтверждаешь, что сделка уже отменена или завершена в CryptoBot?\n"
            "Эта кнопка только снимает паузу в помощнике и не отменяет сделку."
        )
        return [
            notice(
                text,
                [
                    [button("Да, закрыта", callback=f"closed:{active['id']}")],
                    [button("Назад", callback="menu")],
                ],
            )
        ]
    elif action.startswith("closed:"):
        active = state["active"]
        if not active or active["id"] != action.removeprefix("closed:"):
            return [notice("Эта кнопка устарела. Состояние не изменено.")]
        if active.get("managed"):
            return [notice("Сначала проверь реальный статус сделки.")]
        if active.get("offer_id"):
            state["ignored"] = list(dict.fromkeys(state["ignored"] + [active["offer_id"]]))[-2000:]
        audit(state, "manually_closed", paper=active["paper"])
        state["active"] = None
    elif action != "menu":
        return [notice("Кнопка не поддерживается. /start — меню.")]
    return [menu(state, runtime)]
