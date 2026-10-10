import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, TelegramError

from cryptowatcher.bot import COMMANDS, Controller, application
from cryptowatcher.market import RadarResult
from cryptowatcher.storage import Store

from .conftest import update


def result():
    return RadarResult(
        {
            "BTCUSDT": {
                "price": 100,
                "sma_tf_turnover": 250,
                "signal_timeframe": "5m",
                "price_change_tf": -3,
                "current_tf_turnover": 1000,
                "turnover_spike_ratio": 4,
                "turnover24h": 2000000,
                "candle_end_ms": 1800000000000,
                "meets_signal": True,
            }
        },
        {},
        1,
    )


@pytest.fixture
def controller(infrastructure, store):
    return Controller(
        infrastructure, store, SimpleNamespace(snapshot=AsyncMock(return_value=result()), close=AsyncMock())
    )


@pytest.mark.parametrize("command", list(COMMANDS))
async def test_every_command_rejects_stranger_without_state_access(controller, command):
    before = controller.store.db.total_changes
    message = update(f"/{command}", owner=False)
    await controller.handle(message, None)
    assert controller.store.db.total_changes == before
    message.effective_message.reply_text.assert_not_awaited()
    controller.market.snapshot.assert_not_awaited()


@pytest.mark.parametrize("callback", ["st:pm:1.0", "st:help", "al:mute:BYBIT:BTCUSDT:3600", "rb:all"])
async def test_callbacks_and_groups_cannot_mutate(controller, callback):
    before = controller.store.db.total_changes
    message = update(group=True, callback=callback)
    await controller.handle(message, None)
    assert controller.store.db.total_changes == before
    message.callback_query.answer.assert_awaited_once()
    controller.market.snapshot.assert_not_awaited()


async def test_owner_control_deck_list_mutations_and_restart(controller):
    for command in ("/start", "/settings", "/terms", "/help", "/watchlist"):
        message = update(command)
        await controller.handle(message, None)
        assert message.effective_message.reply_text.await_count > 0
    for command in ("/addcoin", "BTC,ETH,BTC", "/removecoin", "ETH"):
        await controller.handle(update(command), None)
    assert controller.store.snapshot().settings.custom_pairs == ("BTCUSDT",)
    await controller.handle(update("/clearlist"), None)
    reopened = Store(controller.store.path, controller.store.owner_id)
    try:
        assert reopened.snapshot().settings.custom_pairs == ()
    finally:
        reopened.close()


async def test_profiles_manual_detectors_history_and_callbacks_remain_owner_scoped(controller):
    before = controller.store.snapshot()
    await controller.handle(update("/profiles"), None)
    assert controller.store.snapshot() == before
    await controller.handle(update(callback="profile:normal"), None)
    assert controller.store.snapshot().settings.profile == "normal"
    await controller.handle(update("/set breakout_lookback 28"), None)
    current = controller.store.snapshot()
    assert (
        current.settings.profile == "custom"
        and dict(current.settings.detector_parameters)["breakout_lookback"] == 28
    )
    await controller.handle(update("/set volatility_ratio nan"), None)
    assert controller.store.snapshot() == current
    await controller.handle(update(callback="detector:price:on"), None)
    assert "price" in controller.store.snapshot().settings.alert_detectors
    state = controller.store.snapshot()
    for callback in (
        "deck:detectors",
        "profile:sensitive",
        "detector:composite:off",
        "event:00000000-0000-0000-0000-000000000000",
    ):
        await controller.handle(update(owner=False, callback=callback), None)
        assert controller.store.snapshot() == state
    message = update("/history")
    await controller.handle(message, None)
    assert "пуста" in message.effective_message.reply_text.call_args.args[0]


async def test_preset_status_and_sample_show_effective_values_not_legacy_fields(controller):
    controller.change(price_move_min=7, turnover_spike_min=6, sma_periods=20, signal_timeframe="15m")
    await controller.handle(update("/profile normal"), None)
    snapshot = controller.store.snapshot()
    assert snapshot.settings.price_move_min == 7 and snapshot.settings.sma_periods == 20
    message = update("/settings")
    await controller.handle(message, None)
    text = "".join(call.args[0] for call in message.effective_message.reply_text.call_args_list)
    assert "15m: ≥ 4%" in text and "SMA periods: 12" in text and "Спайк оборота: ≥ x3" in text
    message = update("/sample_alert")
    await controller.handle(message, None)
    assert "TF 15m · +4.3%" in message.effective_message.reply_text.call_args.args[0]
    assert "Средний оборот (12 candles)" in message.effective_message.reply_text.call_args.args[0]
    assert controller.store.snapshot() == snapshot


@pytest.mark.parametrize(
    ("category", "timeframe", "url"),
    [
        ("spot", "1m", "https://www.bybit.com/trade/spot/SOL/USDT"),
        ("linear", "15m", "https://www.bybit.com/trade/usdt/SOLUSDT"),
    ],
)
async def test_sample_uses_active_settings_and_preserves_alert_controls(controller, category, timeframe, url):
    controller.change(
        bybit_category=category,
        signal_timeframe=timeframe,
        price_move_min=7,
        turnover_spike_min=6,
        liquidity_floor_24h=20_000_000,
        sma_periods=20,
    )
    before = controller.store.snapshot()
    changes = controller.store.db.total_changes
    message = update("/sample_alert")
    await controller.handle(message, None)
    reply = message.effective_message.reply_text.call_args
    text = reply.args[0]
    assert "Пример алерта" in text and "Демонстрационные данные" in text
    assert f"TF {timeframe} · +7.3%" in text
    assert "Спайк оборота: x6.2" in text
    assert "Оборот 24ч: 20,000,000.0 USDT" in text
    assert "Средний оборот (20 candles)" in text
    assert "≥ 7%, ≥ x6, ≥ 20,000,000 USDT" in text
    assert f"Configuration revision: {before.revision}" in text
    assert ("Spot USDT" if category == "spot" else "USDT Perpetual") in text
    buttons = [button for row in reply.kwargs["reply_markup"].inline_keyboard for button in row]
    assert [button.url for button in buttons if button.url] == [url]
    assert {button.callback_data for button in buttons if button.callback_data} == {
        f"al:mute:BYBIT:{category}:SOLUSDT:3600",
        f"al:mute:BYBIT:{category}:SOLUSDT:86400",
        f"al:coin:BYBIT:{category}:SOLUSDT",
    }
    assert controller.store.snapshot() == before
    assert controller.store.db.total_changes == changes
    controller.market.snapshot.assert_not_awaited()


