import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

from sniper.accounts import (
    AccountMissing,
    AccountStore,
    add_account,
    import_legacy_session,
    remove_account,
    select_account,
)
from sniper.config import Config
from sniper.control import handle
from sniper.engine import active_buttons, ingest, notice
from sniper.runtime import AccountWorker, Runtime
from sniper.store import initial_state


async def make_accounts(store):
    ids = []

    def setup(state):
        ids.extend(add_account(state, "encrypted-" + str(i), i, f"Account {i}") for i in (1, 2))

    await store.apply(setup)
    return ids, [AccountStore(store, key) for key in ids]


async def test_parallel_workers_preserve_settings_and_active_deals(store, offer):
    ids, (one, two) = await make_accounts(store)
    for scoped in (one, two):
        await scoped.apply(lambda s: s["settings"].update(running=True, notify_min="80", notify_max="90"))
    await one.apply(lambda s: s.update(active={"id": "first-deal"}))
    await asyncio.gather(one.apply(lambda s: ingest(s, offer)), two.apply(lambda s: ingest(s, offer)))
    assert (await one.read())["active"]["id"] == "first-deal"
    assert (await one.read())["stats"]["eligible"] == 0
    assert (await two.read())["active"] is None
    assert (await two.read())["stats"]["eligible"] == 1
    await store.apply(lambda s: select_account(s, ids[1]))
    assert (await store.read())["stats"]["eligible"] == 1
    await one.apply(lambda s: s["settings"].update(notify_max="88"))
    assert (await store.read())["settings"]["notify_max"] == "90"
    await store.apply(lambda s: select_account(s, ids[0]))
    assert (await store.read())["settings"]["notify_max"] == "88"
    assert (await store.read())["active"]["id"] == "first-deal"


async def test_notification_action_selects_its_account_and_cannot_cancel_other(store):
    ids, (one, two) = await make_accounts(store)
    for scoped, name in ((one, "a"), (two, "b")):
        await scoped.apply(
            lambda s, name=name: s.update(
                active={
                    "id": name,
                    "deal_id": "D123",
                    "managed": True,
                    "paper": False,
                    "stage": "REQUESTED",
                    "link": "https://t.me/CryptoBot",
                }
            )
        )
    await two.apply(lambda s: [notice("Trade", active_buttons(s["active"]))])
    msg = (await store.outgoing())[0][2]
    action = next(
        k["callback_data"] for row in msg["buttons"] for k in row if "cancel:" in k.get("callback_data", "")
    )
    assert action == f"acct:{ids[1]}:cancel:b"
    await store.apply(lambda s: handle(s, {"callback_query": {"data": action}}, {"live_enabled": True}))
    state = await store.read()
    assert state["selected_account"] == ids[1]
    assert not (await one.read())["active"].get("cancel_requested")
    assert not (await two.read())["active"].get("cancel_requested")  # confirmation still required


async def test_removed_account_cannot_commit_inflight_mutations(store):
    ids, (one, _) = await make_accounts(store)
    await store.apply(lambda s: remove_account(s, ids[0]))
    with pytest.raises(AccountMissing):
        await one.apply(lambda s: s["settings"].update(running=True))
    assert (await store.read())["selected_account"] == ids[1]


def test_remove_requires_pause_and_no_active_trade():
    state = initial_state()
    key = add_account(state, "encrypted", 42, "First")
    state["settings"]["running"] = True
    with pytest.raises(ValueError, match="account_busy"):
        remove_account(state, key)
    state["settings"]["running"] = False
    state["active"] = {"id": "trade"}
    with pytest.raises(ValueError, match="account_busy"):
        remove_account(state, key)


async def test_legacy_session_import_preserves_state_and_is_not_reimported(store, tmp_path):
    encrypted = store.cipher.fernet.encrypt(b"session").decode()
    file = tmp_path / "old.enc"
    file.write_text(encrypted)
    cfg = SimpleNamespace(
        user_session_encrypted=SimpleNamespace(get_secret_value=lambda: ""),
        user_session_file=file,
        max_accounts=3,
    )
    await store.apply(lambda s: s["settings"].update(auto_max="89"))
    await import_legacy_session(store, cfg)
    state = await store.read()
    assert state["settings"]["auto_max"] == "89"
    key = state["selected_account"]
    await store.apply(lambda s: remove_account(s, key))
    await import_legacy_session(store, cfg)
    assert not (await store.read())["accounts"]


async def test_supervisor_runs_independent_workers_and_stops_removed_account(store):
    ids, _ = await make_accounts(store)
    cfg = Config(
        _env_file=None,
        bot_token="123456:fake",
        admin_user_id=42,
        app_key=Fernet.generate_key().decode(),
        webhook_secret="x" * 32,
    )
    runtime = Runtime(cfg)
    await runtime.bot.session.close()
    await runtime.store.engine.dispose()
    runtime.store = store
    started = []

    async def work(worker):
        started.append(worker.store.account_id)
        await asyncio.Event().wait()

    with patch.object(AccountWorker, "user_client", work):
        task = asyncio.create_task(runtime.supervise_accounts())
        try:
            for _ in range(100):
                if len(started) == 2:
                    break
                await asyncio.sleep(0.01)
            assert set(started) == set(ids)
            assert runtime.workers[ids[0]].monitor_wake is not runtime.workers[ids[1]].monitor_wake
            await store.apply(lambda s: remove_account(s, ids[0]))
            runtime.account_wake.set()
            for _ in range(100):
                if ids[0] not in runtime.workers:
                    break
                await asyncio.sleep(0.01)
            assert set(runtime.workers) == {ids[1]}
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def test_account_limit_duplicate_and_callback_sizes():
    state = initial_state()
    add_account(state, "encrypted", 42, "First")
    with pytest.raises(ValueError, match="duplicate_account"):
        add_account(state, "another", 42, "Duplicate")
    with pytest.raises(ValueError, match="account_limit"):
        add_account(state, "another", 43, "Other", limit=1)
    state["settings"].update(mode="auto", auto_max="90")
    state["account_checked"] = True
    panel = handle(state, {"callback_query": {"data": "run"}}, {"live_enabled": True})[0]
    assert all(len(b.get("callback_data", "").encode()) <= 64 for row in panel["buttons"] for b in row)


def test_stop_all_preserves_active_deals():
    state = initial_state()
    first = add_account(state, "encrypted", 42, "First")
    second = add_account(state, "encrypted2", 43, "Second")
    state["settings"]["running"] = True
    state["active"] = {"id": "trade"}
    state["accounts"][second]["profile"]["settings"]["running"] = True
    handle(state, {"message": {"text": "/stopall"}}, {})
    assert state["active"]["id"] == "trade" and not state["settings"]["running"]
    assert not state["accounts"][second]["profile"]["settings"]["running"]
    assert state["selected_account"] == first
