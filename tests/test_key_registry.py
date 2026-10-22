import copy
import random
import sqlite3
import time
import uuid
from contextlib import closing

import pytest

from cryptowatcher import expiry_index, key_registry, reference_layout
from cryptowatcher.history_codec import Codec
from cryptowatcher.market_storage import enable
from cryptowatcher.storage import Store
from cryptowatcher.storage_lifecycle import body_links, forget
from tests.test_reference_layout import reference


def test_sql_registry_cache_uses_exact_pure_inputs_and_preserves_current_rows_and_rollback(
    store, monkeypatch
):
    original_pack, original_unpack = key_registry.pack, key_registry.unpack
    calls = []

    def packed(value):
        calls.append(("pack", value))
        return original_pack(value)

    def unpacked(value):
        calls.append(("unpack", value))
        return original_unpack(value)

    monkeypatch.setattr(key_registry, "pack", packed)
    monkeypatch.setattr(key_registry, "unpack", unpacked)
    key = "radar:v1:event:" + str(uuid.UUID(int=1)) + ":0000000001"
    token = original_pack(key)
    assert tuple(store.db.execute("SELECT cw_key_pack(?),cw_key_unpack(?)", (key, token)).fetchone()) == (
        token,
        key,
    )
    count = len(calls)
    assert tuple(store.db.execute("SELECT cw_key_pack(?),cw_key_unpack(?)", (key, token)).fetchone()) == (
        token,
        key,
    )
    assert len(calls) == count
    with store.db:
        store.db.execute("CREATE TABLE registry_cache_probe(id INTEGER PRIMARY KEY,external)")
        store.db.execute("INSERT INTO registry_cache_probe VALUES(1,?)", (token,))
    other_key = key.replace("0000000001", "0000000002")
    other_token = original_pack(other_key)
    with pytest.raises(RuntimeError), store.db:
        store.db.execute("UPDATE registry_cache_probe SET id=2,external=?", (other_token,))
        assert tuple(
            store.db.execute("SELECT id,cw_key_unpack(external) FROM registry_cache_probe").fetchone()
        ) == (2, other_key)
        raise RuntimeError("Rollback database state, retaining only pure conversions")
    assert tuple(
        store.db.execute("SELECT id,cw_key_unpack(external) FROM registry_cache_probe").fetchone()
    ) == (1, key)
    for invalid in (None, 1, b"CK1\x00\xff", token[:-1]):
        for function in ("cw_key_pack", "cw_key_unpack"):
            with pytest.raises(sqlite3.OperationalError):
                store.db.execute(f"SELECT {function}(?)", (invalid,)).fetchone()


def test_sql_registry_cache_shares_existing_budget_and_plain_connections_keep_frozen_codecs(store):
    with closing(sqlite3.connect(":memory:")) as plain:
        key_registry.install_functions(plain)
        for i in range(2200):
            key = f"radar:v1:outcome:{uuid.UUID(int=i)}:{i:010d}:" + "МСК/😀" * 8
            token = key_registry.pack(key)
            expected = (token, key)
            assert (
                tuple(store.db.execute("SELECT cw_key_pack(?),cw_key_unpack(?)", (key, token)).fetchone())
                == expected
            )
            assert (
                plain.execute("SELECT cw_key_pack(?),cw_key_unpack(?)", (key, token)).fetchone() == expected
            )
            assert store.reference_cache_bytes <= 1024 * 1024
    assert store.reference_cache_bytes == sum(size for _, size in store.reference_cache.values())
    assert 0 < len(store.reference_cache) < 4400


def test_registry_codec_frozen_dictionary_exact_keys_and_bad_frames():
    rng = random.Random(19102026)
    cases = ["", "unknown", "данные/😀", "1234567890" * 4, "0" * 64, "radar:v1:" + "x" * 9000]
    for i in range(1000):
        identity = str(uuid.UUID(int=rng.getrandbits(128)))
        cases.append(
            f"{key_registry.ROOTS[1 + i % (len(key_registry.ROOTS) - 1)]}{identity}:{i:010d}:000001792246245000/МСК"
        )
    cases += ["radar:v1:" + "a" * 64, "historyblob:v1:" + "0123456789abcdef" * 4]
    for original in cases:
        packed = key_registry.pack(original)
        assert key_registry.unpack(packed) == original
        assert key_registry.pack(packed) == packed
        assert len(packed if isinstance(packed, bytes) else packed.encode()) <= len(original.encode())
    assert key_registry.pack(
        "radar:v1:event:" + "01234567-89ab-cdef-0123-456789abcdef"
    ) == key_registry.MAGIC + b"\x02\x01" + bytes.fromhex("0123456789abcdef0123456789abcdef")
    for invalid in (
        b"",
        b"CK9\x00broken",
        key_registry.MAGIC + b"\xff",
        key_registry.MAGIC + b"\x02\x01short",
        key_registry.MAGIC + b"\x00\xff",
        key_registry.MAGIC + b"\x00\x00\x00",
    ):
        with pytest.raises(ValueError):
            key_registry.unpack(invalid)


