"""Regression checks for cancellation, independent controls and amount limits. No live Telegram."""

import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet
from test_execution import FakeUI, engine, screens

from sniper.accounts import AccountStore, add_account
from sniper.config import Config
from sniper.control import handle
from sniper.engine import eligible
from sniper.panel import deliver_panel
from sniper.parser import deal_status, listed_amount_ceiling
from sniper.runtime import Runtime
from sniper.store import initial_state
from sniper.telegram_ui import InterfaceChanged, Key, Screen, TelegramUI
from sniper.trading import Execution


@pytest.mark.parametrize(
    "text,status",
    [
        ("Сделка #D123456 отменена", "CANCELLED"),
        ("✅ Вы отменили сделку #D123456", "CANCELLED"),
        ("Заявка на покупку #D123456 была отменена", "CANCELLED"),
        ("Сделка #D123456\nСтатус: отменена", "CANCELLED"),
        ("Продавец отклонил заявку #D123456", "REJECTED"),
        ("Сделка #D123456 успешно завершена", "COMPLETED"),
        ("Если сделка отменена, обратитесь в поддержку", "UNKNOWN"),
        ("Условия сделки:\nСделка #D123456 отменена", "UNKNOWN"),
        ("Вы уверены, что хотите отменить сделку #D123456?", "UNKNOWN"),
    ],
)
def test_terminal_status_variants_and_untrusted_conditions(text, status):
    assert deal_status(text) == status


def active():
    return dict(
        id="local",
        deal_id="D123456",
        managed=True,
        stage="ACCEPTED",
        message_id=10,
        offer_id="O1000",
        paper=False,
        link="https://t.me/CryptoBot",
    )


async def test_terminal_repeats_and_late_open_message_do_not_reblock(store):
    await store.apply(lambda s: s.update(active=active()))
    await store.apply(lambda s: s["settings"].update(running=True))
    execution = Execution(store, None, None, None, asyncio.Event())
    runtime = SimpleNamespace(
        store=store,
        execution=execution,
        config=SimpleNamespace(monitor_source="userbot"),
        monitor_wake=asyncio.Event(),
        wake=asyncio.Event(),
    )
    for text in (
        "Вы отменили сделку #D123456",
        "Сделка #D123456 отменена",
        "Заявка создана. Сделка #D123456. Ожидание продавца.",
    ):
        await Runtime.receive_screen(runtime, Screen(10, text))
        assert (await store.read())["active"] is None
    state = await store.read()
    assert state["settings"]["running"] and state["account_checked"]
    assert "D123456" in state["closed_deals"]
    assert len(await store.outgoing()) == 1


async def test_idless_terminal_only_closes_known_message(store):
    await store.apply(lambda s: s.update(active=active()))
    execution = Execution(store, None, None, None, asyncio.Event())
    await execution.observe(Screen(99, "Сделка отменена"))
    assert (await store.read())["active"]
    await execution.observe(Screen(10, "Сделка отменена"))
    assert (await store.read())["active"] is None


async def test_new_idless_cancellation_schedules_verification_instead_of_sticking(store):
    await store.apply(lambda s: s.update(active=active()))
    execution = Execution(store, None, None, None, asyncio.Event())
    runtime = SimpleNamespace(
        store=store,
        execution=execution,
        config=SimpleNamespace(monitor_source="userbot"),
        monitor_wake=asyncio.Event(),
        wake=asyncio.Event(),
    )
    await Runtime.receive_screen(runtime, Screen(99, "Вы отменили сделку"))
    state = await store.read()
    assert state["active"] and state["reconcile_requested"]
    assert runtime.monitor_wake.is_set()


@pytest.mark.parametrize("empty", [True, False])
async def test_reconcile_missing_detail_requires_explicit_empty_active_list(store, empty):
    await store.apply(lambda s: s.update(active=active()))
    await store.apply(lambda s: s["settings"].update(running=True))
    ui = SimpleNamespace(
        open_deal=AsyncMock(side_effect=InterfaceChanged("gone")),
        active_deals=AsyncMock(
            return_value=Screen(11, "Нет активных сделок" if empty else "Количество подходящих сделок: 0")
        ),
    )
    execution = Execution(store, ui, None, None, asyncio.Event())
    await execution.reconcile()
    state = await store.read()
    assert (state["active"] is None) == empty
    assert state["settings"]["running"]


