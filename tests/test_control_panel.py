import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText

from sniper.control import callback_hint, handle, menu, panel_response, rates
from sniper.panel import deliver_panel
from sniper.store import initial_state
from sniper.telegram_ui import Key, Screen
from sniper.trading import Execution


def callback(data, message_id=123):
    return {"callback_query": {"id": "cb", "data": data, "message": {"message_id": message_id}}}


def find_action(payload, action):
    return next(
        item["callback_data"]
        for row in payload["buttons"]
        for item in row
        if item.get("callback_data", "").split(":", 2)[-1] == action
    )


async def enqueue_panel(store, view=menu):
    await store.apply(lambda state: [panel_response(state, view(state))])
    return (await store.outgoing())[0][2]


def test_double_tap_reuses_confirmation_and_ignores_old_buttons():
    state = initial_state()
    state["settings"].update(mode="auto", auto_max="90")
    state["account_checked"] = True
    home = handle(state, {"message": {"text": "/start"}}, {})[0]
    click = callback(find_action(home, "run"))
    confirmation = handle(state, click, {"live_enabled": True})[0]
    nonce = state["arm"]["nonce"]
    assert not state["settings"]["running"]
    assert handle(state, click, {"live_enabled": True}) == []
    assert state["arm"]["nonce"] == nonce
    confirm = callback(find_action(confirmation, "armed:" + nonce))
    handle(state, confirm, {"live_enabled": True})
    assert state["settings"]["running"]
    assert handle(state, confirm, {"live_enabled": True})[0]["text"].startswith("CryptoBot Assistant")
    assert sum(item["action"] == "real_execution_armed" for item in state["audit"]) == 1


def test_legacy_mode_button_cannot_restore_removed_mode():
    state = initial_state()
    state["settings"]["running"] = True
    handle(state, {"message": {"text": "/start"}}, {})
    handle(state, callback("mode:notify"), {})
    assert state["settings"]["running"]


def test_unconfigured_auto_prompts_then_offers_start_without_arming():
    state = initial_state()
    state["settings"].update(running=False, notify_min="89.87", notify_max="90")
    runtime = {"live_enabled": True}
    home = handle(state, {"message": {"text": "/start"}}, runtime)[0]
    prompt = handle(state, callback(find_action(home, "set:auto_max")), runtime)[0]
    assert "максимальный курс" in prompt["text"]
    assert state["pending"]["field"] == "auto_max"
    assert state["settings"]["auto_max"] is None
    assert not state["settings"]["running"]
    saved = handle(state, {"message": {"text": "89,5"}}, runtime)[0]
    assert state["settings"]["auto_max"] == "89.5"
    assert (state["settings"]["notify_min"], state["settings"]["notify_max"]) == ("89.87", "90")
    assert not state["settings"]["running"] and not state.get("arm")
    assert find_action(saved, "run")
    handle(state, callback(find_action(saved, "run")), runtime)
    assert state["reconcile_requested"] and state["arm_after_reconcile"]
    assert not state["settings"]["running"]


@pytest.mark.parametrize("entry", ["/start", "mode:auto", "run"])
def test_unconfigured_auto_has_direct_setup_path(entry):
    state = initial_state()
    state["settings"].update(mode="auto", notify_min="89.87", notify_max="90")
    update = {"message": {"text": entry}} if entry.startswith("/") else callback(entry)
    view = handle(state, update, {"live_enabled": True})[0]
    if entry == "/start":
        assert "Для запуска укажи курс" in view["text"]
        handle(state, callback(find_action(view, "set:auto_max")), {"live_enabled": True})
    assert state["pending"]["field"] == "auto_max"
    assert not state["settings"]["running"]


def test_configured_auto_mode_preserves_threshold_and_does_not_start():
    state = initial_state()
    state["settings"].update(mode="notify", running=True, auto_max="88.5", notify_max="90")
    view = handle(state, callback("mode:auto"), {"live_enabled": True})[0]
    assert state["settings"]["auto_max"] == "88.5"
    assert not state["settings"]["running"] and state["pending"] is None
    assert find_action(view, "run")


