"""Counter ledger guarantees on explicitly owned disposable SQLite."""

import sqlite3
from contextlib import closing

import pytest

from cryptowatcher.events import EventEngine
from cryptowatcher.storage import StateError, encode
from tests import counter_delta_prototype as delta
from tests.test_events import NOW, window
from tests.test_runtime_repository import opened


def initialized(path, enabled):
    context = delta.installed(enabled)
    stats = context.__enter__()
    store = opened(path, True)
    # Install namespace meter and domain source layout before activation.
    from cryptowatcher import domain_repository, runtime_meter

    runtime_meter.install(store.db)
    domain_repository.install(store)
    if enabled:
        delta.activate(store, stats)
    return context, stats, store


def snapshot(db, table, qualified=False):
    return [
        tuple(r)
        for r in db.execute("SELECT * FROM " + ("main." if qualified else "") + table + " ORDER BY 1")
    ]


def test_read_your_writes_gross_deltas_and_final_flush(tmp_path):
    context, stats, store = initialized(tmp_path / "d.sqlite3", True)
    try:
        with store.db:
            store.db.execute("INSERT INTO market_counts VALUES('test',1,10,10,0)")
            store.db.execute(
                "UPDATE market_counts SET logical_bytes=logical_bytes-3,deleted_bytes=deleted_bytes+3 WHERE category='test'"
            )
            store.db.execute(
                "UPDATE market_counts SET logical_bytes=logical_bytes+3,inserted_bytes=inserted_bytes+3 WHERE category='test'"
            )
            assert tuple(
                store.db.execute("SELECT * FROM market_counts WHERE category='test'").fetchone()
            ) == ("test", 1, 10, 13, 3)
            assert (
                sqlite3.Connection.execute(
                    store.db, "SELECT 1 FROM main.market_counts WHERE category='test'"
                ).fetchone()
                is None
            )
        assert tuple(
            store.db.execute("SELECT * FROM main.market_counts WHERE category='test'").fetchone()
        ) == ("test", 1, 10, 13, 3)
        assert stats["durable_counter_changes"] > 0
    finally:
        store.close()
        context.__exit__(None, None, None)


def test_statement_error_savepoint_rollback_and_outer_rollback(tmp_path):
    context, _, store = initialized(tmp_path / "r.sqlite3", True)
    try:
        initial = snapshot(store.db, "market_namespaces")
        with store.db:
            store.db.execute("INSERT INTO market_namespaces VALUES('test',1,10,20)")
            store.db.execute("SAVEPOINT nested")
            store.db.execute("UPDATE market_namespaces SET records=3 WHERE namespace='test'")
            store.db.execute("ROLLBACK TO nested")
            store.db.execute("RELEASE nested")
            assert tuple(
                store.db.execute("SELECT * FROM market_namespaces WHERE namespace='test'").fetchone()
            ) == ("test", 1, 10, 20)
            with pytest.raises(sqlite3.IntegrityError):
                store.db.execute("UPDATE market_namespaces SET records=-1 WHERE namespace='test'")
            assert (
                store.db.execute("SELECT records FROM market_namespaces WHERE namespace='test'").fetchone()[0]
                == 1
            )
        with pytest.raises(RuntimeError), store.db:
            store.db.execute("UPDATE market_namespaces SET records=8 WHERE namespace='test'")
            raise RuntimeError("outer rollback")
        assert (
            store.db.execute("SELECT records FROM market_namespaces WHERE namespace='test'").fetchone()[0]
            == 1
        )
        assert snapshot(store.db, "market_namespaces", True) == snapshot(store.db, "market_namespaces")
        assert initial == []
    finally:
        store.close()
        context.__exit__(None, None, None)


def test_public_data_and_counters_match_baseline(tmp_path):
    values = []
    for enabled in (False, True):
        context, stats, store = initialized(tmp_path / str(enabled), enabled)
        try:
            engine = EventEngine(store)
            for market, tf, at, price in (
                ("spot", "1m", NOW, 104),
                ("spot", "3m", NOW + 180000, 110),
                ("linear", "5m", NOW, 104),
            ):
                engine.observe(f"{market}/BTCUSDT/{tf}", window(at, tf, price), 1e6, 0.01, at, live=True)
            records = encode({k: engine.history.items(k) for k in ("event:", "episode:", "outbox:telegram:")})
            ledger = getattr(store.db, "_counter_ledger", None)
            if ledger and ledger.active:
                ledger.stop()
                store.db.commit()
            import time

            from cryptowatcher.runtime_meter import drain

            with store.db:
                while not drain(store.db, batch=256, deadline=time.monotonic() + 1):
                    pass
            values.append(
                (records, snapshot(store.db, "market_counts"), snapshot(store.db, "market_namespaces"))
            )
            if enabled:
                assert stats["activations"] == 1 and stats["flushes"] > 0
        finally:
            store.close()
            context.__exit__(None, None, None)
    assert values[0] == values[1]


