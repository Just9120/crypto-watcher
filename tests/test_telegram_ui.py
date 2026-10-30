import json
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cryptowatcher.bot import MENU_ACTIONS, PUBLISHED_COMMANDS, Controller
from cryptowatcher.config import OwnerSettings
from cryptowatcher.market import RadarResult
from cryptowatcher.render import menu, settings_keyboard
from cryptowatcher.storage import Store
from cryptowatcher.telegram_ui import CUSTOM, INPUT
from cryptowatcher.telegram_views import (
    agent_state,
    amount,
    diagnostic_section,
    diagnostic_summary,
    source_summary,
    split_message,
)

from .conftest import update


@pytest.fixture
def deck(infrastructure, store):
    return Controller(
        infrastructure, store, SimpleNamespace(snapshot=AsyncMock(return_value=RadarResult({}, {}, 0)))
    )


@pytest.mark.parametrize(
    "label",
    [
        "🔔 Alerts",
        "Outcomes",
        "Статистика",
        "Сравнение",
        "Digest",
        "Trading agent",
        "/start",
        "⚙️ Настройки",
        "Profiles",
        "Detectors",
        "История",
        "Диагностика",
        "Derivatives",
        "📊 Статус",
        "📡 Радар",
        "🗂 История",
        "📈 Аналитика",
        "❔ Помощь",
    ],
)
async def test_menu_views_preserve_off_preferences_and_owner_values(deck, label):
    deck.store.set_enabled(False)
    before = (deck.store.snapshot(), deck.store.enabled(), deck.digest.preference())
    request = update(label)
    await deck.handle(request, None)
    messages = [call.args[0] for call in request.effective_message.reply_text.call_args_list]
    assert messages and not any("Действие не выполнено" in text for text in messages)
    assert (deck.store.snapshot(), deck.store.enabled(), deck.digest.preference()) == before


async def test_explicit_alert_action_is_independent_and_restart_preserves_it(deck):
    deck.store.set_enabled(False)
    before = deck.store.snapshot()
    await deck.handle(update(callback="ui:alerts:on"), None)
    assert deck.store.enabled() and deck.store.snapshot() == before
    assert deck.digest.preference()["enabled"] is False
    await deck.handle(update(callback="ui:alerts:off"), None)
    assert not deck.store.enabled()
    reopened = Store(deck.store.path, deck.store.owner_id, readonly=True)
    try:
        assert not reopened.enabled() and reopened.snapshot() == before
    finally:
        reopened.close()


async def test_legacy_manual_button_opens_parameter_groups_and_cancel_is_available(deck):
    request = update(callback="st:manual")
    await deck.handle(request, None)
    markup = request.effective_message.reply_text.call_args.kwargs["reply_markup"]
    assert any(b.callback_data == "ui:parameter_group:price" for row in markup.inline_keyboard for b in row)
    await deck.handle(update(callback="ui:field:price_1m"), None)
    assert deck.store.get_runtime(INPUT)
    await deck.handle(update(callback="ui:cancel"), None)
    assert not deck.store.get_runtime(INPUT)


async def test_pending_input_cancel_navigation_and_revision_conflict(deck):
    await deck.handle(update(callback="ui:field:price_1m"), None)
    before = deck.store.snapshot()
    await deck.handle(update("nan"), None)
    assert deck.store.snapshot() == before and deck.store.get_runtime(INPUT)
    await deck.handle(update("📡 Радар"), None)
    assert not deck.store.get_runtime(INPUT)
    await deck.handle(update("BTC"), None)
    assert deck.store.snapshot() == before
    for callback in ("ui:parameters", "ui:parameter_group:price", "ui:profile:normal", "st:terms"):
        await deck.handle(update(callback="ui:field:price_1m"), None)
        await deck.handle(update(callback=callback), None)
        assert not deck.store.get_runtime(INPUT)
        await deck.handle(update("7"), None)
        assert deck.store.snapshot() == before
    await deck.handle(update(callback="ui:field:price_1m"), None)
    deck.change(liquidity_floor_24h=2e6)
    current = deck.store.snapshot()
    request = update("7")
    await deck.handle(request, None)
    assert deck.store.snapshot() == current and not deck.store.get_runtime(INPUT)
    assert "изменились" in request.effective_message.reply_text.call_args.args[0]


