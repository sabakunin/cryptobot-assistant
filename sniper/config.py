from pathlib import Path
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from cryptography.fernet import Fernet
from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def database_url(value: str) -> str:
    if value.startswith(("postgres://", "postgresql://")):
        value = "postgresql+asyncpg://" + value.split("://", 1)[1]
    if value.startswith("postgresql+asyncpg://"):
        parts = urlsplit(value)
        query = dict(parse_qsl(parts.query))
        # asyncpg uses ssl, not libpq's sslmode/channel_binding parameters.
        if "sslmode" in query:
            mode = query.pop("sslmode")
            if mode not in {"require", "verify-full", "verify-ca"}:
                raise ValueError("PostgreSQL requires TLS; use sslmode=require or verify-full")
            query["ssl"] = "verify-full"
        query.pop("channel_binding", None)
        value = urlunsplit(parts._replace(query=urlencode(query)))
    return value


def valid_app_key(value: SecretStr) -> SecretStr:
    try:
        Fernet(value.get_secret_value().encode())
    except (ValueError, TypeError):
        raise ValueError("APP_KEY must be a valid Fernet key") from None
    return value


class AuthConfig(BaseSettings):
    """Local session login does not depend on bot hosting or live-trading settings."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8-sig", extra="ignore")
    app_key: SecretStr
    telegram_api_id: int = Field(gt=0)
    telegram_api_hash: SecretStr
    user_session_encrypted: SecretStr = SecretStr("")
    user_session_file: Path = Path(".secrets/session.enc")

    _app_key_valid = field_validator("app_key")(valid_app_key)

    @field_validator("telegram_api_hash")
    @classmethod
    def validate_api_hash(cls, value):
        if not value.get_secret_value().strip():
            raise ValueError("TELEGRAM_API_HASH must not be empty")
        return value


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8-sig", extra="ignore")
    bot_token: SecretStr
    admin_user_id: int = Field(gt=0)
    app_key: SecretStr
    webhook_secret: SecretStr
    database_url: SecretStr = SecretStr("sqlite+aiosqlite:///./assistant.db")
    bot_transport: Literal["polling", "webhook"] = "polling"
    public_base_url: str = ""
    public_base_url_file: Path | None = None
    port: int = Field(default=8000, ge=1, le=65535)
    monitor_source: Literal["off", "passive", "userbot"] = "off"
    live_trading_enabled: bool = False
    p2p_automation_authorized: bool = False  # Legacy option; not evidence of platform permission.
    refresh_seconds: float = Field(default=1, ge=1, le=3600)
    action_interval: float = Field(default=0.2, ge=0.1, le=10)
    telegram_api_id: int = 0
    telegram_api_hash: SecretStr = SecretStr("")
    user_session_encrypted: SecretStr = SecretStr("")
    user_session_file: Path = Path(".secrets/session.enc")
    max_accounts: int = Field(default=3, ge=1, le=20)

    _app_key_valid = field_validator("app_key")(valid_app_key)

    @field_validator("webhook_secret")
    @classmethod
    def validate_webhook_secret(cls, value):
        if len(value.get_secret_value()) < 32:
            raise ValueError("WEBHOOK_SECRET must have at least 32 characters")
        return value

    @model_validator(mode="after")
    def validate_secrets(self):
        if self.monitor_source != "off" and not (
            self.telegram_api_id and self.telegram_api_hash.get_secret_value()
        ):
            raise ValueError("User client needs TELEGRAM_API_ID and TELEGRAM_API_HASH")
        if self.live_trading_enabled and self.monitor_source != "userbot":
            raise ValueError("Real execution requires MONITOR_SOURCE=userbot")
        return self

    @property
    def db_url(self) -> str:
        return database_url(self.database_url.get_secret_value())

    @property
    def connect_url(self) -> str:
        base = self.public_base_url
        if self.public_base_url_file:
            try:
                base = self.public_base_url_file.read_text(encoding="utf-8").strip()[:2048]
            except OSError:
                return ""
        parsed = urlsplit(base)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.query
            or parsed.fragment
        ):
            return ""
        return base.rstrip("/") + "/connect"


def configuration_errors(error: ValidationError) -> str:
    """Describe invalid fields, never their input values, exception contexts or reprs."""
    messages = ["Ошибки настроек .env:"]
    known_model_errors = {
        "Value error, User client needs TELEGRAM_API_ID and TELEGRAM_API_HASH": "для пользовательского клиента нужны TELEGRAM_API_ID и TELEGRAM_API_HASH",
        "Value error, Real execution requires MONITOR_SOURCE=userbot": "реальное исполнение требует MONITOR_SOURCE=userbot",
    }
    for item in error.errors(include_input=False, include_context=False, include_url=False):
        name = ".".join(str(part).upper() for part in item["loc"]) or "CONFIG"
        kind = item["type"]
        if kind == "missing":
            hint = "параметр не задан"
        elif kind in {"bool_parsing", "bool_type"}:
            hint = "укажи true или false"
        elif kind in {"int_parsing", "int_type"}:
            hint = "укажи целое число"
        elif kind in {"greater_than", "greater_than_equal", "less_than", "less_than_equal"}:
            hint = "число вне допустимого диапазона; см. .env.example"
        elif name == "APP_KEY":
            hint = "нужен корректный ключ Fernet из setup; существующий ключ шифрования не заменяй"
        elif name == "WEBHOOK_SECRET":
            hint = "нужно не менее 32 символов"
        elif name == "TELEGRAM_API_HASH":
            hint = "параметр не должен быть пустым"
        elif name == "CONFIG":
            hint = known_model_errors.get(item["msg"], "несовместимые настройки; см. .env.example")
        else:
            hint = "неверный формат или вариант; см. .env.example"
        messages.append(f"- {name}: {hint}.")
    return "\n".join(messages)
