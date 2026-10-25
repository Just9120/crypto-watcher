"""Differential guards for the optional existing-format admission implementation."""

import copy
import hashlib
import random
import sqlite3
import struct
import sys
from pathlib import Path

import pytest

from cryptowatcher import candle_block_codec as codec
from cryptowatcher import candle_blocks, source_slot_json
from cryptowatcher import source_guard_native as native
from cryptowatcher.storage import encode
from tests.test_candle_block_codec import fixture
from tests.test_candle_blocks import prepare


def floating_slots(step=60000):
    tile, values = fixture(step)
    for part in values:
        for row in part:
            row[3][:6] = list(map(float, row[3][:6]))
            row[3][6] = 'цена "🧪"\x00\n'
    return tile, values


def frame(tile, step, values):
    body = source_slot_json.pack(values)
    assert body is not None
    checked = struct.pack(">QQQ", 1, tile, step) + body
    return codec.JSON_MAGIC + hashlib.sha256(checked).digest() + checked


@pytest.fixture
def accelerator():
    if native.load() is None:
        assert sys.platform != "linux", "Build the canonical native helper before Linux tests"
        pytest.skip("Native source guard requires the actual Linux artifact")
    return native


def body(values):
    raw = source_slot_json.pack(values)
    assert raw is not None
    return source_slot_json.data(raw)


def test_preferred_encoding_validates_new_rows_and_preserves_unsupported_types(monkeypatch):
    tile, values = floating_slots()
    with monkeypatch.context() as patch:
        patch.setattr(codec.indexed_frame, "pack", lambda *a: pytest.fail("discarded typed competitor"))
        raw = codec.pack(1, tile, 60000, values, _prefer_json=True)
    assert encode(codec.unpack(raw)[3]) == encode(values)
    values[0][0][3][1] = 1.0
    with pytest.raises(ValueError, match="Invalid frozen"):
        codec.pack(1, tile, 60000, values, _prefer_json=True)
    tile, values = floating_slots()
    values[0][0][3][6] = {"legacy": ["source"]}
    raw = codec.pack(1, tile, 60000, values, _prefer_json=True)
    assert raw.startswith(codec.MAGIC)
    assert encode(codec.unpack(raw)[3]) == encode(values)


@pytest.mark.parametrize("step", codec.STEPS)
def test_native_all_tf_exact_stats_preservation_and_portable_equivalence(accelerator, monkeypatch, step):
    rng = random.Random(step)
    for _ in range(20):
        tile, values = floating_slots(step)
        for part in values:
            for row in part:
                opening, closing = rng.uniform(1, 100), rng.uniform(1, 100)
                row[3][:6] = [opening, max(opening, closing), min(opening, closing), closing, -0.0, 5e-324]
        original = frame(tile, step, values)
        updated = copy.deepcopy(values)
        updated[0][0][1] += 1
        updated[0][0][2] += 1
        # A new immutable revision is admitted while both original revisions remain.
        row = copy.deepcopy(updated[7][-1])
        row[0] += 1
        row[3][3] = row[3][0]
        updated[7].append(row)
        current = frame(tile, step, updated)
        assert accelerator.stats(tile, step, *body(updated)) == (7, min(r[2] for p in updated for r in p))
        assert accelerator.preserved(tile, step, body(values), body(updated)) is True
        actual = codec.stats(current), codec.preserved(original, current)
        with monkeypatch.context() as patch:
            patch.setattr(native, "available", lambda: False)
            assert actual == (codec.stats(current), codec.preserved(original, current))


@pytest.mark.parametrize(
    "change", ["value", "signed_zero", "source", "received", "server", "lease", "remove"]
)
def test_native_never_grants_mutation_shrink_or_removal(accelerator, change):
    tile, values = floating_slots()
    original = frame(tile, 60000, values)
    updated = copy.deepcopy(values)
    row = updated[7][0]
    if change == "value":
        row[3][3] += 0.125
    elif change == "signed_zero":
        row[3][5] = 0.0
    elif change == "source":
        row[3][6] += "x"
    elif change == "received":
        row[3][7] += 1
    elif change == "server":
        row[3][8] += 1
    elif change == "lease":
        row[2] -= 1
    else:
        updated[7].pop(0)
    assert accelerator.preserved(tile, 60000, body(values), body(updated)) is False
    current = frame(tile, 60000, updated)
    if change == "remove":
        removed = codec.preserved(original, current)
        assert len(removed) == 1 and removed[0][1:3] == (tile + 7 * 60000, 1)
    else:
        with pytest.raises(ValueError):
            codec.preserved(original, current)