def test_close_rolls_back_uncommitted_data_and_reopen_keeps_normal_writer(tmp_path):
    path = tmp_path / "reopen.sqlite3"
    context, _, store = initialized(path, True)
    with store.db:
        store.db.execute("INSERT INTO market_counts VALUES('kept',1,1,1,0)")
    store.db.execute("UPDATE market_counts SET records=99 WHERE category='kept'")
    store.close()
    context.__exit__(None, None, None)
    with closing(opened(path, True)) as again:
        assert again.db.execute("SELECT records FROM market_counts WHERE category='kept'").fetchone()[0] == 1
        assert (
            again.db.execute("SELECT 1 FROM sqlite_master WHERE name='cw_counter_originals'").fetchone()
            is None
        )
        assert again.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        with again.db:
            again.db.execute("INSERT INTO runtime VALUES('check','{}')")
        assert (
            again.db.execute("SELECT 1 FROM market_namespaces WHERE namespace='check'").fetchone() is not None
        )


def test_top_level_savepoint_and_cursor_end_flush_exact_values(tmp_path):
    context, stats, store = initialized(tmp_path / "sp.sqlite3", True)
    try:
        cursor = store.db.cursor()
        cursor.execute("SAVEPOINT outer")
        cursor.executemany("INSERT INTO market_counts VALUES(?,1,10,10,0)", [("a",), ("b",)])
        cursor.execute("RELEASE outer")
        assert not store.db.in_transaction
        with closing(sqlite3.connect(store.path)) as reader:
            assert (
                reader.execute("SELECT logical_bytes FROM market_counts WHERE category='a'").fetchone()[0]
                == 10
            )
        assert stats["flushes"] > 0
    finally:
        store.close()
        context.__exit__(None, None, None)


def test_missing_namespace_rejects_original_runtime_mutation(tmp_path):
    context, _, store = initialized(tmp_path / "missing.sqlite3", True)
    try:
        with store.db:
            store.db.execute("INSERT INTO runtime VALUES('qualified:test','{}')")
        with store.db:
            store.db.execute("DELETE FROM market_namespaces WHERE namespace='qualified'")
        before = store.db.execute("SELECT payload_json FROM runtime WHERE key='qualified:test'").fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="Missing namespace"), store.db:
            store.db.execute("UPDATE runtime SET payload_json='123' WHERE key='qualified:test'")
        assert (
            store.db.execute("SELECT payload_json FROM runtime WHERE key='qualified:test'").fetchone()[0]
            == before
        )
    finally:
        store.close()
        context.__exit__(None, None, None)


def test_ddl_deactivation_rollback_restores_transactional_ledger(tmp_path):
    context, _, store = initialized(tmp_path / "ddl.sqlite3", True)
    try:
        with store.db:
            store.db.execute("INSERT INTO market_counts VALUES('kept',1,1,1,0)")
        with pytest.raises(RuntimeError), store.db:
            store.db.execute("UPDATE market_counts SET records=7 WHERE category='kept'")
            store.db.execute("CREATE TABLE temporary_action(x)")
            raise RuntimeError("rollback DDL and source")
        assert store.db._counter_ledger.active
        with store.db:
            store.db.execute("UPDATE market_counts SET records=2 WHERE category='kept'")
        with closing(sqlite3.connect(store.path)) as reader:
            assert (
                reader.execute("SELECT records FROM market_counts WHERE category='kept'").fetchone()[0] == 2
            )
    finally:
        store.close()
        context.__exit__(None, None, None)


def test_owner_change_and_authorizer_keep_fail_closed_boundary(tmp_path):
    context, _, store = initialized(tmp_path / "owner.sqlite3", True)
    try:
        owner = store.owner_id
        with pytest.raises(StateError, match="owner"), store.db:
            store.db.execute("INSERT INTO market_counts VALUES('allowed',1,1,1,0)")
            store.owner_id = owner + 1
            store.db.execute("INSERT INTO market_counts VALUES('denied',1,1,1,0)")
        store.owner_id = owner

        def deny(action, table, *args):
            return (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_INSERT and table == "market_counts"
                else sqlite3.SQLITE_OK
            )

        store.db.set_authorizer(deny)
        with pytest.raises(sqlite3.DatabaseError, match="authoriz"), store.db:
            store.db.execute("INSERT INTO market_counts VALUES('policy',1,1,1,0)")
        store.db.set_authorizer(None)
    finally:
        store.owner_id = 233382575
        store.close()
        context.__exit__(None, None, None)


