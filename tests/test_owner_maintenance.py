import sqlite3
from contextlib import closing

import pytest

from cryptowatcher.config import OwnerSettings
from cryptowatcher.reset_history import LIGHT_POLICY, prepare, private_state, verify_private
from cryptowatcher.settings_snapshot import export_settings, restore_settings
from cryptowatcher.storage import StateError, Store

OWNER = 233382575


def test_settings_snapshot_round_trip_keeps_inode_and_current_settings(tmp_path):
    source = tmp_path / "source.sqlite3"
    snapshot = tmp_path / "settings.sqlite3"
    copied = tmp_path / "copied.sqlite3"
    restored = tmp_path / "restored.sqlite3"
    with closing(Store(source, OWNER)) as current:
        current.initialize(OwnerSettings(watchlist=("spot/BTCUSDT",)))
        current.put_runtime("radar:v1:event-counter", 10)
        first = export_settings(source, snapshot, OWNER)
        inode = snapshot.stat().st_ino
        current.set_enabled(False)
        current.mute("spot/BTCUSDT", 2_000_000_000)
        expected = private_state(current)

    second = export_settings(source, snapshot, OWNER)
    assert first["sha256"] != second["sha256"]
    assert snapshot.stat().st_ino == inode
    assert second["snapshot_bytes"] < 64 * 1024
    with closing(sqlite3.connect(snapshot)) as db, closing(sqlite3.connect(copied)) as copy:
        db.backup(copy)
    assert restore_settings(copied, restored, OWNER)["sha256"] == second["sha256"]
    with closing(Store(restored, OWNER, readonly=True)) as current:
        assert private_state(current) == expected
        assert current.get_runtime("radar:v1:event-counter") is None
    with pytest.raises(StateError, match="destination already exists"):
        restore_settings(snapshot, source, OWNER)
    with pytest.raises(StateError, match="separate"):
        export_settings(source, source, OWNER)
    with pytest.raises(StateError, match="owner or size"):
        restore_settings(snapshot, tmp_path / "foreign.sqlite3", OWNER + 1)


def test_explicit_light_cleanup_keeps_private_state_and_light_model(tmp_path):
    source = tmp_path / "source.sqlite3"
    candidate = tmp_path / "fresh.sqlite3"
    recovery = tmp_path / "private.json"
    generation = {"id": "c" * 32, "model": LIGHT_POLICY}
    with closing(Store(source, OWNER)) as current:
        current.initialize(OwnerSettings(watchlist=("spot/BTCUSDT",)))
        current.set_enabled(False)
        current.mute("spot/BTCUSDT", 2_000_000_000)
        current.put_runtime("history_generation", generation)
        current.put_runtime("radar:v1:event-counter", 10000)
        expected = private_state(current)
    with pytest.raises(StateError, match="explicit matching"):
        prepare(
            source,
            candidate,
            recovery,
            OWNER,
            expected_generation=generation["id"],
            discard_test_history=True,
        )
    with pytest.raises(StateError, match="predecessor generation mismatch"):
        prepare(
            source,
            candidate,
            recovery,
            OWNER,
            expected_generation="d" * 32,
            discard_test_history=True,
            clear_light_test_history=True,
        )
    assert not candidate.exists() and not recovery.exists()
    receipt = prepare(
        source,
        candidate,
        recovery,
        OWNER,
        expected_generation=generation["id"],
        discard_test_history=True,
        clear_light_test_history=True,
    )
    assert receipt["generation"]["model"] == LIGHT_POLICY
    assert receipt["generation"]["id"] != generation["id"]
    assert verify_private(candidate, recovery, OWNER)["state"] == "PASS"
    with closing(Store(candidate, OWNER, readonly=True)) as fresh:
        assert private_state(fresh) == expected
        assert fresh.get_runtime("radar:v1:event-counter") is None
    with closing(Store(source, OWNER, readonly=True)) as current:
        assert current.get_runtime("history_generation") == generation
        assert current.get_runtime("radar:v1:event-counter") == 10000
