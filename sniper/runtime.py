import asyncio
import contextlib
import time
import uuid

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup
from prometheus_client import CollectorRegistry, Counter, Histogram
from pydantic import SecretStr
from telethon import errors, events

from .accounts import AccountMissing, AccountStore, import_legacy_session
from .control import callback_hint
from .engine import chunks, ingest, notice, observe_deal
from .panel import deliver_panel
from .parser import OFFER_ID, deal_reference, is_trade_chat_notice, parse_offer
from .security import log_error, log_event
from .store import Store
from .telegram_ui import InterfaceChanged, Screen, TelegramUI, navigation_retryable
from .trading import Execution
from .users import UserStore, approved, private_sender, route_update, user_state
from .web_login import LoginError, LoginManager

NAVIGATION_RECOVERY_DELAYS = (1, 2, 4)


def restore_speed(state, now):
    """A server pause is mandatory; extra local backoff expires after a quiet minute."""
    slow_until = state.get("user_slow_until")
    if slow_until is None and state.get("user_retry_after") and state.get("user_action_interval"):
        slow_until = state["user_retry_after"] + 60
    if slow_until is not None and now >= max(slow_until, state.get("user_retry_after", 0)):
        state.pop("user_action_interval", None)
        state.pop("user_refresh_interval", None)
        state.pop("user_slow_until", None)
        return True
    return False


