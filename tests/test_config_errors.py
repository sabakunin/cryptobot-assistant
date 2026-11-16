from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from sniper.config import AuthConfig, Config, configuration_errors


def runtime_values():
    return dict(
        bot_token="123456:PRIVATE_TOKEN",
        admin_user_id=42,
        app_key=Fernet.generate_key().decode(),
        webhook_secret="PRIVATE_WEBHOOK_SECRET" * 2,
    )


def test_invalid_boolean_error_names_field_without_leaking_values():
    values = runtime_values()
    with pytest.raises(ValidationError) as caught:
        Config(_env_file=None, live_trading_enabled="PRIVATE_BAD_FLAG", **values)
    output = configuration_errors(caught.value)
    assert "LIVE_TRADING_ENABLED" in output and "true или false" in output
    for secret in (*values.values(), "PRIVATE_BAD_FLAG"):
        if isinstance(secret, str):
            assert secret not in output


def test_auth_ignores_unrelated_runtime_errors(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "APP_KEY=" + Fernet.generate_key().decode() + "\nTELEGRAM_API_ID=123\n"
        "TELEGRAM_API_HASH=PRIVATE_HASH\nLIVE_TRADING_ENABLED=invalid\n"
        "ADMIN_USER_ID=invalid\nMONITOR_SOURCE=invalid\n",
        encoding="utf-8-sig",
    )
    # No bot token or webhook secret is required to authorize a user session.
    config = AuthConfig(_env_file=env)
    assert config.telegram_api_id == 123


@pytest.mark.parametrize("field,value", [("app_key", "PRIVATE_BAD_KEY"), ("webhook_secret", "PRIVATE")])
def test_secret_validation_errors_are_field_specific(field, value):
    values = runtime_values()
    values[field] = value
    with pytest.raises(ValidationError) as caught:
        Config(_env_file=None, **values)
    output = configuration_errors(caught.value)
    assert field.upper() in output and value not in output


def test_auth_requires_application_credentials():
    with pytest.raises(ValidationError) as caught:
        AuthConfig(
            _env_file=None, app_key=Fernet.generate_key().decode(), telegram_api_id=0, telegram_api_hash=""
        )
    output = configuration_errors(caught.value)
    assert "TELEGRAM_API_ID" in output and "TELEGRAM_API_HASH" in output


def test_cli_selects_auth_only_configuration(tmp_path, monkeypatch):
    from sniper.__main__ import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["sniper", "auth"])
    (tmp_path / ".env").write_text(
        "APP_KEY=" + Fernet.generate_key().decode() + "\nTELEGRAM_API_ID=123\n"
        "TELEGRAM_API_HASH=PRIVATE_HASH\nLIVE_TRADING_ENABLED=invalid\n",
        encoding="utf-8",
    )

    async def fake_auth(config, revoke=False):
        assert isinstance(config, AuthConfig)
        return "local-test-no-network"

    with patch("sniper.__main__.auth", fake_auth):
        assert main() == "local-test-no-network"
