"""Per-user Mini App authorization. Passwords and QR tokens stay out of queues/logs."""

import asyncio
import base64
import contextlib
import hashlib
import hmac
import io
import json
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qsl

import qrcode
from telethon import TelegramClient, errors
from telethon.sessions import StringSession

from .accounts import add_account
from .engine import notice


def validate_init_data(raw, bot_token, owner, *, now=None, max_age=600):
    if not raw or len(raw) > 16384:
        raise ValueError("unauthorized")
    pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
    fields = dict(pairs)
    if len(pairs) != len(fields):
        raise ValueError("unauthorized")
    received = fields.pop("hash", "")
    key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    check = "\n".join(f"{name}={value}" for name, value in sorted(fields.items()))
    wanted = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(received, wanted):
        raise ValueError("unauthorized")
    timestamp = int(fields.get("auth_date", "0"))
    age = (time.time() if now is None else now) - timestamp
    user = json.loads(fields.get("user", "{}"))
    if (
        age < -30
        or age > max_age
        or type(user.get("id")) is not int
        or user["id"] <= 0
        or (owner is not None and user["id"] != owner)
    ):
        raise ValueError("unauthorized")
    return user["id"]


class LoginError(RuntimeError):
    def __init__(self, message, status=400):
        self.message, self.status = message, status
        super().__init__(message)


