from pathlib import PurePosixPath as Path

import pytest

from cryptowatcher.maintenance import Unit
from scripts.launch_closed_source_maintenance import commands


def invocation(**kwargs):
    return commands(
        Path("/opt/crypto_watcher/rehearsals/test/operator/runner.py"),
        Path("/opt/crypto_watcher/rehearsals/test/operator"),
        Unit("a" * 64, "sha256:" + "b" * 64, "c" * 40),
        kwargs.get("root", Path("/opt/crypto_watcher/rehearsals/test")),
        Path("/opt/crypto_watcher/rehearsals/test/code"),
        "d" * 64,
        "cw-closed-qual-" + "e" * 32,
        one_time_maintenance=kwargs.get("one_time_maintenance", False),
    )


def test_default_preflight_cannot_stop_production_and_recovery_args_match_executor():
    preflight, launch = invocation()
    assert "--execute" not in preflight and "--restore-only" not in preflight
    assert "--property=RuntimeMaxSec=900" in launch
    assert "--property=UMask=0077" in launch
    restore = next(
        x.removeprefix("--property=ExecStopPost=") for x in launch if x.startswith("--property=ExecStopPost=")
    )
    assert restore.split() == preflight + ["--restore-only"]
    assert launch[-len(preflight) - 1 :] == preflight + ["--execute"]


def test_one_time_limit_is_explicit_and_bound_to_both_executor_and_recovery():
    preflight, launch = invocation(one_time_maintenance=True)
    assert "--property=RuntimeMaxSec=3600" in launch
    assert preflight[-1] == "--one-time-maintenance"
    restore = next(x.split("=", 2)[2] for x in launch if x.startswith("--property=ExecStopPost="))
    assert restore.split() == preflight + ["--restore-only"]
    assert "--one-time-maintenance" not in invocation()[0]


@pytest.mark.parametrize(
    "root",
    [
        "../different",
        "/tmp/../etc",
        "/path with spaces",
        "/path%specifier",
        "/path;command",
        "/path\ncommand",
    ],
)
def test_shell_syntax_specifiers_relative_or_ambiguous_paths_cannot_enter_systemd_properties(root):
    with pytest.raises(ValueError):
        invocation(root=Path(root))
