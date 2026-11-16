import json
import logging
import secrets
from urllib.parse import urlsplit

from cryptography.fernet import Fernet


class Cipher:
    def __init__(self, key: str):
        self.fernet = Fernet(key.encode())

    def seal(self, data: dict) -> str:
        return self.fernet.encrypt(json.dumps(data, ensure_ascii=False).encode()).decode()

    def open(self, data: str) -> dict:
        return json.loads(self.fernet.decrypt(data.encode()))


def safe_link(value: str | None) -> str:
    """Only retain links to the actual CryptoBot, never seller-supplied links."""
    if value:
        parsed = urlsplit(value)
        if (
            parsed.scheme == "https"
            and parsed.netloc == "t.me"
            and parsed.path.lower().rstrip("/") in {"/cryptobot", "/send"}
        ):
            return value
    return "https://t.me/CryptoBot"


def authorized(update: dict, owner: int) -> bool:
    message = update.get("message") or update.get("edited_message")
    callback = update.get("callback_query")
    if callback:
        message = callback.get("message") or {}
        sender = callback.get("from", {}).get("id")
    else:
        sender = (message or {}).get("from", {}).get("id")
    chat = (message or {}).get("chat", {})
    return sender == owner and chat.get("id") == owner and chat.get("type") == "private"


def log_event(event: str, **fields):
    logging.getLogger("assistant").info(json.dumps({"event": event, **fields}, ensure_ascii=False))


def log_error(event: str, error: Exception) -> str:
    diagnostic_id = secrets.token_hex(4)
    # Deliberately exclude exception messages/tracebacks: URLs may contain bot credentials.
    location = None
    traceback = error.__traceback__
    while traceback:
        frame = traceback.tb_frame
        if frame.f_globals.get("__name__", "").startswith("sniper."):
            location = f"{frame.f_globals['__name__']}:{frame.f_code.co_name}:{traceback.tb_lineno}"
        traceback = traceback.tb_next
    log_event(event, diagnostic_id=diagnostic_id, error_type=type(error).__name__, location=location)
    return diagnostic_id
