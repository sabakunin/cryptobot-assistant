import asyncio
import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlencode

import httpx
import pytest
from cryptography.fernet import Fernet
from telethon import errors

from sniper.app import create_app
from sniper.config import Config
from sniper.web_login import Attempt, LoginError, LoginManager, validate_init_data


def config():
    return Config(
        _env_file=None,
        bot_token="123456:fake",
        admin_user_id=42,
        app_key=Fernet.generate_key().decode(),
        webhook_secret="x" * 32,
        telegram_api_id=123,
        telegram_api_hash="fake",
        public_base_url="https://host.test",
    )


def signed(user=42, age=0):
    fields = {"auth_date": str(int(time.time()) - age), "user": json.dumps({"id": user}), "query_id": "test"}
    key = hmac.new(b"WebAppData", b"123456:fake", hashlib.sha256).digest()
    body = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    fields["hash"] = hmac.new(key, body.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def fake_client(password=False, wait=None):
    qr = SimpleNamespace(
        url="tg://login?token=FAKE-TEST-TOKEN",
        expires=datetime.now(timezone.utc) + timedelta(seconds=30),
        wait=wait or AsyncMock(side_effect=errors.SessionPasswordNeededError(None) if password else None),
    )
    return SimpleNamespace(
        connect=AsyncMock(),
        disconnect=AsyncMock(),
        qr_login=AsyncMock(return_value=qr),
        is_user_authorized=AsyncMock(return_value=True),
        log_out=AsyncMock(),
        sign_in=AsyncMock(),
        get_me=AsyncMock(return_value=SimpleNamespace(id=71, first_name="Example", username=None, bot=False)),
        session=SimpleNamespace(save=lambda: "FAKE-SESSION-SECRET"),
    )


async def settled(manager, stage):
    for _ in range(200):
        if manager.attempt.stage == stage:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(manager.attempt.stage)


@pytest.mark.parametrize(
    "make_raw",
    [
        lambda: "",
        lambda: "x=1",
        lambda: signed(user=43),
        lambda: signed(age=601),
        lambda: signed(age=-60),
        lambda: signed() + "&user=bad",
    ],
)
def test_rejects_unsigned_wrong_owner_expired_and_duplicate_fields(make_raw):
    with pytest.raises((ValueError, TypeError)):
        validate_init_data(make_raw(), "123456:fake", 42)


def test_verifies_telegram_signature():
    assert validate_init_data(signed(), "123456:fake", 42) == 42


async def test_qr_success_stores_only_encrypted_session_without_starting_search(store):
    client = fake_client()
    manager = LoginManager(config(), store, asyncio.Event(), asyncio.Event(), lambda: client)
    try:
        await manager.start()
        await settled(manager, "done")
        state = await store.read()
        account = next(iter(state["accounts"].values()))
        assert store.cipher.fernet.decrypt(account["session_encrypted"].encode()) == b"FAKE-SESSION-SECRET"
        assert not state["settings"]["running"]
        client.log_out.assert_not_awaited()
        assert "FAKE-SESSION-SECRET" not in json.dumps(manager.snapshot(manager.attempt))
    finally:
        await manager.close()


async def test_2fa_wrong_password_then_success_never_queues_password(store):
    client = fake_client(password=True)
    manager = LoginManager(config(), store, asyncio.Event(), asyncio.Event(), lambda: client)
    try:
        await manager.start()
        await settled(manager, "password")
        assert not (await store.read()).get("accounts")
        client.sign_in.side_effect = [errors.PasswordHashInvalidError(None), None]
        with pytest.raises(LoginError):
            await manager.password(manager.attempt.id, "wrong-secret")
        result = await manager.password(manager.attempt.id, "correct-secret")
        assert result["stage"] == "done" and not result["qr_png"]
        assert "correct-secret" not in json.dumps(await store.read())
        assert "correct-secret" not in json.dumps(await store.outgoing())
    finally:
        await manager.close()


async def test_repeated_start_reuses_pending_attempt_and_cancel_revokes_unsaved(store):
    event = asyncio.Event()
    client = fake_client(wait=event.wait)
    manager = LoginManager(config(), store, asyncio.Event(), asyncio.Event(), lambda: client)
    try:
        first = await manager.start()
        second = await manager.start()
        assert first["id"] == second["id"]
        await settled(manager, "qr")
        await manager.cancel(first["id"])
        assert not (await store.read()).get("accounts")
        client.log_out.assert_awaited_once()
        client.disconnect.assert_awaited_once()
    finally:
        await manager.close()


async def test_duplicate_login_revokes_new_session_and_preserves_saved_one(store):
    clients = [fake_client(), fake_client()]
    manager = LoginManager(config(), store, asyncio.Event(), asyncio.Event(), lambda: clients.pop(0))
    try:
        await manager.start()
        await settled(manager, "done")
        original = (await store.read())["accounts"]
        await manager.start()
        await settled(manager, "error")
        await asyncio.sleep(0.02)
        assert (await store.read())["accounts"] == original
        manager.attempt.client.log_out.assert_awaited_once()
    finally:
        await manager.close()


async def test_login_api_requires_valid_owner_and_rejects_large_or_malformed_bodies():
    login = SimpleNamespace(start=AsyncMock(return_value={"stage": "connecting"}), password=AsyncMock())
    runtime = SimpleNamespace(ready=True, login=login, login_for=AsyncMock(return_value=login))
    app = create_app(config(), runtime)
    headers = {"x-telegram-init-data": signed(), "origin": "https://host.test"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://host.test"
    ) as client:
        assert (await client.post("/connect/api/start")).status_code == 401
        assert (await client.post("/connect/api/start", headers=headers)).status_code == 200
        bad = {**headers, "origin": "https://evil.test"}
        assert (await client.post("/connect/api/start", headers=bad)).status_code == 403
        assert (
            await client.post("/connect/api/password", headers=headers, content=b"x" * 8193)
        ).status_code == 413
        assert (
            await client.post("/connect/api/password", headers=headers, content=b"[1]")
        ).status_code == 400
        page = await client.get("/connect")
        assert "no-store" in page.headers["cache-control"]
        assert "frame-ancestors" in page.headers["content-security-policy"]
        assert (await client.get("/connect/connect.js")).status_code == 200
    login.start.assert_awaited_once()
    login.password.assert_not_awaited()


async def test_three_wrong_passwords_close_login_without_saving(store):
    client = fake_client(password=True)
    client.sign_in.side_effect = errors.PasswordHashInvalidError(None)
    manager = LoginManager(config(), store, asyncio.Event(), asyncio.Event(), lambda: client)
    try:
        await manager.start()
        await settled(manager, "password")
        for _ in range(2):
            with pytest.raises(LoginError):
                await manager.password(manager.attempt.id, "wrong")
        assert (await manager.password(manager.attempt.id, "wrong"))["stage"] == "error"
        assert not (await store.read()).get("accounts")
        client.disconnect.assert_awaited_once()
    finally:
        await manager.close()


async def test_login_flood_wait_is_durable_and_prevents_another_request(store):
    client = fake_client()
    client.qr_login.side_effect = errors.FloodWaitError(None, capture=30)
    manager = LoginManager(config(), store, asyncio.Event(), asyncio.Event(), lambda: client)
    try:
        await manager.start()
        await settled(manager, "error")
        assert (await store.read())["login_retry_after"] > time.time() + 29
        with pytest.raises(LoginError) as error:
            await manager.start()
        assert error.value.status == 429
        client.qr_login.assert_awaited_once()
    finally:
        await manager.close()


async def test_cancelled_cleanup_still_disconnects_and_does_not_repeat(store):
    client = fake_client()
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_disconnect():
        entered.set()
        await release.wait()

    client.disconnect.side_effect = slow_disconnect
    manager = LoginManager(config(), store, asyncio.Event(), asyncio.Event(), lambda: client)
    attempt = Attempt("test", client, time.time() + 60)
    task = asyncio.create_task(manager.disconnect(attempt))
    await entered.wait()
    task.cancel()
    release.set()
    await asyncio.gather(task, return_exceptions=True)
    assert attempt.closed
    await manager.disconnect(attempt)
    client.disconnect.assert_awaited_once()
    client.log_out.assert_awaited_once()
