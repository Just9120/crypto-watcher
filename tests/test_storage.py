import json
import sqlite3
from pathlib import Path

import pytest

from cryptowatcher.config import OwnerSettings
from cryptowatcher.lease import RuntimeLease
from cryptowatcher.storage import RevisionConflict, StateError, Store, migrate_legacy

OWNER = 233382575


@pytest.mark.parametrize("history_writer", [False, True])
def test_runtime_replay_skips_writes_and_changed_payload_keeps_row_identity(store, history_writer):
    from cryptowatcher.events import PREFIX, History

    history = History(store)
    key = "fixture:pending-delivery"
    sql_key = PREFIX + key if history_writer else key

    def write(value):
        with store.db:
            if history_writer:
                history.put(key, value)
            else:
                store.put_runtime(key, value)

    write({"value": 1, "text": "история"})
    rowid = store.db.execute("SELECT rowid FROM runtime WHERE key=?", (sql_key,)).fetchone()[0]
    changes = store.db.total_changes
    wal = Path(str(store.path) + "-wal")
    wal_size = wal.stat().st_size
    for _ in range(250):
        write({"text": "история", "value": 1})
    assert store.db.total_changes == changes
    assert wal.stat().st_size == wal_size
    write({"value": 1.0, "text": "история"})
    assert store.db.total_changes == changes + 1
    assert store.db.execute("SELECT rowid FROM runtime WHERE key=?", (sql_key,)).fetchone()[0] == rowid
    with pytest.raises(RuntimeError, match="rollback"):
        with store.db:
            history.put("fixture:rollback", {"new": True})
            if history_writer:
                history.put(key, {"value": 2})
            else:
                # The caller owns this transaction; exercise the same live SQL without a nested commit.
                from cryptowatcher.storage import RUNTIME_UPSERT, encode

                store.db.execute(RUNTIME_UPSERT, (key, encode({"value": 2})))
            raise RuntimeError("rollback")
    assert store.get_runtime(sql_key) == {"value": 1.0, "text": "история"}
    assert store.get_runtime(PREFIX + "fixture:rollback") is None


def migration(tmp_path: Path, settings=None, *, payload=None):
    source = tmp_path / "legacy.json"
    raw = payload or {
        "version": 2,
        "chats": {
            str(OWNER): {
                "settings": settings or {},
                "enabled": False,
                "mutes": {"CMC:BTC": 2000000000},
                "baselines": {"BYBIT:BTCUSDT": {"last_alert": 100}},
            },
            "999": {"settings": {"price_move_min": "invalid-other-chat"}},
        },
    }
    source.write_text(json.dumps(raw), encoding="utf-8")
    before = source.read_bytes()
    result = migrate_legacy(source, tmp_path / "store.db", OWNER, tmp_path / "backups", OwnerSettings())
    assert source.read_bytes() == before
    assert next((tmp_path / "backups").glob("*.json")).read_bytes() == before
    return source, result


@pytest.mark.parametrize(
    "settings,mode,pairs",
    [
        ({"bybit_pairs": ["BTC", "ETH", "BTC"], "bybit_top_limit": 0}, "custom", ("BTCUSDT", "ETHUSDT")),
        ({"watchlist": ["SOL"], "bybit_top_limit": 100}, "top", ("SOLUSDT",)),
        ({"custom_pairs": [], "bybit_pairs": ["BTC"], "alert_universe_mode": "custom"}, "custom", ()),
    ],
)
def test_raw_legacy_semantics_and_archive(tmp_path, settings, mode, pairs):
    source, result = migration(tmp_path, settings)
    assert result["archived_chats"] == 1
    store = Store(tmp_path / "store.db", OWNER)
    try:
        snapshot = store.snapshot()
        assert snapshot.settings.custom_pairs == pairs
        assert snapshot.settings.alert_universe_mode == mode
        assert snapshot.settings.signal_timeframe == "5m"
        assert not store.enabled()
        assert store.muted("BTCUSDT", 100)
        assert store.get_runtime("legacy_baselines")["BYBIT:BTCUSDT"]["last_alert"] == 100
        assert (
            "invalid-other-chat" in store.db.execute("SELECT original_json FROM legacy_import").fetchone()[0]
        )
    finally:
        store.close()
    assert (
        migrate_legacy(source, tmp_path / "store.db", OWNER, tmp_path / "backups", OwnerSettings())["status"]
        == "already_imported"
    )


