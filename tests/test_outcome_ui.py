from types import SimpleNamespace
from unittest.mock import AsyncMock

from cryptowatcher.bot import Controller
from cryptowatcher.events import History
from cryptowatcher.market import RadarResult
from cryptowatcher.outcomes import MINUTE
from cryptowatcher.sources import Sources

from .conftest import update
from .test_outcomes import NOW, future, observe


async def test_event_to_outcome_original_reference_frozen_horizons_and_manual_validation(
    infrastructure, store
):
    controller = Controller(
        infrastructure, store, SimpleNamespace(snapshot=AsyncMock(return_value=RadarResult({}, {}, 0)))
    )
    observe(store, controller.outcomes)
    event = next(e for e in History(store).events(10) if e["direction"] == "UP")
    request = update(callback="event:" + event["event_id"])
    await controller.handle(request, None)
    keyboard = request.effective_message.reply_text.call_args.kwargs["reply_markup"]
    assert any(
        b.callback_data == "outcomes:" + event["event_id"] for row in keyboard.inline_keyboard for b in row
    )
    await controller.handle(update("/horizons 1,2"), None)
    assert store.snapshot().settings.outcome_horizons == (1, 2)
    revision = store.snapshot().revision
    invalid = update("/horizons 0,1")
    await controller.handle(invalid, None)
    assert store.snapshot().revision == revision
    Sources(store).put("spot/BTCUSDT/1m", future())
    controller.outcomes.work(NOW + 60 * MINUTE)
    request = update(callback="outcomes:" + event["event_id"])
    await controller.handle(request, None)
    shown = "\n".join(c.args[0] for c in request.effective_message.reply_text.call_args_list)
    assert "5 min · COMPLETE" in shown and "15 min · INCOMPLETE" in shown
    assert f"{event['reference_price']:g} USDT" in shown
    assert "Late analytics" in shown and "mfe_pct" in shown
    assert "1 min ·" not in shown


async def test_digest_toggle_is_independent_and_status_button_is_readonly(infrastructure, store):
    controller = Controller(
        infrastructure, store, SimpleNamespace(snapshot=AsyncMock(return_value=RadarResult({}, {}, 0)))
    )
    await controller.handle(update("/alerts off"), None)
    await controller.handle(update("/digest on 30"), None)
    assert store.enabled() is False and controller.digest.preference()["enabled"] is True
    assert store.snapshot().settings.digest_interval_min == 30
    before = store.db.total_changes
    await controller.handle(update(callback="digest:status"), None)
    assert store.db.total_changes == before
    await controller.handle(update(callback="digest:off"), None)
    assert controller.digest.preference()["enabled"] is False
    await controller.handle(update("/alerts on"), None)
    assert controller.digest.preference()["enabled"] is False and store.enabled() is True
    for bad in ("/digest on nan", "/digest x", "/horizons nan", "/analytics token"):
        before = store.db.total_changes
        await controller.handle(update(bad), None)
        assert store.db.total_changes == before


async def test_new_generation_history_explains_missing_old_detail_and_limited_sample(infrastructure, store):
    from cryptowatcher.market_storage import enable

    enable(store.db)
    store.bounded = True
    store.put_runtime("history_generation", {"id": "fresh", "started_ms": NOW})
    controller = Controller(
        infrastructure, store, SimpleNamespace(snapshot=AsyncMock(return_value=RadarResult({}, {}, 0)))
    )
    request = update("/history")
    await controller.handle(request, None)
    text = request.effective_message.reply_text.call_args.args[0]
    assert "Старая тестовая история недоступна" in text and "N ограничены" in text
    assert "30 дней" in text and "История пока пуста" in text
