"""Native owner proofs must retain portable integrity and connection boundaries."""

import ctypes
import sqlite3
import struct
import sys
import time
from contextlib import closing

import pytest

from cryptowatcher import numeric_block_codec as blocks
from cryptowatcher import numeric_frame_codec as codec
from cryptowatcher import numeric_frame_native as native
from cryptowatcher import numeric_frame_proof as proof
from cryptowatcher import numeric_frame_work as work
from cryptowatcher import numeric_frames, numeric_search
from cryptowatcher.storage import Store
from scripts import qualify_numeric_boundary as boundary
from tests.test_numeric_frames import prepared, publish


@pytest.fixture
def accelerator():
    if sys.platform != "linux" and not native.artifact().is_file():
        pytest.skip("Native proof is REQUIRED in Linux CI/image checks")
    library = native.load()
    assert library is not None, "Run canonical native build before Linux checks"
    proof.configure(library)
    return library


def operands(store):
    _, _, sources = prepared(store)
    identity = publish(store, sources)
    payload, mask = store.db.execute(
        "SELECT payload,slots FROM market_numeric_frames WHERE id=?", (identity,)
    ).fetchone()
    assert proof.install(store.db)
    return identity, codec.data(bytes(payload)), bytes(mask), sources


def test_native_and_portable_current_nr2_geometry_have_identical_counts_and_no_writes(
    tmp_path, accelerator, monkeypatch
):
    path = tmp_path / "owned.sqlite3"
    boundary.materialize(path, 3)
    with closing(Store(path, 233382575, readonly=True)) as reader:
        reader.db.execute("BEGIN")
        actual = work.measure(reader.db, time.monotonic() + 5, reader.cached_bytes)
        with monkeypatch.context() as portable:
            portable.setattr(proof, "install", lambda db: False)
            expected = work.measure(reader.db, time.monotonic() + 5, reader.cached_bytes)
        assert actual.pop("owner_proof_backend") == "SQLITE_OPENSSL"
        assert expected.pop("owner_proof_backend") == "PYTHON"
        assert actual == expected
        assert actual["state"] == "MEASURED" and actual["owned_blocks"] == 192
        assert reader.db.total_changes == 0
    # A read-only SQLite connection may leave its sidecars after closing. A
    # fresh writer must checkpoint without a retained native read statement.
    with closing(Store(path, 233382575)) as writer:
        assert writer.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
    assert not any(path.with_name(path.name + suffix).exists() for suffix in ("-wal", "-shm"))


def test_nr1_and_detached_dead_slots_use_actual_current_rows(store, accelerator):
    identity, flat, mask, sources = operands(store)
    assert proof.owners(store.db, identity, flat, mask, False) == 2
    block = sources[0][0]
    from cryptowatcher import numeric_blocks

    frames, details, quality, _ = numeric_blocks.read(store.db, block)
    details[0] += 1
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        numeric_blocks.put(store.db, block, frames, details, quality)
        assert numeric_frames.collect(store.db, time.monotonic() + 3, cache=store.cached_bytes) == 0
    payload, mask = store.db.execute(
        "SELECT payload,slots FROM market_numeric_frames WHERE id=?", (identity,)
    ).fetchone()
    assert codec.raw_entries(bytes(payload))[0][:8] == struct.pack(">Q", codec.DEAD)
    assert proof.owners(store.db, identity, codec.data(bytes(payload)), bytes(mask), False) == 1
    with pytest.raises(ValueError, match="cw_nf:"):
        proof.owners(store.db, identity, flat, b"\0" * 7 + b"\3", False)


@pytest.mark.parametrize("damage", ["lease_checksum", "lease_header", "detail", "mode"])
def test_authenticated_lease_and_current_mode_are_checked_after_a_warm_proof(store, accelerator, damage):
    identity, flat, mask, sources = operands(store)
    assert proof.owners(store.db, identity, flat, mask, False) == 2
    block = sources[0][0]
    if damage == "mode":
        payload = b"\0" * 128
        import zstandard

        counts = blocks.seal(
            b"NR2\0",
            struct.pack(">QH", block, 128) + zstandard.ZstdCompressor(write_checksum=True).compress(payload),
        )
    elif damage == "lease_header":
        # Correct outer SHA and NS2 cannot authorize an invalid NR2 header.
        counts = blocks.seal(b"NR2\0", struct.pack(">QH", block, 128) + b"invalid-frame")
    else:
        counts = bytes(
            store.db.execute("SELECT counts FROM market_numeric_block_refs WHERE id=?", (block,)).fetchone()[
                0
            ]
        )
        if damage == "lease_checksum":
            counts = counts[:4] + bytes([counts[4] ^ 1]) + counts[5:]
    pointer = numeric_frames.pointer(identity, 0)
    entry = codec.raw_entries(
        bytes(
            store.db.execute("SELECT payload FROM market_numeric_frames WHERE id=?", (identity,)).fetchone()[
                0
            ]
        )
    )[0]
    seal = numeric_search.signature(block, pointer, counts, binding=entry)
    with store.db:
        store.db.execute(
            "UPDATE market_numeric_block_refs SET counts=?,search_seal=?,detail_slots=? WHERE id=?",
            (counts, seal, int(damage == "detail"), block),
        )
    before = list(store.db.iterdump())
    with pytest.raises(ValueError, match="cw_nf:"):
        proof.owners(store.db, identity, flat, mask, damage != "mode")
    assert list(store.db.iterdump()) == before


