import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from cryptography.fernet import Fernet
from telethon import errors, types

from sniper.config import AuthConfig
from sniper.login import LoginProblem, code_login, delivery_hint, login_error, qr_login, show_qr


def code_client(kind=None):
    sent = SimpleNamespace(type=kind or types.auth.SentCodeTypeApp(length=5), phone_code_hash="PRIVATE_HASH")
    return SimpleNamespace(send_code_request=AsyncMock(return_value=sent), sign_in=AsyncMock())


async def test_code_login_explains_delivery_without_leaking_credentials():
    client = code_client()
    values = iter(["+15555550123", "12345"])
    output = []
    await code_login(client, lambda prompt: next(values), output.append)
    client.send_code_request.assert_awaited_once_with("+15555550123")
    client.sign_in.assert_awaited_once_with(
        phone="+15555550123", code="12345", phone_code_hash="PRIVATE_HASH"
    )
    text = "\n".join(output)
    assert "служебный чат Telegram" in text
    assert "PRIVATE_HASH" not in text and "12345" not in text and "15555550123" not in text


async def test_wrong_code_does_not_request_another_code():
    client = code_client()
    client.sign_in.side_effect = [errors.PhoneCodeInvalidError(request=None), None]
    values = iter(["+15555550123", "11111", "22222"])
    await code_login(client, lambda prompt: next(values), lambda text: None)
    assert client.send_code_request.await_count == 1
    assert client.sign_in.await_count == 2


async def test_bad_phone_stops_before_network():
    client = code_client()
    with pytest.raises(LoginProblem, match="международном"):
        await code_login(client, lambda prompt: "not-a-number", lambda text: None)
    client.send_code_request.assert_not_awaited()


async def test_two_factor_password_is_requested_locally():
    client = code_client()
    client.sign_in.side_effect = [errors.SessionPasswordNeededError(request=None), None]
    values = iter(["+15555550123", "12345", "PRIVATE_PASSWORD"])
    output = []
    await code_login(client, lambda prompt: next(values), output.append)
    assert client.sign_in.await_args.kwargs == {"password": "PRIVATE_PASSWORD"}
    assert "PRIVATE_PASSWORD" not in "\n".join(output)


async def test_flood_wait_is_not_retried():
    client = code_client()
    client.send_code_request.side_effect = errors.FloodWaitError(request=None, capture=45)
    with pytest.raises(errors.FloodWaitError) as caught:
        await code_login(client, lambda prompt: "+15555550123", lambda text: None)
    assert client.send_code_request.await_count == 1
    assert "45" in login_error(caught.value)


def test_unsupported_delivery_does_not_dump_server_object():
    kind = SimpleNamespace(secret="PRIVATE_VALUE")
    _, hint = delivery_hint(SimpleNamespace(type=kind, phone_code_hash="PRIVATE_HASH"))
    assert "auth-qr" in hint and "PRIVATE" not in hint


async def test_qr_registers_waiter_before_display_and_cleans_up():
    scanned = asyncio.Event()
    started = False
    paths = []
    output = []

    async def wait():
        nonlocal started
        started = True
        await scanned.wait()

    qr = SimpleNamespace(url="tg://login?token=PRIVATE_TOKEN", wait=wait)
    client = SimpleNamespace(qr_login=AsyncMock(return_value=qr))

    def display(url, path):
        assert started
        assert url == qr.url
        path.write_bytes(b"test-qr-placeholder")
        paths.append(path)
        scanned.set()

    await qr_login(client, write=output.append, ready=lambda prompt: "", display=display)
    assert paths and not paths[0].exists()
    assert "PRIVATE_TOKEN" not in "\n".join(output)


async def test_expired_qr_does_not_refresh_automatically():
    qr = SimpleNamespace(
        url="tg://login?token=PRIVATE_TOKEN", wait=AsyncMock(side_effect=asyncio.TimeoutError)
    )
    client = SimpleNamespace(qr_login=AsyncMock(return_value=qr))
    with pytest.raises(LoginProblem, match="истёк"):
        await qr_login(
            client, write=lambda text: None, ready=lambda prompt: "", display=lambda url, path: None
        )
    client.qr_login.assert_awaited_once()


def test_qr_png_generation_without_opening_a_window(tmp_path):
    with patch("sniper.login.os.startfile", create=True):
        target = tmp_path / "qr.png"
        show_qr("tg://login?token=NOT_A_REAL_LOGIN_TOKEN", target)
        assert target.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
        assert b"NOT_A_REAL_LOGIN_TOKEN" not in target.read_bytes()


async def test_auth_qr_saves_only_encrypted_session(tmp_path, capsys):
    from sniper.__main__ import auth

    key = Fernet.generate_key()
    config = AuthConfig(
        _env_file=None,
        app_key=key.decode(),
        telegram_api_id=123,
        telegram_api_hash="PRIVATE_API_HASH",
        user_session_file=tmp_path / "session.enc",
    )
    client = SimpleNamespace(
        connect=AsyncMock(),
        disconnect=AsyncMock(),
        is_user_authorized=AsyncMock(return_value=True),
        session=SimpleNamespace(save=lambda: "PRIVATE_SESSION"),
    )
    with (
        patch("telethon.TelegramClient", return_value=client),
        patch("sniper.login.qr_login", AsyncMock()) as login,
    ):
        await auth(config, qr=True)
        login.assert_awaited_once_with(client)
    assert Fernet(key).decrypt(config.user_session_file.read_bytes()) == b"PRIVATE_SESSION"
    assert "PRIVATE_SESSION" not in capsys.readouterr().out
    client.disconnect.assert_awaited_once()
