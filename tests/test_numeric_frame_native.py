import ctypes
import random
import struct
import sys

import pytest

from cryptowatcher import numeric_frame_codec, numeric_frame_native
from tests.test_numeric_frame_codec import forged, row_body, values


@pytest.fixture(autouse=True)
def clear_loader_cache():
    numeric_frame_native.load.cache_clear()
    yield
    numeric_frame_native.load.cache_clear()


def test_missing_artifact_selects_portable_path_but_invalid_installed_artifact_is_not_ignored(
    tmp_path, monkeypatch
):
    path = tmp_path / "numeric_frame.so"
    monkeypatch.setattr(numeric_frame_native, "artifact", lambda: path)
    assert not numeric_frame_native.available()
    assert numeric_frame_native.transpose(bytes([2, 1, 3]), b"acdbef") is None
    numeric_frame_native.load.cache_clear()
    path.write_bytes(b"invalid installed artifact")
    with pytest.raises(ValueError, match="Invalid installed"):
        numeric_frame_native.available()


def test_unknown_installed_abi_is_rejected_before_using_any_decoder(tmp_path, monkeypatch):
    path = tmp_path / "numeric_frame.so"
    path.write_bytes(b"ABI fixture; loader is mocked")
    monkeypatch.setattr(numeric_frame_native, "artifact", lambda: path)

    class Unknown:
        @staticmethod
        def cw_nf_abi():
            return 99

    monkeypatch.setattr(numeric_frame_native.ctypes, "CDLL", lambda path: Unknown())
    with pytest.raises(ValueError, match="Unknown numeric decoder ABI"):
        numeric_frame_native.available()


@pytest.mark.parametrize(
    "lengths,planes",
    [(b"", b"a"), (b"\1" * 4097, b"a"), (b"\1", b""), (b"\1", b"a" * 139265)],
    ids=["empty-lengths", "slot-count-overflow", "empty-planes", "payload-overflow"],
)
def test_python_allocation_bounds_precede_optional_loader(lengths, planes, monkeypatch):
    monkeypatch.setattr(numeric_frame_native, "load", lambda: pytest.fail("must reject before loading"))
    with pytest.raises(ValueError, match="allocation bound"):
        numeric_frame_native.transpose(lengths, planes)


@pytest.fixture
def accelerator():
    if sys.platform != "linux" and not numeric_frame_native.artifact().is_file():
        pytest.skip("Linux native transpose artifact is REQUIRED in native CI/image checks")
    result = numeric_frame_native.load()
    assert result is not None, "Run canonical native build before Linux checks"
    return result


@pytest.mark.parametrize("count", [1, 64, 4096])
def test_native_matches_portable_exact_bytes_at_all_bounds(accelerator, count, monkeypatch):
    rng = random.Random(35117 + count)
    lengths = bytes(rng.choice([0, 6, 7, 8, 11, 26, 34]) for _ in range(count))
    if not any(lengths):
        lengths = b"\6" + lengths[1:]
    values = [bytes(rng.randrange(256) for _ in range(size)) for size in lengths]
    planes = b"".join(bytes(value[i] for value in values if len(value) > i) for i in range(max(lengths)))
    expected = b"".join(values)
    assert numeric_frame_native.transpose(lengths, planes) == expected
    with monkeypatch.context() as portable:
        portable.setattr(numeric_frame_native, "transpose", lambda *args: None)
        assert numeric_frame_codec.plane_rows(lengths, planes) == expected


def test_native_accepts_exact_maximum_output_without_losing_the_last_byte(accelerator):
    lengths = bytes([34]) * 4096
    planes = b"".join(bytes((slot + column) % 256 for slot in range(4096)) for column in range(34))
    expected = b"".join(bytes((slot + column) % 256 for column in range(34)) for slot in range(4096))
    assert len(expected) == numeric_frame_native.MAX_BYTES
    assert numeric_frame_native.transpose(lengths, planes) == expected


