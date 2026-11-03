"""Subject storage accounting, selected population and explicit unknowns."""

import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cryptowatcher.analytics_pool import Pool, selection_status, summary
from cryptowatcher.bot import Controller
from cryptowatcher.domain_repository import History, install
from cryptowatcher.events import EventEngine
from cryptowatcher.outcomes import OutcomeWorker
from cryptowatcher.render import outcome_text
from cryptowatcher.repository_meter import install as install_meter
from cryptowatcher.storage import StateError, Store
from cryptowatcher.storage_meter import report, sample
from tests.test_events import NOW, window
from tests.test_runtime_repository import opened


def test_actual_publication_meter_rollback_reopen_and_selected_population(tmp_path):
    path = tmp_path / "accounted.sqlite3"
    with closing(opened(path, True)) as store:
        install(store)
        worker = OutcomeWorker(store)
        pool = Pool(worker)
        pool.activate()
        engine = EventEngine(store, on_event=worker.admit)
        engine.history = History(store)
        sample(store, NOW)
        for i in range(12):
            engine.observe(f"spot/M{i}USDT/1m", window(), 2000000, 0.01, NOW, live=True)
        pool.tick(NOW)
        counts = dict(store.db.execute("SELECT category,logical_bytes FROM market_counts"))
        assert all(
            counts[key] > 0 for key in ("direct_revisions", "direct_shared", "direct_members", "pool_choices")
        )
        before = list(store.db.iterdump())
        with pytest.raises(RuntimeError), store.db:
            engine.observe("linear/ROLLBACKUSDT/1m", window(), 2000000, 0.01, NOW, live=True, commit=False)
            raise RuntimeError("rollback")
        assert list(store.db.iterdump()) == before
        value = report(store, NOW + 120000)
        assert value["subject_repository_layout"]["state"] == "MEASURED_FLOW"
        assert 0 < value["observed_selected_episodes_total"] < value["observed_episodes_total"]
        assert (
            value["observed_selected_episodes_per_day_extrapolated"]
            < value["observed_episodes_per_day_extrapolated"]
        )
        assert value["projected_working_bytes"] is not None
        assert summary(store)["admitted_total"] == value["observed_selected_episodes_total"]
        selected = value["observed_selected_episodes_total"]
    with closing(opened(path, True)) as store:
        install(store)
        assert summary(store)["admitted_total"] == selected
        assert dict(store.db.execute("SELECT category,logical_bytes FROM market_counts")) == counts
        assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_missing_ledger_aborts_write_and_invalid_admission_epoch_is_unknown(tmp_path):
    with closing(opened(tmp_path / "guard.sqlite3", True)) as store:
        install(store)
        pool = Pool(OutcomeWorker(store))
        sample(store, NOW)
        with store.db:
            store.db.execute("DELETE FROM market_counts WHERE category='direct_shared'")
        before = list(store.db.iterdump())
        with pytest.raises(sqlite3.IntegrityError, match="counter"), store.db:
            store.db.execute("INSERT INTO direct_snapshots VALUES('uncommitted','{}',0)")
        assert list(store.db.iterdump()) == before
        assert report(store, NOW + 120000)["projected_working_bytes"] is None
        with store.db:
            store.db.execute("DELETE FROM analytics_pool_totals WHERE id=1")
        assert summary(store)["state"] == "INCOMPLETE"
        value = report(store, NOW + 120000)
        assert value["observed_selected_episodes_per_day_extrapolated"] is None
        assert "pool:admission_counter_epoch" in value["subject_repository_layout"]["missing"]
        assert pool.worker.admission_policy is None


def test_counter_reversal_cannot_be_reported_as_zero_growth(tmp_path):
    with closing(opened(tmp_path / "counter.sqlite3", True)) as store:
        install(store)
        Pool(OutcomeWorker(store))
        with store.db:
            store.db.execute("UPDATE analytics_pool_totals SET admitted=10")
        sample(store, NOW)
        with store.db:
            store.db.execute("UPDATE analytics_pool_totals SET admitted=9")
        value = report(store, NOW + 120000)
        assert value["projected_working_bytes"] is None
        assert value["observed_selected_episodes_per_day_extrapolated"] is None
        assert value["subject_repository_layout"]["state"] == "INCOMPLETE"


def test_large_unmetered_population_is_preserved_without_hidden_startup_scan(tmp_path):
    with closing(opened(tmp_path / "old.sqlite3", True)) as store:
        with store.db:
            store.db.execute("CREATE TABLE custom_detail(value BLOB)")
            store.db.executemany("INSERT INTO custom_detail VALUES(?)", [(b"preserve",)] * 513)
        before = list(store.db.iterdump())
        with pytest.raises(StateError, match="explicit initialization"):
            install_meter(store, "direct", {"custom_detail": "direct_revisions"})
        assert list(store.db.iterdump()) == before


