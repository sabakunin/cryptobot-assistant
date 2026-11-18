"""Synthetic network delays + real encrypted SQLite, never contacts Telegram."""

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(
    0, str(Path(sys.argv[1]).resolve()) if len(sys.argv) > 1 else str(Path(__file__).resolve().parents[1])
)
from cryptography.fernet import Fernet

from sniper.config import Config
from sniper.runtime import Runtime
from sniper.store import Base, Store
from sniper.telegram_ui import Key, Screen, TelegramUI


def message(screen):
    return SimpleNamespace(
        id=screen.id,
        raw_text=screen.text,
        out=False,
        entities=[],
        buttons=[[SimpleNamespace(text=k.text, data=k.data, url=k.url)] for k in screen.keys],
    )


async def main():
    screen = Screen(1, "Здесь вы можете купить USDT за RUB через СБП.", (Key("· 1 ·", b"one"),))
    client = AsyncMock()
    ui = TelegramUI(client, "fake-peer")
    ui.action_interval = 0

    async def reply(request):
        await asyncio.sleep(0.01)
        ui.remember(screen)
        return SimpleNamespace(alert=False)

    client.side_effect = reply
    client.get_messages.return_value = [message(screen)]
    timings = []
    for _ in range(5):
        ui.remember(screen)
        began = time.perf_counter()
        await ui.click(screen, screen.keys[0], fresh=False, allow_same=True)
        timings.append((time.perf_counter() - began) * 1000)
    result = {
        "unchanged_refresh_ms": round(sum(timings) / len(timings), 2),
        "refresh_history_requests": client.get_messages.await_count,
    }
    with tempfile.TemporaryDirectory(prefix="cryptobot-speed-") as tmp:
        key = Fernet.generate_key().decode()
        cfg = Config(
            _env_file=None,
            bot_token="123456:synthetic",
            admin_user_id=42,
            app_key=key,
            webhook_secret="x" * 32,
            monitor_source="off",
        )
        runtime = Runtime(cfg)
        await runtime.bot.session.close()
        await runtime.store.engine.dispose()
        store = Store("sqlite+aiosqlite:///" + (Path(tmp) / "bench.db").as_posix(), key)
        runtime.store = store
        async with store.engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        await store.initialize()
        started = asyncio.Event()

        async def slow_send(*args, **kwargs):
            started.set()
            await asyncio.sleep(0.5)
            return SimpleNamespace(message_id=100)

        runtime.bot = SimpleNamespace(
            send_message=slow_send,
            edit_message_text=AsyncMock(),
            answer_callback_query=AsyncMock(side_effect=lambda *a, **k: None),
        )
        await store.apply(lambda s: [dict(text="bench", buttons=[])])
        tasks = [asyncio.create_task(runtime.delivery())]
        if hasattr(runtime, "control_loop"):
            tasks.append(asyncio.create_task(runtime.control_loop()))
        await asyncio.wait_for(started.wait(), 2)
        data = {
            "update_id": 1,
            "callback_query": {
                "id": "test",
                "from": {"id": 42},
                "message": {"message_id": 100, "chat": {"id": 42, "type": "private"}},
                "data": "set:auto_max",
            },
        }
        began = time.perf_counter()
        await runtime.accept(data)
        while not (await store.read()).get("pending"):
            if time.perf_counter() - began > 3:
                raise RuntimeError("control timed out")
            await asyncio.sleep(0.005)
        result["control_while_send_delayed_500ms_ms"] = round((time.perf_counter() - began) * 1000, 2)
        runtime.stopping.set()
        runtime.wake.set()
        if hasattr(runtime, "input_wake"):
            runtime.input_wake.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 4)
        if getattr(runtime, "callback_tasks", None):
            await asyncio.gather(*runtime.callback_tasks, return_exceptions=True)
        await store.engine.dispose()
    print(json.dumps(result, indent=2))


asyncio.run(main())
