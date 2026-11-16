import asyncio
import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

import httpx
import pytest
from cryptography.fernet import Fernet

from sniper.accounts import AccountStore, add_account
from sniper.app import create_app
from sniper.config import Config
from sniper.control import handle
from sniper.panel import deliver_panel
from sniper.runtime import AccountWorker, Runtime
from sniper.store import initial_state
from sniper.users import AccessDenied, UserStore, approved, private_sender, route_update
from sniper.web_login import Attempt, LoginError

ADMIN = 42


def message(uid, text="/start", update_id=1):
    return {
        "update_id": update_id,
        "message": {
            "message_id": 1,
            "text": text,
            "from": {"id": uid, "first_name": "Example"},
            "chat": {"id": uid, "type": "private"},
        },
    }


def callback(uid, data, update_id=1):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": "cb",
            "data": data,
            "from": {"id": uid},
            "message": {"message_id": 1, "chat": {"id": uid, "type": "private"}},
        },
    }


def status(state, uid):
    return {
        "is_admin": uid == ADMIN,
        "live_enabled": True,
        "accounts_required": True,
        "connect_url": "https://host.test/connect",
    }


def route(state, update):
    return route_update(state, update, ADMIN, status)


def allow(state, uid):
    route(state, message(uid))
    return route(state, message(ADMIN, f"/allow {uid}"))


def cfg():
    return Config(
        _env_file=None,
        bot_token="123456:fake",
        admin_user_id=ADMIN,
        app_key=Fernet.generate_key().decode(),
        webhook_secret="x" * 32,
        public_base_url="https://host.test",
        bot_transport="webhook",
    )


async def runtime_for(store):
    runtime = Runtime(cfg())
    await runtime.bot.session.close()
    await runtime.store.engine.dispose()
    runtime.store = store
    runtime.login.store = runtime.user_store(ADMIN)
    runtime.bot = SimpleNamespace(
        send_message=AsyncMock(side_effect=lambda *a, **k: SimpleNamespace(message_id=100 + int(a[0]))),
        edit_message_text=AsyncMock(),
        answer_callback_query=AsyncMock(),
        session=SimpleNamespace(close=AsyncMock()),
    )
    return runtime


def test_request_waits_and_repeated_start_does_not_spam_owner():
    state = initial_state()
    first = route(state, message(99))
    assert len(first) == 2
    assert "99" in first[0]["text"] and "_recipient" not in first[0]
    assert first[1]["_recipient"] == 99 and "Дождитесь" in first[1]["text"]
    assert state["users"]["99"]["status"] == "pending"
    assert route(state, message(99)) == []
    state["users"]["99"]["last_reply"] = 0
    again = route(state, message(99))
    assert len(again) == 1 and again[0]["_recipient"] == 99
    assert route(state, callback(99, "run")) == []
    assert not state["settings"]["running"]


def test_only_owner_approves_and_old_approval_cannot_restore_revoked_access():
    state = initial_state()
    request = route(state, message(99))[0]
    action = request["buttons"][0][0]["callback_data"]
    assert route(state, callback(99, action)) == []
    result = route(state, callback(ADMIN, action))
    assert approved(state, 99, ADMIN)
    assert len([m for m in result if m.get("_recipient") == 99]) == 2
    assert all(
        len(b.get("callback_data", "").encode()) <= 64 for m in result for row in m["buttons"] for b in row
    )
    route(state, message(ADMIN, "/revoke 99"))
    route(state, callback(ADMIN, action))
    assert not approved(state, 99, ADMIN)


def test_approved_guest_cannot_grant_access_to_others_or_see_admin_button():
    state = initial_state()
    allow(state, 99)
    route(state, message(100))
    route(state, message(99, "/allow 100"))
    assert state["users"]["100"]["status"] == "pending"
    reply = route(state, message(99))[0]
    assert "Доступ пользователей" not in json.dumps(reply, ensure_ascii=False)
    route(state, message(ADMIN, "/deny 100"))
    assert state["users"]["100"]["status"] == "denied"


@pytest.mark.parametrize(
    "change",
    [
        lambda u: u["message"]["chat"].update(type="group"),
        lambda u: u["message"]["chat"].update(id=100),
        lambda u: u["message"]["from"].update(is_bot=True),
    ],
)
def test_group_forwarded_identity_and_bots_cannot_request_access(change):
    data = message(99)
    change(data)
    assert private_sender(data) is None
    assert route(initial_state(), data) == []