def test_fresh_legacy_physical_writer_has_pool_without_reset(infrastructure):
    with closing(Store(infrastructure.database, infrastructure.owner_id, writer_connection=True)) as store:
        from cryptowatcher.config import OwnerSettings

        store.initialize(OwnerSettings())
        point = store.snapshot()
        schema = store.db.execute("PRAGMA user_version").fetchone()[0]
        store.put_runtime("history_generation", {"id": "preserve"})
        controller = Controller(infrastructure, store, SimpleNamespace(snapshot=AsyncMock()))
        assert controller.analytics_pool is not None
        assert store.snapshot() == point
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == schema
        assert store.get_runtime("history_generation") == {"id": "preserve"}


@pytest.mark.parametrize("state", ["WAITING", "NOT_SELECTED"])
def test_unselected_result_is_not_a_pending_calculation_or_zero(tmp_path, state):
    with closing(opened(tmp_path / "display.sqlite3", True)) as store:
        install(store)
        pool = Pool(OutcomeWorker(store))
        pool.activate()
        engine = EventEngine(store, on_event=pool.worker.admit)
        engine.history = History(store)
        engine.observe("spot/BTCUSDT/1m", window(), 2000000, 0.01, NOW, live=True)
        event = engine.history.events(1)[0]
        with store.db:
            store.db.execute("UPDATE analytics_pool_choices SET status=?", (state,))
        selection = selection_status(store, event)
        shown = outcome_text(event, [None] * 4, (5, 15, 30, 60), selection=selection)
        assert "не назначены" in shown and "не входит в N" in shown
        assert "ожидается срок" not in shown and "0%" not in shown
        assert summary(store)["admitted_total"] == 0


def fixture_meter(store):
    with store.db:
        store.db.execute("CREATE TABLE meter_fixture(id INTEGER PRIMARY KEY,payload BLOB,refs INTEGER)")
    install_meter(store, "direct", {"meter_fixture": "direct_shared"})


def fixture_counts(store):
    return tuple(
        store.db.execute(
            "SELECT records,logical_bytes,inserted_bytes,deleted_bytes FROM market_counts WHERE category='direct_shared'"
        ).fetchone()
    )


def logical_row(row):
    return sum(
        0
        if value is None
        else 8
        if isinstance(value, (int, float))
        else len(value.encode("utf-8"))
        if isinstance(value, str)
        else len(value)
        for value in row
    )


@pytest.mark.parametrize("writer", [False, True])
def test_exact_bytes_and_net_flow_across_sqlite_types_rollback_and_reopen(tmp_path, writer):
    path = tmp_path / "exact.sqlite3"
    with closing(opened(path, writer)) as store:
        fixture_meter(store)
        inserted, deleted = 0, 0
        actions = [
            ("INSERT INTO meter_fixture VALUES(1,?,?)", ("Привет\x00😀", 1)),
            ("INSERT INTO meter_fixture VALUES(2,?,?)", (b"a" * 65536, None)),
            ("UPDATE meter_fixture SET refs=? WHERE id=1", (2,)),
            ("UPDATE meter_fixture SET payload=?,refs=? WHERE id=1", ("12345678", "12345678")),
            ("UPDATE meter_fixture SET payload=?,refs=? WHERE id=1", (12345678, 1.5)),
            ("UPDATE meter_fixture SET payload=?,refs=? WHERE id=1", (None, "abcdefghijklmnop")),
            ("UPDATE meter_fixture SET payload=?,refs=? WHERE id=1", (b"x" * 70000, -9)),
            ("UPDATE meter_fixture SET payload=?,refs=? WHERE id=1", (float("inf"), 2)),
            ("DELETE FROM meter_fixture WHERE id=2", ()),
        ]
        for sql, parameters in actions:
            old = sum(logical_row(r) for r in store.db.execute("SELECT * FROM meter_fixture"))
            with store.db:
                store.db.execute(sql, parameters)
            rows = list(store.db.execute("SELECT * FROM meter_fixture"))
            current = sum(logical_row(r) for r in rows)
            inserted += max(0, current - old)
            deleted += max(0, old - current)
            assert fixture_counts(store) == (len(rows), current, inserted, deleted)
        before = list(store.db.iterdump())
        with pytest.raises(RuntimeError), store.db:
            store.db.execute("UPDATE meter_fixture SET payload=?", (b"rollback",))
            raise RuntimeError("abort")
        assert list(store.db.iterdump()) == before
        expected = fixture_counts(store)
    with closing(opened(path, writer)) as store:
        install_meter(store, "direct", {"meter_fixture": "direct_shared"})
        assert fixture_counts(store) == expected
        assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_unchanged_large_blob_is_not_remeasured_and_changed_bytes_are_measured_once(tmp_path):
    with closing(opened(tmp_path / "measure.sqlite3", True)) as store:
        fixture_meter(store)
        measured = []

        def length(value):
            measured.append(len(value))
            return len(value)

        store.db.create_function("length", 1, length)
        with store.db:
            store.db.execute("INSERT INTO meter_fixture VALUES(1,?,1)", (b"a" * 65536,))
        assert measured == [65536]
        measured.clear()
        with store.db:
            store.db.execute("UPDATE meter_fixture SET refs=refs+1 WHERE id=1")
        assert measured == []
        with store.db:
            store.db.execute("UPDATE meter_fixture SET payload=? WHERE id=1", (b"b" * 70000,))
        assert sorted(measured) == [65536, 70000]
        measured.clear()
        with store.db:
            store.db.execute("DELETE FROM meter_fixture WHERE id=1")
        assert measured == [70000]