def test_optional_minimum_input_updates_only_minimum():
    state = initial_state()
    state["settings"].update(notify_min="80", notify_max="81")
    view = handle(state, {"message": {"text": "/settings"}}, {})[0]
    handle(state, callback(find_action(view, "set:min_order_rub")), {})
    handle(state, {"message": {"text": "5 000"}}, {})
    assert state["settings"]["min_order_rub"] == "5000"
    assert state["settings"]["notify_min"] == "80"
    assert state["settings"]["notify_max"] == "81"
    assert state["pending"] is None


def test_cancel_confirmation_is_single_use_and_legacy_confirmations_blocked():
    state = initial_state()
    state["active"] = {
        "id": "trade",
        "deal_id": "D123",
        "managed": True,
        "stage": "ACCEPTED",
        "paper": False,
        "link": "https://t.me/CryptoBot",
    }
    view = handle(state, callback("deal"), {})[0]
    confirm = handle(state, callback(find_action(view, "cancel:trade")), {})[0]
    handle(state, callback(find_action(confirm, "cancelok:trade")), {"live_enabled": True})
    assert state["active"]["cancel_requested"]
    assert handle(state, callback(find_action(confirm, "cancelok:trade")), {"live_enabled": True})
    assert sum(item["action"] == "cancel_requested" for item in state["audit"]) == 1
    state["active"]["cancel_requested"] = False
    handle(state, callback("cancelok:trade"), {"live_enabled": True})
    assert not state["active"]["cancel_requested"]


async def test_burst_panel_updates_coalesce_and_cas_preserves_newest(store):
    for _ in range(12):
        await store.apply(lambda state: handle(state, {"message": {"text": "/start"}}, {}))
    outgoing = await store.outgoing()
    assert len(outgoing) == 1 and outgoing[0][0] == "control-panel"
    old = outgoing[0][2]
    new = await enqueue_panel(store, rates)
    assert not await store.sent("control-panel", old["_delivery"])
    await store.retry("control-panel", 7, 300, old["_delivery"])
    outgoing = await store.outgoing()
    assert outgoing[0][1] == 0 and outgoing[0][2] == new
    assert await store.sent("control-panel", new["_delivery"])
    assert await store.outgoing() == []


async def test_panel_sends_once_then_edits_same_message(store):
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=321)), edit_message_text=AsyncMock()
    )
    first = await enqueue_panel(store)
    await deliver_panel(store, bot, 42, first)
    second = await enqueue_panel(store, rates)
    await deliver_panel(store, bot, 42, second)
    await deliver_panel(store, bot, 42, second)
    bot.send_message.assert_awaited_once()
    bot.edit_message_text.assert_awaited_once()
    assert bot.edit_message_text.call_args.kwargs["message_id"] == 321
    assert (await store.read())["control_panel"]["message_id"] == 321


@pytest.mark.parametrize(
    "reason,send_count", [("message is not modified", 0), ("message to edit not found", 1)]
)
async def test_expected_edit_errors_do_not_create_copies(store, reason, send_count):
    await store.apply(lambda state: state.update(control_panel={"message_id": 321}))
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=322)),
        edit_message_text=AsyncMock(
            side_effect=TelegramBadRequest(
                method=EditMessageText(chat_id=42, message_id=321, text="test"), message=reason
            )
        ),
    )
    payload = await enqueue_panel(store)
    await deliver_panel(store, bot, 42, payload)
    assert bot.send_message.await_count == send_count


async def test_unknown_initial_send_is_not_repeated_automatically(store):
    bot = SimpleNamespace(
        send_message=AsyncMock(side_effect=TimeoutError("response lost")), edit_message_text=AsyncMock()
    )
    payload = await enqueue_panel(store)
    with pytest.raises(TimeoutError):
        await deliver_panel(store, bot, 42, payload)
    await deliver_panel(store, bot, 42, payload)
    bot.send_message.assert_awaited_once()
    # The delivered panel's callback recovers its ID after the lost response.
    data = callback(find_action(payload, "set:auto_max"), message_id=321)
    await store.apply(lambda state: handle(state, data, {}))
    recovered = (await store.outgoing())[0][2]
    await deliver_panel(store, bot, 42, recovered)
    assert bot.edit_message_text.call_args.kwargs["message_id"] == 321
    bot.send_message.assert_awaited_once()


