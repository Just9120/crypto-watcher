"""Finite fresh/historical scheduling over the canonical durable agent queues."""

from __future__ import annotations

from typing import Any

from .events import PREFIX, History
from .storage import StateError

STAGES = {"intent": "agent:intent-due:", "send": "agent:send-due:"}


class Ready:
    def __init__(self, history: History) -> None:
        self.history, self.store = history, history.store
        self.cursors: dict[str, str | None] = {}
        for stage, prefix in STAGES.items():
            if history.items(prefix, 1):
                self.cursors[stage] = PREFIX + prefix

    @staticmethod
    def root(stage: str, historical: bool) -> str:
        return "agent:ready:" + stage + (":history:" if historical else ":fresh:")

    @classmethod
    def index(cls, stage: str, key: str, historical: bool) -> str:
        if not key.startswith(STAGES[stage]):
            raise StateError("Invalid canonical agent queue binding")
        return cls.root(stage, historical) + key[len(STAGES[stage]) :]

    def publish(
        self, stage: str, key: str, historical: bool, event: str, fresh: int, previous: bool | None = None
    ) -> None:
        if previous is not None and previous != historical:
            self.history.delete(self.index(stage, key, previous))
        self.history.put(
            self.index(stage, key, historical),
            {
                "key": key,
                "event_id": event,
                "fresh_until_ms": fresh,
            },
        )

    def remove(self, stage: str, key: str, historical: bool) -> None:
        self.history.delete(self.index(stage, key, historical))

    def historical_turn(self) -> bool:
        value = self.history.get("agent:fair-turn", 0)
        if type(value) is not int or not 0 <= value < 4:
            raise StateError("Unknown agent scheduling state; preserved")
        return value == 3

    def advance(self) -> None:
        value = self.history.get("agent:fair-turn", 0)
        if type(value) is not int or not 0 <= value < 4:
            raise StateError("Unknown agent scheduling state; preserved")
        self.history.put("agent:fair-turn", (value + 1) % 4)

    def mark_historical(self, key: str, record: dict[str, Any]) -> None:
        record["historical"] = True
        if self.store.bounded:
            record["episode"] = self.history.revision_reference("episode", record["episode"])
            record["outcomes"] = [
                self.history.revision_reference("outcome", value) for value in record["outcomes"]
            ]
        self.history.put(key, record)

    def recover(self, stage: str, now: int) -> None:
        """At most eight old canonical bindings per call, including after restart."""
        prefix = STAGES[stage]
        if stage not in self.cursors:
            if any(self.history.items(self.root(stage, old), 1) for old in (False, True)):
                return
            if not self.history.items(prefix, 1):
                return
            self.cursors[stage] = PREFIX + prefix
        after = self.cursors[stage]
        rows = list(self.store.scan_runtime(PREFIX + prefix, limit=8, after=after))
        with self.store.db:
            for raw_key, binding in rows:
                key = raw_key[len(PREFIX) :]
                if stage == "intent":
                    record = self.history.get(binding["key"])
                    if record is None:
                        self.history.delete(key)
                        continue
                    event = self.history.metadata(
                        f"event:{record['event_id']}:{record['event_revision']:010d}"
                    )
                    if event is None:
                        raise StateError("Missing retained agent intent event")
                    old = record["historical"] or now > event["fresh_until_ms"]
                    if type(old) is not bool:
                        raise StateError("Invalid retained agent intent class")
                    if old != record["historical"]:
                        self.mark_historical(binding["key"], record)
                    event_id, fresh = record["event_id"], event["fresh_until_ms"]
                else:
                    record = self.history.get("agent:outbox:" + binding["delivery_id"])
                    if record is None:
                        self.history.delete(key)
                        continue
                    old = record["kind"] == "HISTORICAL_OUTCOME_UPDATE"
                    event_id, fresh = record["event_id"], record["expires_ms"]
                self.publish(stage, key, old, event_id, fresh)
        if len(rows) < 8:
            self.cursors.pop(stage, None)
        elif rows:
            self.cursors[stage] = rows[-1][0]

    def pick(self, stage: str, now: int) -> tuple[str, dict[str, Any]] | None:
        self.recover(stage, now)
        order = (True, False) if self.historical_turn() else (False, True)
        for old in order:
            for _ in range(8):
                entries = self.history.items(self.root(stage, old), 1)
                if not entries:
                    break
                index, value = entries[0]
                key = value["key"]
                if self.index(stage, key, old) != index:
                    raise StateError("Conflicting agent ready index")
                due = int(key[len(STAGES[stage]) :].partition(":")[0])
                if due > now:
                    break
                binding = self.history.get(key)
                if binding is None:
                    with self.store.db:
                        self.history.delete(index)
                    continue
                if stage == "intent" and not old and now > value["fresh_until_ms"]:
                    with self.store.db:
                        record = self.history.get(binding["key"])
                        if record is None:
                            self.history.delete(key)
                            self.history.delete(index)
                            continue
                        self.mark_historical(binding["key"], record)
                        self.remove(stage, key, False)
                        self.publish(stage, key, True, record["event_id"], value["fresh_until_ms"])
                    continue
                return key, binding
        return None
