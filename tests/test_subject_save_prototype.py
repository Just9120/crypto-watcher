"""Subject-save guarantees on disposable SQLite; no runtime activation."""

import sqlite3
from contextlib import closing

import pytest

from cryptowatcher import domain_repository, expiry_index
from cryptowatcher.domain_repository import History
from cryptowatcher.events import EventEngine
from cryptowatcher.storage import StateError, encode
from tests import subject_save_prototype as prototype
from tests.test_events import NOW, window
from tests.test_runtime_repository import opened


def capture(path, enabled):
    with prototype.installed(enabled) as stats, closing(opened(path, True)) as store:
        domain_repository.install(store)
        engine = EventEngine(store)
        for market, tf, at, price in (
            ("spot", "1m", NOW, 104),
            ("spot", "3m", NOW + 180000, 110),
            ("linear", "5m", NOW, 104),
            ("spot", "1m", NOW + 240000, 120),
        ):
            engine.observe(f"{market}/BTCUSDT/{tf}", window(at, tf, price), 1e6, 0.01, at, live=True)
        actual = {
            prefix: engine.history.items(prefix)
            for prefix in (
                "event:",
                "episode:",
                "history:",
                "episode-event:",
                "outbox:telegram:",
                "pending:telegram:",
            )
        }
        leases = [tuple(r) for r in store.db.execute("SELECT * FROM market_lifetimes ORDER BY key")]
        assert store.db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        return encode(actual), leases, dict(stats)


def test_public_revisions_inputs_deliveries_and_leases_match(tmp_path):
    baseline, before, _ = capture(tmp_path / "base.sqlite3", False)
    actual, after, stats = capture(tmp_path / "subject.sqlite3", True)
    assert actual == baseline and after == before
    assert stats["saved"] > 0 and stats["parent_reuse"] > stats["parent_reads"]
    assert stats["peak_parents"] <= prototype.MAX_PARENTS


@pytest.fixture
def core(tmp_path):
    with prototype.installed() as stats, closing(opened(tmp_path / "core.sqlite3", True)) as store:
        domain_repository.install(store)
        yield store, History(store), stats


def root(store, history):
    identity = "00000000-0000-5000-8000-000000000001"
    with store.db:
        store.db.execute("INSERT INTO runtime VALUES (?,?)", ("radar:v1:event-head:" + identity, "{}"))
        expiry_index.put(store.db, "radar:v1:event-head:" + identity, NOW + 100000, "event-head", identity)
    return identity


def test_parent_mutation_and_caught_rollback_invalidate_proof(core):
    store, history, stats = core
    identity = root(store, history)
    from cryptowatcher import domain_transition

    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        with domain_transition.publication(history):
            history.put("service:first", {"event_id": identity})
            assert expiry_index.expires(store.db, "radar:v1:service:first") == (NOW + 100000,)
            store.db.execute("SAVEPOINT callback")
            expiry_index.renew(store.db, "radar:v1:event-head:" + identity, NOW + 200000)
            history.put("service:inside", {"event_id": identity})
            assert expiry_index.expires(store.db, "radar:v1:service:inside") == (NOW + 200000,)
            store.db.execute("ROLLBACK TO callback")
            store.db.execute("RELEASE callback")
            history.put("service:after", {"event_id": identity})
            assert expiry_index.expires(store.db, "radar:v1:service:after") == (NOW + 100000,)
    assert history.get("service:inside") is None
    assert stats["parent_invalidations"] > 0


def test_duplicate_parent_binding_is_not_hidden_by_subject(core):
    store, history, _ = core
    identity = root(store, history)
    before = list(store.db.iterdump())
    from cryptowatcher import domain_transition

    with pytest.raises(ValueError, match="Conflicting native"), store.db:
        store.db.execute("BEGIN IMMEDIATE")
        with domain_transition.publication(history):
            history.put("service:first", {"event_id": identity})
            store.db.execute(
                "INSERT INTO market_expiry_legacy VALUES (?,?,?,?)",
                ("radar:v1:event-head:" + identity, NOW + 123, "event-head", identity),
            )
            history.put("service:second", {"event_id": identity})
    assert list(store.db.iterdump()) == before


def test_owner_or_readonly_change_rejects_all_writes(core):
    store, history, _ = core
    from cryptowatcher import domain_transition

    before = list(store.db.iterdump())
    try:
        with pytest.raises(StateError, match="owner|transaction"), store.db:
            store.db.execute("BEGIN IMMEDIATE")
            with domain_transition.publication(history):
                history.put("service:first", {"marker": 1})
                store.owner_id += 1
                history.put("service:second", {"marker": 2})
    finally:
        store.owner_id -= 1
    assert list(store.db.iterdump()) == before


