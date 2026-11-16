import asyncio
import contextlib
import time
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet
from telethon.errors import FloodWaitError

from sniper.config import Config
from sniper.runtime import Runtime, restore_speed


async def test_user_flood_wait_persists_deadline_without_immediate_retry(store):
    config = Config(
        _env_file=None,
        bot_token="123456:fake",
        admin_user_id=42,
        app_key=Fernet.generate_key().decode(),
        webhook_secret="x" * 32,
        monitor_source="userbot",
        telegram_api_id=123,
        telegram_api_hash="fake",
    )
    runtime = Runtime(config)
    await runtime.bot.session.close()
    await runtime.store.engine.dispose()
    runtime.store = store
    connect = AsyncMock(side_effect=FloodWaitError(request=None, capture=7))
    with patch("sniper.runtime.TelegramUI.connect", connect):
        task = asyncio.create_task(runtime.user_client())
        try:
            for _ in range(100):
                state = await store.read()
                if state.get("user_retry_after"):
                    break
                await asyncio.sleep(0.01)
            assert state["user_retry_after"] > time.time() + 6
            assert state["user_action_interval"] == 3.0
            assert state["user_refresh_interval"] >= 3.0
            assert state["user_slow_until"] >= state["user_retry_after"] + 60
            assert runtime.status["listener"] == "flood_wait"
            await asyncio.sleep(0.03)
            connect.assert_awaited_once()
            assert len(await store.outgoing()) == 1
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


def test_speed_recovers_after_quiet_period_but_never_before_server_deadline():
    state = {
        "user_retry_after": 100,
        "user_slow_until": 160,
        "user_action_interval": 3,
        "user_refresh_interval": 3,
    }
    assert not restore_speed(state, 99)
    assert not restore_speed(state, 159)
    assert restore_speed(state, 160)
    assert state == {"user_retry_after": 100}
    assert not restore_speed(state, 200)


def test_old_persistent_slowdown_recovers_without_shortening_flood_wait():
    state = {"user_retry_after": 100, "user_action_interval": 3}
    assert not restore_speed(state, 101)
    assert restore_speed(state, 160)
    future = {"user_retry_after": 1000, "user_slow_until": 160, "user_action_interval": 3}
    assert not restore_speed(future, 999)
