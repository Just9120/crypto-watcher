import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import Forbidden, NetworkError, RetryAfter

from cryptowatcher.config import OwnerSettings
from cryptowatcher.detectors import FORMULAS, Detection, evaluate
from cryptowatcher.events import EventEngine
from cryptowatcher.render import event_keyboard, notification_text
from cryptowatcher.sources import Candle
from cryptowatcher.storage import StateError, Store
from cryptowatcher.telegram_delivery import TelegramDelivery

NOW = 1_800_000_000_000


def window(now=NOW, tf="1m", close=102, turnover=1200):
    step = int(tf[:-1]) * 60_000
    return [
        Candle.parse(
            [
                now - (24 - i) * step,
                100,
                max(101, close) if i == 23 else 101,
                min(99, close) if i == 23 else 99,
                close if i == 23 else 100,
                1,
                turnover if i == 23 else 400,
            ],
            tf,
            "websocket",
            now,
            now,
        )
        for i in range(24)
    ]


def selected(*families):
    def detector(family, candles, *args):
        if family in families:
            return evaluate(family, candles, *args)
        return Detection(family, False, "NEUTRAL", 0, {}, ())

    return detector


@pytest.fixture
def state(tmp_path):
    store = Store(tmp_path / "radar.db", 233382575)
    store.initialize(OwnerSettings())
    yield store
    store.close()


def observe(engine, now=NOW, tf="1m", close=102, *, live=True, market="spot"):
    engine.observe(f"{market}/BTCUSDT/{tf}", window(now, tf, close), 1e6, 0.01, now, live=live)


def candidate(history):
    bindings = history.items("pending:telegram:")
    return history.get("outbox:telegram:" + bindings[0][1]["delivery_id"])


def delivery(state):
    return TelegramDelivery(
        state,
        state.owner_id,
        lambda event, outbox, now: event["event_id"] + ":" + outbox["reason"],
        lambda event: None,
    )


def test_atomic_history_identity_markets_settings_and_inputs(state):
    engine = EventEngine(state)
    observe(engine)
    history = engine.history
    events = history.events()
    assert {e["detector_id"] for e in events} == {"price", "turnover", "breakout", "composite"}
    assert all(
        e["source_inputs"] and e["settings_revision"] == 1 and e["reference_time_ms"] == NOW for e in events
    )
    assert all(e["trading_authorized"] is False and e["model_invoked"] is False for e in events)
    assert all(len(e["source_inputs"]) >= 16 for e in events)  # Frozen ATR methodology inputs too.
    rows = state.db.execute("SELECT count(*) FROM runtime").fetchone()[0]
    observe(engine)
    assert state.db.execute("SELECT count(*) FROM runtime").fetchone()[0] == rows
    observe(engine, market="linear")
    assert len(history.events()) == 8
    assert len(history.items("active:")) == 4  # Directional/neutral, independent markets.
    before = state.snapshot()
    state.update(before.revision, before.settings.changed(price_move_min=1.5))
    observe(engine)
    assert {e["settings_revision"] for e in history.events()} == {1, 2}
    assert history.get("event:" + events[0]["event_id"] + ":0000000001") == events[0]


def test_observation_context_is_resolved_once_only_for_new_events_and_keeps_every_family_snapshot(state):
    calls = []
    context = {"ticker": {"available_at_ms": NOW}, "instrument": {"tick_size": 0.01}}
    enrichment = {"state": "DISABLED", "optional": True, "detector_input": False}

    def provide():
        calls.append(NOW)
        return context, enrichment

    silent = EventEngine(state, selected())
    silent.observe("spot/BTCUSDT/1m", window(), 1e6, 0.01, NOW, live=True, context_provider=provide)
    assert not calls and not silent.history.events()
    engine = EventEngine(state)
    engine.observe("spot/BTCUSDT/1m", window(), 1e6, 0.01, NOW, live=True, context_provider=provide)
    events = engine.history.events()
    assert len(events) == 4 and calls == [NOW]
    assert all(e["source_context"] == context and e["enrichment"] == enrichment for e in events)
    engine.observe("spot/BTCUSDT/1m", window(), 1e6, 0.01, NOW, live=True, context_provider=provide)
    assert calls == [NOW] and engine.history.events() == events


