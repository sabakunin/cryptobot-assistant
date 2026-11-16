import asyncio
import logging
from contextlib import nullcontext
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telethon.errors import BotResponseTimeoutError, FloodWaitError

import sniper.runtime as runtime_module
from sniper.parser import ParseError
from sniper.runtime import Runtime
from sniper.security import log_error
from sniper.telegram_ui import InterfaceChanged, Key, Screen, TelegramUI
from sniper.trading import Execution

LISTING = Screen(10, "Здесь вы можете купить USDT за RUB через СБП.", (Key("· 1 ·", b"one"),))


def runtime_for(store, monkeypatch):
    monkeypatch.setattr(runtime_module, "NAVIGATION_RECOVERY_DELAYS", (0.01, 0.02, 0.04))
    runtime = Runtime.__new__(Runtime)
    runtime.config = SimpleNamespace(refresh_seconds=1, action_interval=0.2)
    runtime.store = store
    runtime.ui = SimpleNamespace(ready_listing=lambda _: None, offer_list=AsyncMock(return_value=LISTING))
    runtime.execution = SimpleNamespace(reconcile=AsyncMock())
    runtime.stopping, runtime.monitor_wake, runtime.wake = asyncio.Event(), asyncio.Event(), asyncio.Event()
    runtime.errors = SimpleNamespace(inc=Mock())
    runtime.scan_time = SimpleNamespace(time=nullcontext)
    return runtime


async def configure(store):
    def apply(state):
        state["settings"].update(mode="auto", running=True, auto_max="90")
        state["account_checked"] = True
    await store.apply(apply)


@pytest.mark.parametrize("error", [InterfaceChanged("changed"), ParseError("card replaced"), BotResponseTimeoutError(None)])
async def test_navigation_recovers_without_disarming_or_replaying_callback(store, monkeypatch, error):
    runtime = runtime_for(store, monkeypatch)
    await configure(store)
    seen = []

    async def scan(listing):
        seen.append(listing)
        if len(seen) == 1:
            raise error
        runtime.stopping.set()
        runtime.monitor_wake.set()

    runtime.scan = scan
    await asyncio.wait_for(runtime.monitor_loop(), 2)
    state = await store.read()
    assert state["settings"]["running"] and not state["active"]
    assert seen == [None, LISTING]
    runtime.ui.offer_list.assert_awaited_once_with(reset=True)
    assert len(await store.outgoing()) == 2  # One recovery notice and one success, no duplicate flood.


async def test_persistent_unknown_interface_has_bounded_recovery(store, monkeypatch):
    runtime = runtime_for(store, monkeypatch)
    await configure(store)
    runtime.scan = AsyncMock(side_effect=InterfaceChanged("unsupported"))
    task = asyncio.create_task(runtime.monitor_loop())
    try:
        async with asyncio.timeout(2):
            while (await store.read())["settings"]["running"]:
                await asyncio.sleep(0.005)
        assert runtime.scan.await_count == 4
        assert runtime.ui.offer_list.await_count == 3
        assert len(await store.outgoing()) == 2
    finally:
        runtime.stopping.set()
        runtime.monitor_wake.set()
        await asyncio.wait_for(task, 1)


async def test_push_events_cannot_bypass_recovery_delay(store, monkeypatch):
    runtime = runtime_for(store, monkeypatch)
    monkeypatch.setattr(runtime_module, "NAVIGATION_RECOVERY_DELAYS", (0.15,))
    await configure(store)
    started = asyncio.Event()

    async def scan(_listing):
        started.set()
        raise InterfaceChanged("race")

    runtime.scan = AsyncMock(side_effect=scan)
    task = asyncio.create_task(runtime.monitor_loop())
    try:
        await asyncio.wait_for(started.wait(), 1)
        for _ in range(5):
            runtime.monitor_wake.set()
            await asyncio.sleep(0.005)
        assert runtime.scan.await_count == 1
        runtime.ui.offer_list.assert_not_awaited()
    finally:
        runtime.stopping.set()
        runtime.monitor_wake.set()
        await asyncio.wait_for(task, 1)


async def test_unknown_submission_never_enters_navigation_recovery(store, monkeypatch):
    runtime = runtime_for(store, monkeypatch)
    await configure(store)

    async def scan(_listing):
        await store.apply(lambda s: s.update(active={"id": "x", "managed": True, "stage": "UNKNOWN"}))
        runtime.stopping.set()
        runtime.monitor_wake.set()
        raise InterfaceChanged("lost submission response")

    runtime.scan = AsyncMock(side_effect=scan)
    await asyncio.wait_for(runtime.monitor_loop(), 1)
    assert (await store.read())["active"]["stage"] == "UNKNOWN"
    runtime.ui.offer_list.assert_not_awaited()
    assert runtime.scan.await_count == 1


