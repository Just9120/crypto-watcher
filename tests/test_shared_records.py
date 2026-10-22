import dataclasses
import hashlib
import sqlite3
import time
from contextlib import closing

import pytest

from cryptowatcher import domain_packets, packet_store, record_blocks, shared_records
from cryptowatcher.compact_json import pack as typed_pack
from cryptowatcher.market_storage import enable
from cryptowatcher.storage import Store
from tests.test_record_blocks import entries


def setup(store):
    enable(store.db)
    packet_store.install(store.db)
    originals = []
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        for values in (entries()[:2], entries()[2:]):
            raw = record_blocks.pack(values)
            identity = packet_store.append(store.db, "detail_packets", 130, raw)
            packet_store.retain(store.db, identity)
            originals.append((identity, raw))
    shared_records.install(store.db, store.cached_bytes)
    return originals


def prepare(store, originals):
    store.db.execute("BEGIN")
    try:
        return shared_records.prepare(store.db, [identity for identity, _ in originals])
    finally:
        store.db.rollback()


def equal(old, new):
    assert [typed_pack(r) for r in record_blocks.bodies(old)] == [
        typed_pack(r) for r in record_blocks.bodies(new)
    ]
    assert record_blocks.projections(old) == record_blocks.projections(new)


def test_actual_shared_transition_snapshot_relocation_reopen_and_collection_preserve_values(store):
    originals = setup(store)
    selected = prepare(store, originals)
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        reader.db.execute("BEGIN")
        assert packet_store.read(reader.db, originals[0][0]) == originals[0][1]
        with store.db:
            store.db.execute("BEGIN IMMEDIATE")
            group = shared_records.publish(store.db, selected, store.cached_bytes)
        for identity, old in originals:
            assert packet_store.read(reader.db, identity) == old
            assert packet_store.read(store.db, identity).startswith(shared_records.MAGIC)
            equal(old, domain_packets.read(store.db, identity, store.cached_bytes))
        reader.db.rollback()
        for identity, old in originals:
            equal(old, domain_packets.read(reader.db, identity, reader.cached_bytes))
    shared_records.validate(store.db)
    shared_records.install(store.db, store.cached_bytes)
    assert (
        store.db.execute(
            "SELECT logical_bytes FROM market_counts WHERE category='detail_packets'"
        ).fetchone()[0]
        == 96
    )
    assert packet_store.summary(store.db)["live_bytes"] == 96
    with closing(Store(store.path, store.owner_id)) as reopened:
        for identity, old in originals:
            equal(old, domain_packets.read(reopened.db, identity, reopened.cached_bytes))
        with reopened.db:
            reopened.db.execute("BEGIN IMMEDIATE")
            packet_store.relocate(reopened.db, originals[0][0])
            packet_store.release(reopened.db, originals[0][0])
            packet_store.collect(reopened.db)
            assert shared_records.collect(reopened.db) == 0
            equal(originals[1][1], domain_packets.read(reopened.db, originals[1][0]))
            packet_store.release(reopened.db, originals[1][0])
            packet_store.collect(reopened.db)
            assert shared_records.collect(reopened.db) == 1
        assert reopened.db.execute("SELECT COUNT(*) FROM market_shared_members").fetchone()[0] == 0
        assert (
            reopened.db.execute("SELECT COUNT(*) FROM market_shared_records WHERE id=?", (group,)).fetchone()[
                0
            ]
            == 0
        )
        for category in (shared_records.GROUPS, shared_records.MEMBERS, "detail_packets"):
            assert tuple(
                reopened.db.execute(
                    "SELECT records,logical_bytes FROM market_counts WHERE category=?", (category,)
                ).fetchone()
            ) == (0, 0)
        assert not reopened.db.execute("PRAGMA foreign_key_check").fetchall()
        assert reopened.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.mark.parametrize("stage", ["group", "member", "move", "resize", "second_move"])
def test_shared_failure_at_each_publication_stage_rolls_back_every_byte_and_counter(store, stage):
    originals = setup(store)
    selected = prepare(store, originals)
    if stage == "group":
        target = "AFTER INSERT ON market_shared_records"
    elif stage == "member":
        target = "AFTER INSERT ON market_shared_members"
    else:
        suffix = f" WHEN NEW.id={originals[1][0]}" if stage == "second_move" else ""
        target = f"AFTER UPDATE OF {'length' if stage == 'resize' else 'arena'} ON market_packets{suffix}"
    with store.db:
        store.db.execute(
            f"CREATE TRIGGER fail_shared {target} BEGIN SELECT RAISE(ABORT,'publication interrupted'); END"
        )
    before = list(store.db.iterdump())
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.IntegrityError, match="publication interrupted"):
            shared_records.publish(store.db, selected, store.cached_bytes)
        assert list(store.db.iterdump()) == before
    for identity, old in originals:
        assert packet_store.read(store.db, identity) == old