@pytest.mark.parametrize("bounded", [False, True])
def test_context_provider_failure_or_ambiguous_input_preserves_the_whole_transaction(state, bounded):
    if bounded:
        from cryptowatcher.market_storage import enable

        enable(state.db)
        state.bounded = True
    engine = EventEngine(state)

    def saved():
        return {
            name: sorted(repr(tuple(row)) for row in state.db.execute('SELECT * FROM "' + name + '"'))
            for (name,) in state.db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }

    def fail():
        raise RuntimeError("context unavailable")

    before = saved()
    with pytest.raises(RuntimeError):
        engine.observe("spot/BTCUSDT/1m", window(), 1e6, 0.01, NOW, live=True, context_provider=fail)
    assert saved() == before
    with pytest.raises(ValueError):
        engine.observe(
            "spot/BTCUSDT/1m", window(), 1e6, 0.01, NOW, live=True, source_context={}, context_provider=fail
        )
    assert saved() == before


def test_transaction_failure_rolls_back_event_episode_and_outbox(state, monkeypatch):
    engine = EventEngine(state)
    original = engine.history.put

    def fail(key, value):
        if key.startswith("outbox:"):
            raise sqlite3.OperationalError("injected full disk")
        original(key, value)

    monkeypatch.setattr(engine.history, "put", fail)
    with pytest.raises(sqlite3.OperationalError):
        observe(engine)
    assert (
        not engine.history.events()
        and not engine.history.items("active:")
        and not engine.history.items("pending:")
    )


def test_detector_fault_isolated_and_secret_safe(state):
    def fault(family, *args):
        if family == "price":
            raise ValueError("sensitive injected details")
        return evaluate(family, *args)

    engine = EventEngine(state, fault)
    observe(engine)
    assert any(e["detector_id"] == "composite" for e in engine.history.events())
    errors = engine.history.diagnostics()["detector_errors"]
    assert errors["diagnostic:spot/BTCUSDT/1m:price"]["reason"] == "ValueError"
    assert "sensitive" not in str(errors)


async def test_coalesce_escalation_success_baseline_and_restart(state):
    engine = EventEngine(state, selected("price", "composite"))
    settings = state.snapshot()
    state.update(settings.revision, settings.settings.changed(alert_detectors=("price", "composite")))
    observe(engine)
    history = engine.history
    assert len(history.items("pending:telegram:")) == 1
    first = candidate(history)
    assert len(first["event_ids"]) == 2
    assert history.get("episode-head:" + first["episode_id"])["last_notified"] is None
    transport = delivery(state)
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=3)))
    await transport.send_due(bot, NOW + 1999)
    bot.send_message.assert_not_called()
    await transport.send_due(bot, NOW + 2000)
    assert (
        history.get("episode-head:" + first["episode_id"])["last_notified"]["delivery_id"]
        == first["delivery_id"]
    )
    observe(engine, NOW + 60_000)
    assert not history.items("pending:telegram:")  # Unchanged signal is history, not another alert.
    observe(engine, NOW + 120_000)
    observe(engine, NOW + 180_000, tf="3m")
    escalation = candidate(history)
    assert escalation["reason"] == "new_timeframe"
    reopened = Store(state.path, state.owner_id)
    try:
        engine2 = EventEngine(reopened)
        assert engine2.history.get("outbox:telegram:" + escalation["delivery_id"]) == escalation
        assert engine2.history.events()
    finally:
        reopened.close()


def test_forced_closure_and_two_complete_bars_rearm(state):
    engine = EventEngine(state, selected("composite"))
    observe(engine)
    for minutes in range(1, 61):
        observe(engine, NOW + minutes * 60_000)
    history = engine.history
    episodes = [e for _, e in history.items("episode-head:")]
    assert any(e.get("close_reason") == "max_lifetime" for e in episodes)
    assert not history.items("active:")
    assert history.events(1)[0]["observation_reason"] == "awaiting_rearm"
    observe(engine, NOW + 61 * 60_000, close=100)
    observe(engine, NOW + 62 * 60_000, close=100)
    observe(engine, NOW + 63 * 60_000)
    assert len(history.items("active:")) == 1
    assert history.events(1)[0]["live"] is True
    engine.tick(NOW + 65 * 60_000)
    assert not history.items("active:")  # Two longest-TF periods of inactivity.


def test_historical_and_stale_inputs_never_create_live_episode_or_notification(state):
    engine = EventEngine(state, selected("composite"))
    observe(engine, live=False)
    assert engine.history.events()[0]["live"] is False
    assert not engine.history.items("active:") and not engine.history.items("pending:")
    old = window()
    engine.observe("linear/BTCUSDT/1m", old, 1e6, 0.01, NOW + 60_001, live=True)
    assert all(e["live"] is False for e in engine.history.events())
    assert not engine.history.items("active:")


