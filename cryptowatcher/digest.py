"""UTC-period digest from shared history with independent durable Telegram delivery."""

from __future__ import annotations

import asyncio
from typing import Any

from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError

from .catalogue_sql import read_snapshot, source
from .events import PREFIX, History, identity
from .outcomes import MINUTE
from .storage import Store, json_payload


def aggregate(store: Store, start: int, end: int, now: int) -> dict[str, Any]:
    with read_snapshot(store):
        return _aggregate(store, start, end, now)


def _aggregate(store: Store, start: int, end: int, now: int) -> dict[str, Any]:
    base = (
        " FROM "
        + source(store, "event", head=True)
        + " WHERE key>=? AND key<? AND json_extract(cw_record_json(payload_json),'$.market_observation_ms')>=? AND json_extract(cw_record_json(payload_json),'$.market_observation_ms')<?"
    )
    args = (PREFIX + "event-head:", PREFIX + "event-head:~", start, end)
    groups = [
        dict(row)
        for row in store.db.execute(
            "SELECT json_extract(cw_record_json(payload_json),'$.market_type') AS market, json_extract(cw_record_json(payload_json),'$.detector_id') AS detector,COUNT(*) AS events,COUNT(DISTINCT json_extract(cw_record_json(payload_json),'$.symbol')) AS instruments"
            + base
            + " GROUP BY market,detector ORDER BY market,detector",
            args,
        )
    ]
    count = store.db.execute(
        "SELECT COUNT(*),COUNT(DISTINCT json_extract(cw_record_json(payload_json),'$.market_type') || '/' || json_extract(cw_record_json(payload_json),'$.symbol'))"
        + base,
        args,
    ).fetchone()
    strongest = [
        dict(row)
        for row in store.db.execute(
            "SELECT json_extract(cw_record_json(payload_json),'$.episode_id') AS episode_id,"
            "json_extract(cw_record_json(payload_json),'$.market_type') || '/' || json_extract(cw_record_json(payload_json),'$.symbol') AS instrument,"
            "json_extract(cw_record_json(payload_json),'$.direction') AS direction,MAX(json_extract(cw_record_json(payload_json),'$.strength')) AS strength,COUNT(*) AS events"
            + base
            + " GROUP BY episode_id,instrument,direction ORDER BY strength DESC,episode_id LIMIT 5",
            args,
        )
    ]
    activity = [
        dict(row)
        for row in store.db.execute(
            "SELECT json_extract(cw_record_json(payload_json),'$.market_type') || '/' || "
            "json_extract(cw_record_json(payload_json),'$.symbol') AS instrument,COUNT(*) AS events"
            + base
            + " GROUP BY instrument ORDER BY events DESC,instrument LIMIT 5",
            args,
        )
    ]
    ongoing = [
        History(store).resolve(row[0][len(PREFIX) :], json_payload(row[1]))
        for row in store.db.execute(
            "SELECT key,payload_json FROM "
            + source(store, "episode", head=True)
            + " WHERE key>=? AND key<? AND json_extract(cw_record_json(payload_json),'$.state')='ACTIVE' AND json_extract(cw_record_json(payload_json),'$.available_at_ms')<=? ORDER BY json_extract(cw_record_json(payload_json),'$.strength') DESC,key LIMIT 5",
            (PREFIX + "episode-head:", PREFIX + "episode-head:~", now),
        )
    ]
    return {
        "schema": "cryptowatcher.digest.v1",
        "start_ms": start,
        "end_ms": end,
        "snapshot_ms": now,
        "event_count": count[0],
        "active_instruments": count[1],
        "groups": groups,
        "strongest_episodes": strongest,
        "most_active_instruments": activity,
        "activity_metric": "SAVED_EVENT_OBSERVATIONS",
        "ongoing_episodes": ongoing,
        "period": "[start,end)",
        "ongoing_at": "snapshot_ms",
    }


def text(snapshot: dict[str, Any]) -> str:
    from .telegram_views import DIRECTION, FAMILY, amount, market, when

    lines = [
        f"📋 Дайджест · {when(snapshot['start_ms'])} — {when(snapshot['end_ms'])}",
        f"Событий: {snapshot['event_count']} · инструментов: {snapshot['active_instruments']}",
        "Оба рынка; сохранённые наблюдения включают исторические и без уведомлений.",
    ]
    lines += [
        f"{market(r['market'])} · {FAMILY.get(r['detector'], r['detector'])}: {r['events']} событий / {r['instruments']} инструментов"
        for r in snapshot["groups"]
    ]
    lines.append("Самые активные инструменты · число сохранённых наблюдений:")
    lines += [f"{v['instrument']} · {v['events']}" for v in snapshot.get("most_active_instruments", [])]
    lines.append("Самые сильные эпизоды периода:")
    lines += [
        f"{e['instrument']} · {DIRECTION.get(e['direction'], e['direction'])} · интенсивность ×{amount(round(e['strength'], 2))} · наблюдений {e['events']}"
        for e in snapshot["strongest_episodes"]
    ]
    lines.append("Продолжающиеся эпизоды на момент сборки:")
    lines += [
        f"{e['instrument']} · {DIRECTION.get(e['direction'], e['direction'])} · интенсивность ×{amount(round(e['strength'], 2))}"
        for e in snapshot["ongoing_episodes"]
    ]
    return "\n".join(lines)


