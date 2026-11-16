import argparse
import asyncio
import getpass
import logging
import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet
from pydantic import ValidationError


def setup():
    target = Path(".env")
    if target.exists():
        raise SystemExit(".env already exists; edit it locally. It was not overwritten.")
    token = getpass.getpass("BotFather bot token (hidden): ").strip()
    admin = input("Your numeric Telegram user ID: ").strip()
    if not admin.isdigit() or not token or any(c in token for c in "\r\n\"'"):
        raise SystemExit("Invalid token or administrator ID")
    text = Path(".env.example").read_text(encoding="utf-8")
    values = {
        "BOT_TOKEN": token,
        "ADMIN_USER_ID": admin,
        "APP_KEY": Fernet.generate_key().decode(),
        "WEBHOOK_SECRET": secrets.token_urlsafe(36),
    }
    lines = [
        f"{line.split('=', 1)[0]}={values[line.split('=', 1)[0]]}"
        if "=" in line and line.split("=", 1)[0] in values
        else line
        for line in text.splitlines()
    ]
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write("\n".join(lines) + "\n")
    print("Created local .env. Secrets were not printed. Keep this file private.")


async def auth(config, revoke=False, qr=False):
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    from .login import LoginProblem, code_login, login_error, qr_login
    from .security import Cipher

    if not config.telegram_api_id or not config.telegram_api_hash.get_secret_value():
        raise SystemExit("Set TELEGRAM_API_ID and TELEGRAM_API_HASH locally first.")
    if not revoke and config.user_session_file.exists():
        raise SystemExit("Session file exists. Revoke/remove it explicitly before replacing.")
    cipher = Cipher(config.app_key.get_secret_value())
    if revoke:
        encrypted = (
            config.user_session_encrypted.get_secret_value() or config.user_session_file.read_text().strip()
        )
        session = StringSession(cipher.fernet.decrypt(encrypted.encode()).decode())
    else:
        session = StringSession()
    client = TelegramClient(
        session,
        config.telegram_api_id,
        config.telegram_api_hash.get_secret_value(),
        request_retries=0,
        flood_sleep_threshold=0,
    )
    try:
        if revoke:
            await client.connect()
            await client.log_out()
            print("This user session was revoked. Remove its encrypted copy from hosting secrets.")
            return
        # Login is initiated by the owner locally; codes and QR tokens are never logged.
        await client.connect()
        try:
            await (qr_login(client) if qr else code_login(client))
        except Exception as error:
            hint = login_error(error)
            if hint:
                raise LoginProblem(hint) from None
            raise
        if not await client.is_user_authorized():
            raise LoginProblem("Telegram не подтвердил авторизацию. Сессия не сохранена.")
        path = config.user_session_file
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            raise SystemExit("Session file exists. Revoke/remove it explicitly before replacing.")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(cipher.fernet.encrypt(client.session.save().encode()).decode())
        print("Вход подтверждён. Зашифрованная сессия сохранена локально. Теперь можно запускать serve.")
    finally:
        await client.disconnect()


async def webhook(config, remove=False):
    from aiogram import Bot

    async with Bot(config.bot_token.get_secret_value()) as bot:
        if remove:
            await bot.delete_webhook(drop_pending_updates=False)
        else:
            if not config.public_base_url.startswith("https://"):
                raise SystemExit("PUBLIC_BASE_URL must be HTTPS")
            await bot.set_webhook(
                config.public_base_url.rstrip("/") + "/telegram/update",
                secret_token=config.webhook_secret.get_secret_value(),
                allowed_updates=["message", "edited_message", "callback_query"],
                drop_pending_updates=False,
            )
    print("Webhook updated. Existing pending updates were preserved.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command", choices=["setup", "auth", "auth-qr", "revoke", "serve", "migrate", "webhook", "polling"]
    )
    command = parser.parse_args().command
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    for name in ("telethon", "aiogram", "httpx", "httpcore", "sqlalchemy"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    if command == "setup":
        return setup()
    from .config import AuthConfig, Config, configuration_errors
    from .login import LoginProblem

    try:
        config = AuthConfig() if command in {"auth", "auth-qr", "revoke"} else Config()
        if command == "auth-qr":
            return asyncio.run(auth(config, qr=True))
        if command in {"auth", "revoke"}:
            return asyncio.run(auth(config, revoke=command == "revoke"))
        if command in {"webhook", "polling"}:
            return asyncio.run(webhook(config, remove=command == "polling"))
        if command in {"serve", "migrate"}:
            from alembic import command as alembic_command
            from alembic.config import Config as AlembicConfig

            alembic_command.upgrade(AlembicConfig("alembic.ini"), "head")
        if command == "serve":
            import uvicorn

            from .app import create_app

            uvicorn.run(
                create_app(config),
                host="0.0.0.0",
                port=config.port,
                access_log=False,
                workers=1,
                log_level="warning",
            )
    except ValidationError as error:
        raise SystemExit(configuration_errors(error)) from None
    except LoginProblem as error:
        raise SystemExit(str(error)) from None
    except KeyboardInterrupt:
        raise SystemExit("Вход/работа остановлены пользователем.") from None
    except Exception as error:
        from .security import log_error

        diagnostic = log_error("startup_failed", error)
        raise SystemExit(
            f"Startup failed ({type(error).__name__}). Check local .env. Diagnostic: {diagnostic}"
        ) from None


if __name__ == "__main__":
    main()
