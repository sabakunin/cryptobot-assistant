from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from aiogram.types import Update
from cryptography.fernet import Fernet

from sniper.app import create_app
from sniper.config import Config
from sniper.control import handle
from sniper.runtime import Runtime
from sniper.users import route_update


@pytest.mark.parametrize("transport", ["polling", "webhook"])
@pytest.mark.parametrize("kind", ["message", "edited_message", "callback_query"])
@pytest.mark.parametrize("sender", [42, 99], ids=["owner", "other_user"])
async def test_real_aiogram_update_routes_owner_and_access_requests(store, transport, kind, sender):
    cfg = Config(
        _env_file=None,
        bot_token="123456:fake",
        admin_user_id=42,
        app_key=Fernet.generate_key().decode(),
        webhook_secret="x" * 32,
        bot_transport=transport,
    )
    runtime = Runtime(cfg)
    await runtime.bot.session.close()
    await runtime.store.engine.dispose()
    runtime.store = store
    runtime.ready = True
    user = {"id": sender, "is_bot": False, "first_name": "Test"}
    message = {
        "message_id": 1,
        "date": 1,
        "from": user,
        "chat": {"id": sender, "type": "private"},
        "text": "/start",
    }
    if kind == "callback_query":
        value = {
            "id": "test-callback",
            "from": user,
            "message": {**message, "from": {"id": 123456, "is_bot": True, "first_name": "Bot"}},
            "chat_instance": "test-chat",
            "data": "menu",
        }
    else:
        value = message
    data = {"update_id": 1, kind: value}
    if transport == "polling":

        async def one_batch(**kwargs):
            runtime.stopping.set()
            return [Update.model_validate(data)]

        runtime.bot = SimpleNamespace(get_updates=AsyncMock(side_effect=one_batch))
        await runtime.poll()
        runtime.bot.get_updates.assert_awaited_once()
    else:
        app = create_app(cfg, runtime)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://test") as client:
            result = await client.post(
                "/telegram/update",
                json=data,
                headers={"x-telegram-bot-api-secret-token": "x" * 32},
            )
            assert result.status_code == 200

    incoming = await store.inbox()
    if sender != cfg.admin_user_id:
        if kind == "message":
            assert len(incoming) == 1
            update_id, received = incoming[0]
            await store.apply(
                lambda state: route_update(state, received, 42, runtime.control_status), inbox_id=update_id
            )
            state = await store.read()
            assert state["users"]["99"]["status"] == "pending"
            assert not state["settings"]["running"]
            assert len(await store.outgoing()) == 2
            return
        assert incoming == []
        assert await store.outgoing() == []
        return
    assert len(incoming) == 1
    update_id, received = incoming[0]
    assert received[kind]["from"]["id"] == sender
    await store.apply(lambda state: handle(state, received, runtime.status), inbox_id=update_id)
    outgoing = await store.outgoing()
    assert len(outgoing) == 1
    assert "CryptoBot Assistant" in outgoing[0][2]["text"]
    assert outgoing[0][2]["buttons"]
