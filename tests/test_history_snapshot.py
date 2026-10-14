import time

import pytest

from cryptowatcher import history_snapshot
from cryptowatcher.events import History
from cryptowatcher.storage import StateError, Store

from .test_history_codec import large_record


def test_live_writer_can_advance_while_candidate_preserves_exact_pinned_snapshot(
    store, tmp_path, monkeypatch
):
    history = History(store)
    original = large_record("event")
    with store.db:
        history.version("event", original)
    real = history_snapshot.copy_snapshot

    def concurrent(source, destination, owner, maximum, deadline):
        # Source has already pinned a read transaction; the independent writer is not stopped.
        with store.db:
            history.version("event", large_record("event", revision=2))
        return real(source, destination, owner, maximum, deadline)

    monkeypatch.setattr(history_snapshot, "copy_snapshot", concurrent)
    destination = tmp_path / "candidate.sqlite3"
    result = history_snapshot.snapshot(store.path, destination, 233382575, 1, time.monotonic() + 10, 1048576)
    candidate = Store(destination, 233382575, readonly=True)
    try:
        assert History(candidate).get("event-head:one") == original
        assert History(candidate).get("event:one:0000000002") is None
        assert history.get("event-head:one")["revision"] == 2
        assert result["source_full_physical_integrity"] == "NOT_CONFIRMED"
        assert result["promotion_eligible"] is False and result["full_logical_row_readback"] == "PASS"
        assert candidate.snapshot() == store.snapshot()
    finally:
        candidate.close()


@pytest.mark.parametrize("fault", ["owner", "revision", "existing", "same_source", "deadline", "size"])
def test_snapshot_refuses_unsafe_target_or_wrong_owner_revision(store, tmp_path, fault):
    destination = tmp_path / "new.sqlite3"
    owner = 233382575
    revision = 1
    maximum = 1048576
    deadline = time.monotonic() + 10
    if fault == "owner":
        owner = 7
    elif fault == "revision":
        revision = 2
    elif fault == "existing":
        destination.write_bytes(b"preserve")
    elif fault == "same_source":
        destination = store.path
    elif fault == "deadline":
        deadline = time.monotonic() + 1000
    else:
        maximum = 1024
    with pytest.raises(StateError):
        history_snapshot.snapshot(store.path, destination, owner, revision, deadline, maximum)
    if fault == "existing":
        assert destination.read_bytes() == b"preserve"
    assert store.snapshot().revision == 1
