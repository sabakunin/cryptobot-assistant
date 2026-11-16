import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from sniper.telegram_ui import Key, Screen
from sniper.trading import Execution


def screens(card):
    return [
        Screen(1, card, (Key("📈 Купить 53.088984 USDT", b"buy"),)),
        Screen(2, "Пришлите сумму USDT", (Key("Макс. · 53.088984 USDT", b"max"),)),
        Screen(
            3,
            "Вы уверены, что хотите купить 53.088984 USDT за 4 771.11 RUB?\nЦена за 1 USDT: 89.87 RUB",
            (Key("📥 Создать сделку", b"create"),),
        ),
        Screen(
            4,
            "Заявка на покупку создана. Сделка #D123456. Ожидание продавца.",
            (Key("Отменить сделку", b"cancel"),),
        ),
    ]


class FakeUI:
    def __init__(self, items, fail_create=False, hook=None):
        self.items, self.calls = items, []
        self.fail_create, self.hook = fail_create, hook
        self.position = 0

    async def click(self, screen, key, **kwargs):
        self.calls.append(key.data)
        if self.hook:
            await self.hook(key)
        if key.data == b"create" and self.fail_create:
            raise TimeoutError("Response lost after possible submission")
        self.position += 1
        return self.items[self.position]

    async def open_deal(self, deal_id):
        return self.items[self.position]


async def engine(store, ui):
    await store.claim("test-worker")

    def configure(s):
        s["account_checked"] = True
        s["settings"].update(mode="auto", auto_max="90", running=True)

    await store.apply(configure)
    config = SimpleNamespace(live_trading_enabled=True, p2p_automation_authorized=True)
    return Execution(store, ui, config, "test-worker", asyncio.Event())


async def test_complete_creation_and_cancellation_flow(store, offer, card):
    items = screens(card) + [
        Screen(5, "Вы уверены, что хотите отменить сделку #D123456?", (Key("Да, отменить", b"confirm"),)),
        Screen(6, "Сделка #D123456 отменена"),
    ]
    ui = FakeUI(items)
    executor = await engine(store, ui)
    await executor.buy(offer, items[0])
    active = (await store.read())["active"]
    assert active["stage"] == "REQUESTED" and active["deal_id"] == "D123456"
    await store.apply(lambda s: s["active"].update(cancel_requested=True))
    await executor.cancel(active["id"])
    assert ui.calls == [b"buy", b"max", b"create", b"cancel", b"confirm"]
    state = await store.read()
    assert state["active"] is None and offer.id in state["ignored"]
    await executor.buy(offer, items[0])
    assert len(ui.calls) == 5


async def test_unknown_submission_never_retried(store, offer, card):
    ui = FakeUI(screens(card), fail_create=True)
    executor = await engine(store, ui)
    with pytest.raises(TimeoutError):
        await executor.buy(offer, ui.items[0])
    assert (await store.read())["active"]["stage"] == "UNKNOWN"
    await executor.buy(offer, ui.items[0])
    assert ui.calls.count(b"create") == 1


async def test_stop_before_submit_prevents_purchase(store, offer, card):
    async def hook(key):
        if key.data == b"max":
            await store.apply(lambda s: s["settings"].update(running=False))

    ui = FakeUI(screens(card), hook=hook)
    executor = await engine(store, ui)
    with pytest.raises(RuntimeError):
        await executor.buy(offer, ui.items[0])
    assert b"create" not in ui.calls
    assert (await store.read())["active"] is None


async def test_final_price_rechecked(store, offer, card):
    items = screens(card)
    items[2] = replace(
        items[2],
        text="Вы уверены, что хотите купить 53.088984 USDT за 4 830.04 RUB?\nЦена за 1 USDT: 90.98 RUB",
    )
    ui = FakeUI(items)
    executor = await engine(store, ui)
    with pytest.raises(RuntimeError, match="price exceeds"):
        await executor.buy(offer, items[0])
    assert b"create" not in ui.calls


async def test_parallel_candidates_create_only_one(store, offer, card):
    ui = FakeUI(screens(card))
    executor = await engine(store, ui)
    await asyncio.gather(
        executor.buy(offer, ui.items[0]), executor.buy(replace(offer, id="O999"), ui.items[0])
    )
    assert ui.calls.count(b"create") == 1


async def test_disabled_execution_has_no_side_effects(store, offer, card):
    ui = FakeUI(screens(card))
    executor = await engine(store, ui)
    executor.config.live_trading_enabled = False
    with pytest.raises(RuntimeError):
        await executor.buy(offer, ui.items[0])
    assert not ui.calls


async def test_cancel_refuses_paid_trade(store, offer, card):
    ui = FakeUI(screens(card))
    executor = await engine(store, ui)
    await executor.buy(offer, ui.items[0])
    active = (await store.read())["active"]
    ui.items[3] = Screen(4, "Сделка #D123456. Вы отметили оплату.", (Key("Отменить сделку", b"cancel"),))
    await store.apply(lambda s: s["active"].update(cancel_requested=True))
    await executor.cancel(active["id"])
    assert b"cancel" not in ui.calls
    assert (await store.read())["active"]["stage"] == "PAID"


async def test_wrong_trade_cannot_resolve_unknown(store, offer, card):
    ui = FakeUI(screens(card))
    executor = await engine(store, ui)
    await executor.buy(offer, ui.items[0])
    await executor.observe(Screen(10, "Сделка #D999999 отменена"))
    assert (await store.read())["active"] is not None


async def test_lost_lease_prevents_submission(store, offer, card):
    async def hook(key):
        if key.data == b"max":
            await store.release("test-worker")

    ui = FakeUI(screens(card), hook=hook)
    executor = await engine(store, ui)
    with pytest.raises(RuntimeError, match="lease"):
        await executor.buy(offer, ui.items[0])
    assert b"create" not in ui.calls