async def test_sample_accepts_finite_extremes_without_overflow(controller):
    from cryptowatcher.render import sample_alert_text

    controller.change(
        price_move_min=sys.float_info.max,
        turnover_spike_min=sys.float_info.max,
        liquidity_floor_24h=sys.float_info.max,
    )
    # The old sample's liquidity multiplier overflowed valid finite settings.
    text = sample_alert_text(controller.store.snapshot())
    assert "inf" not in text.lower() and "nan" not in text.lower()
    message = update("/sample_alert")
    await controller.handle(message, None)
    assert message.effective_message.reply_text.call_args.kwargs["reply_markup"] is not None


async def test_noop_and_message_not_modified_are_quiet(controller):
    revision = controller.store.snapshot().revision
    message = update(callback="st:pm:2.0")
    message.callback_query.edit_message_text.side_effect = BadRequest("Message is not modified")
    await controller.handle(message, None)
    assert controller.store.snapshot().revision == revision
    message.effective_message.reply_text.assert_not_awaited()
    await controller.handle(update(callback="st:noop"), None)
    assert controller.store.snapshot().revision == revision


async def test_manual_invalid_value_and_legacy_callbacks_preserve_values(controller):
    before = controller.store.snapshot()
    await controller.handle(update("/set price_move_min nan"), None)
    assert controller.store.snapshot() == before
    await controller.handle(update(callback="st:src:CMC"), None)
    await controller.handle(update(callback="al:coin:CMC:BTC"), None)
    assert controller.store.snapshot() == before
    controller.market.snapshot.assert_not_awaited()
    await controller.handle(update("/set price_move_min 7"), None)
    assert controller.store.snapshot().settings.price_move_min == 7


async def test_settings_changed_during_fetch_discards_poll_and_status(controller):
    async def fetch(settings):
        snapshot = controller.store.snapshot()
        controller.store.update(
            snapshot.revision, snapshot.settings.changed(price_move_min=settings.price_move_min + 1)
        )
        return result()

    controller.market.snapshot.side_effect = fetch
    context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
    await controller.poll(context)
    context.bot.send_message.assert_not_awaited()
    assert controller.store.get_runtime("last_poll_status")["state"] == "DISCARDED"
    message = update("/status")
    await controller.handle(message, None)
    assert "Настройки изменились" in message.effective_message.reply_text.call_args.args[0]


async def test_failed_send_does_not_consume_cooldown_and_same_candle_is_not_repeated(controller):
    context = SimpleNamespace(
        bot=SimpleNamespace(send_message=AsyncMock(side_effect=TelegramError("never-echo-this-secret")))
    )
    await controller.poll(context)
    assert controller.store.get_runtime("last_alert:spot:BTCUSDT:5m") is None
    assert "never-echo" not in str(controller.diagnostics())
    controller.store.put_runtime("last_poll_ts", 0)
    context.bot.send_message.side_effect = None
    await controller.poll(context)
    sent = controller.store.get_runtime("last_alert:spot:BTCUSDT:5m")
    assert sent["candle_end_ms"] == 1800000000000
    controller.store.put_runtime("last_poll_ts", 0)
    controller.store.put_runtime("last_alert:spot:BTCUSDT:5m", {**sent, "at": 0})
    await controller.poll(context)
    assert context.bot.send_message.await_count == 2


async def test_mute_and_delivery_off_keep_market_calculation(controller):
    context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
    await controller.handle(update("/alerts off"), None)
    await controller.poll(context)
    controller.market.snapshot.assert_awaited_once()
    context.bot.send_message.assert_not_awaited()
    await controller.handle(update("/alerts on"), None)
    legacy = update(callback="al:mute:BYBIT:BTCUSDT:3600")
    legacy.callback_query.message = SimpleNamespace(text="BTCUSDT · Spot USDT")
    await controller.handle(legacy, None)
    controller.store.put_runtime("last_poll_ts", 0)
    await controller.poll(context)
    context.bot.send_message.assert_not_awaited()


async def test_unmute_all_preserves_owner_config(controller):
    snapshot = controller.store.snapshot()
    controller.store.mute("BTCUSDT", 2000000000)
    controller.store.mute("ETHUSDT", 2000000000)
    await controller.handle(update("/unmute all"), None)
    assert not controller.store.muted("BTCUSDT", 100)
    assert not controller.store.muted("ETHUSDT", 100)
    assert controller.store.snapshot() == snapshot


async def test_application_identity_guard_before_command_registration(controller):
    app = application(controller)
    wrong_bot = SimpleNamespace(
        get_me=AsyncMock(return_value=SimpleNamespace(id=1)), set_my_commands=AsyncMock()
    )
    from cryptowatcher.config import ConfigError

    with pytest.raises(ConfigError, match="identity mismatch"):
        await app.post_init(SimpleNamespace(bot=wrong_bot))
    wrong_bot.set_my_commands.assert_not_awaited()