async def test_stop_during_failed_navigation_is_preserved(store, monkeypatch):
    runtime = runtime_for(store, monkeypatch)
    await configure(store)

    async def scan(_listing):
        await store.apply(lambda s: s["settings"].update(running=False))
        runtime.stopping.set()
        runtime.monitor_wake.set()
        raise InterfaceChanged("changed")

    runtime.scan = scan
    await asyncio.wait_for(runtime.monitor_loop(), 1)
    assert not (await store.read())["settings"]["running"]
    runtime.ui.offer_list.assert_not_awaited()


@pytest.mark.parametrize("error", [InterfaceChanged("changed"), FloodWaitError(None, 4), BotResponseTimeoutError(None)])
async def test_preparation_failure_releases_reservation_and_allows_fresh_attempt(store, offer, error):
    await configure(store)
    await store.claim("owner")
    execution = Execution(store, None, SimpleNamespace(live_trading_enabled=True), "owner", asyncio.Event())
    execution.prepare = AsyncMock(side_effect=error)
    screen = Screen(1, "offer")
    with pytest.raises(type(error)):
        await execution.buy(offer, screen)
    state = await store.read()
    assert state["settings"]["running"] and state["active"] is None
    assert f"auto:{offer.id}" not in state["notified"]
    # Same offer must reach fresh validation again; no Create is available in this fake UI.
    with pytest.raises(type(error)):
        await execution.buy(offer, screen)
    assert execution.prepare.await_count == 2


async def test_flood_wait_propagates_without_scan_retries_or_disarming(store, monkeypatch):
    runtime = runtime_for(store, monkeypatch)
    await configure(store)
    runtime.scan = AsyncMock(side_effect=FloodWaitError(None, 4))
    with pytest.raises(FloodWaitError):
        await runtime.monitor_loop()
    assert (await store.read())["settings"]["running"]
    runtime.ui.offer_list.assert_not_awaited()
    assert not await store.outgoing()


async def test_interrupted_preparation_recovers_then_creates_exactly_once(store, offer, monkeypatch):
    runtime = runtime_for(store, monkeypatch)
    await configure(store)
    await store.claim("owner")
    confirmation = Screen(12, "confirmation", (Key("Создать сделку", b"create"),))
    runtime.ui.click = AsyncMock(return_value=Screen(13, "Заявка создана. Сделка #D123456. Ожидание продавца."))
    runtime.execution = Execution(
        store, runtime.ui, SimpleNamespace(live_trading_enabled=True), "owner", runtime.wake
    )
    runtime.execution.prepare = AsyncMock(side_effect=[
        InterfaceChanged("changed before submission"),
        (confirmation, Decimal("89.87"), Decimal("53.088984"), Decimal("4771.11")),
    ])

    async def scan(_listing):
        await runtime.execution.buy(offer, Screen(11, "offer"))
        runtime.stopping.set()
        runtime.monitor_wake.set()

    runtime.scan = scan
    await asyncio.wait_for(runtime.monitor_loop(), 2)
    runtime.ui.click.assert_awaited_once_with(confirmation, confirmation.keys[0])
    state = await store.read()
    assert state["settings"]["running"]
    assert state["active"]["stage"] == "REQUESTED"
    assert state["active"]["deal_id"] == "D123456"


@pytest.mark.parametrize("error", [InterfaceChanged("unknown result"), FloodWaitError(None, 4)])
async def test_create_error_preserves_unknown_transaction_instead_of_retrying(store, offer, error):
    await configure(store)
    await store.claim("owner")
    confirmation = Screen(12, "confirmation", (Key("Создать сделку", b"create"),))
    ui = SimpleNamespace(click=AsyncMock(side_effect=error))
    execution = Execution(store, ui, SimpleNamespace(live_trading_enabled=True), "owner", asyncio.Event())
    execution.prepare = AsyncMock(return_value=(confirmation, Decimal("89"), Decimal("1"), Decimal("89")))
    with pytest.raises(type(error)):
        await execution.buy(offer, Screen(11, "offer"))
    await execution.buy(offer, Screen(11, "offer"))
    assert (await store.read())["active"]["stage"] == "UNKNOWN"
    assert ui.click.await_count == 1


async def test_recovery_explicitly_reopens_market_even_from_stale_listing():
    ui = TelegramUI(AsyncMock(), "peer")
    market = Screen(11, "market", (Key("Купить", b"buy"),))
    coins = Screen(11, "coins", (Key("USDT", b"usdt"),))
    banks = Screen(11, "banks", (Key("СБП · 90 ₽", b"sbp"),))
    ui.current = AsyncMock(return_value=LISTING)
    ui.market = AsyncMock(return_value=market)
    ui.click = AsyncMock(side_effect=[coins, banks, LISTING])
    assert await ui.offer_list(reset=True) == LISTING
    ui.market.assert_awaited_once()
    assert [call.args[1].data for call in ui.click.await_args_list] == [b"buy", b"usdt", b"sbp"]


def test_diagnostics_do_not_log_exception_secrets(caplog):
    with caplog.at_level(logging.INFO, logger="assistant"):
        log_error("test", RuntimeError("secret-token-in-url"))
    assert "secret-token-in-url" not in caplog.text
    assert "diagnostic_id" in caplog.text