def test_missing_parent_guard_fails_closed(core):
    store, history, _ = core
    store.db.execute("DROP TRIGGER temp.cw_subject_market_expiry_update")
    store.db.commit()
    before = list(store.db.iterdump())
    from cryptowatcher import domain_transition

    with pytest.raises(StateError, match="parent guards"), store.db:
        store.db.execute("BEGIN IMMEDIATE")
        with domain_transition.publication(history):
            history.put("service:never", {"marker": 1})
    assert list(store.db.iterdump()) == before


def test_delta_references_validate_common_dependencies_and_rollback(core):
    store, history, _ = core

    def value(*names):
        return {"_history_storage": {"fields": {"f": {"mode": "value", "ids": list(names)}}}}

    with store.db:
        store.db.executemany("INSERT INTO market_body_counts VALUES (?,0)", [("a",), ("b",), ("c",)])
        history.put("service:refs", value("a", "b"))
    from cryptowatcher import domain_transition

    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        with domain_transition.publication(history):
            history.put("service:refs", value("b", "c"))
    assert {tuple(r) for r in store.db.execute("SELECT body,refs FROM market_body_counts")} == {
        ("a", 0),
        ("b", 1),
        ("c", 1),
    }
    with store.db:
        store.db.execute("DELETE FROM market_body_counts WHERE body='b'")
    before = list(store.db.iterdump())
    with pytest.raises(sqlite3.IntegrityError, match="Missing shared"), store.db:
        store.db.execute("BEGIN IMMEDIATE")
        with domain_transition.publication(history):
            history.put("service:refs", value("a", "b"))
    assert list(store.db.iterdump()) == before


def test_reopen_does_not_reuse_transaction_parent_proofs(tmp_path):
    path = tmp_path / "reopen.sqlite3"
    with prototype.installed() as stats:
        with closing(opened(path, True)) as store:
            domain_repository.install(store)
            history = History(store)
            identity = root(store, history)
            from cryptowatcher import domain_transition

            with store.db:
                store.db.execute("BEGIN IMMEDIATE")
                with domain_transition.publication(history):
                    history.put("service:retained", {"event_id": identity})
        with closing(opened(path, True)) as store:
            domain_repository.install(store)
            history = History(store)
            with store.db:
                expiry_index.renew(store.db, "radar:v1:event-head:" + identity, NOW + 200000)
            reads = stats["parent_reads"]
            with store.db:
                store.db.execute("BEGIN IMMEDIATE")
                with domain_transition.publication(history):
                    history.put("service:after-reopen", {"event_id": identity})
            assert stats["parent_reads"] > reads
            assert expiry_index.expires(store.db, "radar:v1:service:after-reopen") == (NOW + 200000,)


def test_fused_value_keeps_exact_freeze_refs_and_original_depth_error(core):
    _, history, _ = core
    value = {
        "nested": [
            {"_history_storage": {"fields": {"x": {"mode": "value2", "id": "a"}}}},
            {"_history_storage": {"fields": {"y": {"mode": "window", "ids": ["b", "c"]}}}},
        ]
    }
    expected = history._freeze_services(value)
    actual, refs = prototype.prepare_value(history, value, freeze=True)
    assert actual == expected and refs == prototype.references(expected) == {"a", "b", "c"}
    for _ in range(27):
        value = {"nested": value}
    with pytest.raises(StateError, match="nesting"):
        prototype.prepare_value(history, value, freeze=True)


def test_noncanonical_parent_lease_cannot_reuse_a_canonical_proof(core):
    store, history, _ = core
    identity = root(store, history)
    from cryptowatcher import domain_transition

    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        with domain_transition.publication(history):
            history.put("service:first", {"event_id": identity})
            store.db.execute(
                "UPDATE market_expiry SET category='other',expires=? WHERE event_ref=(SELECT id FROM market_event_ids WHERE external=?) AND category='event-head'",
                (NOW + 200000, identity),
            )
            history.put("service:second", {"event_id": identity})
            assert expiry_index.expires(store.db, "radar:v1:service:second") == (NOW + 200000,)
            store.db.execute("UPDATE market_expiry SET expires=? WHERE category='other'", (NOW + 300000,))
            history.put("service:third", {"event_id": identity})
            assert expiry_index.expires(store.db, "radar:v1:service:third") == (NOW + 300000,)


