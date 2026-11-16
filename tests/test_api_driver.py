import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography.fernet import Fernet
from prometheus_client import CollectorRegistry

from sniper.app import create_app
from sniper.config import Config, database_url
from sniper.telegram_ui import InterfaceChanged, Key, Screen, TelegramUI


def test_database_url_handles_neon_libpq_options():
    value = database_url("postgresql://user:secret@host/db?sslmode=require&channel_binding=require")
    assert value.startswith("postgresql+asyncpg://") and "ssl=verify-full" in value
    assert "sslmode" not in value and "channel_binding" not in value


async def test_webhook_auth_and_model_validation():
    cfg = Config(
        _env_file=None,
        bot_token="123456:fake",
        admin_user_id=42,
        app_key=Fernet.generate_key().decode(),
        webhook_secret="x" * 32,
    )
    runtime = SimpleNamespace(ready=True, accept=AsyncMock(), registry=CollectorRegistry())
    app = create_app(cfg, runtime)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as client:
        assert (await client.post("/telegram/update", json={})).status_code == 401
        headers = {"x-telegram-bot-api-secret-token": "x" * 32}
        assert (await client.post("/telegram/update", json={}, headers=headers)).status_code == 400
        assert (
            await client.post("/telegram/update", json={"update_id": 1}, headers=headers)
        ).status_code == 200
        assert (await client.get("/metrics")).status_code == 401
        assert (
            await client.post("/telegram/update", content=b"x" * 128001, headers=headers)
        ).status_code == 413
    runtime.accept.assert_awaited_once()


async def test_driver_blocks_stale_screen_before_rpc():
    client = AsyncMock()
    ui = TelegramUI(client, "peer")
    old = Screen(1, "old", (Key("Создать сделку", b"observed-bytes"),))
    ui.current = AsyncMock(return_value=Screen(1, "new"))
    with pytest.raises(InterfaceChanged):
        await ui.click(old, old.keys[0])
    client.assert_not_awaited()


def test_first_page_button_is_not_a_price():
    screen = Screen(
        1,
        "Здесь вы можете купить USDT за RUB через СБП.",
        (Key("· 1 ·", b"refresh"), Key("Exchange · 89.87₽ · 6.08K", b"offer")),
    )
    assert len(screen.listings()) == 1
    assert screen.find(r"^[\s·.•]*1[\s·.•]*$").data == b"refresh"


async def test_ui_wait_uses_edit_event_without_polling_history():
    ui = TelegramUI(AsyncMock(), "peer")
    before = Screen(1, "old")
    after = Screen(1, "new")
    ui.latest = before
    ui.current = AsyncMock(return_value=before)

    async def edit():
        await asyncio.sleep(0.01)
        ui.latest = after
        ui.changed.set()

    task = asyncio.create_task(edit())
    assert await ui.wait_changed(before) == after
    await task
    assert ui.current.await_count == 0


async def test_single_page_without_pagination_reopens_sbp():
    ui = TelegramUI(AsyncMock(), "peer")
    listing = Screen(1, "Здесь вы можете купить USDT за RUB через СБП.",
                     (Key("89.99₽ · 10K", b"offer"), Key("‹ Назад", b"back")))
    payments = Screen(1, "Выберите способ оплаты", (Key("СБП · 89.99₽ · 1", b"sbp"),))
    ui.offer_list = AsyncMock(return_value=listing)
    ui.click = AsyncMock(side_effect=[payments, listing])
    assert await ui.refresh_first_page() == listing
    assert [call.args[1].data for call in ui.click.await_args_list] == [b"back", b"sbp"]


async def test_paginated_list_refreshes_only_page_one():
    ui = TelegramUI(AsyncMock(), "peer")
    listing = Screen(1, "Здесь вы можете купить USDT за RUB через СБП.",
                     (Key("· 1 ·", b"one"), Key("2", b"two"), Key("‹ Назад", b"back")))
    ui.offer_list = AsyncMock(return_value=listing)
    ui.click = AsyncMock(return_value=listing)
    assert await ui.refresh_first_page() == listing
    ui.click.assert_awaited_once_with(listing, listing.keys[0], allow_same=True, fresh=False)
