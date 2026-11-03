"""Finite verified episode materialization memo inside one producer operation.

Every hit follows a fresh SQLite row read. Only the exact validated bytes can be
reused; linked snapshot bodies are never memoized as a complete episode.
"""

import copy
from collections import OrderedDict
from typing import Any


class EpisodeReads:
    def __init__(self, stats: Any) -> None:
        self.entries: OrderedDict[Any, Any] = OrderedDict()
        self.bytes = 0
        self.stats = stats

    def clear(self) -> Any:
        self.entries.clear()
        self.bytes = 0

    def key(self, key: Any, row: Any) -> Any:
        return key, tuple(row)

    def checked(self, key: Any, row: Any) -> Any:
        item = self.entries.get(self.key(key, row))
        self.stats["episode_row_hits" if item is not None else "episode_row_misses"] += 1
        return item is not None

    def remember(self, key: Any, row: Any) -> Any:
        identity = self.key(key, row)
        if identity in self.entries:
            return
        # Both retained SQL text and its parsed public copy are bounded.
        size = 3 * (
            len(key.encode())
            + sum(
                len(v.encode()) if isinstance(v, str) else len(v) for v in row if isinstance(v, (str, bytes))
            )
        )
        if size > 1024**2:
            return
        while self.entries and (len(self.entries) >= 32 or self.bytes + size > 1024**2):
            _, (old_size, _) = self.entries.popitem(last=False)
            self.bytes -= old_size
        self.entries[identity] = size, None
        self.bytes += size
        self.stats["episode_peak_bytes"] = max(self.stats["episode_peak_bytes"], self.bytes)

    def record(self, key: Any, row: Any, value: Any = None) -> Any:
        identity = self.key(key, row)
        item = self.entries.get(identity)
        if item is None:
            return None
        size, previous = item
        if value is None:
            if previous is not None:
                self.stats["episode_record_hits"] += 1
                return copy.deepcopy(previous)
            return None
        self.entries[identity] = size, copy.deepcopy(value)
        return None


def current(history: Any, key: Any) -> Any:
    db = history.store.db
    batch = getattr(db, "hot", None)
    if (
        batch is not None
        and batch.history.store is history.store
        and batch.owner == (history.store.owner_id, history.store.readonly)
        and db.in_transaction
        and not history.store.readonly
        and key.startswith(("episode:", "episode-head:"))
    ):
        return batch.episodes
    return None