def test_process_loss_recovery_restores_last_commit_and_normal_triggers(tmp_path):
    path = tmp_path / "crash.sqlite3"
    context, _, store = initialized(path, True)
    with store.db:
        store.db.execute("INSERT INTO runtime VALUES('committed','{}')")
    expected = snapshot(store.db, "market_counts")
    store.db.execute("INSERT INTO runtime VALUES('uncommitted','{}')")
    # Simulate abrupt connection loss: do not run the prototype's close hook.
    sqlite3.Connection.close(store.db)
    context.__exit__(None, None, None)
    with delta.installed(), closing(opened(path, True)) as reopened:
        assert reopened.db.execute("SELECT 1 FROM runtime WHERE key='committed'").fetchone()
        assert not reopened.db.execute("SELECT 1 FROM runtime WHERE key='uncommitted'").fetchone()
        assert snapshot(reopened.db, "market_counts") == expected
        assert not reopened.db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='cw_counter_originals'"
        ).fetchone()
        with reopened.db:
            reopened.db.execute("INSERT INTO runtime VALUES('after_recovery','{}')")
        assert reopened.db.execute(
            "SELECT 1 FROM market_namespaces WHERE namespace='after_recovery'"
        ).fetchone()


def test_recovery_rejects_corrupt_descriptor_without_touching_source_data(tmp_path):
    path = tmp_path / "corrupt.sqlite3"
    context, _, store = initialized(path, True)
    with store.db:
        store.db.execute("UPDATE cw_counter_originals SET sha='invalid'")
    sqlite3.Connection.close(store.db)
    context.__exit__(None, None, None)
    with delta.installed(), pytest.raises(StateError, match="Corrupt counter recovery"):
        opened(path, True)


def test_concurrent_reader_sees_only_commits_and_snapshot_survives_writer(tmp_path):
    context, _, store = initialized(tmp_path / "reader.sqlite3", True)
    try:
        with store.db:
            store.db.execute("INSERT INTO market_counts VALUES('reader',1,1,1,0)")
        with closing(sqlite3.connect(store.path)) as reader:
            reader.execute("BEGIN")
            assert (
                reader.execute("SELECT records FROM market_counts WHERE category='reader'").fetchone()[0] == 1
            )
            store.db.execute("UPDATE market_counts SET records=2 WHERE category='reader'")
            assert (
                store.db.execute("SELECT records FROM market_counts WHERE category='reader'").fetchone()[0]
                == 2
            )
            assert (
                reader.execute("SELECT records FROM market_counts WHERE category='reader'").fetchone()[0] == 1
            )
            store.db.commit()
            assert (
                reader.execute("SELECT records FROM market_counts WHERE category='reader'").fetchone()[0] == 1
            )
            reader.rollback()
            assert (
                reader.execute("SELECT records FROM market_counts WHERE category='reader'").fetchone()[0] == 2
            )
    finally:
        store.close()
        context.__exit__(None, None, None)


def test_denied_commit_in_publication_does_not_flush_or_end_source(tmp_path):
    from cryptowatcher import domain_transition
    from cryptowatcher.domain_repository import History

    context, stats, store = initialized(tmp_path / "joined.sqlite3", True)
    try:
        with store.db:
            store.db.execute("BEGIN IMMEDIATE")
            with domain_transition.publication(History(store)):
                store.db.execute("INSERT INTO market_counts VALUES('joined',1,1,1,0)")
                before = stats["flushes"]
                with pytest.raises(StateError, match="cannot end"):
                    store.db.execute("COMMIT")
                assert stats["flushes"] == before and store.db.in_transaction
        assert (
            store.db.execute("SELECT records FROM main.market_counts WHERE category='joined'").fetchone()[0]
            == 1
        )
    finally:
        store.close()
        context.__exit__(None, None, None)