@dataclass(repr=False)
class Attempt:
    id: str
    client: object
    deadline: float
    stage: str = "connecting"
    qr_png: str = ""
    qr_expires: float = 0
    message: str = "Подключаюсь к Telegram…"
    failures: int = 0
    saved: bool = False
    task: object = None
    label: str = ""
    closed: bool = False
    cleanup: object = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class LoginManager:
    def __init__(self, config, store, wake, account_wake, client_factory=None):
        self.config, self.store = config, store
        self.wake, self.account_wake = wake, account_wake
        self.client_factory = client_factory or self.make_client
        self.attempt = None
        self.lock = asyncio.Lock()

    def make_client(self):
        return TelegramClient(
            StringSession(),
            self.config.telegram_api_id,
            self.config.telegram_api_hash.get_secret_value(),
            request_retries=0,
            connection_retries=1,
            flood_sleep_threshold=0,
            device_model="CryptoBot Assistant",
            app_version="0.4.0",
        )

    async def start(self):
        async with self.lock:
            state = await self.store.read()
            if state.get("login_retry_after", 0) > time.time():
                raise LoginError("Telegram ограничил вход. Попробуй позже.", 429)
            if not self.config.telegram_api_id or not self.config.telegram_api_hash.get_secret_value():
                raise LoginError("На хостинге не настроены TELEGRAM_API_ID и TELEGRAM_API_HASH.", 503)
            if self.attempt and self.attempt.stage in {"connecting", "qr", "password"}:
                if self.attempt.deadline > time.time():
                    return self.snapshot(self.attempt)
            if len(state.get("accounts", {})) >= self.config.max_accounts:
                raise LoginError("Достигнут лимит аккаунтов. Удали неиспользуемый в разделе «Аккаунты».")
            await self.close_attempt()
            attempt = Attempt(secrets.token_urlsafe(24), self.client_factory(), time.time() + 300)
            self.attempt = attempt
            attempt.task = asyncio.create_task(self.flow(attempt))
            return self.snapshot(attempt)

    @staticmethod
    def snapshot(attempt):
        return {
            "id": attempt.id,
            "stage": attempt.stage,
            "message": attempt.message,
            "qr_png": attempt.qr_png if attempt.stage == "qr" else "",
            "qr_expires": attempt.qr_expires,
            "expires": attempt.deadline,
            "label": attempt.label,
        }

    def current(self, attempt_id):
        if not self.attempt or not secrets.compare_digest(self.attempt.id, attempt_id):
            raise LoginError("Вход не найден. Начни заново.", 404)
        return self.snapshot(self.attempt)

    async def flow(self, attempt):
        try:
            await asyncio.wait_for(attempt.client.connect(), 20)
            while time.time() < attempt.deadline:
                qr = await asyncio.wait_for(attempt.client.qr_login(), 20)
                waiter = asyncio.create_task(qr.wait())
                await asyncio.sleep(0)  # Register the update handler before displaying the token.
                try:
                    buffer = io.BytesIO()
                    qrcode.make(qr.url).save(buffer, format="PNG")
                    attempt.qr_png = base64.b64encode(buffer.getvalue()).decode()
                    attempt.qr_expires = qr.expires.timestamp()
                    attempt.stage = "qr"
                    attempt.message = "Telegram на телефоне → Настройки → Устройства → Подключить устройство."
                    await asyncio.wait_for(waiter, max(0.01, attempt.deadline - time.time()))
                except asyncio.TimeoutError:
                    continue
                except errors.SessionPasswordNeededError:
                    attempt.stage, attempt.qr_png = "password", ""
                    attempt.message = "Введи облачный пароль 2FA. Он не попадёт в переписку или базу данных."
                    return
                finally:
                    if not waiter.done():
                        waiter.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await waiter
                async with attempt.lock:
                    await self.save(attempt)
                return
            attempt.stage, attempt.message = "expired", "Время входа истекло. Нажми «Начать заново»."
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self.failed(attempt, error)
        finally:
            if attempt.stage != "password":
                await self.disconnect(attempt)

    async def save(self, attempt):
        if time.time() >= attempt.deadline:
            raise LoginError("Время входа истекло. Начни заново.")
        me = await asyncio.wait_for(attempt.client.get_me(), 15)
        if not me or getattr(me, "bot", False):
            raise LoginError("Нужен личный Telegram-аккаунт.")
        session = attempt.client.session.save()
        encrypted = self.store.cipher.fernet.encrypt(session.encode()).decode()
        label = (getattr(me, "first_name", None) or "Telegram")[:50]
        if getattr(me, "username", None):
            label += " · @" + me.username

        async def commit():
            def added(state):
                add_account(state, encrypted, me.id, label, self.config.max_accounts)
                return [
                    notice(f"✅ Аккаунт {label} подключён.\nОткрой «Аккаунты», выбери его и настрой курс.")
                ]

            await self.store.apply(added)
            attempt.saved = True
            attempt.stage, attempt.message, attempt.label = (
                "done",
                "Аккаунт подключён. Можно вернуться в бот.",
                label,
            )
            attempt.qr_png = ""
            self.wake.set()
            self.account_wake.set()

        # A cancellation after a committed row must not revoke the saved session.
        task = asyncio.create_task(commit())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    async def password(self, attempt_id, password):
        self.current(attempt_id)
        attempt = self.attempt
        async with attempt.lock:
            if attempt.stage != "password" or time.time() >= attempt.deadline:
                raise LoginError("Открой новый вход по QR.", 409)
            if not isinstance(password, str) or not password or len(password) > 1024:
                raise LoginError("Введи облачный пароль Telegram.")
            try:
                await asyncio.wait_for(attempt.client.sign_in(password=password), 20)
                await self.save(attempt)
            except errors.PasswordHashInvalidError:
                attempt.failures += 1
                if attempt.failures < 3:
                    raise LoginError("Пароль не подошёл. Это облачный пароль Telegram.") from None
                attempt.stage, attempt.message = "error", "Три неверных пароля. Вход остановлен."
            except Exception as error:
                await self.failed(attempt, error)
            finally:
                password = None
                if attempt.stage != "password":
                    await self.disconnect(attempt)
        return self.snapshot(attempt)

    async def failed(self, attempt, error):
        attempt.qr_png = ""
        if isinstance(error, errors.FloodWaitError):
            until = time.time() + error.seconds + 1
            await self.store.apply(lambda s: s.update(login_retry_after=until))
            attempt.message = f"Telegram ограничил вход на {error.seconds} сек."
        elif isinstance(error, ValueError) and str(error) == "duplicate_account":
            attempt.message = "Этот аккаунт уже подключён. Выбери его в разделе «Аккаунты»."
        elif isinstance(error, ValueError) and str(error) == "account_limit":
            attempt.message = "Достигнут лимит аккаунтов."
        elif isinstance(error, ValueError) and str(error) == "account_owned_elsewhere":
            attempt.message = "Этот Telegram-аккаунт уже подключён к другому профилю пользователя."
        elif isinstance(error, LoginError):
            attempt.message = error.message
        else:
            attempt.message = (
                "Не удалось войти. Проверь API-настройки и доступ хостинга к Telegram, затем начни заново."
            )

        attempt.stage = "error"

    async def disconnect(self, attempt):
        async def cleanup():
            if not attempt.saved:
                with contextlib.suppress(Exception):
                    if await asyncio.wait_for(attempt.client.is_user_authorized(), 5):
                        await asyncio.wait_for(attempt.client.log_out(), 5)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(attempt.client.disconnect(), 5)
            attempt.qr_png, attempt.closed = "", True

        if attempt.cleanup is None:
            attempt.cleanup = asyncio.create_task(cleanup())
        try:
            await asyncio.shield(attempt.cleanup)
        except asyncio.CancelledError:
            await attempt.cleanup
            raise

    async def close_attempt(self):
        attempt = self.attempt
        if not attempt:
            return
        if attempt.task and not attempt.task.done():
            attempt.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await attempt.task
        async with attempt.lock:
            await self.disconnect(attempt)
            if not attempt.saved:
                attempt.stage, attempt.message = "expired", "Вход закрыт."

    async def cancel(self, attempt_id):
        async with self.lock:
            self.current(attempt_id)
            await self.close_attempt()

    async def sweep(self):
        while True:
            await asyncio.sleep(10)
            async with self.lock:
                if self.attempt and self.attempt.deadline <= time.time():
                    await self.close_attempt()

    async def close(self):
        async with self.lock:
            await self.close_attempt()
