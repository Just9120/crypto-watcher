from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import Forbidden, RetryAfter, TelegramError

from cryptowatcher.digest import Digest, aggregate
from cryptowatcher.events import History, identity
from cryptowatcher.outcomes import MINUTE


def test_empty_digest_distinguishes_absent_observations_from_zero_return():
    from cryptowatcher.digest import text

    shown = text(
        {
            "start_ms": 1_800_000_000_000,
            "end_ms": 1_800_003_600_000,
            "event_count": 0,
            "active_instruments": 0,
            "groups": [],
            "most_active_instruments": [],
            "strongest_episodes": [],
            "ongoing_episodes": [],
        }
    )
    assert "Нет наблюдений за период." in shown and "Нет эпизодов за период." in shown
    assert "Нет продолжающихся эпизодов." in shown
    assert "доходность 0" not in shown


def records(store, start):
    history = History(store)
    with store.db:
        for i, (market, family) in enumerate(
            (("spot", "price"), ("linear", "composite"), ("spot", "turnover"))
        ):
            history.version(
                "event",
                {
                    "event_id": identity(f"digest/{i}"),
                    "revision": 1,
                    "episode_id": identity(f"episode/{i}"),
                    "market_observation_ms": start + MINUTE,
                    "market_type": market,
                    "detector_id": family,
                    "symbol": "BTCUSDT",
                    "timeframe": "1m",
                    "direction": "UP",
                    "strength": i + 1,
                    "reference_price": 100,
                    "reference_time_ms": start + MINUTE,
                },
            )
    return history


async def test_shared_history_fixed_boundary_and_independent_preferences(store):
    start = 1_800_000_000_000 // (60 * MINUTE) * (60 * MINUTE)
    records(store, start)
    digest = Digest(store, store.owner_id)
    assert digest.preference()["enabled"] is False
    digest.schedule(start + 60 * MINUTE)
    assert digest.history.count("digest-pending:") == 0
    store.set_enabled(False)
    store.mute("spot/BTCUSDT", start / 1000 + 10000)
    digest.configure(True, start + MINUTE)
    digest.schedule(start + 60 * MINUTE - 1)
    assert digest.history.count("digest-pending:") == 0
    digest.schedule(start + 60 * MINUTE)
    candidate = digest.history.items("digest-outbox:")[0][1]
    assert candidate["snapshot"]["event_count"] == 3
    assert candidate["snapshot"]["active_instruments"] == 2
    assert {v["market"] for v in candidate["snapshot"]["groups"]} == {"spot", "linear"}
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=5)))
    await digest.send_due(bot, start + 60 * MINUTE)
    assert bot.send_message.await_count == 1 and store.enabled() is False
    digest = Digest(store, store.owner_id)
    digest.schedule(start + 60 * MINUTE)
    await digest.send_due(bot, start + 60 * MINUTE)
    assert bot.send_message.await_count == 1
    assert (
        aggregate(store, start + 60 * MINUTE, start + 120 * MINUTE, start + 120 * MINUTE)["event_count"] == 0
    )


async def test_retry_frozen_payload_restart_ambiguity_and_permanent_target(store):
    start = 1_800_000_000_000 // (60 * MINUTE) * (60 * MINUTE)
    records(store, start)
    digest = Digest(store, store.owner_id)
    digest.configure(True, start + MINUTE)
    digest.schedule(start + 60 * MINUTE)
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=RetryAfter(10)))
    await digest.send_due(bot, start + 60 * MINUTE)
    original_text = bot.send_message.call_args.args[1]
    candidate = digest.history.items("digest-outbox:")[0][1]
    assert candidate["next_attempt_ms"] == start + 60 * MINUTE + 10000
    await digest.send_due(bot, start + 60 * MINUTE + 9999)
    assert bot.send_message.await_count == 1
    # Failed transient delivery preserves the same snapshot; unknown acknowledgement is explicit.
    bot.send_message.side_effect = TelegramError("transient")
    await digest.send_due(bot, start + 60 * MINUTE + 10000)
    candidate = digest.history.items("digest-outbox:")[0][1]
    assert candidate["attempts"][-1]["acknowledgement"] == "UNKNOWN"
    candidate["state"] = "IN_FLIGHT"
    candidate["attempts"][-1]["result"] = "IN_FLIGHT"
    with store.db:
        digest.history.put("digest-outbox:" + candidate["delivery_id"], candidate)
    digest = Digest(store, store.owner_id)
    bot.send_message.side_effect = None
    bot.send_message.return_value = SimpleNamespace(message_id=8)
    await digest.send_due(bot, candidate["next_attempt_ms"])
    saved = digest.history.items("digest-outbox:")[0][1]
    assert saved["attempts"][-2]["result"] == "AMBIGUOUS_AFTER_RESTART"
    assert bot.send_message.call_args.args[1] == original_text
    digest.schedule(start + 120 * MINUTE)
    bot.send_message.side_effect = Forbidden("blocked")
    await digest.send_due(bot, start + 120 * MINUTE)
    assert digest.history.get("target:telegram")["state"] == "BLOCKED"
    digest.configure(False, start + 121 * MINUTE)
    assert digest.history.count("digest-pending:") == 0
    digest.configure(True, start + 122 * MINUTE)
    assert digest.history.get("target:telegram")["state"] == "READY"


