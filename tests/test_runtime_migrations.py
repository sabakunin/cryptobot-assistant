import asyncio
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet

from sniper.config import Config
from sniper.runtime import Runtime


async def wait_until(predicate):
    for _ in range(100):
        if await predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("Runtime did not settle")


async def test_durable_runtime_restart_disarms_live_and_preserves_unknown(store):
    await store.apply(lambda s: s.update(active={"id": "unknown", "managed": True, "stage": "ATTEMPTING"}))
    await store.apply(lambda s: s["settings"].update(mode="auto", running=True, auto_max="90"))
    cfg = Config(
        _env_file=None,
        bot_token="123456:fake",
        admin_user_id=42,
        app_key=Fernet.generate_key().decode(),
        webhook_secret="x" * 32,
        bot_transport="webhook",
    )
    runtime = Runtime(cfg)
    await runtime.bot.session.close()
    await runtime.store.engine.dispose()
    runtime.store = store
    runtime.bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=123)),
        answer_callback_query=AsyncMock(),
        session=SimpleNamespace(close=AsyncMock()),
    )
    await runtime.start()
    try:
        state = await store.read()
        assert state["active"]["stage"] == "UNKNOWN"
        assert not state["settings"]["running"]
        good = {
            "update_id": 42,
            "message": {
                "message_id": 1,
                "from": {"id": 42},
                "chat": {"id": 42, "type": "private"},
                "text": "/start",
            },
        }
        await runtime.accept(good)
        await runtime.accept(good)

        async def delivered():
            return runtime.bot.send_message.await_count == 1

        await wait_until(delivered)
        bad = {**good, "update_id": 43, "message": {**good["message"], "from": {"id": 99}}}
        await runtime.accept(bad)
        assert not await store.inbox()
    finally:
        await runtime.close()


def test_migration_upgrade_is_repeatable(tmp_path):
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "DATABASE_URL": "sqlite+aiosqlite:///" + (tmp_path / "migrations.db").as_posix()}
    for _ in range(2):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
    import sqlite3

    with sqlite3.connect(tmp_path / "migrations.db") as connection:
        names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"app_state", "telegram_inbox", "notification_outbox", "worker_lease"} <= names
