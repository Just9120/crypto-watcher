"""Subject storage accounting, selected population and explicit unknowns."""

import sqlite3
from contextlib import closing
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