async def test_per_tf_edit_and_explicit_all_tf_scope(deck):
    before = deck.store.snapshot()
    deck.store.update(before.revision, before.settings.with_profile("normal"))
    await deck.handle(update(callback="ui:field:price_1m"), None)
    await deck.handle(update("1.75"), None)
    p = dict(deck.store.snapshot().settings.detector_parameters)
    assert [p["price_" + tf] for tf in ("1m", "3m", "5m", "15m")] == [1.75, 2, 2.5, 4]
    await deck.handle(update(callback="ui:field:price_move_min"), None)
    await deck.handle(update("3"), None)
    p = dict(deck.store.snapshot().settings.detector_parameters)
    assert {p["price_" + tf] for tf in ("1m", "3m", "5m", "15m")} == {3}


async def test_profile_preview_restore_and_stale_apply_do_not_lose_custom(deck):
    deck.change(price_move_min=7)
    original = deck.store.snapshot()
    await deck.handle(update(callback="ui:profile:normal"), None)
    assert deck.store.snapshot() == original
    await deck.handle(update(callback=f"ui:apply:normal:{original.revision}"), None)
    assert deck.store.snapshot().settings.profile == "normal"
    assert deck.store.get_runtime(CUSTOM)["price_1m"] == 7
    reopened = Store(deck.store.path, deck.store.owner_id, readonly=True)
    try:
        assert reopened.get_runtime(CUSTOM) == deck.store.get_runtime(CUSTOM)
    finally:
        reopened.close()
    current = deck.store.snapshot()
    await deck.handle(update(callback=f"ui:apply:sensitive:{original.revision}"), None)
    assert deck.store.snapshot() == current
    await deck.handle(update(callback=f"ui:restore:{current.revision}"), None)
    assert deck.store.snapshot().settings.profile == "custom"
    assert dict(deck.store.snapshot().settings.detector_parameters)["price_1m"] == 7


async def test_typed_list_dialog_does_not_change_market_or_resurrect_after_cancel(deck):
    before = deck.store.snapshot()
    await deck.handle(update(callback="ui:input:add:linear"), None)
    await deck.handle(update("BTC,spot/ETH"), None)
    assert deck.store.snapshot().settings.instruments() == ("linear/BTCUSDT", "spot/ETHUSDT")
    assert deck.store.snapshot().settings.bybit_category == before.settings.bybit_category
    await deck.handle(update(callback="ui:input:remove:linear"), None)
    await deck.handle(update("отмена"), None)
    await deck.handle(update("BTC"), None)
    assert len(deck.store.snapshot().settings.instruments()) == 2


async def test_raw_diagnostic_file_and_summary_are_separate_readonly_paths(deck):
    request = update("Диагностика")
    await deck.handle(request, None)
    shown = request.effective_message.reply_text.call_args.args[0]
    assert "сводка" in shown and "не равно числу просроченных" in shown
    assert len(request.effective_message.reply_text.call_args_list) == 1
    request = update(callback="ui:diag:file")
    request.effective_message.reply_document = AsyncMock()
    await deck.handle(request, None)
    args = request.effective_message.reply_document.call_args.kwargs
    assert args["filename"].endswith(".json")
    value = json.loads(args["document"].getvalue())
    assert value["configuration_revision"] == deck.store.snapshot().revision and "storage" in value


@pytest.mark.parametrize(
    "callback", ["ui:field:price_1m", "ui:apply:normal:1", "ui:clear:1", "ui:diag:file", "ui:alerts:on"]
)
async def test_new_callbacks_reject_foreign_and_group_before_any_state_read(deck, callback):
    before = deck.store.db.total_changes
    for request in (update(owner=False, callback=callback), update(group=True, callback=callback)):
        await deck.handle(request, None)
        request.effective_message.reply_text.assert_not_awaited()
    assert deck.store.db.total_changes == before