def test_two_complete_recovered_non_signal_bars_rearm_after_restart(state):
    engine = EventEngine(state, selected("composite"))
    observe(engine)
    engine.tick(NOW + 120_000)
    assert not engine.history.items("active:")
    reopened = Store(state.path, state.owner_id)
    try:
        restarted = EventEngine(reopened, selected("composite"))
        # Persisted window proves the two intervening bars did not satisfy the condition.
        restarted.observe("spot/BTCUSDT/1m", window(NOW + 180_000), 1e6, 0.01, NOW + 180_000, live=True)
        event = restarted.history.events(1)[0]
        assert event["live"] and event["rearm_from_complete_sources"]
        assert len(restarted.history.items("active:")) == 1
    finally:
        reopened.close()


def test_history_revision_conflicts_reference_and_causal_cutoff(state):
    engine = EventEngine(state, selected("composite"))
    observe(engine)
    history = engine.history
    original = history.events(1)[0]
    revised = {
        **original,
        "revision": 2,
        "available_at_ms": NOW + 100_000,
        "analytics_update": {"version": "1"},
    }
    with state.db:
        history.version("event", revised)
        history.version("event", original)  # Reordering never replaces the newer head.
    assert history.get("event-head:" + original["event_id"])["revision"] == 2
    assert history.get("event:" + original["event_id"] + ":0000000001") == original
    with pytest.raises(StateError, match="Conflicting immutable"):
        with state.db:
            history.version("event", {**original, "strength": 900})
    with pytest.raises(StateError, match="reference is immutable"):
        with state.db:
            history.version("event", {**revised, "revision": 3, "reference_price": 999})
    versions = [e for _, e in history.items("event:" + original["event_id"] + ":")]
    assert [e["revision"] for e in versions if e["available_at_ms"] <= NOW] == [1]


@pytest.mark.parametrize("muted", [False, True])
def test_alert_toggle_mute_preserve_all_history(state, muted):
    if muted:
        state.mute("spot/BTCUSDT", (NOW + 3_600_000) / 1000)
    else:
        state.set_enabled(False)
    engine = EventEngine(state)
    observe(engine)
    assert engine.history.events() and not engine.history.items("pending:")


async def test_retry_after_network_permanent_and_success_are_durable(state):
    engine = EventEngine(state, selected("composite"))
    observe(engine)
    first = candidate(engine.history)
    transport = delivery(state)
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=RetryAfter(4)))
    await transport.send_due(bot, NOW + 2000)
    current = candidate(engine.history)
    assert current["state"] == "RETRY" and current["next_attempt_ms"] == NOW + 6000
    assert engine.history.get("episode-head:" + first["episode_id"])["last_notified"] is None
    bot.send_message.side_effect = NetworkError("redacted fixture")
    await transport.send_due(bot, NOW + 6000)
    assert candidate(engine.history)["attempts"][-1]["acknowledgement"] == "UNKNOWN"
    bot.send_message.side_effect = Forbidden("blocked")
    await transport.send_due(bot, NOW + 11_000)
    assert engine.history.get("target:telegram")["state"] == "BLOCKED"
    transport.resume()
    bot.send_message.side_effect = None
    bot.send_message.return_value = SimpleNamespace(message_id=7)
    await transport.send_due(bot, NOW + 12_000)
    completed = engine.history.get("outbox:telegram:" + first["delivery_id"])
    assert completed["state"] == "DELIVERED" and completed["attempts"][-1]["message_id"] == 7
    assert len(set(call.args[1] for call in bot.send_message.call_args_list)) == 1
    assert len(engine.history.events()) == 1


async def test_expiry_config_race_and_unknown_ack_on_restart(state):
    engine = EventEngine(state, selected("composite"))
    observe(engine)
    original = candidate(engine.history)
    original["state"] = "IN_FLIGHT"
    original["attempts"].append({"at_ms": NOW + 2000, "result": "IN_FLIGHT"})
    with state.db:
        engine.history.put("outbox:telegram:" + original["delivery_id"], original)
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=3)))
    observe(engine, NOW + 60_000)  # Continuous condition keeps its episode active during delivery outage.
    await delivery(state).send_due(bot, NOW + 60_001)
    completed = engine.history.get("outbox:telegram:" + original["delivery_id"])
    assert completed["state"] == "EXPIRED_LIVE"
    assert completed["attempts"][-1]["result"] == "AMBIGUOUS_AFTER_RESTART"
    bot.send_message.assert_not_called()

    observe(engine, NOW + 120_000)
    current = candidate(engine.history)
    snapshot = state.snapshot()
    state.update(snapshot.revision, snapshot.settings.changed(price_move_min=3))
    await delivery(state).send_due(bot, NOW + 122_000)
    assert (
        engine.history.get("outbox:telegram:" + current["delivery_id"])["state"] == "OBSOLETE_CONFIGURATION"
    )
    bot.send_message.assert_not_called()