@pytest.mark.parametrize("fault", ["length", "sum", "capacity", "count", "null"])
def test_native_invalid_buffers_are_rejected_before_any_output_write(accelerator, fault):
    lengths = bytes([6, 7, 0, 34])
    planes = b"a" * sum(lengths)
    output = ctypes.create_string_buffer(b"!" * (len(planes) + 2))
    count = len(lengths)
    size = len(planes)
    capacity = size
    if fault == "length":
        lengths = bytes([6, 35, 0, 34])
    elif fault == "sum":
        size -= 1
    elif fault == "capacity":
        capacity -= 1
    elif fault == "count":
        count = 4097
    else:
        lengths = None
    before = output.raw
    assert accelerator.cw_nf_untranspose(lengths, count, planes, size, output, capacity) == 0
    assert output.raw == before


@pytest.mark.parametrize("count", [1, 2, 64])
@pytest.mark.parametrize("planes", [False, True])
def test_native_flat_layout_matches_portable_for_every_original_slot_and_representation(
    accelerator, monkeypatch, count, planes
):
    body = row_body(values(count))
    if planes:
        start = 5 + count * 73
        lengths = body[5 + count * 9 : start]
        row = body[start:]
        offsets = [0]
        for size in lengths:
            offsets.append(offsets[-1] + size)
        transposed = b"".join(
            bytes(row[offsets[slot] + column] for slot, size in enumerate(lengths) if size > column)
            for column in range(max(lengths))
        )
        body = b"BP1\0" + body[4:start] + transposed
    with monkeypatch.context() as portable:
        portable.setattr(numeric_frame_native, "flatten", lambda body: None)
        portable.setattr(numeric_frame_native, "transpose", lambda *args: None)
        expected = numeric_frame_codec.data(forged(body))
    assert numeric_frame_native.flatten(body) == expected


@pytest.mark.parametrize("damage", ["null", "capacity", "count", "duplicate", "length", "identity"])
def test_native_flatten_rejects_invalid_shapes_without_touching_output_or_size(accelerator, damage):
    body = bytearray(row_body(values(2)))
    capacity = numeric_frame_native.MAX_FLAT
    if damage == "count":
        body[4] = 65
    elif damage == "duplicate":
        body[13:21] = body[5:13]
    elif damage == "length":
        body[23] = 255
    elif damage == "identity":
        body[5:13] = struct.pack(">Q", (2**63 - 2) // 64 + 1)
    elif damage == "capacity":
        capacity = 1
    output = ctypes.create_string_buffer(b"!" * numeric_frame_native.MAX_FLAT)
    before = output.raw
    size = ctypes.c_size_t(173)
    assert (
        accelerator.cw_nf_flatten(
            None if damage == "null" else bytes(body), len(body), output, capacity, ctypes.byref(size)
        )
        != 1
    )
    assert output.raw == before and size.value == 173


def test_native_flatten_maximum_body_fills_exact_output_between_canaries(accelerator):
    body = (
        b"BR1\0"
        + bytes([64])
        + struct.pack(">64Q", *range(64))
        + bytes(64)
        + bytes([34]) * 4096
        + bytes(i % 251 for i in range(4096 * 34))
    )
    assert len(body) == numeric_frame_native.MAX_BODY
    output = ctypes.create_string_buffer(b"!" * (numeric_frame_native.MAX_FLAT + 2))
    size = ctypes.c_size_t()
    assert (
        accelerator.cw_nf_flatten(
            body,
            len(body),
            ctypes.byref(output, 1),
            numeric_frame_native.MAX_FLAT,
            ctypes.byref(size),
        )
        == 1
    )
    assert size.value == numeric_frame_native.MAX_FLAT
    assert output.raw[0:1] == output.raw[-2:-1] == b"!"
    assert output.raw[1 : 1 + size.value] == numeric_frame_native.flatten(body)