async def test_cancellation_idless_new_message_checks_active_list(store, offer, card):
    items = screens(card) + [Screen(5, "Сделка отменена")]
    ui = FakeUI(items)
    ui.active_deals = AsyncMock(return_value=Screen(6, "Нет активных сделок"))
    execution = await engine(store, ui)
    await execution.buy(offer, items[0])
    await store.apply(lambda s: s["active"].update(cancel_requested=True))
    await execution.cancel((await store.read())["active"]["id"])
    assert (await store.read())["active"] is None
    assert ui.calls == [b"buy", b"max", b"create", b"cancel"]
    ui.active_deals.assert_awaited_once()


@pytest.mark.parametrize(
    "minimum,creates", [(None, True), ("4771.11", True), ("4771.12", False), ("6000", False)]
)
async def test_exact_final_amount_filter(store, offer, card, minimum, creates):
    ui = FakeUI(screens(card))
    execution = await engine(store, ui)
    await store.apply(lambda s: s["settings"].update(min_order_rub=minimum))
    await execution.buy(offer, ui.items[0])
    assert (b"create" in ui.calls) == creates
    assert (await store.read())["settings"]["running"]
    if not creates:
        assert (await store.read())["active"] is None


async def test_minimum_changed_during_preparation_is_rechecked(store, offer, card):
    async def hook(key):
        if key.data == b"max":
            await store.apply(lambda s: s["settings"].update(min_order_rub="5000"))

    ui = FakeUI(screens(card), hook=hook)
    execution = await engine(store, ui)
    await execution.buy(offer, ui.items[0])
    assert ui.calls == [b"buy", b"max"]
    assert (await store.read())["active"] is None