async def test_active_check_returns_confirmation_in_panel_without_arming(store):
    def setup(state):
        state["settings"].update(mode="auto", auto_max="90")
        return handle(state, {"message": {"text": "/start"}}, {})

    await store.apply(setup)
    home = (await store.outgoing())[0][2]
    await store.apply(lambda state: handle(state, callback(find_action(home, "run")), {"live_enabled": True}))
    empty = Screen(
        1,
        "Здесь сделки за всё время. Количество подходящих сделок: 0",
        tuple(
            Key(label, b"filter") for label in ["Активные", "Все типы", "Все монеты", "Все способы оплаты"]
        ),
    )
    ui = SimpleNamespace(active_deals=AsyncMock(return_value=empty))
    executor = Execution(store, ui, SimpleNamespace(live_trading_enabled=True), None, asyncio.Event())
    await executor.reconcile()
    state = await store.read()
    assert state["account_checked"] and state["arm"] and not state["settings"]["running"]
    outgoing = await store.outgoing()
    assert len(outgoing) == 1 and outgoing[0][2]["kind"] == "panel"
    assert find_action(outgoing[0][2], "armed:" + state["arm"]["nonce"])


async def test_start_sends_visible_panel_then_navigation_edits_it(store):
    await store.apply(lambda s: s.update(control_panel={"message_id": 321}))
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=322)),
        edit_message_text=AsyncMock(),
    )
    await store.apply(lambda s: handle(s, {"message": {"text": "/start"}}, {}))
    first = (await store.outgoing())[0][2]
    await deliver_panel(store, bot, 42, first)
    bot.send_message.assert_awaited_once()
    bot.edit_message_text.assert_not_awaited()
    await store.apply(lambda s: handle(s, callback(find_action(first, "set:auto_max"), message_id=322), {}))
    next_panel = (await store.outgoing())[0][2]
    await deliver_panel(store, bot, 42, next_panel)
    assert bot.edit_message_text.call_args.kwargs["message_id"] == 322
    bot.send_message.assert_awaited_once()


async def test_start_burst_reuses_opening_but_later_start_reopens(store):
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=322)), edit_message_text=AsyncMock()
    )
    for _ in range(3):
        await store.apply(lambda s: handle(s, {"message": {"text": "/start"}}, {}))
        await deliver_panel(store, bot, 42, (await store.outgoing())[0][2])
    bot.send_message.assert_awaited_once()
    await store.apply(lambda s: s.update(panel_open_at=0))
    await store.apply(lambda s: handle(s, {"message": {"text": "/start"}}, {}))
    await deliver_panel(store, bot, 42, (await store.outgoing())[0][2])
    assert bot.send_message.await_count == 2


async def test_uncertain_start_send_is_not_duplicated_by_new_payload_generation(store):
    await store.apply(lambda s: s.update(control_panel={"message_id": 321}))
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=TimeoutError()), edit_message_text=AsyncMock())
    await store.apply(lambda s: handle(s, {"message": {"text": "/start"}}, {}))
    payload = (await store.outgoing())[0][2]
    with pytest.raises(TimeoutError):
        await deliver_panel(store, bot, 42, payload)
    await store.apply(lambda s: handle(s, {"message": {"text": "/start"}}, {}))
    payload = (await store.outgoing())[0][2]
    await deliver_panel(store, bot, 42, payload)
    bot.send_message.assert_awaited_once()
    await store.apply(lambda s: handle(s, callback(find_action(payload, "set:auto_max"), message_id=322), {}))
    await deliver_panel(store, bot, 42, (await store.outgoing())[0][2])
    assert bot.edit_message_text.call_args.kwargs["message_id"] == 322
    bot.send_message.assert_awaited_once()


def test_stale_button_has_visible_hint_but_stop_remains_available():
    state = initial_state()
    state["ui_revision"] = 5
    assert callback_hint(state, {"data": "ui:4:rates"}) is None
    assert callback_hint(state, {"data": "ui:4:armed:old"})
    assert callback_hint(state, {"data": "ui:5:rates"}) is None
    assert callback_hint(state, {"data": "ui:4:stop"}) is None
    assert callback_hint(state, {"data": "ui:4:acct:example:stop"}) is None