def test_stale_capture_changed_bindings_and_forged_values_are_refused_without_new_state(store):
    originals = setup(store)
    selected = prepare(store, originals)
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        packet_store.retain(store.db, originals[0][0])
        before = list(store.db.iterdump())
        with pytest.raises(ValueError, match="Stale"):
            shared_records.publish(store.db, selected)
        assert list(store.db.iterdump()) == before
    selected = prepare(store, originals)
    fake = dataclasses.replace(
        selected, projection=selected.projection[:-1] + bytes([selected.projection[-1] ^ 1])
    )
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        before = list(store.db.iterdump())
        with pytest.raises((sqlite3.DatabaseError, ValueError)):
            shared_records.publish(store.db, fake)
        assert list(store.db.iterdump()) == before


@pytest.mark.parametrize(
    "change", ["member_delete", "member_move", "group_body", "group_refs", "group_delete", "packet_forgery"]
)
def test_shared_sql_guards_refuse_direct_changes_and_arbitrary_packet_rewrite(store, change):
    originals = setup(store)
    selected = prepare(store, originals)
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        group = shared_records.publish(store.db, selected)
    before = list(store.db.iterdump())
    with pytest.raises(sqlite3.DatabaseError), store.db:
        if change == "member_delete":
            store.db.execute("DELETE FROM market_shared_members WHERE packet=?", (originals[0][0],))
        elif change == "member_move":
            store.db.execute("UPDATE market_shared_members SET first_slot=1")
        elif change == "group_body":
            store.db.execute("UPDATE market_shared_records SET body=zeroblob(100)")
        elif change == "group_refs":
            store.db.execute("UPDATE market_shared_records SET refs=0")
        elif change == "group_delete":
            store.db.execute("DELETE FROM market_shared_records WHERE id=?", (group,))
        else:
            store.db.execute("BEGIN IMMEDIATE")
            raw = b"arbitrary rewrite"
            old_arena = store.db.execute(
                "SELECT arena FROM market_packets WHERE id=?", (originals[0][0],)
            ).fetchone()[0]
            arena, offset = packet_store.write_segment(
                store.db, "detail_packets", 130, raw, exclude=old_arena
            )
            store.db.execute(
                "UPDATE market_packets SET arena=?,offset=?,length=?,sha256=? WHERE id=?",
                (arena, offset, len(raw), hashlib.sha256(raw).digest(), originals[0][0]),
            )
    assert list(store.db.iterdump()) == before


def test_shared_read_rechecks_actual_group_bytes_and_binding_after_pure_cache_hit(store):
    originals = setup(store)
    selected = prepare(store, originals)
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        shared_records.publish(store.db, selected)
    identity, old = originals[0]
    equal(old, domain_packets.read(store.db, identity, store.cached_bytes))
    with store.db:
        row = store.db.execute("SELECT id,body FROM market_shared_records").fetchone()
        # Simulate actual page/blob damage, bypassing the SQL guards only in
        # this owned fixture. The cached pure transform must not hide it.
        with store.db.blobopen("market_shared_records", "body", row[0], readonly=False) as blob:
            blob.seek(len(row[1]) - 1)
            blob.write(bytes([row[1][-1] ^ 1]))
    with pytest.raises(ValueError):
        domain_packets.read(store.db, identity, store.cached_bytes)


def test_shared_installer_refuses_unknown_previous_guard_atomically_and_no_automatic_activation(store):
    enable(store.db)
    packet_store.install(store.db)
    with closing(Store(store.path, store.owner_id)) as reopened:
        assert not reopened.db.execute("SELECT 1 FROM market_meta WHERE key='shared_records'").fetchone()
    with store.db:
        store.db.execute("DROP TRIGGER market_packet_move")
        store.db.execute(
            "CREATE TRIGGER market_packet_move BEFORE UPDATE ON market_packets BEGIN SELECT 1; END"
        )
    before = list(store.db.iterdump())
    with pytest.raises(ValueError, match="Unknown packet relocation"):
        shared_records.install(store.db)
    assert list(store.db.iterdump()) == before


def test_shared_outer_rollback_preserves_every_source_and_rejects_cross_day_and_duplicate_group(store):
    originals = setup(store)
    selected = prepare(store, originals)
    before = list(store.db.iterdump())
    with pytest.raises(RuntimeError), store.db:
        store.db.execute("BEGIN IMMEDIATE")
        shared_records.publish(store.db, selected)
        raise RuntimeError("outer abort")
    assert list(store.db.iterdump()) == before
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        other = packet_store.append(store.db, "detail_packets", 131, originals[0][1])
        packet_store.retain(store.db, other)
        with pytest.raises(ValueError, match="retention day"):
            shared_records.prepare(store.db, [originals[0][0], other])
        with pytest.raises(ValueError, match="Duplicate"):
            shared_records.prepare(store.db, [other, other])


