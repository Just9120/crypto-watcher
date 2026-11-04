"""Post-phase evidence must not replace a transactional consumer.

The ordinary replay reads every retained immutable event once workers have
stopped. Production callback identity, buffering and rollback rules are untouched.
Heavy diagnostic tracing is a separate explicitly instrumented mode.
"""

from __future__ import annotations

MAX_EVENTS = 20000


class Observation:
    def __init__(self, engine, *, runtime_counter=True):
        self.engine = engine
        self.runtime_counter = runtime_counter
        self.binding = self.current_binding()

    def current_binding(self):
        return (
            self.engine.on_event,
            self.engine.on_event_snapshot,
            self.engine.on_episode,
            *getattr(self.engine, "_publication_consumers", ()),
        )

    def check(self):
        current = self.current_binding()
        if len(current) != len(self.binding) or any(
            actual is not original for actual, original in zip(current, self.binding, strict=True)
        ):
            raise AssertionError("Measurement replaced the runtime consumer binding")

    def collect(self, history):
        self.check()
        if history.store.db.in_transaction:
            raise AssertionError("Evidence needs committed revisions after the phase")
        rows = history.items("event:", MAX_EVENTS + 1)
        if len(rows) > MAX_EVENTS:
            raise AssertionError("Persisted global evidence budget exceeded")
        identities = set()
        events = []
        for key, event in rows:
            identity = (event["event_id"], event["revision"])
            expected = f"event:{identity[0]}:{identity[1]:010d}"
            if key != expected or identity in identities:
                raise AssertionError("Duplicate or conflicting persisted event evidence")
            identities.add(identity)
            events.append(event)
        declared = self.engine.writer_stats["facts"] if self.runtime_counter else None
        if self.runtime_counter and len(events) != declared:
            raise AssertionError("Persisted events differ from the producer fact count")
        self.check()
        return events, {
            "mode": "PERSISTED_REVISIONS_AFTER_PHASE",
            "consumer_wiring_unchanged": True,
            "events_read": len(events),
            "producer_facts": declared,
            "fact_count_source": "RUNTIME_WRITER" if self.runtime_counter else "INDEPENDENT_ORACLE_REQUIRED",
        }