@pytest.mark.parametrize("field,value", [(0, 100), (4, 0), (7, 2**63 - 1)])
def test_native_integer_tail_fallback_and_large_metadata_remain_exact(accelerator, field, value):
    tile, values = floating_slots()
    values[0][0][3][field] = value
    result = accelerator.stats(tile, 60000, *body(values))
    if field < 6:
        assert result is None
    else:
        assert result[0] == 6
    raw = frame(tile, 60000, values)
    assert codec.stats(raw)[3] == 6
    assert encode(codec.unpack(raw)[3]) == encode(values)


def test_native_256_revision_bound_and_invalid_semantics_fall_back(accelerator):
    tile, values = floating_slots()
    base = values[0][0]
    values = [[] for _ in range(32)]
    for revision in range(256):
        row = copy.deepcopy(base)
        row[0] = revision
        values[0].append(row)
    assert accelerator.stats(tile, 60000, *body(values))[0] == 256
    for mutate in (
        lambda r: r[3].__setitem__(1, 1.0),
        lambda r: r.__setitem__(0, 0),
        lambda r: r[3].__setitem__(8, tile),
    ):
        changed = copy.deepcopy(values)
        mutate(changed[0][-1])
        assert accelerator.stats(tile, 60000, *body(changed)) is None
        with pytest.raises(ValueError):
            codec.stats(frame(tile, 60000, changed))


def test_native_sql_immutable_and_transaction_rollback_keep_original_bytes(store, accelerator):
    sid, tile, public = prepare(store)
    for value in public:
        for key in codec.TAIL[:6]:
            value[key] = float(value[key])
    with store.db:
        candle_blocks.admit_many(store.db, sid, public)
    original = candle_blocks.tile(store.db, sid, tile, 60000)
    assert original.startswith(codec.JSON_MAGIC)
    with pytest.raises(RuntimeError), store.db:
        candle_blocks.admit_many(store.db, sid, [{**public[0], "close": 102.0}], tile + 40 * 86400000)
        raise RuntimeError("consumer failed")
    assert candle_blocks.tile(store.db, sid, tile, 60000) == original
    values = codec.unpack(original)[3]
    values[0][0][3][3] = 102.0
    changed = codec.pack(sid, tile, 60000, values, _prefer_json=True)
    with pytest.raises(sqlite3.OperationalError, match="user-defined function"), store.db:
        candle_blocks.put(store.db, changed, original)
    assert candle_blocks.tile(store.db, sid, tile, 60000) == original


def test_native_status_bounds_and_missing_artifact_are_explicit(tmp_path, monkeypatch):
    native.load.cache_clear()
    monkeypatch.setattr(native, "artifact", lambda: tmp_path / "absent.so")
    try:
        assert native.available() is False
        assert native.stats(0, 60000, (), b"") is None
        assert native.preserved(0, 60000, ((), b""), ((), b"")) is False
    finally:
        native.load.cache_clear()
    with pytest.raises(MemoryError):
        native._status(2)
    with pytest.raises(ValueError):
        native._status(3)
    for offsets, raw in [
        (tuple(range(33)), b"x"),
        ((0,) * 33, b"xx"),
        (tuple(range(33)), b""),
        ((0,) * 32, b"xx"),
    ]:
        with pytest.raises(ValueError):
            native._body(offsets, raw)


def test_installed_invalid_artifact_fails_closed(tmp_path, monkeypatch):
    path = tmp_path / "source_guard.so"
    path.write_bytes(b"invalid-not-a-library")
    monkeypatch.setattr(native, "artifact", lambda: path)
    native.load.cache_clear()
    try:
        with pytest.raises(ValueError, match="Incompatible native"):
            native.available()
    finally:
        native.load.cache_clear()