@pytest.mark.parametrize(
    "index,value",
    [(0, 0), (0, "1"), (1, b""), (1, b"\xff" * 144198), (2, b"\0" * 9), (3, 2**32), (3, -(2**32))],
    ids=["zero-id", "text-id", "empty-flat", "oversize-flat", "mask-size", "mode-wrap", "negative-mode-wrap"],
)
def test_sql_scalar_input_bounds_reject_without_mutation(store, accelerator, index, value):
    identity, flat, mask, _ = operands(store)
    args = [identity, flat, mask, 0]
    args[index] = value
    before = list(store.db.iterdump())
    with pytest.raises(sqlite3.OperationalError, match="cw_nf:"):
        store.db.execute("SELECT cw_nf_proof(?,?,?,?)", args).fetchone()
    assert list(store.db.iterdump()) == before


def test_extension_remains_direct_only_and_load_permission_is_restored(store, accelerator):
    identity, flat, mask, _ = operands(store)
    assert proof.owners(store.db, identity, flat, mask, False) == 2
    with pytest.raises(sqlite3.OperationalError, match="not authorized"):
        store.db.load_extension(str(native.artifact().resolve()))
    store.db.execute("CREATE VIEW unsafe_numeric AS SELECT cw_nf_proof(1,x'',zeroblob(8),0)")
    with pytest.raises(sqlite3.OperationalError, match="unsafe use"):
        store.db.execute("SELECT * FROM unsafe_numeric").fetchall()


def test_failed_extension_load_also_disables_loading(accelerator):
    calls = []

    class Connection:
        @staticmethod
        def enable_load_extension(enabled):
            calls.append(enabled)

        @staticmethod
        def load_extension(path):
            raise sqlite3.OperationalError("injected extension load failure")

    with pytest.raises(sqlite3.OperationalError, match="injected"):
        proof.install(Connection())
    assert calls == [True, False]


@pytest.mark.parametrize("failure", ["interrupt", "authorizer"])
def test_native_errors_keep_sqlite_error_codes_and_leave_no_retained_statement(store, accelerator, failure):
    identity, flat, mask, _ = operands(store)
    if failure == "interrupt":
        store.db.set_progress_handler(lambda: 1, 1)
        code = sqlite3.SQLITE_INTERRUPT
    else:
        store.db.set_authorizer(
            lambda action, table, *args: (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_READ and table == "market_numeric_blocks"
                else sqlite3.SQLITE_OK
            )
        )
        code = sqlite3.SQLITE_AUTH
    try:
        with pytest.raises(sqlite3.DatabaseError) as error:
            proof.owners(store.db, identity, flat, mask, False)
        assert error.value.sqlite_errorcode == code
    finally:
        store.db.set_progress_handler(None, 0)
        store.db.set_authorizer(None)
    assert proof.owners(store.db, identity, flat, mask, False) == 2


def test_digest_bounds_do_not_modify_output_and_missing_artifact_does_not_load_sql(
    accelerator, store, monkeypatch
):
    output = ctypes.create_string_buffer(b"!" * 34)
    original = output.raw
    for payload, size, capacity in [(None, 1, 32), (b"a" * 3472, 3472, 32), (b"", 0, 31)]:
        assert accelerator.cw_nf_digest(payload, size, output, capacity) == 0
        assert output.raw == original
    with monkeypatch.context() as missing:
        missing.setattr(native, "load", lambda: None)
        assert not proof.install(store.db)
    with pytest.raises(sqlite3.OperationalError, match="no such function"):
        store.db.execute("SELECT cw_nf_proof(1,x'',zeroblob(8),0)").fetchone()