async def test_strength_boundary_new_family_and_send_time_settings_race(state):
    engine = EventEngine(state, selected("price", "composite"))
    s = state.snapshot()
    state.update(s.revision, s.settings.changed(alert_detectors=("price", "composite")))
    engine.observe("spot/BTCUSDT/1m", window(turnover=400), 1e6, 0.01, NOW, live=True)
    transport = delivery(state)
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)))
    await transport.send_due(bot, NOW + 2000)
    observe(engine, NOW + 60_000)
    assert candidate(engine.history)["reason"] == "new_detector"
    await transport.send_due(bot, NOW + 62_000)
    observe(engine, NOW + 120_000, close=103)
    assert candidate(engine.history)["reason"] == "strengthening"  # Exactly 1.5 of notified price strength.

    async def changing(*args, **kwargs):
        snapshot = state.snapshot()
        state.update(snapshot.revision, snapshot.settings.changed(price_move_min=4))
        return SimpleNamespace(message_id=2)

    bot.send_message.side_effect = changing
    queued = candidate(engine.history)
    await transport.send_due(bot, NOW + 122_000)
    completed = engine.history.get("outbox:telegram:" + queued["delivery_id"])
    assert completed["state"] == "DELIVERED_PRIOR_SETTINGS"
    episode = engine.history.get("episode-head:" + queued["episode_id"])
    assert episode["last_notified"]["settings_revision"] != state.snapshot().revision


async def test_confirmation_during_send_gets_followup_with_frozen_revisions(state):
    engine = EventEngine(state, selected("composite"))
    observe(engine)
    first = candidate(engine.history)
    original = engine.history.get("event:" + first["event_ids"][0] + ":0000000001")
    with state.db:
        engine.history.version(
            "event",
            {
                **original,
                "revision": 2,
                "available_at_ms": NOW + 1000,
                "metrics": {**original["metrics"], "price_return": 999},
            },
        )
    clock = [NOW + 2000]
    transport = TelegramDelivery(
        state,
        state.owner_id,
        lambda event, outbox, now: f"{event['revision']}:{event['metrics']['price_return']:g}",
        lambda event: None,
        engine.reconcile_notifications,
    )

    async def interleaved(*args, **kwargs):
        assert not state.db.in_transaction
        clock[0] = NOW + 3000
        engine.observe("spot/BTCUSDT/3m", window(tf="3m"), 1e6, 0.01, clock[0], live=True)
        clock[0] = NOW + 4000
        return SimpleNamespace(message_id=1)

    bot = SimpleNamespace(send_message=AsyncMock(side_effect=interleaved))
    await transport.send_due(bot, clock[0], clock=lambda: clock[0])
    assert bot.send_message.call_args.args[1].startswith(
        "1:2"
    )  # Later event revision never enters frozen live context.
    followup = candidate(engine.history)
    assert followup["reason"] == "new_timeframe" and followup["delivery_id"] != first["delivery_id"]
    assert followup["event_revisions"] == {followup["event_ids"][0]: 1}
    clock[0] = NOW + 6000
    bot.send_message.side_effect = None
    bot.send_message.return_value = SimpleNamespace(message_id=2)
    await transport.send_due(bot, clock[0], clock=lambda: clock[0])
    assert not engine.history.items("pending:") and bot.send_message.await_count == 2
    records = engine.history.items("decision:telegram:" + followup["event_ids"][0] + ":")
    assert [v["reason"] for _, v in records] == ["delivery_pending", "new_timeframe"]


async def test_bounded_retry_batch_does_not_starve_later_ready_delivery(state):
    engine = EventEngine(state, selected("composite"))
    for i in range(25):
        engine.observe(f"spot/TOKEN{i}USDT/1m", window(), 1e6, 0.01, NOW, live=True)
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=RetryAfter(20)))
    transport = delivery(state)
    await transport.send_due(bot, NOW + 2000)
    assert bot.send_message.await_count == 20
    bot.send_message.side_effect = None
    bot.send_message.return_value = SimpleNamespace(message_id=1)
    await transport.send_due(bot, NOW + 3000)
    assert bot.send_message.await_count == 25
    assert len(engine.history.items("pending:")) == 20


