import asyncio
from types import SimpleNamespace

import pytest
from test_users import ADMIN, allow, cfg, message, route, runtime_for
from test_web_login import fake_client, settled

from sniper.local_tunnel import TUNNEL_URL, publish_url
from sniper.users import UserStore


async def test_parallel_qr_logins_save_to_the_matching_user_only(store):
    await store.apply(lambda s: allow(s, 99) + allow(s, 100))
    runtime = await runtime_for(store)
    managers = [await runtime.login_for(uid) for uid in (99, 100)]
    try:
        for index, manager in enumerate(managers):
            client = fake_client(password=True)
            client.get_me.return_value = SimpleNamespace(
                id=700 + index, first_name=f"User {index}", username=None, bot=False
            )
            manager.client_factory = lambda client=client: client
            manager.config = manager.config.model_copy(
                update={"telegram_api_id": 123, "telegram_api_hash": cfg().webhook_secret}
            )
        await asyncio.gather(*(manager.start() for manager in managers))
        await asyncio.gather(*(settled(manager, "password") for manager in managers))
        await asyncio.gather(
            *(manager.password(manager.attempt.id, "test-only-password") for manager in managers)
        )
        for uid, expected in ((99, 700), (100, 701)):
            state = await UserStore(store, uid, ADMIN).read()
            assert {account["user_id"] for account in state["accounts"].values()} == {expected}
            assert not state["settings"]["running"]
        assert not (await store.read()).get("accounts")
    finally:
        await runtime.close()


async def test_revocation_during_qr_password_prevents_saving_session(store):
    await store.apply(lambda s: allow(s, 99))
    runtime = await runtime_for(store)
    manager = await runtime.login_for(99)
    client = fake_client(password=True)
    manager.client_factory = lambda: client
    manager.config = manager.config.model_copy(
        update={"telegram_api_id": 123, "telegram_api_hash": cfg().webhook_secret}
    )
    try:
        await manager.start()
        await settled(manager, "password")
        await store.apply(lambda s: route(s, message(ADMIN, "/revoke 99")))
        result = await manager.password(manager.attempt.id, "test-only-password")
        assert result["stage"] == "error"
        assert not (await store.read())["users"]["99"]["state"].get("accounts")
        client.log_out.assert_awaited_once()
    finally:
        await runtime.close()


def test_rotating_local_https_updates_button_url_without_restarting_bot(tmp_path):
    path = tmp_path / "public-url.txt"
    config = cfg().model_copy(update={"public_base_url_file": path})
    assert config.connect_url == ""
    for host in ("https://first.app.lhr.life", "https://second.app.lhr.life"):
        assert TUNNEL_URL.search(host + " tunneled").group() == host
        publish_url(path, host)
        assert config.connect_url == host + "/connect"
    publish_url(path, "")
    assert not config.connect_url
    assert not TUNNEL_URL.search("https://admin.localhost.run/")
    with pytest.raises(ValueError):
        publish_url(path, "https://unrelated.test")