class Digest:
    def __init__(self, store: Store, chat_id: int) -> None:
        if chat_id != store.owner_id:
            raise ValueError("Digest target must be owner's private chat")
        self.store, self.chat_id, self.history = store, chat_id, History(store)
        self.lock = asyncio.Lock()

    def preference(self) -> dict[str, Any]:
        return self.history.get("digest-preferences", {"enabled": False})

    def configure(self, enabled: bool, now: int) -> None:
        step = self.store.snapshot().settings.digest_interval_min * MINUTE
        with self.store.db:
            self.cancel_pending("DIGEST_PREFERENCE_CHANGED" if enabled else "DIGEST_DISABLED", now)
            self.history.put(
                "digest-preferences",
                {
                    "enabled": enabled,
                    "interval_min": step // MINUTE,
                    "next_end_ms": (now // step + 1) * step,
                    "changed_ms": now,
                },
            )
            if enabled:
                self.history.put("target:telegram", {"state": "READY"})
            else:
                self.cancel_pending("DIGEST_DISABLED", now)

    def cancel_pending(self, reason: str, now: int) -> None:
        for key, binding in self.history.items("digest-pending:", 1):
            candidate = self.history.get("digest-outbox:" + binding["delivery_id"])
            if candidate is None:
                self.history.delete(key)
                continue
            candidate.update(state=reason, completed_ms=now)
            self.history.put("digest-outbox:" + candidate["delivery_id"], candidate)
            self.history.delete(key)

    def schedule(self, now: int) -> None:
        pref = self.preference()
        if not pref["enabled"]:
            return
        step = self.store.snapshot().settings.digest_interval_min * MINUTE
        if pref.get("interval_min") != step // MINUTE:
            self.configure(True, now)
            return
        if now < pref["next_end_ms"]:
            return
        end = now // step * step
        delivery_id = identity(f"digest/{step}/{end - step}/{end}")
        with self.store.db:
            self.cancel_pending("SUPERSEDED_PERIOD", now)
            snapshot = aggregate(self.store, end - step, end, now)
            if self.history.get("digest-outbox:" + delivery_id) is None:
                self.history.put(
                    "digest-outbox:" + delivery_id,
                    {
                        "delivery_id": delivery_id,
                        "state": "PENDING",
                        "snapshot": snapshot,
                        "text": text(snapshot),
                        "attempts": [],
                        "next_attempt_ms": now,
                        "preference_changed_ms": pref["changed_ms"],
                    },
                )
                self.history.put("digest-pending:" + delivery_id, {"delivery_id": delivery_id})
            missed = (end - pref["next_end_ms"]) // step
            if missed:
                self.history.put(
                    "digest-skipped:" + delivery_id,
                    {
                        "from_end_ms": pref["next_end_ms"],
                        "through_end_ms": end - step,
                        "periods": missed,
                        "reason": "LATEST_PERIOD_AFTER_RESTART",
                    },
                )
            pref["next_end_ms"] = end + step
            self.history.put("digest-preferences", pref)

    async def send_due(self, bot: Any, now: int) -> None:
        async with self.lock:
            if not self.preference()["enabled"]:
                return
            for key, binding in self.history.items("digest-pending:", 1):
                candidate = self.history.get("digest-outbox:" + binding["delivery_id"])
                if candidate is None:
                    with self.store.db:
                        self.history.delete(key)
                    continue
                if (
                    candidate["next_attempt_ms"] > now
                    or self.history.get("target:telegram", {}).get("state") == "BLOCKED"
                ):
                    return
                with self.store.db:
                    if candidate["state"] == "IN_FLIGHT":
                        candidate["attempts"][-1]["result"] = "AMBIGUOUS_AFTER_RESTART"
                    candidate["state"] = "IN_FLIGHT"
                    candidate["attempts"].append({"at_ms": now, "result": "IN_FLIGHT"})
                    self.history.put("digest-outbox:" + candidate["delivery_id"], candidate)
                try:
                    message = await bot.send_message(self.chat_id, candidate["text"])
                except TelegramError as exc:
                    candidate["attempts"][-1]["result"] = type(exc).__name__
                    candidate["state"] = "RETRY"
                    with self.store.db:
                        if isinstance(exc, (Forbidden, BadRequest)):
                            self.history.put(
                                "target:telegram",
                                {"state": "BLOCKED", "reason": type(exc).__name__, "at_ms": now},
                            )
                        else:
                            retry = (
                                float(exc.retry_after)
                                if isinstance(exc, RetryAfter)
                                else min(300, 2 ** min(len(candidate["attempts"]), 8))
                            )
                            candidate["next_attempt_ms"] = now + int(retry * 1000)
                            candidate["attempts"][-1]["acknowledgement"] = (
                                "UNKNOWN" if not isinstance(exc, RetryAfter) else "REJECTED_RATE_LIMIT"
                            )
                        current = self.preference()
                        if (
                            not current["enabled"]
                            or current["changed_ms"] != candidate["preference_changed_ms"]
                        ):
                            candidate.update(state="CANCELED_AFTER_FAILED_ATTEMPT", completed_ms=now)
                            self.history.delete(key)
                        self.history.put("digest-outbox:" + candidate["delivery_id"], candidate)
                    continue
                with self.store.db:
                    candidate["attempts"][-1].update(result="ACKNOWLEDGED", message_id=message.message_id)
                    current = self.preference()
                    candidate.update(
                        state="DELIVERED"
                        if current["enabled"] and current["changed_ms"] == candidate["preference_changed_ms"]
                        else "DELIVERED_PRIOR_PREFERENCE",
                        completed_ms=now,
                    )
                    self.history.put("digest-outbox:" + candidate["delivery_id"], candidate)
                    self.history.delete(key)

    def diagnostics(self) -> dict[str, Any]:
        return {
            "preferences": self.preference(),
            "pending": self.history.count("digest-pending:"),
            "target": self.history.get("target:telegram", {"state": "READY"}),
        }