def test_dangling_native_artifact_is_not_the_portable_absence_case(monkeypatch):
    class Dangling:
        def is_symlink(self):
            return True

        def exists(self):
            pytest.fail("A dangling installed link cannot silently select fallback")

    monkeypatch.setattr(native, "artifact", Dangling)
    native.load.cache_clear()
    try:
        with pytest.raises(ValueError, match="Invalid native source guard artifact"):
            native.available()
    finally:
        native.load.cache_clear()


def test_installed_unknown_native_abi_fails_before_decoder_use(tmp_path, monkeypatch):
    path = tmp_path / "source_guard.so"
    path.write_bytes(b"loader-only ABI fixture")

    class Function:
        def __call__(self):
            return 99

    class Library:
        cw_source_guard_abi = Function()

    monkeypatch.setattr(native, "artifact", lambda: path)
    monkeypatch.setattr(native.ctypes, "PyDLL", lambda path: Library())
    native.load.cache_clear()
    try:
        with pytest.raises(ValueError, match="Unknown native source guard ABI"):
            native.available()
    finally:
        native.load.cache_clear()


@pytest.mark.skipif(sys.platform != "linux", reason="Actual Linux compiler and ASAN/UBSAN")
def test_native_allocator_offsets_and_malformed_bodies_under_sanitizers(tmp_path):
    from tests.test_vendor_security import compile_run

    root = Path(__file__).resolve().parents[1]
    code = r"""
        #include <assert.h>
        #include "source_guard.c"
        void *PyExc_MemoryError = NULL;
        double PyOS_string_to_double(const char *p, char **end, void *error) {
            (void)error; return strtod(p, end);
        }
        void *PyErr_Occurred(void) { return NULL; }
        void PyErr_Clear(void) {}
        int PyErr_ExceptionMatches(void *error) { (void)error; return 0; }
        static void offset(unsigned char *p, uint32_t n) {
            p[0]=(unsigned char)(n>>24); p[1]=(unsigned char)(n>>16);
            p[2]=(unsigned char)(n>>8); p[3]=(unsigned char)n;
        }
        int main(void) {
            unsigned char index[132]={0}; uint64_t result[2]={77,88};
            assert(cw_source_guard_abi()==1);
            assert(cw_source_guard_stats(NULL,0,index,132,0,60000,result)==SG_ARGUMENT);
            char bytes[2048]; uint32_t state=92849;
            for(size_t trial=0; trial<2000; trial++) {
                size_t n=1+trial%sizeof(bytes);
                for(size_t i=0;i<n;i++) {
                    state=state*1664525u+1013904223u; bytes[i]=(char)(state>>24);
                }
                for(size_t i=0;i<33;i++) offset(index+i*4,(uint32_t)(n*i/32));
                int r=cw_source_guard_stats(bytes,n,index,132,0,60000,result);
                assert(r!=SG_VALID);
                r=cw_source_guard_preserved(bytes,n,index,132,bytes,n,index,132,0,60000);
                assert(r!=SG_VALID);
                offset(index+4,UINT32_MAX);
                assert(cw_source_guard_stats(bytes,n,index,132,0,60000,result)==SG_ARGUMENT);
            }
            budget b={0}; void *p=limited_malloc(&b,128); assert(p && b.used>128);
            assert(!limited_realloc(&b,p,128,SIZE_MAX) && b.failed);
            limited_free(&b,p); assert(b.used==0);
            assert(!limited_malloc(&b,SG_MEMORY)); assert(b.used==0);
            p=limited_malloc(&b,128); assert(p);
            p=limited_realloc(&b,p,128,4096); assert(p);
            assert(!limited_realloc(&b,p,4096,0) && b.used==0);
            return 0;
        }
    """
    compile_run(
        tmp_path,
        code,
        [root / "native/vendor/yyjson/yyjson.c"],
        [root / "native", root / "native/vendor/yyjson"],
    )
