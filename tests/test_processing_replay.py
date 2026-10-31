"""Joint offered-arrival qualification; Linux and live-host gates are distinct."""

import logging
import sys

import pytest

from .processing_replay import replay


def assert_joint_result(result):
    assert result["producer_errors"] == result["worker_errors"] == result["receiver_errors"] == []
    assert result["offered_closed"] == result["accepted_closed"]
    assert result["max_source_lag_ms"] <= 30000
    assert result["max_completed_due_lag_ms"] <= 60000
    assert result["due_pending"] == 0
    assert result["future_jobs"] > 0
    assert result["comparison_jobs"] == 0
    assert result["families"] == ["breakout", "composite", "price", "turnover", "volatility"]
    assert result["telegram_attempts"] > 0 and result["signed_agent_receipts"] > 0
    assert result["analytics"]["state"] == "OK"
    assert result["recovery_worker"]["state"] == "OK"
    assert result["source_freshness_at_endpoint"]["late_streams"] == 0
    assert result["source_freshness_at_endpoint"]["unknown_streams"] == 0
    assert result["repack"]


async def test_paced_receivers_calculations_delivery_and_maintenance_share_one_writer(tmp_path, caplog):
    result = await replay(tmp_path / "paced.sqlite3", instruments=2, seconds=12)
    assert result["streams"] == 16
    assert_joint_result(result)
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


@pytest.mark.skipif(
    sys.platform != "linux", reason="Required Linux/native contour; Windows is not its qualification"
)
async def test_native_broad_universe_joint_processing_deadlines(tmp_path, caplog):
    result = await replay(tmp_path / "broad.sqlite3", instruments=206, seconds=130)
    assert result["native_backend"] is True
    assert result["streams"] == 1648
    assert result["offered_closed"] == 2472
    assert_joint_result(result)
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
