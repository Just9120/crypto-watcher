"""Durable Telegram attempts; network I/O occurs outside SQLite transactions."""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError

from .detectors import freshness
from .events import History
from .storage import Store


class TelegramDelivery:
    def __init__(
        self,
        store: Store,
        chat_id: int,
        renderer: Callable[[dict[str, Any], dict[str, Any], int], str],
        keyboard: Callable[[dict[str, Any]], Any],
        on_success: Callable[[str, int], None] | None = None,
    ) -> None:
        if chat_id != store.owner_id:
            raise ValueError("Telegram target must be the owner's private chat")
        self.store, self.chat_id = store, chat_id
        self.history = History(store)
        self.renderer, self.keyboard = renderer, keyboard
        self.on_success = on_success
        self.lock = asyncio.Lock()

    def finish(self, candidate: dict[str, Any], state: str, now_ms: int) -> None:
        if candidate["state"] == "IN_FLIGHT" and candidate["attempts"][-1]["result"] == "IN_FLIGHT":
            candidate["attempts"][-1]["result"] = "AMBIGUOUS_AFTER_RESTART"
        candidate.update(state=state, completed_ms=now_ms)
        self.history.put("outbox:telegram:" + candidate["delivery_id"], candidate)
        self.history.delete(candidate["pending_key"])
        episode = self.history.get("episode-head:" + candidate["episode_id"])
        if episode["pending_notification"] == candidate["delivery_id"]:
            episode["pending_notification"] = None
        if state.startswith("DELIVERED"):
            snapshot = candidate["episode_snapshot"]
            episode["last_notified"] = {
                **candidate["notification_state"],
                "delivery_id": candidate["delivery_id"],
                "at_ms": now_ms,
                "available_cutoff_ms": snapshot["available_at_ms"],
            }
        episode["revision"] += 1
        episode["available_at_ms"] = now_ms
        self.history.version("episode", episode)

    async def send_due(self, bot: Any, now_ms: int, *, clock: Callable[[], int] | None = None) -> None:
        clock = clock or (lambda: now_ms)
        async with self.lock:
            # A bounded batch yields to commands, heartbeat and ingestion even after an outage.
            for pending, binding in self.history.items("pending:telegram:", 20):
                candidate = self.history.get("outbox:telegram:" + binding["delivery_id"])
                if candidate is None:
                    with self.store.db:
                        self.history.delete(pending)
                    continue
                if candidate["state"] not in ("PENDING", "RETRY", "IN_FLIGHT"):
                    continue
                settings = self.store.snapshot()
                events = [
                    self.history.get(f"event:{key}:{candidate['event_revisions'][key]:010d}")
                    for key in candidate["event_ids"]
                ]
                fresh = [event for event in events if event["live"] and freshness(event, clock()) == "FRESH"]
                if not fresh:
                    with self.store.db:
                        self.finish(candidate, "EXPIRED_LIVE", clock())
                    continue
                if candidate["settings_revision"] != settings.revision:
                    with self.store.db:
                        self.finish(candidate, "OBSOLETE_CONFIGURATION", clock())
                    continue
                event = max(fresh, key=lambda e: (e["market_observation_ms"], e["strength"]))
                if "event_id" in candidate:
                    event = self.history.get(
                        f"event:{candidate['event_id']}:{candidate['event_revisions'][candidate['event_id']]:010d}"
                    )
                    if freshness(event, clock()) != "FRESH":
                        with self.store.db:
                            self.finish(candidate, "EXPIRED_LIVE", clock())
                        continue
                instrument = event["market_type"] + "/" + event["symbol"]
                muted = self.store.muted(instrument, clock() / 1000) or (
                    event["market_type"] == settings.settings.bybit_category
                    and self.store.muted(event["symbol"], clock() / 1000)
                )
                if not self.store.enabled() or muted:
                    with self.store.db:
                        self.finish(candidate, "MUTED" if muted else "ALERTS_DISABLED", clock())
                    continue
                target = self.history.get("target:telegram", {"state": "READY"})
                if target["state"] == "BLOCKED" or candidate["next_attempt_ms"] > clock():
                    continue
                with self.store.db:
                    if candidate["state"] == "IN_FLIGHT":
                        candidate["attempts"][-1]["result"] = "AMBIGUOUS_AFTER_RESTART"
                    if "text" not in candidate:
                        candidate["event_id"] = event["event_id"]
                        candidate["text"] = self.renderer(event, candidate, clock())
                    candidate["state"] = "IN_FLIGHT"
                    candidate["attempts"].append({"at_ms": clock(), "result": "IN_FLIGHT"})
                    self.history.put("outbox:telegram:" + candidate["delivery_id"], candidate)
                # Persist before send. The same frozen text/identity is retained after an ambiguous ack.
                try:
                    message = await bot.send_message(
                        self.chat_id,
                        candidate["text"],
                        reply_markup=self.keyboard(event),
                    )
                except TelegramError as exc:
                    with self.store.db:
                        candidate["attempts"][-1].update(result=type(exc).__name__)
                        if isinstance(exc, (Forbidden, BadRequest)):
                            self.history.put(
                                "target:telegram",
                                {"state": "BLOCKED", "reason": type(exc).__name__, "at_ms": clock()},
                            )
                            candidate["state"] = "RETRY"
                        else:
                            retry = (
                                float(exc.retry_after)
                                if isinstance(exc, RetryAfter)
                                else min(300, 2 ** min(len(candidate["attempts"]), 8))
                            )
                            candidate.update(state="RETRY", next_attempt_ms=clock() + int(retry * 1000))
                            if not isinstance(exc, RetryAfter):
                                candidate["attempts"][-1]["acknowledgement"] = "UNKNOWN"
                        self.history.delete(candidate["pending_key"])
                        candidate["pending_key"] = (
                            f"pending:telegram:{candidate['next_attempt_ms']:016d}:{candidate['delivery_id']}"
                        )
                        self.history.put(candidate["pending_key"], {"delivery_id": candidate["delivery_id"]})
                        self.history.put("outbox:telegram:" + candidate["delivery_id"], candidate)
                    self.store.put_runtime(
                        "telegram_delivery",
                        {"state": "FAILED", "error_class": type(exc).__name__, "at": clock() / 1000},
                    )
                    continue
                with self.store.db:
                    candidate["attempts"][-1].update(result="ACKNOWLEDGED", message_id=message.message_id)
                    state = (
                        "DELIVERED"
                        if self.store.snapshot().revision == candidate["settings_revision"]
                        else "DELIVERED_PRIOR_SETTINGS"
                    )
                    self.finish(candidate, state, clock())
                self.store.put_runtime(
                    "telegram_delivery",
                    {"state": state, "delivery_id": candidate["delivery_id"], "at": clock() / 1000},
                )
                if self.on_success is not None:
                    self.on_success(candidate["episode_id"], clock())

    def resume(self) -> None:
        with self.store.db:
            self.history.put("target:telegram", {"state": "READY"})
