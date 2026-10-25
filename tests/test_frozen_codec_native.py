"""Exact optional frozen operations; no storage guard is delegated to a test mock."""

import random
import struct
import sys

import pytest

from cryptowatcher import frozen_codec_native as native
from cryptowatcher import numeric_block_codec as codec
from cryptowatcher.compact_json import integer


@pytest.fixture
def accelerator():
    if native.load() is None:
        assert sys.platform != "linux", "Build the canonical native helper before Linux tests"
        pytest.skip("Actual native frozen operation artifact required")
    return native.load()


@pytest.mark.parametrize("compressed", [False, True])
def test_native_lease_bits_limits_fresh_outputs_and_portable_parity(accelerator, monkeypatch, compressed):
    rng = random.Random(92025)
    calls = []
    original = accelerator.lease_counts

    def counted(raw):
        calls.append(len(raw))
        return original(raw)

    monkeypatch.setattr(accelerator, "lease_counts", counted)
    zeros = codec.counts(19, [0] * 64, [0] * 64, compressed=compressed)
    assert zeros.startswith(b"NR2\0" if compressed else b"NR1\0")
    assert codec.leases(zeros, 19) == ([0] * 64, [0] * 64)
    for _ in range(64):
        details = [rng.randrange(2**63) for _ in range(64)]
        quality = [0, 1, 127, 128, 2**63 - 1] + [0] * 59
        raw = codec.counts(19, details, quality, compressed=compressed)
        actual = codec.leases(raw, 19)
        assert actual == (details, quality)
        assert actual[0] is not actual[1]
        actual[0][0] = -1
        assert codec.leases(raw, 19) == (details, quality)
        with monkeypatch.context() as patch:
            patch.setattr(native, "leases", lambda raw: None)
            assert codec.leases(raw, 19) == (details, quality)
    assert len(calls) == 129


@pytest.mark.parametrize(
    "raw",
    [b"", b"\0" * 127, b"\0" * 129, b"\x80\0" + b"\0" * 127, b"\xff" * 9 + b"\1" + b"\0" * 127],
)
def test_native_malformed_counts_keep_the_original_rejection(accelerator, raw):
    assert accelerator.lease_counts(raw) is NotImplemented
    sealed = codec.seal(b"NR1\0", struct.pack(">Q", 19) + raw)
    with pytest.raises(ValueError):
        codec.leases(sealed, 19)


def test_native_never_runs_before_enclosing_authentication_or_identity(accelerator, monkeypatch):
    raw = codec.counts(19, [0] * 64, [0] * 64)
    monkeypatch.setattr(accelerator, "lease_counts", lambda raw: pytest.fail("Guard bypass"))
    with pytest.raises(ValueError):
        codec.leases(raw[:-1] + b"x", 19)
    with pytest.raises(ValueError):
        codec.leases(raw, 20)


def test_native_keeps_no_input_reference_and_accepts_every_unsigned63_boundary(accelerator):
    values = [0, 1, 127, 128, 2**56, 2**63 - 1] * 21 + [2, 3]
    raw = b"".join(integer(v) for v in values)
    refs = sys.getrefcount(raw)
    for _ in range(1000):
        result = accelerator.lease_counts(raw)
        assert result == (values[:64], values[64:])
    assert sys.getrefcount(raw) == refs
    assert accelerator.lease_counts(bytearray(raw)) is NotImplemented


def test_absent_native_library_keeps_portable_lease_reader(tmp_path, monkeypatch):
    monkeypatch.setattr(native, "artifact", lambda: tmp_path / "missing.so")
    native.load.cache_clear()
    try:
        assert native.available() is False
        raw = codec.counts(19, [127] * 64, [128] * 64, compressed=True)
        assert codec.leases(raw, 19) == ([127] * 64, [128] * 64)
    finally:
        native.load.cache_clear()


def test_installed_invalid_module_fails_closed(tmp_path, monkeypatch):
    path = tmp_path / "frozen_codec.so"
    path.write_bytes(b"invalid installed extension")
    monkeypatch.setattr(native, "artifact", lambda: path)
    native.load.cache_clear()
    try:
        with pytest.raises(ValueError, match="Incompatible frozen operation artifact"):
            native.available()
    finally:
        native.load.cache_clear()


def test_foreign_python_build_is_rejected_before_python_object_initialization(tmp_path, monkeypatch):
    path = tmp_path / "frozen_codec.so"
    path.write_bytes(b"Scalar ABI fixture; actual loading is replaced")

    class Function:
        def __init__(self, value):
            self.value = value

        def __call__(self):
            return self.value

    class Foreign:
        cw_frozen_python_minor = Function((sys.version_info.major << 8 | sys.version_info.minor) + 1)
        cw_frozen_pointer_bits = Function(native.ctypes.sizeof(native.ctypes.c_void_p) * 8)

    monkeypatch.setattr(native, "artifact", lambda: path)
    monkeypatch.setattr(native.ctypes, "CDLL", lambda path: Foreign())
    monkeypatch.setattr(
        native.importlib.util, "module_from_spec", lambda spec: pytest.fail("Foreign runtime initialized")
    )
    native.load.cache_clear()
    try:
        with pytest.raises(ValueError, match="Python runtime mismatch"):
            native.available()
    finally:
        native.load.cache_clear()