def test_secondary_registry_drain_preserves_ids_foreign_keys_expiry_refs_and_rollback(store):
    enable(store.db)
    store.bounded = True
    keys = [f"radar:v1:event:{uuid.uuid5(uuid.NAMESPACE_URL, str(i))}:0000000001" for i in range(1000)]
    with store.db:
        body = Codec(store.db).save({"settings": "frozen"})
        for i, key in enumerate(keys):
            store.db.execute("INSERT INTO runtime VALUES(?,?)", (key, "{}"))
            # Existing v3 physical coordinates; the stable IDs are authoritative.
            store.db.execute("INSERT INTO market_runtime_keys(external) VALUES(?)", (key,))
            store.db.execute(
                "INSERT INTO market_lifetimes VALUES(?,?,?,?)", (key, 10000 + i, "event", str(i))
            )
            body_links(store, key, reference(body))
    before = {
        row[0]: key_registry.unpack(row[1])
        for row in store.db.execute("SELECT id,external FROM market_runtime_keys")
    }
    lifetimes = sorted(tuple(row) for row in store.db.execute("SELECT * FROM market_lifetimes"))
    links = sorted(tuple(row) for row in store.db.execute("SELECT * FROM market_body_links"))
    state = {}
    with pytest.raises(RuntimeError), store.db:
        key_registry.drain(store.db, copy.deepcopy(state), 17, time.monotonic() + 1)
        raise RuntimeError("rollback tokens and byte counters")
    assert all(
        isinstance(row[0], str)
        for row in store.db.execute(
            "SELECT external FROM market_runtime_keys WHERE id IN(SELECT rid FROM market_expiry)"
        )
    )
    for _ in range(100):
        with store.db:
            if key_registry.drain(store.db, state, 17, time.monotonic() + 1):
                break
    assert before == {
        row[0]: key_registry.unpack(row[1])
        for row in store.db.execute("SELECT id,external FROM market_runtime_keys")
    }
    assert all(
        isinstance(row[0], bytes)
        for row in store.db.execute(
            "SELECT external FROM market_runtime_keys WHERE id IN(SELECT rid FROM market_expiry)"
        )
    )
    assert sorted(tuple(row) for row in store.db.execute("SELECT * FROM market_lifetimes")) == lifetimes
    assert sorted(tuple(row) for row in store.db.execute("SELECT * FROM market_body_links")) == links
    assert not store.db.execute("PRAGMA foreign_key_check").fetchall()
    # A point operation must not scan the entire decoded compatibility view.
    steps = 0

    def budget():
        nonlocal steps
        steps += 1
        return int(steps > 40)

    store.db.set_progress_handler(budget, 100)
    try:
        assert expiry_index.expires(store.db, keys[-1])[0] == 10999
        assert reference_layout.owner_refs(store.db, keys[-1]) == {body}
    finally:
        store.db.set_progress_handler(None, 0)
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        assert sorted(tuple(row) for row in reader.db.execute("SELECT * FROM market_lifetimes")) == lifetimes
    with store.db:
        expiry_index.renew(store.db, keys[0], 42)
        assert expiry_index.expires(store.db, keys[0])[0] == 42
        for key in keys:
            forget(store, key)
            store.db.execute("DELETE FROM runtime WHERE key=?", (key,))
    assert not store.db.execute("SELECT 1 FROM market_expiry").fetchone()
    assert not store.db.execute("SELECT 1 FROM market_references").fetchone()
    assert store.db.execute("SELECT refs FROM market_body_counts WHERE body=?", (body,)).fetchone()[0] == 0


def test_pre_token_views_upgrade_without_rewriting_rows_or_owner_ids(store):
    enable(store.db)
    store.bounded = True
    key = "radar:v1:event-head:" + str(uuid.uuid5(uuid.NAMESPACE_URL, "old"))
    with store.db:
        store.db.execute("INSERT INTO runtime VALUES(?,?)", (key, "{}"))
        store.db.execute("INSERT INTO market_runtime_keys(external) VALUES(?)", (key,))
        store.db.execute("INSERT INTO market_lifetimes VALUES(?,?,?,?)", (key, 123, "event-head", "event"))
        store.db.execute("UPDATE market_meta SET value='3' WHERE key='expiry_layout'")
        store.db.execute("DROP TRIGGER market_expiry_key_bytes")
        store.db.execute("UPDATE market_meta SET value='1' WHERE key='reference_layout'")
    before = [tuple(row) for row in store.db.execute("SELECT * FROM market_runtime_keys")]
    expiry_index.install(store.db)
    reference_layout.install(store.db)
    assert [tuple(row) for row in store.db.execute("SELECT * FROM market_runtime_keys")] == before
    assert expiry_index.expires(store.db, key)[0] == 123
    assert store.db.execute("SELECT value FROM market_meta WHERE key='expiry_layout'").fetchone()[0] == "4"
