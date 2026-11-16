import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sniper.parser import deal_status, is_trade_chat_notice
from sniper.runtime import Runtime
from sniper.telegram_ui import InterfaceChanged, Key, Screen, TelegramUI
from sniper.trading import Execution

CHAT = (
    "📩 Сообщение от Shortcut Tanager по сделке #D40601482:\n\n"
    "⚠️ Не используйте платёжные реквизиты или контактную информацию из этого сообщения.\n\n"
    "Почему отменил?"
)
LISTING = "Здесь вы можете купить USDT за RUB через СБП."


def message(screen):
    return SimpleNamespace(
        id=screen.id,
        raw_text=screen.text,
        out=False,
        entities=[],
        buttons=[[SimpleNamespace(text=k.text, data=k.data, url=k.url)] for k in screen.keys],
    )


def chat_screen(body=CHAT, message_id=20):
    return Screen(message_id, body, (Key("Ответить", b"reply"), Key("Посмотреть сделку", b"view")))


@pytest.mark.parametrize(
    "body", ["Почему отменил?", "Сделка отменена", "Сделка завершена", "Продавец принял сделку"]
)
def test_counterparty_quotes_never_become_trade_status(body):
    text = CHAT.replace("Почему отменил?", body)
    assert is_trade_chat_notice(text)
    assert deal_status(text) == "UNKNOWN"


def test_actual_trade_status_is_still_recognized():
    assert not is_trade_chat_notice("Сделка #D40601482 отменена")
    assert deal_status("Сделка #D40601482 отменена") == "CANCELLED"


def test_chat_events_and_old_edits_do_not_replace_current_navigation():
    ui = TelegramUI(AsyncMock(), "peer")
    listing = Screen(10, LISTING, (Key("· 1 ·", b"refresh"),))
    ui.remember(listing)
    ui.changed.clear()
    assert not ui.remember(chat_screen())
    assert not ui.remember(Screen(5, "Сделка #D40601482 отменена"))
    assert ui.latest == listing and not ui.changed.is_set()
    assert ui.remember(Screen(21, "Неизвестный ответ CryptoBot"))  # Don't hide genuine UI errors.


async def test_history_skips_seller_message_even_after_restart():
    listing = Screen(10, LISTING, (Key("‹ Назад", b"back"),))
    client = AsyncMock()
    client.get_messages.return_value = [message(chat_screen()), message(listing)]
    ui = TelegramUI(client, "peer")
    assert await ui.current(force=True) == listing


async def test_burst_of_messages_uses_known_screen_by_id():
    listing = Screen(10, LISTING, (Key("· 1 ·", b"refresh"),))
    client = AsyncMock()
    client.get_messages.side_effect = [
        [message(chat_screen(message_id=20 + i)) for i in range(20)],
        message(listing),
    ]
    ui = TelegramUI(client, "peer")
    ui.remember(listing)
    assert await ui.current(force=True) == listing
    assert client.get_messages.call_args.kwargs["ids"] == 10


async def test_refresh_click_survives_chat_before_and_during_response():
    listing = Screen(10, LISTING, (Key("· 1 ·", b"refresh"), Key("89.89₽ · 96K", b"offer")))
    updated = Screen(10, LISTING, (Key("· 1 ·", b"refresh"), Key("89.90₽ · 96K", b"offer")))
    client = AsyncMock()
    client.get_messages.return_value = [message(chat_screen()), message(listing)]
    ui = TelegramUI(client, "peer")

    async def response(_request):
        ui.remember(chat_screen())
        ui.remember(updated)
        return SimpleNamespace(alert=False)

    client.side_effect = response
    assert await ui.click(listing, listing.keys[0]) == updated
    client.assert_awaited_once()
    request = client.call_args.args[0]
    assert request.msg_id == listing.id and request.data == b"refresh"


async def test_wait_for_ui_change_does_not_finish_on_seller_message():
    listing = Screen(10, LISTING)
    updated = Screen(10, LISTING + " Обновлено")
    ui = TelegramUI(AsyncMock(), "peer")
    ui.remember(listing)

    async def events():
        await asyncio.sleep(0.01)
        ui.remember(chat_screen())
        await asyncio.sleep(0.01)
        ui.remember(updated)

    task = asyncio.create_task(events())
    assert await ui.wait_changed(listing) == updated
    await task
    ui.client.get_messages.assert_not_awaited()


async def test_runtime_does_not_pause_or_observe_trade_on_seller_message():
    runtime = SimpleNamespace(
        store=SimpleNamespace(read=AsyncMock()), execution=SimpleNamespace(observe=AsyncMock())
    )
    await Runtime.receive_screen(runtime, chat_screen())
    runtime.store.read.assert_not_awaited()
    runtime.execution.observe.assert_not_awaited()


async def test_seller_claim_does_not_close_active_trade_or_associate_submission(store):
    active = {"id": "mine", "deal_id": "D40601482", "managed": True, "stage": "ACCEPTED", "message_id": 10}
    await store.apply(lambda s: s.update(active=dict(active)))
    execution = Execution(store, None, None, None, asyncio.Event())
    screen = chat_screen(CHAT.replace("Почему отменил?", "Сделка отменена"))
    await execution.observe(screen)
    assert (await store.read())["active"] == active
    with pytest.raises(InterfaceChanged, match="not a submission result"):
        await execution.observe(screen, associate=True)
    assert (await store.read())["active"] == active