def frozen_v1(store):
    for operation in ("insert", "delete", "update"):
        store.db.execute("DROP TRIGGER cw_meter_meter_fixture_" + operation)
    store.db.execute("DELETE FROM market_meta WHERE key='direct_flow_meter_triggers'")
    store.db.commit()
    store.db.executescript((Path(__file__).parent / "fixtures" / "repository_meter_v1.sql").read_text())


def test_frozen_v1_trigger_upgrade_preserves_existing_epoch_private_state_and_counters(tmp_path):
    path = tmp_path / "upgrade.sqlite3"
    with closing(opened(path, True)) as store:
        fixture_meter(store)
        frozen_v1(store)
        with store.db:
            store.db.executemany(
                "INSERT INTO meter_fixture VALUES(?,?,?)", [(i, "сохранить", i) for i in range(600)]
            )
        private = store.snapshot()
        counts = fixture_counts(store)
        # An existing v1 population exceeds the fresh-install512-row limit.
        # Trigger-only upgrade must neither scan/reseed nor reject this history.
        install_meter(store, "direct", {"meter_fixture": "direct_shared"})
        assert store.snapshot() == private
        assert fixture_counts(store) == counts
        assert (
            store.db.execute("SELECT value FROM market_meta WHERE key='direct_flow_meter'").fetchone()[0]
            == "1"
        )
        assert (
            store.db.execute(
                "SELECT value FROM market_meta WHERE key='direct_flow_meter_triggers'"
            ).fetchone()[0]
            == "2"
        )
        with store.db:
            store.db.execute("UPDATE meter_fixture SET payload=? WHERE id=1", (b"x",))
        expected = fixture_counts(store)
    with closing(opened(path, True)) as store:
        install_meter(store, "direct", {"meter_fixture": "direct_shared"})
        assert fixture_counts(store) == expected
        assert store.snapshot() == private
        assert store.db.execute("SELECT COUNT(*) FROM meter_fixture").fetchone()[0] == 600


@pytest.mark.parametrize("damage", ["counter", "trigger", "definition", "epoch"])
def test_unknown_upgrade_state_fails_closed_without_replacing_triggers_or_data(tmp_path, damage):
    with closing(opened(tmp_path / "damaged.sqlite3", True)) as store:
        fixture_meter(store)
        frozen_v1(store)
        with store.db:
            store.db.execute("INSERT INTO meter_fixture VALUES(1,'preserve',1)")
            if damage == "counter":
                store.db.execute("DELETE FROM market_counts WHERE category='direct_shared'")
            elif damage in ("trigger", "definition"):
                store.db.execute("DROP TRIGGER cw_meter_meter_fixture_update")
                if damage == "definition":
                    store.db.execute(
                        "CREATE TRIGGER cw_meter_meter_fixture_update AFTER UPDATE ON meter_fixture BEGIN SELECT 1; END"
                    )
            else:
                store.db.execute("INSERT INTO market_meta VALUES('direct_flow_meter_triggers','999')")
        before = list(store.db.iterdump())
        with pytest.raises(StateError, match="preserved"):
            install_meter(store, "direct", {"meter_fixture": "direct_shared"})
        assert list(store.db.iterdump()) == before


def test_upgrade_ddl_failure_restores_all_old_triggers_and_meter_epoch(tmp_path):
    with closing(opened(tmp_path / "ddl-rollback.sqlite3", True)) as store:
        fixture_meter(store)
        frozen_v1(store)
        with store.db:
            store.db.execute("INSERT INTO meter_fixture VALUES(1,'keep',1)")
        before = list(store.db.iterdump())

        def authorizer(operation, name, *rest):
            if operation == sqlite3.SQLITE_CREATE_TRIGGER and name.endswith("_update"):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        store.db.set_authorizer(authorizer)
        try:
            with pytest.raises(sqlite3.DatabaseError):
                install_meter(store, "direct", {"meter_fixture": "direct_shared"})
        finally:
            store.db.set_authorizer(None)
        assert list(store.db.iterdump()) == before
        install_meter(store, "direct", {"meter_fixture": "direct_shared"})
        assert fixture_counts(store) == (1, 20, 20, 0)
