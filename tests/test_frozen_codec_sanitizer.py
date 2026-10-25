"""Exercise real CPython reference/materialization paths under ASAN and UBSAN."""

import os
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform != "linux", reason="Actual Linux CPython/compiler and ASAN/UBSAN")
def test_frozen_operations_actual_python_memory_and_integer_bounds(tmp_path):
    root = Path(__file__).resolve().parents[1]
    output = tmp_path / "frozen_codec.so"
    build = subprocess.run(
        [
            "gcc",
            "-std=c99",
            "-O1",
            "-g",
            "-fPIC",
            "-shared",
            "-ffp-contract=off",
            "-fno-omit-frame-pointer",
            "-fsanitize=address,undefined",
            "-isystem",
            sysconfig.get_path("include"),
            "-I",
            str(root / "native/vendor/yyjson"),
            str(root / "native/frozen_codec.c"),
            str(root / "native/vendor/yyjson/yyjson.c"),
            "-lm",
            "-o",
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert build.returncode == 0, build.stderr[-8000:]
    library = subprocess.run(
        ["gcc", "-print-file-name=libasan.so"], capture_output=True, text=True, timeout=5, check=True
    ).stdout.strip()
    assert Path(library).is_file()
    environment = dict(
        os.environ,
        LD_PRELOAD=library,
        ASAN_OPTIONS="detect_leaks=0:abort_on_error=1",
        UBSAN_OPTIONS="halt_on_error=1:print_stacktrace=0",
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONMALLOC="malloc",
    )
    # Reference-count controls run in the ordinary differential tests as well.
    # LeakSanitizer is disabled for the embedding interpreter, not advertised as
    # a process-wide leak proof. Address/UB failures remain terminal.
    code = """
import sys
from pathlib import Path
from cryptowatcher import frozen_codec_native as native
native.artifact = lambda: Path(sys.argv[1])
native.load.cache_clear()
assert native.available()
import pytest
raise SystemExit(pytest.main([
    '-q', '-p', 'no:cacheprovider', '--basetemp', sys.argv[2],
    'tests/test_frozen_codec_native.py', 'tests/test_frozen_typed_native.py',
    'tests/test_frozen_source_native.py', 'tests/test_source_slot_columns.py',
]))
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", code, str(output), str(tmp_path / "pytest")],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-10000:] + result.stderr[-8000:]
    assert "AddressSanitizer" not in result.stderr and "runtime error:" not in result.stderr
