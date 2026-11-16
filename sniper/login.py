"""Interactive local authorization; never log codes, login tokens or QR URLs."""

import asyncio
import contextlib
import getpass
import os
import re
import tempfile
from pathlib import Path

from telethon import errors


class LoginProblem(RuntimeError):
    """A fixed, user-facing message that contains no credentials."""


def delivery_hint(sent):
    kind = type(getattr(sent, "type", None)).__name__
    hints = {
        "SentCodeTypeApp": "Telegram выбрал доставку в служебный чат Telegram на уже подключённых устройствах. Проверь также архив.",
        "SentCodeTypeSms": "Telegram выбрал доставку по SMS на указанный номер.",
        "SentCodeTypeCall": "Telegram выбрал телефонный звонок с кодом.",
        "SentCodeTypeEmailCode": "Telegram выбрал почту, привязанную к входу в аккаунт. Для этого клиента используй auth-qr.",
        "SentCodeTypeSetUpEmailRequired": "Telegram требует настроить почту для входа в официальном приложении. Можно попробовать auth-qr.",
        "SentCodeTypeFirebaseSms": "Telegram выбрал способ SMS-входа для официального мобильного приложения. Используй auth-qr.",
        "SentCodeTypeFragmentSms": "Telegram выбрал доставку через Fragment. В этом клиенте используй auth-qr.",
    }
    return kind, hints.get(kind, "Telegram выбрал неподдерживаемый здесь способ доставки. Используй auth-qr.")


def login_error(error):
    if isinstance(error, errors.FloodWaitError):
        return (
            f"Telegram ограничил запросы. Подожди минимум {error.seconds} сек. Повторный запрос не отправлен."
        )
    messages = {
        "PhoneNumberFloodError": "Telegram ограничил попытки входа для номера. Не повторяй запросы; дождись снятия ограничения.",
        "PhoneNumberInvalidError": "Проверь номер: международный формат с + и кодом страны.",
        "PhoneNumberBannedError": "Telegram отклонил вход для этого номера. Проверь доступ через официальное приложение.",
        "ApiIdInvalidError": "Telegram отклонил TELEGRAM_API_ID / TELEGRAM_API_HASH. Проверь данные приложения в my.telegram.org.",
        "PhoneCodeExpiredError": "Срок кода истёк. Новый код автоматически не запрошен. Можно использовать auth-qr.",
        "PasswordHashInvalidError": "Пароль 2FA не подошёл. Это облачный пароль Telegram, не код блокировки приложения.",
    }
    return messages.get(type(error).__name__)


async def enter_password(client, read_secret=getpass.getpass, write=print):
    for _ in range(3):
        # Keep console reads in the main thread so Ctrl+C interrupts getpass reliably.
        password = read_secret("Облачный пароль Telegram (2FA, ввод скрыт): ")
        if not password:
            raise LoginProblem("Ввод пароля отменён.")
        try:
            await client.sign_in(password=password)
            return
        except errors.PasswordHashInvalidError:
            write("Пароль 2FA не подошёл. Попробуй ещё раз.")
    raise LoginProblem("Три неверных пароля. Вход остановлен.")


async def code_login(client, read_secret=getpass.getpass, write=print):
    phone = read_secret("Номер с + и кодом страны (ввод скрыт): ")
    phone = re.sub(r"[\s()-]", "", phone)
    if not re.fullmatch(r"\+[1-9][0-9]{7,14}", phone):
        raise LoginProblem("Номер должен быть в международном формате: +, код страны и номер.")
    write("Запрашиваю код у Telegram один раз…")
    try:
        sent = await client.send_code_request(phone)
    except errors.SessionPasswordNeededError:
        await enter_password(client, read_secret, write)
        return
    kind, hint = delivery_hint(sent)
    write(hint)
    if kind not in {"SentCodeTypeApp", "SentCodeTypeSms", "SentCodeTypeCall"}:
        raise LoginProblem("Этот способ входа здесь не поддержан. Запусти python -m sniper auth-qr.")
    write("Если код не приходит: Ctrl+C, затем python -m sniper auth-qr. Код сюда в чат не присылай.")
    for _ in range(3):
        code = read_secret("Код Telegram (ввод скрыт): ")
        if not code.strip():
            raise LoginProblem("Код не введён. Новый запрос не отправлен.")
        try:
            await client.sign_in(phone=phone, code=code.strip(), phone_code_hash=sent.phone_code_hash)
            return
        except errors.SessionPasswordNeededError:
            await enter_password(client, read_secret, write)
            return
        except (errors.PhoneCodeInvalidError, errors.PhoneCodeEmptyError):
            write("Код не подошёл. Введи последний полученный код; новый код не запрашиваю.")
    raise LoginProblem("Три неверных кода. Вход остановлен без повторной отправки.")


def show_qr(url: str, path: Path):
    try:
        import qrcode
    except ImportError:
        raise LoginProblem(
            "Для QR установи зависимости: python -m pip install -r requirements.lock"
        ) from None
    # The token lives in a private temporary image, never in terminal output or the project repository.
    image = qrcode.make(url)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as file:
        image.save(file, format="PNG")
    if os.name == "nt":
        try:
            os.startfile(str(path))
        except OSError:
            print("Открой QR-файл вручную: " + str(path))
    else:
        print("Открой QR-файл на своём компьютере: " + str(path))


async def qr_login(client, read_secret=getpass.getpass, write=print, ready=input, display=show_qr):
    write("На телефоне открой Telegram → Настройки → Устройства → Подключить устройство.")
    ready("Когда сканер готов, нажми Enter здесь. Появится QR-код: ")
    qr = await client.qr_login()
    with tempfile.TemporaryDirectory(prefix="cryptobot-login-", ignore_cleanup_errors=True) as temporary:
        # Telethon requires wait() to register its update handler BEFORE the QR is scanned.
        waiter = asyncio.create_task(qr.wait())
        await asyncio.sleep(0)
        try:
            display(qr.url, Path(temporary) / "login-qr.png")
            write("Отсканируй QR телефоном и подтверди подключение. QR никому не отправляй.")
            try:
                await waiter
            except errors.SessionPasswordNeededError:
                await enter_password(client, read_secret, write)
            except asyncio.TimeoutError:
                raise LoginProblem(
                    "Срок QR истёк. Запусти auth-qr ещё раз, подготовив сканер на телефоне."
                ) from None
        finally:
            if not waiter.done():
                waiter.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await waiter
