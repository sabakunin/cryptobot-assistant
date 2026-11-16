import asyncio
from types import SimpleNamespace

import pytest

from sniper.runtime import Runtime


def waiting_runtime(store):
    # Exercise the real database claim without creating a Bot/network client.
    return SimpleNamespace(store=store, owner="replacement-worker")


async def test_restart_waits_for_crashed_worker_lease_to_expire(store):
    assert await store.claim("crashed-worker", ttl=0.08)
    runtime = waiting_runtime(store)
    await Runtime.acquire_lease(runtime, timeout=2, retry_interval=0.01)
    assert await store.owns(runtime.owner)
    assert not await store.owns("crashed-worker")


async def test_restart_waits_for_previous_worker_to_release(store):
    assert await store.claim("previous-worker")
    runtime = waiting_runtime(store)

    async def shutdown():
        await asyncio.sleep(0.03)
        await store.release("previous-worker")

    task = asyncio.create_task(shutdown())
    try:
        await Runtime.acquire_lease(runtime, timeout=2, retry_interval=0.01)
        assert await store.owns(runtime.owner)
    finally:
        await task


async def test_restart_does_not_take_over_live_worker_on_timeout(store):
    assert await store.claim("live-worker")
    runtime = waiting_runtime(store)
    with pytest.raises(RuntimeError, match="другим экземпляром"):
        await Runtime.acquire_lease(runtime, timeout=0.04, retry_interval=0.01)
    assert await store.owns("live-worker")
    assert not await store.owns(runtime.owner)


async def test_cancelled_wait_keeps_existing_worker_lease(store):
    assert await store.claim("live-worker")
    runtime = waiting_runtime(store)
    task = asyncio.create_task(Runtime.acquire_lease(runtime, timeout=2, retry_interval=0.01))
    await asyncio.sleep(0.03)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await store.owns("live-worker")
    assert not await store.owns(runtime.owner)
