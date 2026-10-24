"""Plain alert emphasis and safe reporting of ambiguous reply failures."""

import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram import MessageEntity
from telegram.error import NetworkError, TimedOut

from cryptowatcher.bot import Controller
from cryptowatcher.config import OwnerSettings
from cryptowatcher.events import EventEngine, History
from cryptowatcher.market_storage import enable
from cryptowatcher.render import notification_entities, notification_text
from cryptowatcher.storage import Store
from cryptowatcher.telegram_delivery import TelegramDelivery

from .conftest import update
from .test_events import NOW, candidate, selected, window


def population(path, bounded, count=8):
    store = Store(path, 233382575)
    store.initialize(OwnerSettings())
    if bounded:
        enable(store.db)
        store.bounded = True
        store.clock_ms = lambda: NOW
    engine = EventEngine(store, selected("composite"))
    for i in range(count):
        engine.observe(f"spot/COIN{i}USDT/1m", window(), 1e6, 0.01, NOW, live=True)
    assert len(engine.history.items("active:")) == count
    return store, engine


async def test_ambiguous_menu_timeout_does_not_send_second_reply_or_leak_error(infrastructure, store, caplog):
    controller = Controller(infrastructure, store, SimpleNamespace(close=AsyncMock()))
    message = update("/start")
    message.effective_message.reply_text.side_effect = TimedOut("never-log-private-details")
    before = controller.store.snapshot()
    await controller.handle(message, None)
    message.effective_message.reply_text.assert_awaited_once()
    assert controller.store.snapshot() == before
    assert "TimedOut" in caplog.text and "command=start" in caplog.text
    assert "never-log-private-details" not in caplog.text


async def test_safe_command_age_distinguishes_queue_from_handler_without_logging_input(
    infrastructure, store, caplog, monkeypatch
):
    controller = Controller(infrastructure, store, SimpleNamespace(close=AsyncMock()))
    message = update("/unknown private-input-must-stay-private")
    message.effective_message.date = datetime.fromtimestamp(1000, timezone.utc)
    monkeypatch.setattr("cryptowatcher.bot.time.time", lambda: 1125)
    caplog.set_level(logging.INFO, logger="cryptowatcher.bot")
    await controller.handle(message, None)
    assert "command=input queued_seconds=125.0" in caplog.text
    assert "command=input outcome=OK elapsed_seconds=" in caplog.text
    assert "private-input-must-stay-private" not in caplog.text


def entity_values(text, entities):
    raw = text.encode("utf-16-le")
    previous = 0
    result = []
    for entity in entities:
        assert entity.type == MessageEntity.BOLD
        assert 0 <= previous <= entity.offset < entity.offset + entity.length <= len(raw) // 2
        result.append(raw[entity.offset * 2 : (entity.offset + entity.length) * 2].decode("utf-16-le"))
        previous = entity.offset + entity.length
    return result


def test_signal_entities_emphasize_exact_values_with_utf16_and_literal_markup():
    text = (
        "🔔 Первое уведомление 🪙 <b>буквальный текст</b> [_]\n\n"
        "• Изменение цены: -2.35 %\n"
        "• К обычному обороту: 8.64 ×\n"
        "• Расстояние пробоя: 0.00001234 USDT\n"
        "• Расстояние пробоя: 1.25 тыс. USDT\n"
        "• К обычному диапазону: 3.5 ×\n"
        "• Оборот 24ч: 1.68 млн USDT\n"
        "Интенсивность: ×1.16 от порога\n"
        "Время наблюдения: 24.10.2026 12:34:56 МСК"
    )
    assert entity_values(text, notification_entities(text)) == [
        "-2.35 %",
        "8.64 ×",
        "0.00001234 USDT",
        "1.25 тыс. USDT",
        "3.5 ×",
        "×1.16",
    ]
    assert notification_entities("• Изменение цены: неизвестно\nИнтенсивность: nan от порога") == []
    assert "<b>буквальный текст</b> [_]" in text


async def test_new_alert_entities_are_frozen_across_unknown_ack_and_reopen(tmp_path):
    store, engine = population(tmp_path / "emphasis.sqlite3", True, 1)
    reopened = None
    try:
        first = candidate(engine.history)
        transport = TelegramDelivery(
            store, store.owner_id, notification_text, lambda e: None, emphasis=notification_entities
        )
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=NetworkError("unknown ACK")))
        await transport.send_due(bot, NOW + 2000)
        saved = engine.history.get("outbox:telegram:" + first["delivery_id"])
        assert saved["state"] == "RETRY" and saved["text_entities"]
        assert entity_values(saved["text"], bot.send_message.call_args.kwargs["entities"])
        assert "parse_mode" not in bot.send_message.call_args.kwargs
        original_text, original_entities = saved["text"], saved["text_entities"]
        store.close()
        reopened = Store(store.path, store.owner_id)

        def never_rebuild(*args):
            raise AssertionError("A frozen retry must not rebuild text or entities")

        retry = TelegramDelivery(
            reopened, reopened.owner_id, never_rebuild, lambda e: None, emphasis=never_rebuild
        )
        bot.send_message.side_effect = None
        bot.send_message.return_value = SimpleNamespace(message_id=7)
        await retry.send_due(bot, saved["next_attempt_ms"])
        assert bot.send_message.call_args.args[1] == original_text
        assert [e.to_dict() for e in bot.send_message.call_args.kwargs["entities"]] == original_entities
        completed = History(reopened).get("outbox:telegram:" + first["delivery_id"])
        assert completed["state"] == "DELIVERED"
        assert "text" not in completed and "text_entities" not in completed
    finally:
        if reopened is not None:
            reopened.close()
        else:
            store.close()


async def test_legacy_frozen_plain_alert_is_not_reformatted(tmp_path):
    store, engine = population(tmp_path / "legacy-text.sqlite3", True, 1)
    try:
        first = candidate(engine.history)
        first["text"] = "Сохранённый текст до изменения: <1.2> [_]"
        with store.db:
            engine.history.put("outbox:telegram:" + first["delivery_id"], first)

        def never_rebuild(*args):
            raise AssertionError("Legacy pending text stays plain and frozen")

        transport = TelegramDelivery(
            store, store.owner_id, never_rebuild, lambda e: None, emphasis=never_rebuild
        )
        bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=9)))
        await transport.send_due(bot, NOW + 2000)
        assert bot.send_message.call_args.args[1] == first["text"]
        assert "entities" not in bot.send_message.call_args.kwargs
        assert "parse_mode" not in bot.send_message.call_args.kwargs
    finally:
        store.close()
