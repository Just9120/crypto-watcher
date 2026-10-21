import hashlib
import random
import sqlite3
import time
from contextlib import closing

import apsw
import pytest

from cryptowatcher import compressed_validation as cv
from cryptowatcher import owner_slots as slots
from cryptowatcher.disk_validation import validated_disk
from cryptowatcher.storage import IntegrityError, Store, check_integrity


def create_probe(db):
    slots.install_functions(db)
    db.execute("""CREATE TABLE owner_probe(
        parent INTEGER NOT NULL CHECK(typeof(parent)='integer' AND parent>0),
        packet INTEGER NOT NULL CHECK(typeof(packet)='integer' AND packet>0),
        slots BLOB NOT NULL CHECK(typeof(slots)='blob' AND length(slots)=8),
        checksum BLOB NOT NULL CHECK(typeof(checksum)='blob' AND length(checksum)=32
            AND cw_owner_digest(parent,packet,slots)=checksum))""")
    mask = bytes.fromhex("8000000000000001")
    db.execute("INSERT INTO owner_probe VALUES(1,2,?,?)", (mask, slots.signature(1, 2, mask)))
    db.commit()


def test_frozen_mask_byte_order_golden_checksum_and_last_slot():
    mask = slots.mask([0, 63])
    assert mask.hex() == "8000000000000001"
    assert (
        slots.signature(1, 2, mask).hex()
        == "a56d9adedb9ef206ebda59e5f7753b8922e40ffabd747dabf9eb85f3e15963ae"
    )
    assert slots.has(mask, 0) and slots.has(mask, 63) and not slots.has(mask, 1)
    assert slots.remove(mask, 63) == bytes.fromhex("0000000000000001")
    assert slots.remove(slots.remove(mask, 63), 0) == slots.ZERO
    assert slots.sql_signature(1, 2) == b"" and slots.sql_signature(1, 2, mask, mask) == b""
    with pytest.raises(ValueError, match="Missing"):
        slots.remove(mask, 1)
    with pytest.raises(ValueError, match="Duplicate"):
        slots.mask([1, 1])
    for invalid in (-1, 64, True, 1.0):
        with pytest.raises(ValueError):
            slots.mask([invalid])
    for invalid in (None, bytes(7), bytes(9), "00000000"):
        with pytest.raises(ValueError):
            slots.signature(1, 2, invalid)
        assert slots.sql_signature(1, 2, invalid) == b""
    for invalid in (0, -1, 2**63, True, 1.0, "1", None):
        for parent, packet in ((invalid, 2), (1, invalid)):
            with pytest.raises(ValueError):
                slots.signature(parent, packet, mask)
            assert slots.sql_signature(parent, packet, mask) == b""


def test_mask_subsets_keep_exact_slot_set_and_coordinate_checksums():
    randomizer = random.Random(1910)
    for _ in range(100):
        chosen = randomizer.sample(range(64), randomizer.randrange(65))
        value = slots.mask(chosen)
        assert {i for i in range(64) if slots.has(value, i)} == set(chosen)
        before = slots.signature(1, 2, value)
        assert before != slots.signature(2, 2, value) and before != slots.signature(1, 3, value)
        for slot in chosen:
            value = slots.remove(value, slot)
        assert value == slots.ZERO


@pytest.mark.parametrize("damage", [None, "checksum", "mask", "parent", "packet"])
def test_full_sqlite_apsw_and_disk_check_share_bounded_checksum_and_preserve_every_source_byte(
    tmp_path, damage
):
    path = tmp_path / "owners.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        create_probe(db)
        if damage is not None:
            db.execute("PRAGMA ignore_check_constraints=ON")
            if damage == "checksum":
                db.execute("UPDATE owner_probe SET checksum=zeroblob(32)")
            elif damage == "mask":
                db.execute("UPDATE owner_probe SET slots=zeroblob(7)")
            elif damage == "parent":
                db.execute("UPDATE owner_probe SET parent='invalid'")
            else:
                db.execute("UPDATE owner_probe SET packet=-1")
            db.commit()
    before = hashlib.sha256(path.read_bytes()).digest()
    with closing(sqlite3.connect(path)) as db:
        if damage is None:
            check_integrity(db, deadline=time.monotonic() + 5)
        else:
            with pytest.raises(IntegrityError) as failure:
                check_integrity(db, deadline=time.monotonic() + 5)
            assert failure.value.code == "DATABASE_CORRUPT"
    with closing(apsw.Connection(str(path))) as db:
        if damage is None:
            cv.full_check(db, time.monotonic() + 5)
        else:
            with pytest.raises(cv.QualificationError, match="DATABASE_CORRUPT"):
                cv.full_check(db, time.monotonic() + 5)
    if damage is None:
        with validated_disk(path, deadline=time.monotonic() + 5) as db:
            assert list(db.execute("SELECT parent,packet FROM owner_probe")) == [(1, 2)]
    else:
        with pytest.raises(IntegrityError) as failure, validated_disk(path, deadline=time.monotonic() + 5):
            pytest.fail("Corrupt owner checksum must fail the ordinary full CHECK")
        assert failure.value.code == "DATABASE_CORRUPT"
    assert hashlib.sha256(path.read_bytes()).digest() == before


def test_reader_opened_before_mask_schema_can_read_and_check_after_writer_commit(store):
    with closing(Store(store.path, store.owner_id, readonly=True)) as earlier:
        with store.db:
            create_probe(store.db)
        row = earlier.db.execute(
            "SELECT cw_owner_digest(parent,packet,slots)=checksum FROM owner_probe"
        ).fetchone()
        assert row[0] == 1
        assert earlier.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