def test_shared_descriptors_share_reserved_tail_and_normal_packets_keep_separate_allocation(store):
    originals = setup(store)
    selected = prepare(store, originals)
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        shared_records.publish(store.db, selected)
        tail = store.db.execute("SELECT arena FROM market_shared_tails WHERE bucket=130").fetchone()[0]
        assert {r[0] for r in store.db.execute("SELECT arena FROM market_packets")} == {tail}
        other = packet_store.append(store.db, "detail_packets", 130, originals[0][1])
        packet_store.retain(store.db, other)
        assert store.db.execute("SELECT arena FROM market_packets WHERE id=?", (other,)).fetchone()[0] != tail
    selected = prepare(store, [(other, originals[0][1])])
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        # An owned fixture fills the unused suffix to exercise the full-tail
        # branch without manufacturing21,845 source packets.
        used = store.db.execute("SELECT used FROM market_packet_arenas WHERE id=?", (tail,)).fetchone()[0]
        for length in (packet_store.MAXIMUM, packet_store.CAPACITY - used - packet_store.MAXIMUM):
            packet_store.write_segment(store.db, "detail_packets", 130, b"x" * length, target=tail)
        shared_records.publish(store.db, selected)
        current = store.db.execute("SELECT arena FROM market_shared_tails WHERE bucket=130").fetchone()[0]
        assert current != tail
        assert (
            store.db.execute("SELECT arena FROM market_packets WHERE id=?", (other,)).fetchone()[0] == current
        )
    for identity, old in [*originals, (other, originals[0][1])]:
        equal(old, domain_packets.read(store.db, identity))


@pytest.mark.parametrize("operation", ["delete", "bucket", "arena"])
def test_shared_tail_guards_preserve_reservation_until_real_collection(store, operation):
    originals = setup(store)
    selected = prepare(store, originals)
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        shared_records.publish(store.db, selected)
    before = list(store.db.iterdump())
    with pytest.raises(sqlite3.IntegrityError), store.db:
        if operation == "delete":
            store.db.execute("DELETE FROM market_shared_tails")
        elif operation == "bucket":
            store.db.execute("UPDATE market_shared_tails SET bucket=bucket+1")
        else:
            store.db.execute("UPDATE market_shared_tails SET arena=arena")
    assert list(store.db.iterdump()) == before


@pytest.mark.parametrize("validator", ["disk", "compressed"])
def test_full_validation_checks_real_shared_groups_and_classifies_corruption(store, validator):
    import apsw

    from cryptowatcher.compressed_validation import QualificationError, full_check
    from cryptowatcher.disk_validation import validated_disk
    from cryptowatcher.storage import IntegrityError

    originals = setup(store)
    selected = prepare(store, originals)
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        shared_records.publish(store.db, selected)

    def check():
        deadline = time.monotonic() + 10
        if validator == "disk":
            with validated_disk(store.path, deadline=deadline):
                pass
        else:
            with closing(apsw.Connection(str(store.path))) as database:
                database.execute("PRAGMA query_only=ON")
                full_check(database, deadline)

    check()
    row = store.db.execute("SELECT id,body FROM market_shared_records").fetchone()
    with store.db, store.db.blobopen("market_shared_records", "body", row[0], readonly=False) as blob:
        blob.seek(len(row[1]) - 1)
        blob.write(bytes([row[1][-1] ^ 1]))
    bad = store.db.execute("SELECT body FROM market_shared_records").fetchone()[0]
    with pytest.raises((IntegrityError, QualificationError)) as failure:
        check()
    assert failure.value.code == "DATABASE_CORRUPT"
    assert store.db.execute("SELECT body FROM market_shared_records").fetchone()[0] == bad


def test_shared_catalogue_relocation_releases_members_with_exact_public_and_dependency_leases(store):
    from cryptowatcher import record_relocation
    from tests.test_record_relocation import fragmented, public, references

    records = fragmented(store)
    expected, leases = public(store), references(store)
    shared_records.install(store.db, store.cached_bytes)
    identities = sorted({r.binding[0] for r in records})
    for identity in identities:
        prepared = prepare(store, [(identity, b"unused")])
        with store.db:
            store.db.execute("BEGIN IMMEDIATE")
            shared_records.publish(store.db, prepared, store.cached_bytes)
    assert public(store) == expected and references(store) == leases
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        fresh = [record_relocation.capture(store, r.coordinate) for r in records]
        record_relocation.relocate(store, fresh)
        packet_store.collect(store.db, 256)
        assert shared_records.collect(store.db, 256) == len(identities)
    assert public(store) == expected and references(store) == leases
    assert not store.db.execute("SELECT 1 FROM market_shared_members").fetchone()
    assert not store.db.execute("PRAGMA foreign_key_check").fetchall()
