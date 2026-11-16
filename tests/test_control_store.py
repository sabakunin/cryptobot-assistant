import asyncio
import time

from sqlalchemy import select

from sniper.control import handle, handle_action
from sniper.engine import ingest, notice
from sniper.security import authorized, safe_link
from sniper.store import Outbox, State, initial_state


def test_owner_private_chat_only():
    good = {"message": {"from": {"id": 42}, "chat": {"id": 42, "type": "private"}}}
    assert authorized(good, 42)
    assert not authorized(good, 43)
    good["message"]["chat"] = {"id": -100, "type": "group"}
    assert not authorized(good, 42)
    assert safe_link("https://t.me/CryptoBot.evil?start=x") == "https://t.me/CryptoBot"
    assert safe_link("https://t.me@evil.com/CryptoBot") == "https://t.me/CryptoBot"


async def test_encrypted_state_and_atomic_inbox(store):
    update = {"update_id": 1, "secret": "sensitive-body"}
    await store.enqueue(update)
    await store.enqueue(update)
    assert len(await store.inbox()) == 1
    assert await store.apply(lambda s: [notice("sensitive-reply")], inbox_id=1)
    assert not await store.apply(lambda s: [notice("duplicate")], inbox_id=1)
    async with store.sessions() as session:
        state = await session.scalar(select(State.payload))
        reply = await session.scalar(select(Outbox.payload))
        assert "settings" not in state and "sensitive-reply" not in reply
    assert len(await store.outgoing()) == 1


async def test_concurrent_duplicate_cards_one_notification(store, offer):
    await store.apply(lambda s: s["settings"].update(running=True, notify_min="80", notify_max="90"))
    await asyncio.gather(*(store.apply(lambda s: ingest(s, offer)) for _ in range(20)))
    assert (await store.read())["stats"]["eligible"] == 1
    assert len(await store.outgoing()) == 1  # card and terms together


async def test_lease_excludes_second_worker(store):
    assert await store.claim("one")
    assert not await store.claim("two")
    assert await store.owns("one")
    await store.release("one")
    assert await store.claim("two")


def test_fsm_persists_and_validates_range():
    state = initial_state()
    handle_action(state, "set:notify_min", {})
    handle(state, {"message": {"text": "89,5"}}, {})
    assert state["settings"]["notify_min"] == "89.5"
    handle_action(state, "set:notify_max", {})
    messages = handle(state, {"message": {"text": "88"}}, {})
    assert "граница" in messages[0]["text"]
    assert state["settings"]["notify_max"] is None


def test_stale_close_does_not_clear_new_trade():
    state = initial_state()
    state["active"] = {"id": "new", "paper": True}
    handle_action(state, "closed:old", {})
    assert state["active"]["id"] == "new"


def test_arm_requires_configuration_and_explicit_confirmation():
    state = initial_state()
    state["settings"].update(mode="auto", auto_max="90")
    state["account_checked"] = True
    handle_action(state, "run", {"live_enabled": False})
    assert not state["settings"]["running"]
    handle_action(state, "run", {"live_enabled": True})
    assert not state["settings"]["running"]
    handle_action(state, "armed:" + state["arm"]["nonce"], {"live_enabled": True})
    assert state["settings"]["running"]
    handle_action(state, "stop", {})
    assert not state["settings"]["running"]


def test_expired_manual_resolution_cannot_clear_unknown():
    state = initial_state()
    state["active"] = {"id": "x", "managed": True}
    state["resolve"] = {"nonce": "old", "expires": time.time() - 1}
    handle_action(state, "resolved:old", {})
    assert state["active"] is not None
