import asyncio
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import sniper.telegram_ui as driver
from sniper.runtime import Runtime
from sniper.store import initial_state
from sniper.telegram_ui import InterfaceChanged, Key, Screen, TelegramUI

LISTING = "Здесь вы можете купить USDT за RUB через СБП."


def message(screen):
    return SimpleNamespace(
        id=screen.id,
        raw_text=screen.text,
        out=False,
        entities=[],
        buttons=[[SimpleNamespace(text=k.text, data=k.data, url=k.url)] for k in screen.keys],
    )


async def test_fresh_navigation_uses_received_edit_and_submit_rereads_history():
    listing = Screen(1, LISTING, (Key("89 ₽", b"offer"),))
    card = Screen(2, "card", (Key("Создать сделку", b"create"),))
    result = Screen(3, "result")
    client = AsyncMock()
    ui = TelegramUI(client, "peer")
    ui.action_interval = 0
    ui.remember(listing)
    targets = iter([card, result])

    async def answer(request):
        ui.remember(next(targets))
        return SimpleNamespace(alert=False)

    client.side_effect = answer
    client.get_messages.return_value = [message(card)]
    assert await ui.click(listing, listing.keys[0], fresh=False) == card
    client.get_messages.assert_not_awaited()
    assert await ui.click(card, card.keys[0]) == result
    client.get_messages.assert_awaited_once()
    assert client.await_count == 2


async def test_navigation_still_rejects_changed_cached_screen_without_rpc():
    client = AsyncMock()
    ui = TelegramUI(client, "peer")
    old = Screen(1, "old", (Key("Buy", b"buy"),))
    ui.remember(Screen(1, "new", (Key("Buy", b"changed"),)))
    with pytest.raises(InterfaceChanged):
        await ui.click(old, old.keys[0], fresh=False)
    client.assert_not_awaited()
    client.get_messages.assert_not_awaited()


async def test_history_response_does_not_overwrite_newer_push_before_submit():
    client = AsyncMock()
    ui = TelegramUI(client, "peer")
    old = Screen(1, "price 89", (Key("Создать сделку", b"create"),))
    changed = Screen(1, "price 95", (Key("Создать сделку", b"new-price"),))
    ui.remember(old)

    async def history(*args, **kwargs):
        ui.remember(changed)
        return [message(old)]

    client.get_messages.side_effect = history
    with pytest.raises(InterfaceChanged):
        await ui.click(old, old.keys[0])
    client.assert_not_awaited()
    assert ui.latest == changed


@pytest.mark.parametrize(
    "pages,allowed", [((), True), (("· 1 ·", "2"), True), (("1", "· 2 ·"), False), (("1", "2"), False)]
)
def test_only_identified_first_page_is_eligible_for_immediate_scan(pages, allowed):
    ui = TelegramUI(AsyncMock(), "peer")
    screen = Screen(1, LISTING, tuple(Key(label, label.encode()) for label in pages))
    ui.remember(screen)
    assert bool(ui.ready_listing()) == allowed
    assert ui.ready_listing(screen.fingerprint) is None


async def test_listing_push_wakes_scan_before_refresh_deadline_without_database_read():
    state = initial_state()
    state["settings"].update(running=True)
    state["account_checked"] = True
    first_scanned = asyncio.Event()
    latest = Screen(1, LISTING, (Key("· 1 ·", b"one"), Key("89 ₽", b"offer")))
    ui = TelegramUI(AsyncMock(), "peer")
    runtime = SimpleNamespace(
        config=SimpleNamespace(monitor_source="userbot", refresh_seconds=10),
        store=SimpleNamespace(read=AsyncMock(return_value=state)),
        ui=ui,
        monitor_wake=asyncio.Event(),
        stopping=asyncio.Event(),
        wake=asyncio.Event(),
        scan_time=SimpleNamespace(time=nullcontext),
        errors=SimpleNamespace(inc=Mock()),
    )
    seen = []

    async def scan(listing):
        seen.append(listing)
        if len(seen) == 1:
            first_scanned.set()
        else:
            runtime.stopping.set()
            runtime.monitor_wake.set()

    runtime.scan = scan
    task = asyncio.create_task(Runtime.monitor_loop(runtime))
    try:
        await asyncio.wait_for(first_scanned.wait(), 1)
        before = runtime.store.read.await_count
        ui.remember(latest)
        await Runtime.receive_screen(runtime, latest)
        assert runtime.store.read.await_count == before
        await asyncio.wait_for(task, 1)
        assert seen == [None, latest]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_four_click_chain_removes_legacy_waits_and_three_history_reads(monkeypatch):
    clock = SimpleNamespace(now=10.0)
    monkeypatch.setattr(driver, "time", SimpleNamespace(monotonic=lambda: clock.now))

    async def sleep(delay):
        clock.now += delay

    monkeypatch.setattr(
        driver,
        "asyncio",
        SimpleNamespace(
            Event=asyncio.Event,
            Lock=asyncio.Lock,
            wait_for=asyncio.wait_for,
            TimeoutError=asyncio.TimeoutError,
            sleep=sleep,
        ),
    )

    async def chain(interval, cached_navigation):
        clock.now = 10.0
        screens = [Screen(i, str(i), (Key("step", str(i).encode()),)) for i in range(5)]
        client = AsyncMock()
        ui = TelegramUI(client, "peer")
        ui.action_interval, ui.last_action = interval, 9.9
        ui.remember(screens[0])
        index = 0

        async def history(*args, **kwargs):
            clock.now += 0.05
            return [message(ui.latest)]

        async def answer(request):
            nonlocal index
            clock.now += 0.05
            index += 1
            ui.remember(screens[index])
            return SimpleNamespace(alert=False)

        client.get_messages.side_effect = history
        client.side_effect = answer
        for i in range(4):
            await ui.click(screens[i], screens[i].keys[0], fresh=(not cached_navigation or i == 3))
        return clock.now - 10, client.get_messages.await_count

    legacy, legacy_reads = await chain(2.0, False)
    fast, fast_reads = await chain(0.2, True)
    assert legacy == pytest.approx(8.0) and legacy_reads == 4
    assert fast == pytest.approx(0.8) and fast_reads == 1
