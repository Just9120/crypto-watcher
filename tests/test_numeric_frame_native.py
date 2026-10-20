import ctypes
import random
import sys

import pytest

from cryptowatcher import numeric_frame_codec, numeric_frame_native


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
            return 2

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