def test_main_qualified_read_and_write_cursor_remain_read_your_writes(tmp_path):
    context, _, store = initialized(tmp_path / "qualified.sqlite3", True)
    try:
        with store.db:
            store.db.execute("INSERT INTO market_counts VALUES('q',1,1,1,0)")
            assert (
                store.db.execute("SELECT records FROM main.market_counts WHERE category='q'").fetchone()[0]
                == 1
            )
            store.db.cursor().execute("UPDATE main.market_counts SET records=3 WHERE category='q'")
            assert store.db.execute("SELECT records FROM market_counts WHERE category='q'").fetchone()[0] == 3
        with closing(sqlite3.connect(store.path)) as reader:
            assert reader.execute("SELECT records FROM market_counts WHERE category='q'").fetchone()[0] == 3
    finally:
        store.close()
        context.__exit__(None, None, None)


def test_final_flush_error_rolls_back_original_source_transaction(tmp_path, monkeypatch):
    context, _, store = initialized(tmp_path / "flush-error.sqlite3", True)
    try:
        item = store.db._counter_ledger
        original = item.raw

        def fail(sql, parameters=()):
            if sql.startswith("INSERT INTO main."):
                raise sqlite3.OperationalError("injected final flush failure")
            return original(sql, parameters)

        with monkeypatch.context() as local:
            local.setattr(item, "raw", fail)
            with pytest.raises(sqlite3.OperationalError, match="final flush"), store.db:
                store.db.execute("INSERT INTO runtime VALUES('must_rollback','{}')")
        assert not store.db.in_transaction
        assert store.db.execute("SELECT 1 FROM runtime WHERE key='must_rollback'").fetchone() is None
        assert (
            store.db.execute("SELECT 1 FROM market_namespaces WHERE namespace='must_rollback'").fetchone()
            is None
        )
    finally:
        store.close()
        context.__exit__(None, None, None)


def test_readonly_snapshot_cannot_activate_or_mutate_counter_ledger(tmp_path):
    path = tmp_path / "readonly.sqlite3"
    context, _, store = initialized(path, True)
    with store.db:
        store.db.execute("INSERT INTO runtime VALUES('saved','{}')")
    store.close()
    context.__exit__(None, None, None)
    from cryptowatcher.storage import Store

    with delta.installed() as stats, closing(Store(path, 233382575, readonly=True)) as reader:
        assert reader.db.total_changes == 0
        with pytest.raises(StateError, match="idle owned writer"):
            delta.activate(reader, stats)
        assert reader.db.total_changes == 0
        assert reader.db.execute("SELECT payload_json FROM runtime WHERE key='saved'").fetchone()[0] == "{}"


def test_existing_opaque_authorizer_keeps_baseline_path(tmp_path):
    with delta.installed() as stats, closing(opened(tmp_path / "policy.sqlite3", True)) as store:
        from cryptowatcher import domain_repository, runtime_meter

        runtime_meter.install(store.db)
        domain_repository.install(store)

        def deny(action, table, *args):
            return (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_INSERT and table == "market_counts"
                else sqlite3.SQLITE_OK
            )

        store.db.set_authorizer(deny)
        item = delta.activate(store, stats)
        assert not item.active and stats["unsupported_authorizer"] == 1
        with pytest.raises(sqlite3.DatabaseError, match="authoriz"), store.db:
            store.db.execute("INSERT INTO market_counts VALUES('denied',1,1,1,0)")
        store.db.set_authorizer(None)


@pytest.mark.parametrize("method", ["rollback", "connection_sql", "cursor_sql"])
def test_explicit_rollback_after_ddl_restores_ledger_and_next_commit(tmp_path, method):
    context, _, store = initialized(tmp_path / (method + ".sqlite3"), True)
    try:
        with store.db:
            store.db.execute("INSERT INTO market_counts VALUES('kept',1,1,1,0)")
        store.db.execute("UPDATE market_counts SET records=7 WHERE category='kept'")
        store.db.execute("CREATE TABLE rolled_back_ddl(x)")
        assert not store.db._counter_ledger.active
        if method == "rollback":
            store.db.rollback()
        elif method == "connection_sql":
            store.db.execute("ROLLBACK")
        else:
            store.db.cursor().execute("ROLLBACK")
        assert store.db._counter_ledger.active
        with store.db:
            store.db.execute("UPDATE market_counts SET records=2 WHERE category='kept'")
        with closing(sqlite3.connect(store.path)) as reader:
            assert (
                reader.execute("SELECT records FROM market_counts WHERE category='kept'").fetchone()[0] == 2
            )
    finally:
        store.close()
        context.__exit__(None, None, None)