def test_setting_change_cannot_coalesce_old_payload_or_strength_into_new_decision(state):
    engine = EventEngine(state, selected("price", "composite"))
    snapshot = state.snapshot()
    state.update(snapshot.revision, snapshot.settings.changed(alert_detectors=("price", "composite")))
    observe(engine, close=120)
    old = candidate(engine.history)
    snapshot = state.snapshot()
    state.update(snapshot.revision, snapshot.settings.changed(price_move_min=4))
    engine.observe("spot/BTCUSDT/3m", window(tf="3m", close=108), 1e6, 0.01, NOW + 1000, live=True)
    current = candidate(engine.history)
    assert current["delivery_id"] != old["delivery_id"]
    assert engine.history.get("outbox:telegram:" + old["delivery_id"])["state"] == "OBSOLETE_CONFIGURATION"
    assert current["settings_revision"] == state.snapshot().revision
    assert current["notification_state"]["strength"] == pytest.approx(2)
    assert current["notification_state"]["timeframes"] == ["3m"]
    for key, revision in current["event_revisions"].items():
        assert (
            engine.history.get(f"event:{key}:{revision:010d}")["settings_revision"]
            == current["settings_revision"]
        )


def test_frozen_formula_replay_and_all_namespaces_backup(state, tmp_path, monkeypatch):
    engine = EventEngine(state, source_revision="a" * 40)
    observe(engine, close=108)
    for event in engine.history.events():
        replay = evaluate(
            event["detector_id"],
            [Candle(**c) for c in event["source_inputs"]],
            OwnerSettings.from_payload(event["settings_snapshot"]),
            event["market_type"] + "/" + event["symbol"],
            event["metrics"].get("liquidity_24h", 1e6),
        )
        assert replay.signal and replay.direction == event["direction"]
        assert replay.metrics == event["metrics"] and replay.strength == event["strength"]
        assert event["producer_source_revision"] == "a" * 40
    rows = [tuple(row) for row in state.db.execute("SELECT * FROM runtime ORDER BY key")]
    backup = tmp_path / "history-backup.sqlite3"
    state.backup(backup)
    restored = Store(backup, state.owner_id, readonly=True)
    try:
        assert rows == [tuple(row) for row in restored.db.execute("SELECT * FROM runtime ORDER BY key")]
        assert restored.snapshot() == state.snapshot() and restored.enabled() == state.enabled()
    finally:
        restored.close()
    monkeypatch.setitem(FORMULAS, "price", "changed formula without version increment")
    with pytest.raises(StateError, match="without a new version"):
        EventEngine(state)
    assert rows == [tuple(row) for row in state.db.execute("SELECT * FROM runtime ORDER BY key")]


async def test_compact_notification_full_confirmation_set_and_controls(state):
    engine = EventEngine(state)
    for tf in ("1m", "3m", "5m", "15m"):
        observe(engine, tf=tf, close=108)
    item = candidate(engine.history)
    event = max(
        (engine.history.get("event-head:" + key) for key in item["event_ids"]),
        key=lambda e: (e["market_observation_ms"], e["strength"]),
    )
    text = notification_text(event, item, NOW + 2000)
    assert len(text.encode("utf-16-le")) // 2 < 4096
    assert all(
        value in text
        for value in ("Spot USDT", "Интенсивность", "свежее", "Первое уведомление", "подтверждения")
    )
    assert event["event_id"] not in text and "Episode ID" not in text
    markup = event_keyboard(event)
    assert markup.inline_keyboard[0][0].url.endswith("/BTC/USDT")
    assert markup.inline_keyboard[1][0].callback_data == "event:" + event["event_id"]
    assert markup.inline_keyboard[1][1].callback_data == "outcomes:" + event["event_id"]
    assert markup.inline_keyboard[2][0].callback_data == "ui:eventmute:" + event["event_id"]
    assert markup.inline_keyboard[2][1].callback_data == "ui:eventmore:" + event["event_id"]
    assert all(
        len(button.callback_data.encode()) <= 64
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    )
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)))
    transport = TelegramDelivery(state, state.owner_id, notification_text, event_keyboard)
    await transport.send_due(bot, NOW + 2000)
    assert bot.send_message.await_count == 1 and bot.send_message.call_args.args[1] == text


def test_joined_observation_requires_transaction_and_does_not_commit_its_producer(state):
    engine = EventEngine(state)
    args = ("spot/BTCUSDT/1m", window(), 1e6, 0.01, NOW)
    with pytest.raises(StateError, match="producer transaction"):
        engine.observe(*args, live=True, commit=False)
    assert not engine.history.events()
    with pytest.raises(RuntimeError, match="producer rollback"), state.db:
        state.db.execute("BEGIN")
        engine.observe(*args, live=True, commit=False)
        assert state.db.in_transaction and engine.history.events()
        raise RuntimeError("producer rollback")
    assert not engine.history.events() and not engine.history.items("condition:")