@pytest.mark.parametrize(
    "content",
    [
        "{broken",
        '{"version":99,"chats":{}}',
        '{"version":2,"version":1}',
        '{"version":2,"chats":{"233382575":{"settings":{"price_move_min":NaN}}}}',
    ],
)
def test_corrupt_forward_ambiguous_state_preserved(tmp_path, content):
    source = tmp_path / "legacy.json"
    source.write_text(content)
    with pytest.raises(StateError):
        migrate_legacy(source, tmp_path / "store.db", OWNER, tmp_path / "backup", OwnerSettings())
    assert source.read_text() == content
    assert not (tmp_path / "store.db").exists()


def test_invalid_owner_does_not_replace_state(tmp_path):
    source = tmp_path / "legacy.json"
    source.write_text(
        json.dumps({"version": 2, "chats": {str(OWNER): {"settings": {"radar_poll_sec": "bad"}}}})
    )
    with pytest.raises(StateError):
        migrate_legacy(source, tmp_path / "store.db", OWNER, tmp_path / "backup", OwnerSettings())
    assert not (tmp_path / "store.db").exists()


def test_persisted_settings_cas_noop_restart_and_backup(store, infrastructure, tmp_path):
    first = store.snapshot()
    updated = store.update(first.revision, first.settings.changed(price_move_min=7, custom_pairs=()))
    assert updated.revision == first.revision + 1
    assert store.update(updated.revision, updated.settings).revision == updated.revision
    with pytest.raises(RevisionConflict):
        store.update(first.revision, first.settings.changed(price_move_min=9))
    store.put_runtime("pending_work", {"kind": "future_worker", "reference": 123})
    destination = tmp_path / "restored.db"
    store.backup(destination)
    restored = Store(destination, infrastructure.owner_id, readonly=True)
    try:
        assert restored.snapshot() == updated
        assert restored.get_runtime("pending_work") == {"kind": "future_worker", "reference": 123}
    finally:
        restored.close()
    reopened = Store(infrastructure.database, infrastructure.owner_id)
    try:
        reopened.initialize(OwnerSettings(price_move_min=100))
        assert reopened.snapshot() == updated
    finally:
        reopened.close()


def test_unknown_database_and_owner_preserved(tmp_path, store, infrastructure):
    with pytest.raises(StateError, match="owner mismatch"):
        Store(infrastructure.database, 999)
    path = tmp_path / "future.db"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version=99")
    connection.close()
    before = path.read_bytes()
    with pytest.raises(StateError):
        Store(path, OWNER)
    assert path.read_bytes() == before


def test_existing_state_requires_explicit_migration(tmp_path, infrastructure):
    infrastructure.legacy_state.write_text('{"version":2,"chats":{}}')
    instance = Store(infrastructure.database, OWNER)
    try:
        with pytest.raises(StateError, match="explicit migration"):
            instance.initialize(OwnerSettings(), legacy=infrastructure.legacy_state)
        assert instance.db.execute("SELECT COUNT(*) FROM owner_settings").fetchone()[0] == 0
    finally:
        instance.close()


def test_single_writer_lease(tmp_path):
    path = tmp_path / "store.db"
    with RuntimeLease(path):
        with pytest.raises(StateError, match="writer"):
            with RuntimeLease(path):
                pass
    with RuntimeLease(path):
        pass


def test_numeric_legacy_cooldown_normalized_without_modifying_archive(tmp_path):
    _, _ = migration(
        tmp_path,
        payload={
            "version": 2,
            "chats": {str(OWNER): {"settings": {}, "baselines": {"BYBIT:BTCUSDT": {"last_alert": "100"}}}},
        },
    )
    store = Store(tmp_path / "store.db", OWNER)
    try:
        assert store.get_runtime("legacy_baselines")["BYBIT:BTCUSDT"]["last_alert"] == 100.0
        raw = json.loads(store.db.execute("SELECT original_json FROM legacy_import").fetchone()[0])
        assert raw["chats"][str(OWNER)]["baselines"]["BYBIT:BTCUSDT"]["last_alert"] == "100"
    finally:
        store.close()