async def test_concurrent_users_keep_settings_sessions_and_panels_separate(store):
    await store.apply(lambda s: allow(s, 99) + allow(s, 100))
    one, two = UserStore(store, 99, ADMIN), UserStore(store, 100, ADMIN)
    await asyncio.gather(
        one.apply(lambda s: s["settings"].update(auto_max="88")),
        two.apply(lambda s: s["settings"].update(auto_max="91")),
    )
    await one.apply(lambda s: add_account(s, "session-one", 101, "First") and [])
    await two.apply(lambda s: add_account(s, "session-two", 102, "Second") and [])
    assert (await one.read())["settings"]["auto_max"] is None  # a newly selected account has its own settings
    await one.apply(lambda s: s["settings"].update(auto_max="88"))
    await two.apply(lambda s: s["settings"].update(auto_max="91"))
    for scoped in (one, two):
        await scoped.apply(lambda s: handle(s, {"message": {"text": "/start"}}, {}))
    outgoing = {key: payload for key, _, payload in await store.outgoing()}
    assert "panel:99" in outgoing and "panel:100" in outgoing
    bot = SimpleNamespace(
        send_message=AsyncMock(
            side_effect=[SimpleNamespace(message_id=900), SimpleNamespace(message_id=1000)]
        ),
        edit_message_text=AsyncMock(),
    )
    await deliver_panel(one, bot, 99, outgoing["panel:99"])
    await deliver_panel(two, bot, 100, outgoing["panel:100"])
    assert (await one.read())["control_panel"]["message_id"] == 900
    assert (await two.read())["control_panel"]["message_id"] == 1000
    assert (await one.read())["settings"]["auto_max"] == "88"
    assert (await two.read())["settings"]["auto_max"] == "91"
    assert "session-two" not in json.dumps(await one.read())
    assert "users" not in await UserStore(store, ADMIN, ADMIN).read()
    assert (await store.read())["settings"]["auto_max"] is None


async def test_cross_user_account_callbacks_cannot_select_or_cancel_foreign_deal(store):
    await store.apply(lambda s: allow(s, 99) + allow(s, 100))
    one, two = UserStore(store, 99, ADMIN), UserStore(store, 100, ADMIN)
    await one.apply(lambda s: add_account(s, "one", 101, "First") and [])
    first = (await one.read())["selected_account"]
    await one.apply(lambda s: s.update(active={"id": "foreign", "stage": "REQUESTED", "managed": True}))
    await store.apply(lambda s: route(s, callback(100, f"acct:{first}:cancel:foreign")))
    assert not (await one.read())["active"].get("cancel_requested")
    assert not (await two.read()).get("selected_account")
    assert not (await two.read())["active"]


async def test_duplicate_telegram_account_across_users_is_rejected_atomically(store):
    await store.apply(lambda s: allow(s, 99) + allow(s, 100))
    one, two = UserStore(store, 99, ADMIN), UserStore(store, 100, ADMIN)
    await one.apply(lambda s: add_account(s, "first", 101, "First") and [])
    with pytest.raises(ValueError, match="account_owned_elsewhere"):
        await two.apply(lambda s: add_account(s, "second", 101, "Second") and [])
    assert not (await two.read()).get("accounts")


async def test_revocation_stops_all_guest_accounts_and_fences_inflight_writes(store):
    await store.apply(lambda s: allow(s, 99))
    guest = UserStore(store, 99, ADMIN)
    await guest.apply(lambda s: add_account(s, "first", 101, "First") and [])
    await guest.apply(lambda s: add_account(s, "second", 102, "Second") and [])
    ids = list((await guest.read())["accounts"])
    for key in ids:
        await AccountStore(guest, key).apply(lambda s: s["settings"].update(running=True))
    await guest.apply(lambda s: s.update(active={"id": "open", "stage": "ATTEMPTING"}))
    assert await store.claim("worker")
    assert await guest.owns("worker")
    await store.apply(lambda s: route(s, message(ADMIN, "/revoke 99")))
    assert not await guest.owns("worker")
    with pytest.raises(AccessDenied):
        await guest.read()
    with pytest.raises(AccessDenied):
        await AccountStore(guest, ids[0]).apply(lambda s: s.update(active=None))
    data = (await store.read())["users"]["99"]["state"]
    assert not data["settings"]["running"]
    assert all(not a["profile"]["settings"]["running"] for a in data["accounts"].values())
    assert data["active"]["id"] == "open" and data["active"]["stage"] == "UNKNOWN"


