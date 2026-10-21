import asyncio
import json

import pytest

from cryptowatcher.ingestion import BybitRadar


async def test_buffered_receive_preserves_all_frames_and_yields_to_other_work(store):
    radar = BybitRadar(store)
    radar.connections["spot:0"] = {"state": "CONNECTED"}
    allowed = {"spot/BTCUSDT/1m"}
    messages = [
        {"topic": "kline.1.BTCUSDT", "data": [{"confirm": bool(i % 2), "start": i}]} for i in range(512)
    ]
    seen, progress = [], []

    class Buffered:
        async def recv(self):
            if len(seen) == len(messages):
                raise asyncio.CancelledError
            return json.dumps(messages[len(seen)])

    def accept(market, value, streams, *, received_ms):
        assert market == "spot" and streams == allowed and type(received_ms) is int
        seen.append(value)

    radar.accept_message = accept

    async def other_work():
        while len(seen) < len(messages):
            progress.append(len(seen))
            await asyncio.sleep(0)

    sibling = asyncio.create_task(other_work())
    try:
        with pytest.raises(asyncio.CancelledError):
            await radar._receive_messages(Buffered(), "spot:0", "spot", allowed)
        await sibling
        assert seen == messages
        assert any(0 < value < len(messages) for value in progress)
        assert type(radar.connections["spot:0"]["last_received_ms"]) is int
    finally:
        sibling.cancel()
        await asyncio.gather(sibling, return_exceptions=True)
        await radar.close()


@pytest.mark.parametrize("expired", [False, True])
async def test_receive_timeout_and_external_cancel_join_underlying_receive(store, monkeypatch, expired):
    radar = BybitRadar(store)
    radar.connections["spot:0"] = {"state": "CONNECTED"}
    entered, released = asyncio.Event(), asyncio.Event()

    class Blocked:
        async def recv(self):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                released.set()

    if expired:
        timeout = asyncio.timeout

        def fast_timeout(seconds):
            assert seconds == 65
            return timeout(0.001)

        monkeypatch.setattr(asyncio, "timeout", fast_timeout)
    task = asyncio.create_task(radar._receive_messages(Blocked(), "spot:0", "spot", set()))
    try:
        await entered.wait()
        if not expired:
            task.cancel()
        with pytest.raises(TimeoutError if expired else asyncio.CancelledError):
            await task
        assert released.is_set() and task.done()
        assert "last_received_ms" not in radar.connections["spot:0"]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await radar.close()


async def test_receive_does_not_hide_malformed_input_or_publish_a_new_receipt(store):
    radar = BybitRadar(store)
    radar.connections["spot:0"] = {"state": "CONNECTED", "last_received_ms": 123}

    class Malformed:
        async def recv(self):
            return "{invalid-json"

    try:
        with pytest.raises(json.JSONDecodeError):
            await radar._receive_messages(Malformed(), "spot:0", "spot", set())
        assert radar.connections["spot:0"]["last_received_ms"] == 123
    finally:
        await radar.close()


async def test_cancel_at_buffered_receive_completion_is_not_lost(store):
    radar = BybitRadar(store)
    radar.connections["spot:0"] = {"state": "CONNECTED"}
    calls = 0

    class CompletesDuringCancel:
        async def recv(self):
            nonlocal calls
            calls += 1
            if calls == 1:
                # In3.11 wait_for the child finishes before this cancellation
                # reaches its parent; that path can consume the cancellation.
                asyncio.get_running_loop().call_soon(task.cancel)
                return json.dumps({"op": "pong"})
            await asyncio.Event().wait()

    task = asyncio.create_task(radar._receive_messages(CompletesDuringCancel(), "spot:0", "spot", set()))
    try:
        done, pending = await asyncio.wait({task}, timeout=0.5)
        assert done == {task} and not pending
        assert task.cancelled()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await radar.close()