def test_restart_latest_only_and_interval_change_no_historical_flood(store):
    start = 1_800_000_000_000 // (60 * MINUTE) * (60 * MINUTE)
    digest = Digest(store, store.owner_id)
    digest.configure(True, start + MINUTE)
    digest.schedule(start + 8 * 60 * MINUTE)
    assert digest.history.count("digest-pending:") == 1

    assert digest.history.items("digest-skipped:")[0][1]["periods"] == 7
    snapshot = store.snapshot()
    store.update(snapshot.revision, snapshot.settings.changed(digest_interval_min=30))
    digest.schedule(start + 8 * 60 * MINUTE + MINUTE)
    assert digest.preference()["next_end_ms"] == start + 8 * 60 * MINUTE + 30 * MINUTE
    digest.schedule(start + 8 * 60 * MINUTE + 30 * MINUTE)
    assert digest.history.count("digest-pending:") == 1


@pytest.mark.parametrize("failure", [False, True])
async def test_preference_change_during_network_does_not_resurrect_pending(store, failure):
    start = 1_800_000_000_000 // (60 * MINUTE) * (60 * MINUTE)
    digest = Digest(store, store.owner_id)
    digest.configure(True, start + MINUTE)
    digest.schedule(start + 60 * MINUTE)

    async def send(*args, **kwargs):
        assert not store.db.in_transaction
        digest.configure(False, start + 60 * MINUTE + 1)
        if failure:
            raise TelegramError("uncertain acknowledgement")
        return SimpleNamespace(message_id=9)

    await digest.send_due(SimpleNamespace(send_message=send), start + 60 * MINUTE)
    candidate = digest.history.items("digest-outbox:")[0][1]
    assert candidate["state"] == (
        "CANCELED_AFTER_FAILED_ATTEMPT" if failure else "DELIVERED_PRIOR_PREFERENCE"
    )
    assert digest.history.count("digest-pending:") == 0 and digest.preference()["enabled"] is False


@pytest.mark.parametrize("bounded", [False, True])
def test_unique_episode_counts_do_not_inflate_activity_or_overlapping_groups(tmp_path, bounded):
    from contextlib import closing

    from cryptowatcher.config import OwnerSettings
    from cryptowatcher.digest import text
    from cryptowatcher.market_storage import enable
    from cryptowatcher.storage import Store

    start = 1_800_000_000_000
    with closing(Store(tmp_path / "digest-episodes.db", 233382575)) as store:
        store.initialize(OwnerSettings())
        if bounded:
            enable(store.db)
            store.bounded = True
        store.clock_ms = lambda: start
        history = History(store)
        for index, (family, tf, symbol, episode) in enumerate(
            [
                ("price", "1m", "BTCUSDT", "first"),
                ("price", "3m", "BTCUSDT", "first"),
                ("composite", "1m", "BTCUSDT", "first"),
                ("price", "1m", "ETHUSDT", "second"),
            ]
        ):
            with store.db:
                history.version(
                    "event",
                    dict(
                        event_id=identity(f"digest-confirmation/{index}"),
                        revision=1,
                        episode_id=identity("digest-episode/" + episode),
                        market_observation_ms=start + MINUTE,
                        available_at_ms=start + MINUTE,
                        market_type="spot",
                        detector_id=family,
                        symbol=symbol,
                        timeframe=tf,
                        direction="UP",
                        strength=index + 1,
                        reference_price=100,
                        reference_time_ms=start,
                    ),
                )
        before = store.db.total_changes
        snapshot = aggregate(store, start, start + 2 * MINUTE, start + 2 * MINUTE)
        assert store.db.total_changes == before
        assert snapshot["event_count"] == snapshot["episode_count"] == 2
        assert snapshot["active_instruments"] == 2
        assert snapshot["groups_overlap"] is True
        assert {r["detector"]: r["events"] for r in snapshot["groups"]} == {
            "price": 2,
            "composite": 1,
        }
        assert {r["instrument"]: r["events"] for r in snapshot["most_active_instruments"]} == {
            "spot/BTCUSDT": 1,
            "spot/ETHUSDT": 1,
        }
        assert snapshot["activity_metric"] == "UNIQUE_EPISODES"
        rendered = text(snapshot)
        assert "Эпизодов: 2" in rendered and "Группы пересекаются" in rendered