async def test_runtime_delivers_wait_and_owner_request_then_guest_menu(store):
    runtime = await runtime_for(store)
    await runtime.start()
    try:
        await runtime.accept(message(99))
        for _ in range(100):
            if runtime.bot.send_message.await_count >= 2:
                break
            await asyncio.sleep(0.02)
        assert {call.args[0] for call in runtime.bot.send_message.await_args_list} == {42, 99}
        await runtime.accept(message(ADMIN, "/allow 99", 2))
        for _ in range(100):
            if (await store.read())["users"]["99"]["state"].get("control_panel", {}).get("message_id"):
                break
            await asyncio.sleep(0.02)
        root = await store.read()
        assert approved(root, 99, ADMIN)
        assert root["users"]["99"]["state"]["control_panel"]["message_id"] == 199
        assert root["control_panel"]["message_id"] == 142
    finally:
        await runtime.close()


def signed(uid):
    fields = {"auth_date": str(int(time.time())), "user": json.dumps({"id": uid})}
    key = hmac.new(b"WebAppData", b"123456:fake", hashlib.sha256).digest()
    body = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    fields["hash"] = hmac.new(key, body.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


async def test_mini_app_checks_approval_and_attempt_ownership_on_every_request(store):
    runtime = await runtime_for(store)
    runtime.ready = True
    app = create_app(runtime.config, runtime)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://host.test"
        ) as client:
            headers = {"x-telegram-init-data": signed(99), "origin": "https://host.test"}
            assert (await client.post("/connect/api/start", headers=headers)).status_code == 403
            await store.apply(lambda s: allow(s, 99) + allow(s, 100))
            one, two = await runtime.login_for(99), await runtime.login_for(100)
            assert one is not two and one.store.user_id == 99 and two.store.user_id == 100
            one.attempt = Attempt("first-private-attempt", None, time.time() + 60, stage="done", closed=True)
            two.attempt = Attempt("second-private-attempt", None, time.time() + 60, stage="done", closed=True)
            good = await client.post("/connect/api/status", headers=headers, json={"id": one.attempt.id})
            assert good.status_code == 200
            foreign = await client.post("/connect/api/status", headers=headers, json={"id": two.attempt.id})
            assert foreign.status_code == 404 and "second-private-attempt" not in foreign.text
            await store.apply(lambda s: route(s, message(ADMIN, "/revoke 99")))
            assert (
                await client.post("/connect/api/status", headers=headers, json={"id": one.attempt.id})
            ).status_code == 403
            with pytest.raises(LoginError):
                await runtime.login_for(99)
    finally:
        # Test-only attempts have no Telegram client.
        for manager in runtime.logins.values():
            manager.attempt = None
        await runtime.close()


async def test_supervisor_runs_approved_users_and_removes_revoked_workers(store):
    await store.apply(lambda s: allow(s, 99))
    guest = UserStore(store, 99, ADMIN)
    await guest.apply(lambda s: add_account(s, "session", 101, "First") and [])
    key = (await guest.read())["selected_account"]
    runtime = await runtime_for(store)
    with patch.object(AccountWorker, "user_client", AsyncMock(side_effect=lambda: None)):
        # Keep each worker alive without contacting Telegram.
        async def work(worker):
            await asyncio.Event().wait()

        with patch.object(AccountWorker, "user_client", work):
            task = asyncio.create_task(runtime.supervise_accounts())
            try:
                for _ in range(100):
                    if key in runtime.workers:
                        break
                    await asyncio.sleep(0.01)
                assert runtime.workers[key].user_id == 99
                assert not runtime.control_status(await store.read())["accounts"]
                await store.apply(lambda s: route(s, message(ADMIN, "/revoke 99")))
                runtime.account_wake.set()
                for _ in range(100):
                    if key not in runtime.workers:
                        break
                    await asyncio.sleep(0.01)
                assert not runtime.workers
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await runtime.close()