def test_authorizer_policy_on_original_view_is_not_bypassed_when_set_before_unit(core):
    store, history, stats = core
    identity = root(store, history)
    from cryptowatcher import domain_transition

    def deny(action, table, column, database, context):
        return (
            sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_INSERT and table == "market_lifetimes"
            else sqlite3.SQLITE_OK
        )

    store.db.set_authorizer(deny)
    before = list(store.db.iterdump())
    planned = stats["planned"]
    try:
        with pytest.raises(sqlite3.DatabaseError, match="authoriz"), store.db:
            store.db.execute("BEGIN IMMEDIATE")
            with domain_transition.publication(history):
                history.put("service:denied", {"event_id": identity})
    finally:
        store.db.set_authorizer(None)
    assert stats["planned"] == planned
    assert list(store.db.iterdump()) == before


def test_own_duplicate_lease_keeps_conflict_exception_and_rollback(core):
    store, history, _ = core
    with store.db:
        history.put("service:existing", {"created_ms": NOW})
        store.db.execute(
            "INSERT INTO market_expiry_legacy VALUES (?,?,?,NULL)",
            ("radar:v1:service:existing", NOW + 999, "service"),
        )
    before = list(store.db.iterdump())
    from cryptowatcher import domain_transition

    with pytest.raises(ValueError, match="Conflicting native"), store.db:
        store.db.execute("BEGIN IMMEDIATE")
        with domain_transition.publication(history):
            history.put("service:existing", {"created_ms": NOW, "new": True})
    assert list(store.db.iterdump()) == before


def test_lease_plan_repairs_actual_key_charge_and_preserves_previous_deadline(core):
    store, history, stats = core
    with store.db:
        history.put("service:kept", {"marker": 1})
    original = expiry_index.expires(store.db, "radar:v1:service:kept")
    with store.db:
        store.db.execute("UPDATE market_expiry SET key_bytes=1 WHERE category='service'")
    from cryptowatcher import domain_transition

    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        with domain_transition.publication(history):
            history.put("service:kept", {"marker": 2})
    assert expiry_index.expires(store.db, "radar:v1:service:kept") == original
    token = prototype.key_registry.pack("radar:v1:service:kept")
    assert store.db.execute("SELECT key_bytes FROM market_expiry WHERE category='service'").fetchone()[
        0
    ] == len(token)
    assert stats["planned"] > 0


def test_legacy_deadline_is_preserved_and_normalized_inside_same_write(core):
    store, history, _ = core
    with store.db:
        history.put("service:legacy", {"marker": 1})
        store.db.execute(
            "DELETE FROM market_expiry WHERE rid IN(SELECT id FROM market_runtime_keys WHERE external IN(?,cw_key_pack(?)))",
            ("radar:v1:service:legacy", "radar:v1:service:legacy"),
        )
        store.db.execute(
            "INSERT INTO market_expiry_legacy VALUES (?,?,?,NULL)",
            ("radar:v1:service:legacy", NOW + 999, "service"),
        )
    from cryptowatcher import domain_transition

    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        with domain_transition.publication(history):
            history.put("service:legacy", {"marker": 2})
    assert expiry_index.expires(store.db, "radar:v1:service:legacy") == (NOW + 999,)
    assert store.db.execute("SELECT COUNT(*) FROM market_expiry_legacy").fetchone()[0] == 0


def test_insert_failure_rolls_back_payload_metadata_refs_and_source_unit(core):
    store, history, _ = core
    identity = root(store, history)
    store.db.execute(
        "CREATE TRIGGER fail_subject_lease BEFORE INSERT ON market_expiry WHEN NEW.category='service' BEGIN SELECT RAISE(ABORT,'injected metadata failure'); END"
    )
    store.db.commit()
    before = list(store.db.iterdump())
    from cryptowatcher import domain_transition

    with pytest.raises(sqlite3.IntegrityError, match="injected metadata"), store.db:
        store.db.execute("BEGIN IMMEDIATE")
        with domain_transition.publication(history):
            history.put("service:failed", {"event_id": identity})
    assert list(store.db.iterdump()) == before