async def test_long_conditions_are_complete_and_fit_telegram_after_creation(store, offer, card):
    ui = FakeUI(screens(card))
    execution = await engine(store, ui)
    terms = "Многозадачность\n" + "🙂" * 3500
    await execution.buy(replace(offer, terms=terms, multitasking=True), ui.items[0])
    messages = [payload for _, _, payload in await store.outgoing()]
    assert terms in "".join(payload["text"] for payload in messages)
    assert "многозадачность" in messages[0]["text"]
    assert messages[0]["buttons"]
    assert all(len(payload["text"].encode("utf-16-le")) // 2 <= 3400 for payload in messages)


def test_rounded_card_volume_does_not_reject_possible_exact_minimum(offer):
    settings = dict(mode="auto", auto_max="90", min_order_rub="4771.11")
    assert eligible(
        replace(offer, volume="53", amount_source="объём в тексте (может быть округлён)"), settings
    )


@pytest.mark.parametrize(
    "label,ceiling",
    [
        ("Exchange · 89.87₽ · 6.08K", "6090"),
        ("89.9₽ · 15K–21.48K", "21490"),
        ("89.9₽ · 96K", "97000"),
        ("89.9₽ · 1.5M", "1600000"),
        ("89.9₽ · 5000", "5001"),
        ("89.9₽ · неизвестно", None),
        ("Назад", None),
    ],
)
def test_listing_minimum_uses_conservative_rounded_ceiling(label, ceiling):
    from decimal import Decimal

    assert listed_amount_ceiling(label) == (Decimal(ceiling) if ceiling else None)


async def test_small_listing_is_skipped_without_opening_card():
    state = initial_state()
    state["settings"].update(running=True, auto_max="90", min_order_rub="5000")
    listing = Screen(
        1,
        "Здесь вы можете купить USDT за RUB через СБП.",
        (Key("· 1 ·", b"page"), Key("89.87₽ · 4.7K", b"small")),
    )
    runtime = SimpleNamespace(
        store=SimpleNamespace(owns=AsyncMock(return_value=True), read=AsyncMock(return_value=state)),
        owner="test",
        ui=SimpleNamespace(click=AsyncMock()),
    )
    await Runtime.scan(runtime, listing)
    runtime.ui.click.assert_not_awaited()


async def test_refresh_without_push_still_reads_history():
    from test_fast_navigation import message

    client = AsyncMock(return_value=SimpleNamespace(alert=False))
    ui = TelegramUI(client, "fake")
    ui.action_interval = 0
    screen = Screen(1, "Здесь вы можете купить USDT за RUB через СБП.", (Key("· 1 ·", b"one"),))
    ui.remember(screen)
    client.get_messages.return_value = [message(screen)]
    assert await ui.refresh_first_page() == screen
    client.get_messages.assert_awaited_once()


@pytest.mark.parametrize("disable", ["0", "minimum:off"])
def test_minimum_can_be_disabled_without_restarting(disable):
    state = initial_state()
    state["settings"].update(min_order_rub="5000", running=True)
    state["notified"]["auto:O123"] = "previous-filter"
    handle(state, {"callback_query": {"data": "set:min_order_rub"}}, {})
    update = {"message": {"text": disable}} if disable == "0" else {"callback_query": {"data": disable}}
    handle(state, update, {})
    assert state["settings"]["min_order_rub"] is None
    assert state["settings"]["running"] and not state["notified"]


async def test_amount_and_closed_deals_are_isolated_per_account(store):
    ids = []
    await store.apply(lambda s: ids.extend(add_account(s, "encrypted", i, str(i)) for i in (1, 2)))
    one, two = [AccountStore(store, key) for key in ids]
    await one.apply(lambda s: s["settings"].update(min_order_rub="5000"))
    await one.apply(lambda s: s["closed_deals"].update(D123456=1))
    assert (await two.read())["settings"]["min_order_rub"] is None
    assert not (await two.read())["closed_deals"]


def test_old_navigation_buttons_work_without_start():
    state = initial_state()
    state["ui_revision"] = 100
    result = handle(state, {"callback_query": {"data": "ui:1:set:min_order_rub"}}, {})
    assert result and state["pending"]["field"] == "min_order_rub"
    assert "/start" not in result[0]["text"]


async def test_outdated_queued_panel_is_not_sent(store):
    await store.apply(lambda s: handle(s, {"message": {"text": "/start"}}, {}))
    old = (await store.outgoing())[0][2]
    await store.apply(lambda s: handle(s, {"callback_query": {"data": "set:min_order_rub"}}, {}))
    bot = SimpleNamespace(send_message=AsyncMock(), edit_message_text=AsyncMock())
    await deliver_panel(store, bot, 42, old)
    bot.send_message.assert_not_awaited()
    bot.edit_message_text.assert_not_awaited()


async def test_unchanged_push_refresh_avoids_history_and_delay():
    client = AsyncMock()
    ui = TelegramUI(client, "fake")
    ui.action_interval = 0
    screen = Screen(1, "Здесь вы можете купить USDT за RUB через СБП.", (Key("· 1 ·", b"one"),))
    ui.remember(screen)

    async def response(request):
        ui.remember(screen)
        return SimpleNamespace(alert=False)

    client.side_effect = response
    assert await asyncio.wait_for(ui.refresh_first_page(), 0.3) == screen
    client.get_messages.assert_not_awaited()


async def test_control_queue_and_ack_are_independent_of_blocked_send(store):
    config = Config(
        _env_file=None,
        bot_token="123456:fake",
        admin_user_id=42,
        app_key=Fernet.generate_key().decode(),
        webhook_secret="x" * 32,
    )
    runtime = Runtime(config)
    await runtime.bot.session.close()
    await runtime.store.engine.dispose()
    runtime.store = store
    started, release, ack_started = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def send(*args, **kwargs):
        started.set()
        await release.wait()
        return SimpleNamespace(message_id=100)

    async def ack(*args, **kwargs):
        ack_started.set()
        await release.wait()

    runtime.bot = SimpleNamespace(send_message=send, edit_message_text=AsyncMock(), answer_callback_query=ack)
    await store.apply(lambda s: [dict(text="slow notification", buttons=[])])
    tasks = [asyncio.create_task(runtime.delivery()), asyncio.create_task(runtime.control_loop())]
    try:
        await asyncio.wait_for(started.wait(), 2)
        update = dict(
            update_id=1,
            callback_query={
                "id": "test",
                "from": {"id": 42},
                "message": {"message_id": 100, "chat": {"id": 42, "type": "private"}},
                "data": "set:min_order_rub",
            },
        )
        await asyncio.wait_for(runtime.accept(update), 1)
        await asyncio.wait_for(ack_started.wait(), 1)
        deadline = time.monotonic() + 2
        while not (await store.read())["pending"]:
            assert time.monotonic() < deadline
            await asyncio.sleep(0.005)
        assert not release.is_set()
        assert (await store.read())["pending"]["field"] == "min_order_rub"
    finally:
        release.set()
        runtime.stopping.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, *runtime.callback_tasks, return_exceptions=True)