def test_compact_navigation_and_utf16_split_preserve_identifiers():
    root = menu()
    assert len(root.keyboard) == 3 and sum(map(len, root.keyboard)) == 6
    assert all(b.text in MENU_ACTIONS for row in root.keyboard for b in row)
    assert len(PUBLISHED_COMMANDS) < 12
    assert len(settings_keyboard(SimpleNamespace(settings=OwnerSettings())).inline_keyboard) <= 6
    text = "слово " * 600 + "linear/BTCUSDT/1m " + "😀" * 4000
    chunks = split_message(text)
    assert all(len(chunk.encode("utf-16-le")) // 2 <= 3500 for chunk in chunks)
    assert any("linear/BTCUSDT/1m" in chunk for chunk in chunks)
    assert "".join(chunks).replace(" ", "") == text.replace(" ", "")
    assert amount(1e6, compact=True) == "1 млн" and amount(0.0000000001) != "0"


async def test_history_keyset_pages_keep_distinct_events_and_show_observation_time(deck):
    from cryptowatcher.events import EventEngine

    from .test_events import NOW, observe

    engine = EventEngine(deck.store)
    for step in range(3):
        observe(engine, NOW + step * 900_000, tf=("1m", "3m", "5m")[step])
    await history_pages(deck)


async def test_cold_history_keeps_the_same_keyset_navigation_open_result_and_return(infrastructure, store):
    from cryptowatcher.events import EventEngine
    from tests.catalogue_fixture import move_events
    from tests.test_bounded_storage import bounded
    from tests.test_events import NOW, observe

    bounded(store)
    controller = Controller(
        infrastructure, store, SimpleNamespace(snapshot=AsyncMock(return_value=RadarResult({}, {}, 0)))
    )
    engine = EventEngine(store)
    for step in range(3):
        observe(engine, NOW + step * 900_000, tf=("1m", "3m", "5m")[step])
    expected = engine.history.events(100)
    move_events(store)
    assert engine.history.events(100) == expected
    await history_pages(controller)


async def history_pages(deck):
    request = update("История")
    await deck.handle(request, None)
    reply = request.effective_message.reply_text.call_args
    assert "МСК" in reply.args[0] and "не означает отправленное" in reply.args[0]
    markup = reply.kwargs["reply_markup"]
    first = {
        b.callback_data.split(":")[2]
        for row in markup.inline_keyboard
        for b in row
        if b.callback_data.startswith("ui:open:")
    }
    cursor = next(
        b.callback_data
        for row in markup.inline_keyboard
        for b in row
        if b.callback_data.startswith("ui:history:")
    )
    assert len(cursor.encode()) <= 64
    request = update(callback=cursor)
    await deck.handle(request, None)
    markup = request.callback_query.edit_message_text.call_args.kwargs["reply_markup"]
    second = {
        b.callback_data.split(":")[2]
        for row in markup.inline_keyboard
        for b in row
        if b.callback_data.startswith("ui:open:")
    }
    assert first and second and first.isdisjoint(second)
    opened = next(
        b.callback_data
        for row in markup.inline_keyboard
        for b in row
        if b.callback_data.startswith("ui:open:")
    )
    for action in ("open", "result"):
        request = update(callback=opened.replace("ui:open:", "ui:" + action + ":"))
        await deck.handle(request, None)
        markup = request.callback_query.edit_message_text.call_args.kwargs["reply_markup"]
        returned = next(
            b.callback_data
            for row in markup.inline_keyboard
            for b in row
            if (b.callback_data or "").startswith("ui:returnhistory:")
        )
        request = update(callback=returned)
        await deck.handle(request, None)
        markup = request.callback_query.edit_message_text.call_args.kwargs["reply_markup"]
        actual = {
            b.callback_data.split(":")[2]
            for row in markup.inline_keyboard
            for b in row
            if b.callback_data.startswith("ui:open:")
        }
        assert actual == second


async def test_event_details_distinguish_muted_observation_and_preserve_original_json(deck):
    from cryptowatcher.events import EventEngine

    from .test_events import NOW, observe

    deck.store.mute("spot/BTCUSDT", (NOW + 3_600_000) / 1000)
    engine = EventEngine(deck.store)
    observe(engine)
    event = next(e for e in engine.history.events(20) if e["detector_id"] == "composite")
    original = json.dumps(event, sort_keys=True)
    request = update(callback="event:" + event["event_id"])
    await deck.handle(request, None)
    text = "\n".join(call.args[0] for call in request.effective_message.reply_text.call_args_list)
    assert "монета была заглушена" in text and "Event ID" in text and "Цена отсчёта" in text
    request = update(callback="ui:eventfile:" + event["event_id"])
    request.effective_message.reply_document = AsyncMock()
    await deck.handle(request, None)
    value = json.loads(request.effective_message.reply_document.call_args.kwargs["document"].getvalue())
    assert json.dumps(value, sort_keys=True) == original
    assert json.dumps(engine.history.get("event-head:" + event["event_id"]), sort_keys=True) == original


def test_diagnostics_distinguish_actual_forecast_optional_and_unknown(deck):
    data = {
        "at_ms": 1_800_000_000_000,
        "storage": {
            "working_bytes": 2_900_000_000,
            "budget_bytes": 20_000_000_000,
            "projected_working_bytes": 163_277_943_560,
            "files": {"database": 2_500_000_000, "wal": 400_000_000, "shm": 0},
            "indexes": {"state": "STALE"},
        },
        "agent_delivery": {"configuration": "UNSET", "enabled": False},
        "enrichment": {"enabled": False},
        "outcomes": {"pending": {"state": "PENDING"}, "comparison_pending": 12},
        "analytics": {"state": "OK", "at_ms": 1_800_000_000_000},
    }
    text = diagnostic_summary(data)
    assert "2.9 ГБ / 20 ГБ" in text and "163.28 ГБ" in text and "не подключена" in text
    missing_budget = {**data, "storage": {k: v for k, v in data["storage"].items() if k != "budget_bytes"}}
    assert "2.9 ГБ / 30 ГБ" in diagnostic_summary(missing_budget)
    current_limits = {
        **data,
        "storage": {**data["storage"], "target_bytes": 25_000_000_000, "budget_bytes": 30_000_000_000},
    }
    storage_text = diagnostic_section(current_limits, "storage")
    assert "Желаемый объём: 25 ГБ" in storage_text and "Допустимый предел: 30 ГБ" in storage_text
    assert "устарел" in text and "не текущий размер" in text
    queues = diagnostic_section(data, "queues")
    assert (
        "Результаты событий ожидают: неизвестно" in queues
        and "Просрочено" in queues
        and "не измерены" in queues
    )
    assert agent_state({"state": "PENDING"}) == "состояние неизвестно"
    assert "Контекст деривативов (необязательно): неизвестно" in diagnostic_summary({})
    assert "Дайджест: неизвестно" in diagnostic_summary({})
    snapshot = deck.store.snapshot()
    empty = snapshot.settings.changed(alert_universe_mode="custom")
    text = source_summary(
        SimpleNamespace(revision=4, settings=empty), {"active_streams": 1104, "at": 1000}, 1002
    )
    assert "Список пуст" in text and "1 104" in text and "не подтверждено" in text


async def test_inline_panel_edits_are_quiet_and_missing_old_message_falls_back(deck):
    from telegram.error import BadRequest

    request = update(callback="ui:settings")
    request.callback_query.edit_message_text.side_effect = BadRequest("Message is not modified")
    before = deck.store.snapshot()
    await deck.handle(request, None)
    request.effective_message.reply_text.assert_not_awaited()
    assert deck.store.snapshot() == before
    request = update(callback="ui:settings")
    request.callback_query.edit_message_text.side_effect = BadRequest("Message can't be edited")
    await deck.handle(request, None)
    request.effective_message.reply_text.assert_awaited_once()


async def test_all_visible_readonly_panels_have_working_callbacks_and_bounded_data(deck):
    roots = ["⚙️ Настройки", "📡 Радар", "📈 Аналитика", "❔ Помощь"]
    pending = []
    seen = set()
    mutating = (
        "ui:apply:",
        "ui:active:",
        "ui:restore:",
        "ui:clear",
        "ui:radar_",
        "ui:alerts:on",
        "ui:alerts:off",
        "ui:agenttoggle:",
        "ui:cardset:",
        "digest:on",
        "digest:off",
        "enrichment:on",
        "enrichment:off",
        "detector:",
        "al:mute:",
    )
    before = (deck.store.snapshot(), deck.store.enabled(), deck.digest.preference())
    for root in roots:
        request = update(root)
        await deck.handle(request, None)
        pending.append(request)
    while pending:
        previous = pending.pop(0)
        calls = previous.effective_message.reply_text.call_args_list
        if previous.callback_query:
            calls += previous.callback_query.edit_message_text.call_args_list
        for call in calls:
            markup = call.kwargs.get("reply_markup")
            if not hasattr(markup, "inline_keyboard"):
                continue
            for row in markup.inline_keyboard:
                for button in row:
                    data = button.callback_data
                    if (
                        not data
                        or data in seen
                        or data.startswith(mutating)
                        or data.startswith(("ui:statsfile:", "ui:stats:"))
                    ):
                        continue
                    assert len(data.encode()) <= 64
                    seen.add(data)
                    request = update(callback=data)
                    request.effective_message.reply_document = AsyncMock()
                    await deck.handle(request, None)
                    texts = [c.args[0] for c in request.effective_message.reply_text.call_args_list]
                    texts += [c.args[0] for c in request.callback_query.edit_message_text.call_args_list]
                    assert not any("Действие не выполнено" in text for text in texts), (data, texts)
                    assert (deck.store.snapshot(), deck.store.enabled(), deck.digest.preference()) == before
                    pending.append(request)
    assert {
        "ui:card",
        "ui:parameter_group:price",
        "ui:field:breakout_lookback",
        "ui:diag:storage",
        "ui:diag:file",
        "ui:commands",
        "ui:groups:analytics:detector",
    } <= seen


async def test_watchlist_pages_keep_all_instruments_without_message_flood(deck):
    before = deck.store.snapshot()
    names = tuple(f"spot/COIN{i}USDT" for i in range(24))
    deck.store.update(before.revision, before.settings.changed(watchlist=names))
    request = update("Список")
    await deck.handle(request, None)
    shown = request.effective_message.reply_text.call_args.args[0]
    assert "1–10 из 24" in shown and "COIN23USDT" not in shown
    request = update(callback="ui:listpage:20")
    await deck.handle(request, None)
    shown = request.callback_query.edit_message_text.call_args.args[0]
    assert "COIN23USDT" in shown and "21–24 из 24" in shown
    assert deck.store.snapshot().settings.instruments() == names


async def test_statistics_frozen_snapshot_and_download_keep_exact_completed_results(deck):
    from cryptowatcher.outcomes import MINUTE
    from cryptowatcher.sources import Sources

    from .test_outcomes import NOW, future, observe

    observe(deck.store, deck.outcomes)
    Sources(deck.store).put("spot/BTCUSDT/1m", future())
    deck.outcomes.work(NOW + 60 * MINUTE)
    before = deck.store.snapshot()
    request = update("/analytics detector")
    await deck.handle(request, None)
    frozen = json.dumps(deck.ui.statistics["data"], sort_keys=True)
    data = json.loads(frozen)
    assert data["cohorts"] and any(row["sample_size"] for row in data["cohorts"])
    shown = "\n".join(c.args[0] for c in request.effective_message.reply_text.call_args_list)
    assert "Наблюдений:" in shown and "срок, мин:" in shown and "Это не доходность" in shown
    token = deck.ui.statistics["token"]
    observe(deck.store, deck.outcomes, now=NOW + MINUTE)
    request = update(callback="ui:statsfile:" + token)
    request.effective_message.reply_document = AsyncMock()
    await deck.handle(request, None)
    downloaded = json.loads(request.effective_message.reply_document.call_args.kwargs["document"].getvalue())
    assert json.dumps(downloaded, sort_keys=True) == frozen
    assert deck.store.snapshot() == before
    deck.ui.statistics["at"] -= 301
    expired = update(callback="ui:stats:" + token + ":0")
    await deck.handle(expired, None)
    assert "устарел" in expired.effective_message.reply_text.call_args.args[0]


async def test_mutes_pages_expose_all_saved_instruments_and_explicit_unmute(deck):
    import time

    for i in range(14):
        deck.store.mute(f"spot/COIN{i:02}USDT", time.time() + 3600)
    before = deck.store.snapshot()
    first = update("🔕 Mutes")
    await deck.handle(first, None)
    assert "COIN13USDT" not in first.effective_message.reply_text.call_args.args[0]
    second = update(callback="ui:mutespage:10")
    await deck.handle(second, None)
    shown = second.callback_query.edit_message_text.call_args
    assert "COIN13USDT" in shown.args[0]
    controls = [b.callback_data for row in shown.kwargs["reply_markup"].inline_keyboard for b in row]
    assert "ui:unmute:spot/COIN13USDT" in controls
    await deck.handle(update(callback="ui:unmute:spot/COIN13USDT"), None)
    assert not deck.store.muted("spot/COIN13USDT", time.time())
    assert deck.store.muted("spot/COIN12USDT", time.time()) and deck.store.snapshot() == before


def test_notification_separates_old_settings_from_expired_confirmations(deck):
    from copy import deepcopy

    from cryptowatcher.events import EventEngine
    from cryptowatcher.render import notification_text

    from .test_events import NOW, candidate, observe

    engine = EventEngine(deck.store)
    observe(engine)
    delivery = deepcopy(candidate(engine.history))
    event = engine.history.get("event-head:" + delivery["event_ids"][0])
    confirmation = next(iter(delivery["episode_snapshot"]["confirmations"].values()))
    fresh = {**confirmation, "settings_revision": event["settings_revision"]}
    stale = {**fresh, "fresh_until_ms": NOW - 1}
    different = {**fresh, "settings_revision": event["settings_revision"] - 1}
    delivery["episode_snapshot"]["confirmations"] = {
        "price/1m": fresh,
        "turnover/1m": stale,
        "composite/1m": different,
    }
    original = json.dumps(delivery, sort_keys=True)
    shown = notification_text(event, delivery, NOW)
    assert "Актуальные подтверждения:\n• Движение цены · 1m." in shown
    assert "Устаревших подтверждений этой настройки: 1" in shown
    assert "Из других версий настроек: 1" in shown
    assert json.dumps(delivery, sort_keys=True) == original


def test_notification_full_confirmation_grid_fits_without_mutating_event(deck):
    from copy import deepcopy

    from cryptowatcher.events import EventEngine
    from cryptowatcher.render import notification_text
    from cryptowatcher.telegram_views import FAMILY

    from .test_events import NOW, candidate, observe

    engine = EventEngine(deck.store)
    observe(engine)
    delivery = deepcopy(candidate(engine.history))
    event = deepcopy(engine.history.get("event-head:" + delivery["event_ids"][0]))
    confirmation = next(iter(delivery["episode_snapshot"]["confirmations"].values()))
    delivery["episode_snapshot"]["confirmations"] = {
        family + "/" + tf: {**confirmation, "settings_revision": event["settings_revision"]}
        for family in FAMILY
        for tf in ("1m", "3m", "5m", "15m")
    }
    original = json.dumps([event, delivery], sort_keys=True)
    shown = notification_text(event, delivery, NOW)
    assert shown.startswith("🔔 ") and "Время наблюдения:" in shown
    assert len(shown.encode("utf-16-le")) // 2 < 4096
    assert "<b>" not in shown and "**" not in shown
    assert json.dumps([event, delivery], sort_keys=True) == original


async def test_domain_detector_buttons_are_revision_safe_and_independent_from_legacy_notifications(deck):
    original = deck.store.snapshot()
    delivery = (deck.store.enabled(), deck.digest.preference())
    opened = update(callback="ui:detector_activity")
    await deck.handle(opened, None)
    reply = opened.callback_query.edit_message_text.call_args
    assert "Расчёты детекторов" in reply.args[0]
    choices = [
        b.callback_data
        for row in reply.kwargs["reply_markup"].inline_keyboard
        for b in row
        if (b.callback_data or "").startswith("ui:active:")
    ]
    assert len(choices) == 5 and all(len(c.encode()) <= 64 for c in choices)
    assert deck.store.snapshot() == original
    off = f"ui:active:price:off:{original.revision}"
    await deck.handle(update(callback=off), None)
    saved = deck.store.snapshot()
    assert "price" not in saved.settings.active_detectors
    assert saved.settings.alert_detectors == original.settings.alert_detectors
    assert (deck.store.enabled(), deck.digest.preference()) == delivery
    # The same old button cannot undo or replay a newer owner decision.
    again = update(callback=off)
    await deck.handle(again, None)
    assert deck.store.snapshot() == saved
    assert "устарела" in again.effective_message.reply_text.call_args.args[0]
    await deck.handle(update(callback=f"ui:active:price:on:{saved.revision}", owner=False), None)
    assert deck.store.snapshot() == saved
    for bad in (
        f"ui:active:unknown:on:{saved.revision}",
        f"ui:active:price:toggle:{saved.revision}",
        "ui:active:price:on:not-a-revision",
        "ui:active:price",
    ):
        request = update(callback=bad)
        await deck.handle(request, None)
        assert deck.store.snapshot() == saved
        assert "Действие не выполнено" in request.effective_message.reply_text.call_args.args[0]
    await deck.handle(update(callback="detector:composite:off"), None)
    notification = deck.store.snapshot()
    assert notification.settings.alert_detectors == ()
    assert notification.settings.active_detectors == saved.settings.active_detectors
    with closing(Store(deck.store.path, deck.store.owner_id, readonly=True)) as reopened:
        assert reopened.snapshot() == notification
