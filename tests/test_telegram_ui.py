import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cryptowatcher.bot import MENU_ACTIONS, PUBLISHED_COMMANDS, Controller
from cryptowatcher.config import OwnerSettings
from cryptowatcher.market import RadarResult
from cryptowatcher.render import menu, settings_keyboard
from cryptowatcher.storage import Store
from cryptowatcher.telegram_ui import CUSTOM, INPUT
from cryptowatcher.telegram_views import amount, split_message

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