class Runtime:
    def __init__(self, config):
        self.config = config
        self.store = Store(config.db_url, config.app_key.get_secret_value())
        self.bot = Bot(config.bot_token.get_secret_value())
        self.owner = uuid.uuid4().hex
        self.wake, self.monitor_wake, self.stopping = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self.tasks = []
        self.ui = None
        self.execution = None
        self.status = {"listener": "off", "live_enabled": config.live_trading_enabled}
        self.registry = CollectorRegistry()
        self.parse_time = Histogram("card_parse_seconds", "Local parsing duration", registry=self.registry)
        self.scan_time = Histogram("scan_seconds", "One first-page scan duration", registry=self.registry)
        self.errors = Counter("assistant_errors_total", "Handled runtime errors", registry=self.registry)
        self.ready = False
        self.bot_blocked_until = 0
        self.account_wake = asyncio.Event()
        self.workers = {}
        self.login = LoginManager(config, self.user_store(config.admin_user_id), self.wake, self.account_wake)
        self.logins = {config.admin_user_id: self.login}

    def user_store(self, user_id):
        return UserStore(self.store, user_id, self.config.admin_user_id)

    async def login_for(self, user_id):
        if not approved(await self.store.read(), user_id, self.config.admin_user_id):
            raise LoginError("Дождитесь одобрения администратора в боте.", 403)
        if user_id not in self.logins:
            manager = LoginManager(self.config, self.user_store(user_id), self.wake, self.account_wake)
            self.logins[user_id] = manager
            self.tasks.append(asyncio.create_task(manager.sweep()))
        return self.logins[user_id]

    async def start(self):
        await self.store.initialize()
        await self.acquire_lease()
        await import_legacy_session(self.store, self.config)
        self.login.store = self.user_store(self.config.admin_user_id)

        # A process restart must never re-arm real execution on its own.
        def restart(s):
            users = [s, *(entry["state"] for entry in s.get("users", {}).values())]
            profiles = [
                profile
                for user in users
                for profile in [user, *(a["profile"] for a in user.get("accounts", {}).values())]
            ]
            for data in profiles:
                data["account_checked"] = False
                data["arm"] = None
                data["arm_after_reconcile"] = False
                data.pop("reconcile_panel_revision", None)
                if data["settings"]["mode"] == "paper" and not data["active"]:
                    data["settings"].update(mode="notify", running=False)
                if data["settings"]["mode"] == "auto":
                    data["settings"]["running"] = False
                if data["active"] and data["active"].get("stage") in {
                    "ATTEMPTING",
                    "CANCELLING",
                    "PREPARING",
                }:
                    data["active"]["stage"] = "UNKNOWN"
            return []

        await self.store.apply(restart)
        self.bot_blocked_until = (await self.store.read()).get("bot_retry_after", 0)
        self.tasks = [asyncio.create_task(self.heartbeat()), asyncio.create_task(self.delivery())]
        self.tasks.append(asyncio.create_task(self.login.sweep()))
        if self.config.bot_transport == "polling":
            self.tasks.append(asyncio.create_task(self.poll()))
        if self.config.monitor_source != "off":
            self.tasks.append(asyncio.create_task(self.supervise_accounts()))
        self.ready = True
        self.wake.set()
        log_event("started", source=self.config.monitor_source, live=self.config.live_trading_enabled)

    async def acquire_lease(self, timeout=125, retry_interval=2):
        # A killed process cannot release its 120-second lease. Let it expire;
        # never clear another worker's lease or start any tasks before ownership.
        deadline = time.monotonic() + timeout
        announced = False
        while not await self.store.claim(self.owner):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(
                    "База всё ещё занята другим экземпляром. Закрой другой запуск sniper serve "
                    "и повтори команду. Блокировка не сброшена принудительно."
                )
            if not announced:
                log_event(
                    "waiting_for_worker",
                    message="Ожидаю освобождения базы после предыдущего запуска (до 125 секунд).",
                )
                announced = True
            await asyncio.sleep(min(retry_interval, remaining))

    async def close(self):
        self.ready = False
        self.stopping.set()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await asyncio.gather(*(manager.close() for manager in self.logins.values()))
        if self.ui:
            await self.ui.client.disconnect()
        with contextlib.suppress(Exception):
            await self.store.release(self.owner)
        await self.bot.session.close()
        await self.store.engine.dispose()

    def control_status(self, state, user_id=None):
        user_id = self.config.admin_user_id if user_id is None else user_id
        worker = self.workers.get(state.get("selected_account"))
        return {
            "listener": worker.status["listener"] if worker else "authorization_required",
            "live_enabled": self.config.live_trading_enabled,
            "accounts_required": self.config.monitor_source != "off",
            "connect_url": self.config.connect_url,
            "is_admin": user_id == self.config.admin_user_id,
            "accounts": {
                key: value.status["listener"]
                for key, value in self.workers.items()
                if value.user_id == user_id
            },
        }

    async def supervise_accounts(self):
        try:
            while not self.stopping.is_set():
                self.account_wake.clear()
                state = await self.store.read()
                users = [
                    (self.config.admin_user_id, state),
                    *(
                        (int(key), entry["state"])
                        for key, entry in state.get("users", {}).items()
                        if entry["status"] == "approved"
                    ),
                ]
                accounts = {
                    key: (uid, account)
                    for uid, data in users
                    for key, account in data.get("accounts", {}).items()
                }
                for uid, manager in list(self.logins.items()):
                    if (
                        not approved(state, uid, self.config.admin_user_id)
                        and manager.attempt
                        and not manager.attempt.closed
                    ):
                        await manager.close()
                for account_id in list(self.workers):
                    worker = self.workers[account_id]
                    if account_id not in accounts or worker.task.done():
                        worker.task.cancel()
                        await asyncio.gather(worker.task, return_exceptions=True)
                        del self.workers[account_id]
                for account_id, (user_id, account) in accounts.items():
                    if account_id not in self.workers:
                        worker = AccountWorker(self, account_id, account["session_encrypted"], user_id)
                        self.workers[account_id] = worker
                        worker.task = asyncio.create_task(worker.user_client())
                self.status = self.control_status(state)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.account_wake.wait(), timeout=2)
        finally:
            for worker in self.workers.values():
                worker.task.cancel()
            await asyncio.gather(*(w.task for w in self.workers.values()), return_exceptions=True)
            self.workers.clear()

    async def heartbeat(self):
        while not self.stopping.is_set():
            await asyncio.sleep(30)
            try:
                if not await self.store.claim(self.owner):
                    raise RuntimeError("Lost worker lease")
            except Exception as error:
                log_error("lease_lost", error)
                self.ready = False
                self.stopping.set()
                for task in self.tasks:
                    if task is not asyncio.current_task():
                        task.cancel()
                return

    async def accept(self, data):
        user_id = private_sender(data)
        if user_id is None:
            return
        callback = data.get("callback_query")
        root = await self.store.read()
        allowed = approved(root, user_id, self.config.admin_user_id)
        hint = (
            callback_hint(user_state(root, user_id, self.config.admin_user_id), callback)
            if callback and allowed
            else None
        )
        if callback and not allowed:
            hint = "Доступ не разрешён. Отправьте /start и дождитесь одобрения."
        if not allowed:
            message = data.get("message") or {}
            text = (message.get("text") or "").strip().split(maxsplit=1)
            if not callback and (not text or text[0].split("@", 1)[0] != "/start"):
                return
        if allowed or not callback:
            await self.store.enqueue(data)
            self.wake.set()
        if callback and time.time() >= self.bot_blocked_until:
            try:
                await self.bot.answer_callback_query(callback["id"], text=hint, show_alert=bool(hint))
            except TelegramRetryAfter as error:
                await self.defer_bot(error.retry_after)
            except Exception:
                pass

    async def defer_bot(self, seconds):
        self.bot_blocked_until = max(self.bot_blocked_until, time.time() + seconds)
        await self.store.apply(lambda s: s.update(bot_retry_after=self.bot_blocked_until))

    async def poll(self):
        offset = None
        while not self.stopping.is_set():
            try:
                updates = await self.bot.get_updates(
                    offset=offset, timeout=30, allowed_updates=["message", "edited_message", "callback_query"]
                )
                for update in updates:
                    # Authorization expects Telegram's "from", not aiogram's "from_user".
                    await self.accept(update.model_dump(mode="json", exclude_none=True, by_alias=True))
                    offset = update.update_id + 1
            except TelegramRetryAfter as error:
                await asyncio.sleep(error.retry_after)
            except Exception as error:
                self.errors.inc()
                log_error("poll_failed", error)
                await asyncio.sleep(5)

    async def delivery(self):
        last_cleanup = 0
        while not self.stopping.is_set():
            self.wake.clear()
            retry_delay = 600
            try:
                incoming = await self.store.inbox()
                for update_id, data in incoming:
                    try:
                        await self.store.apply(
                            lambda s, data=data: route_update(
                                s, data, self.config.admin_user_id, self.control_status
                            ),
                            inbox_id=update_id,
                        )
                    except Exception as error:
                        diagnostic = log_error("control_failed", error)
                        self.errors.inc()
                        recipient = private_sender(data)
                        await self.store.apply(
                            lambda s, diagnostic=diagnostic, recipient=recipient: [
                                {
                                    **notice(f"Ошибка обработки. Диагностический ID: {diagnostic}"),
                                    "_recipient": recipient,
                                }
                            ],
                            inbox_id=update_id,
                        )
                    self.monitor_wake.set()
                    self.account_wake.set()
                    for worker in self.workers.values():
                        worker.monitor_wake.set()
                outgoing = await self.store.outgoing() if time.time() >= self.bot_blocked_until else []
                for message_id, attempts, payload in outgoing:
                    try:
                        recipient = payload.get("_recipient", self.config.admin_user_id)
                        if not payload.get("_access_notice") and not approved(
                            await self.store.read(), recipient, self.config.admin_user_id
                        ):
                            await self.store.sent(message_id, expected_delivery=payload.get("_delivery"))
                            continue
                        if payload.get("kind") == "panel":
                            await deliver_panel(self.user_store(recipient), self.bot, recipient, payload)
                        else:
                            markup = (
                                InlineKeyboardMarkup(inline_keyboard=payload["buttons"])
                                if payload["buttons"]
                                else None
                            )
                            await self.bot.send_message(
                                recipient,
                                payload["text"],
                                reply_markup=markup,
                                parse_mode=None,
                            )
                        await self.store.sent(message_id, expected_delivery=payload.get("_delivery"))
                    except TelegramForbiddenError:
                        await self.store.sent(message_id, expected_delivery=payload.get("_delivery"))
                    except TelegramRetryAfter as error:
                        await self.defer_bot(error.retry_after)
                        await self.store.retry(
                            message_id, attempts, error.retry_after, payload.get("_delivery")
                        )
                        retry_delay = min(retry_delay, error.retry_after)
                        break  # Honour Telegram's bot-wide rate limit.
                    except Exception as error:
                        self.errors.inc()
                        log_error("notification_failed", error)
                        delay = min(300, 2 ** min(attempts + 1, 8))
                        await self.store.retry(message_id, attempts, delay, payload.get("_delivery"))
                        retry_delay = min(retry_delay, delay)
                if len(incoming) == 50 or len(outgoing) == 50:
                    self.wake.set()
                if time.time() - last_cleanup > 3600:
                    await self.store.clean()
                    last_cleanup = time.time()
                pending_due = await self.store.next_due()
                if pending_due is not None:
                    retry_delay = min(retry_delay, max(1, pending_due - time.time()))
                retry_delay = max(retry_delay, self.bot_blocked_until - time.time())
            except Exception as error:
                self.errors.inc()
                log_error("queue_failed", error)
                retry_delay = 5
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.wake.wait(), timeout=retry_delay)

    async def user_client(self):
        delay = 5
        while not self.stopping.is_set():
            try:
                state = await self.store.read()
                remaining = state.get("user_retry_after", 0) - time.time()
                if remaining > 0:
                    self.status["listener"] = "flood_wait"
                    await asyncio.sleep(remaining)
                if restore_speed(state, time.time()):
                    await self.store.apply(lambda s: restore_speed(s, time.time()) and [])
                self.status["listener"] = "connecting"
                self.ui = await TelegramUI.connect(self.config, self.store.cipher)
                if isinstance(self.store, AccountStore):
                    account_id = self.store.account_id
                    account = state["accounts"].get(account_id, {})
                    if not account.get("user_id"):
                        me = await self.ui.client.get_me()

                        def identify(s, account_id=account_id, me=me):
                            if account_id in s.get("accounts", {}):
                                s["accounts"][account_id]["user_id"] = me.id

                        await self.store.root.apply(identify)
                self.ui.action_interval = max(
                    self.config.action_interval, state.get("user_action_interval", 0)
                )
                self.execution = Execution(self.store, self.ui, self.config, self.owner, self.wake)
                self.status["listener"] = self.config.monitor_source
                delay = 5

                async def receive(event):
                    try:
                        await self.receive_screen(Screen.from_message(event.message))
                    except Exception as error:
                        self.errors.inc()
                        log_error("incoming_card_failed", error)

                self.ui.client.add_event_handler(
                    receive, events.NewMessage(chats=self.ui.peer, incoming=True)
                )
                self.ui.client.add_event_handler(
                    receive, events.MessageEdited(chats=self.ui.peer, incoming=True)
                )
                if self.config.monitor_source == "userbot":
                    await self.monitor_loop()
                else:
                    await self.ui.client.run_until_disconnected()
                if self.stopping.is_set():
                    return
                raise ConnectionError("User connection ended")
            except (errors.AuthKeyUnregisteredError, errors.SessionRevokedError, FileNotFoundError) as error:
                self.status["listener"] = "authorization_required"
                diagnostic = log_error("session_unavailable", error)
                await self.store.apply(
                    lambda s, diagnostic=diagnostic: [
                        notice(
                            f"Нужна авторизация пользовательской сессии: python -m sniper auth. Диагностика: {diagnostic}"
                        )
                    ]
                )
                self.wake.set()
                await asyncio.sleep(60)
            except AccountMissing:
                return
            except errors.FloodWaitError as error:
                seconds = error.seconds + 1
                until = time.time() + seconds

                def defer_user(s, until=until, seconds=seconds):
                    s["user_retry_after"] = max(s.get("user_retry_after", 0), until)
                    s["user_action_interval"] = min(10, s.get("user_action_interval", 2.0) * 1.5)
                    s["user_refresh_interval"] = min(
                        3600, max(3, s.get("user_refresh_interval", self.config.refresh_seconds) * 1.5)
                    )
                    s["user_slow_until"] = until + 60
                    return [
                        notice(f"Telegram ограничил частоту запросов. Пауза {seconds} сек.; затем продолжу.")
                    ]

                await self.store.apply(defer_user)
                self.wake.set()
                log_event("user_flood_wait", seconds=seconds)
                delay = 0  # Persistent deadline at the top also survives process restarts.
                self.status["listener"] = "flood_wait"
            except Exception as error:
                self.errors.inc()
                diagnostic = log_error("user_client_failed", error)
                self.status["listener"] = "disconnected"

                def pause(s, diagnostic=diagnostic):
                    s["settings"]["running"] = False
                    if s["active"]:
                        s["active"]["cancel_requested"] = False
                    s["account_checked"] = False
                    return [
                        notice(
                            f"⏸ Пользовательский клиент отключён. Диагностика: {diagnostic}. "
                            "После восстановления проверь статус и включи поиск."
                        )
                    ]

                await self.store.apply(pause)
                self.wake.set()
            finally:
                if self.ui:
                    await self.ui.client.disconnect()
            await asyncio.sleep(delay)
            delay = min(300, delay * 2)

    async def receive_screen(self, screen):
        if is_trade_chat_notice(screen.text):
            return
        if screen.is_listing and self.config.monitor_source == "userbot":
            self.monitor_wake.set()
            return
        state = await self.store.read()
        # Financial execution and navigation are serialized in monitor_loop, never event callbacks.
        if state["active"] and state["active"].get("managed"):
            # Do not process an immediate submission result twice before buy() associates it.
            if state["active"].get("deal_id"):
                await self.execution.observe(screen)
            return
        if deal_reference(screen.text):
            await self.store.apply(lambda s: observe_deal(s, deal_reference(screen.text)))
            self.wake.set()
            return
        if self.config.monitor_source == "userbot":
            return
        if OFFER_ID.search(screen.text):
            with self.parse_time.time():
                offer = parse_offer(screen.text, [k.text for k in screen.keys], screen.offer_link, "telegram")
            await self.store.apply(lambda s: ingest(s, offer))
            self.wake.set()

    async def monitor_loop(self):
        # Local navigation is single-threaded. The DB lease excludes another deployment.
        last_active_check = 0
        next_scan_at = 0.0
        recovery_attempts = 0
        recovery_at = 0.0
        self.last_listing_fingerprint = None
        while not self.stopping.is_set():
            self.monitor_wake.clear()
            wait = self.config.refresh_seconds
            try:
                state = await self.store.read()
                if restore_speed(state, time.time()):
                    await self.store.apply(lambda s: restore_speed(s, time.time()) and [])
                    self.ui.action_interval = self.config.action_interval
                if state.get("inspect_requested"):
                    screen = await self.ui.current()
                    content = (
                        "Экран CryptoBot:\n"
                        + screen.text
                        + "\n\nКнопки:\n"
                        + "\n".join(k.text for k in screen.keys)
                    )

                    def inspect_done(s, content=content):
                        s["inspect_requested"] = False
                        return [notice(part) for part in chunks(content)]

                    await self.store.apply(inspect_done)
                    self.wake.set()
                if state["reconcile_requested"]:
                    await self.store.apply(lambda s: s.update(reconcile_requested=False))
                    await self.execution.reconcile()
                    continue
                active = state["active"]
                if active:
                    if active.get("managed") and active.get("cancel_requested"):
                        await self.execution.cancel(active["id"])
                    elif (
                        active.get("managed")
                        and active.get("deal_id")
                        and time.monotonic() - last_active_check > 30
                    ):
                        # Read already-observed trade message; do not navigate while owner pays manually.
                        last_active_check = time.monotonic()
                        screen = await self.ui.read_message(active["message_id"])
                        await self.execution.observe(screen)
                    wait = 15
                elif state["settings"]["running"]:
                    if time.monotonic() < recovery_at:
                        # Push updates must not bypass the retry delay.
                        wait = max(0.01, recovery_at - time.monotonic())
                    elif state["settings"]["mode"] == "auto" and not state["account_checked"]:
                        await self.execution.reconcile()
                    else:
                        interval = max(self.config.refresh_seconds, state.get("user_refresh_interval", 0))
                        listing = (
                            await self.ui.offer_list(reset=True)
                            if recovery_attempts
                            else self.ui.ready_listing(self.last_listing_fingerprint)
                        )
                        started = time.monotonic()
                        if listing is not None or started >= next_scan_at:
                            # React to a new first-page edit immediately. Periodic refreshes
                            # use start-to-start cadence rather than adding a full sleep.
                            next_scan_at = started + interval
                            with self.scan_time.time():
                                await self.scan(listing)
                            if recovery_attempts:
                                recovery_attempts = 0
                                recovery_at = 0.0
                                await self.store.apply(lambda s: [notice("✅ Экран восстановлен. Поиск продолжается.")])
                                self.wake.set()
                        wait = max(0.01, next_scan_at - time.monotonic())
                else:
                    recovery_attempts = 0
                    recovery_at = 0.0
                    wait = 60
            except errors.FloodWaitError:
                # Upper connection loop honours the exact server delay without refreshing or clicking.
                raise
            except Exception as error:
                self.errors.inc()
                # Re-read after buy(): it may have reserved/submitted an operation,
                # or the user may have stopped while a Telegram request was in flight.
                current = await self.store.read()
                can_recover = (
                    navigation_retryable(error)
                    and not current["active"]
                    and current["settings"]["running"]
                    and recovery_attempts < len(NAVIGATION_RECOVERY_DELAYS)
                )
                diagnostic = log_error("scan_recovering" if can_recover else "scan_stopped", error)
                if can_recover:
                    wait = max(
                        NAVIGATION_RECOVERY_DELAYS[recovery_attempts], current.get("user_refresh_interval", 0)
                    )
                    recovery_attempts += 1
                    recovery_at = time.monotonic() + wait
                    next_scan_at = recovery_at
                    self.last_listing_fingerprint = None
                    if recovery_attempts == 1:
                        await self.store.apply(
                            lambda s: [notice("🔄 Экран CryptoBot изменился или ответ задержался. Восстанавливаю поиск.")]
                        )
                        self.wake.set()
                    # Wait below; never replay the failed callback directly.
                else:

                    def pause(s, diagnostic=diagnostic):
                        s["settings"]["running"] = False
                        if s["active"]:
                            s["active"]["cancel_requested"] = False
                        return [
                            notice(
                                "⏸ Не удалось восстановить экран или нужно проверить состояние сделки. "
                                f"Диагностика: {diagnostic}. /inspect — текущий экран."
                            )
                        ]

                    await self.store.apply(pause)
                    self.wake.set()
                    wait = 60
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.monitor_wake.wait(), timeout=wait)

    async def scan(self, listing=None):
        if not await self.store.owns(self.owner):
            raise RuntimeError("Lost worker lease")
        if listing is None:
            listing = await self.ui.refresh_first_page()
        elif not listing.is_first_page:
            raise InterfaceChanged("Fast scan requires the first page")
        self.last_listing_fingerprint = listing.fingerprint
        seen = set()
        # A page contains a bounded number of buttons. Refresh/back may reorder it.
        for _ in range(20):
            state = await self.store.read()
            if not state["settings"]["running"] or state["active"]:
                return
            low = state["settings"]["notify_min"]
            high = (
                state["settings"]["notify_max"]
                if state["settings"]["mode"] == "notify"
                else state["settings"]["auto_max"]
            )
            if high is None or (state["settings"]["mode"] == "notify" and low is None):
                return
            from decimal import Decimal

            candidates = [
                (key, price)
                for key, price in listing.listings()
                if key.data not in seen
                and price <= Decimal(high)
                and (state["settings"]["mode"] != "notify" or price >= Decimal(low))
            ]
            if not candidates:
                return
            key, _ = candidates[0]
            seen.add(key.data)
            card = await self.ui.click(listing, key, fresh=False)
            with self.parse_time.time():
                offer = parse_offer(card.text, [k.text for k in card.keys], card.offer_link, "telegram")
            if state["settings"]["mode"] == "auto":
                await self.execution.buy(offer, card)
            else:
                await self.store.apply(lambda s, offer=offer: ingest(s, offer))
                self.wake.set()
            if (await self.store.read())["active"]:
                return
            # Return using actual observed callback; never hardcode callback_data or screen coordinates.
            current = await self.ui.current()
            try:
                listing = await self.ui.click(current, current.find(r"Назад\s+к\s+объявлениям"), fresh=False)
            except InterfaceChanged:
                listing = await self.ui.offer_list()
            if not listing.is_listing:
                raise InterfaceChanged("Return to offers failed")
            self.last_listing_fingerprint = listing.fingerprint


class AccountWorker(Runtime):
    """One MTProto connection and execution loop; no extra bot poller or DB lease."""

    def __init__(self, parent, account_id, encrypted_session, user_id=None):
        self.config = parent.config.model_copy(
            update={"user_session_encrypted": SecretStr(encrypted_session)}
        )
        self.user_id = parent.config.admin_user_id if user_id is None else user_id
        self.store = AccountStore(parent.user_store(self.user_id), account_id)
        self.owner = parent.owner
        self.wake = parent.wake
        self.monitor_wake = asyncio.Event()
        self.stopping = parent.stopping
        self.ui = self.execution = None
        self.status = {"listener": "connecting", "live_enabled": parent.config.live_trading_enabled}
        self.errors, self.parse_time, self.scan_time = parent.errors, parent.parse_time, parent.scan_time
        self.task = None