def test_sql_trigger_parent_change_is_visible_to_next_child(core):
    store, history, _ = core
    identity = root(store, history)
    store.db.execute(
        f"CREATE TRIGGER mutate_subject_parent AFTER INSERT ON runtime WHEN NEW.key='radar:v1:service:trigger' BEGIN UPDATE market_expiry SET expires={NOW + 200000} WHERE category='event-head'; END"
    )
    store.db.commit()
    from cryptowatcher import domain_transition

    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        with domain_transition.publication(history):
            history.put("service:first", {"event_id": identity})
            history.put("service:trigger", {"event_id": identity})
            history.put("service:next", {"event_id": identity})
            assert expiry_index.expires(store.db, "radar:v1:service:trigger") == (NOW + 200000,)
            assert expiry_index.expires(store.db, "radar:v1:service:next") == (NOW + 200000,)


def test_invalidated_parent_maps_remain_bounded(core):
    store, history, _ = core
    identity = root(store, history)
    from cryptowatcher import domain_transition

    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        with domain_transition.publication(history):
            subject = store._subject_save_watcher.active
            for number in range(100):
                history.put("service:bounded", {"event_id": identity, "number": number})
                assert len(subject.parents) <= 1
                assert len(subject.rids) <= 1 and len(subject.externals) <= 2 and len(subject.binary) <= 1
                expiry_index.renew(store.db, "radar:v1:event-head:" + identity, NOW + number + 1)
                assert not subject.parents
                assert not subject.rids and not subject.externals and not subject.binary


def test_noncanonical_parent_does_not_enter_bounded_proof_maps(core):
    store, history, _ = core
    identity = "legacy-" + "x" * 1000
    with store.db:
        store.db.execute("INSERT INTO runtime VALUES (?,?)", ("radar:v1:event-head:" + identity, "{}"))
        expiry_index.put(store.db, "radar:v1:event-head:" + identity, NOW + 100000, "event-head", identity)
    from cryptowatcher import domain_transition

    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        with domain_transition.publication(history):
            history.put("service:legacy-id", {"event_id": identity})
            subject = store._subject_save_watcher.active
            assert not subject.parents and not subject.externals


def test_oversized_reference_set_uses_original_guarantees(core, monkeypatch):
    store, history, stats = core
    refs = [f"body-{number}" for number in range(prototype.MAX_REFS + 1)]
    with store.db:
        store.db.executemany("INSERT INTO market_body_counts VALUES (?,0)", [(ref,) for ref in refs])
    from cryptowatcher import domain_transition

    called = []
    original = prototype.BASE_SAVE

    def baseline(history, key, value):
        called.append(key)
        return original(history, key, value)

    monkeypatch.setattr(prototype, "BASE_SAVE", baseline)
    value = {"_history_storage": {"fields": {"x": {"mode": "value", "ids": refs}}}}
    planned = stats["planned"]
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        with domain_transition.publication(history):
            history.put("service:wide", value)
    assert called == ["service:wide"] and stats["planned"] == planned
    assert store.get_runtime("radar:v1:service:wide") == value
    assert store.db.execute("SELECT COUNT(*) FROM market_body_counts WHERE refs=1").fetchone()[0] == len(refs)


def test_cold_mode_reauthenticates_parent_packet_inside_same_publication(core, monkeypatch):
    store, history, stats = core
    from cryptowatcher import catalogue_lifecycle, domain_transition, record_catalog
    from tests.test_record_catalog import DAY, put

    record_catalog.install(store.db)
    with store.db:
        coordinate, _, _, _, _ = put(store)
        identity = coordinate.identity
        store.db.execute("INSERT INTO market_meta VALUES ('record_catalogue_runtime','1')")
        store.db.execute("INSERT INTO runtime VALUES (?,?)", ("radar:v1:event-head:" + identity, "{}"))
        expiry_index.put(store.db, "radar:v1:event-head:" + identity, 130 * DAY, "event-head", identity)
    authenticated = []
    original = catalogue_lifecycle.domain_packets.read

    def read(db, packet, *args, **kwargs):
        authenticated.append(packet)
        if len(authenticated) > 1:
            raise StateError("injected corrupt authenticated packet")
        return original(db, packet, *args, **kwargs)

    monkeypatch.setattr(catalogue_lifecycle.domain_packets, "read", read)
    before = list(store.db.iterdump())
    with pytest.raises(StateError, match="corrupt authenticated"), store.db:
        store.db.execute("BEGIN IMMEDIATE")
        with domain_transition.publication(history):
            history.put("service:cold-first", {"event_id": identity})
            history.put("service:cold-second", {"event_id": identity})
    assert len(authenticated) >= 2
    assert stats["parent_reuse"] == 0
    assert list(store.db.iterdump()) == before
