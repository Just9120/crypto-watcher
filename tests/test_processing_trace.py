"""Diagnostic integrity, restoration and finite evidence; no acceptance substitution."""

import json
import sqlite3

import pytest

from cryptowatcher.events import EventEngine
from tests.processing_replay import replay
from tests.processing_trace import MAX_REPORT, Trace, point_difference


async def test_trace_preserves_real_flow_and_reports_exact_differences(tmp_path):
    original_connect, original_observe = sqlite3.connect, EventEngine.observe
    result = await replay(
        tmp_path / "trace.sqlite3",
        instruments=3,
        seconds=8,
        direct_sqlite=True,
        bounded_pool=True,
        diagnostic=True,
    )
    trace = result["diagnostic"]
    assert trace["complete"] and trace["mode"] == "DIAGNOSTIC_NOT_ACCEPTANCE"
    assert (
        trace["frozen_oracle_difference"]
        == trace["aligned_oracle_difference"]
        == {
            "missing_count": 0,
            "extra_count": 0,
            "missing": [],
            "extra": [],
        }
    )
    assert trace["aligned_expected_count"] == result["bounded_pool"]["global_actual"]
    assert len(trace["events"]) == result["bounded_pool"]["global_actual"]
    assert all(0 <= event["action"] < len(trace["actions"]) for event in trace["events"])
    assert {a["kind"] for a in trace["actions"]} == {"observe", "tick"}
    assert all(a["phase"] in ("SEED", "PHASE") for a in trace["actions"])
    assert trace["timing"]["PHASE:global:sql_read"]["calls"] > 0
    assert trace["timing"]["PHASE:global:sql_write"]["calls"] > 0
    assert trace["timing"]["PHASE:global:sql_transaction"]["calls"] > 0
    observations = sum(a["kind"] == "observe" and a["phase"] == "PHASE" for a in trace["actions"])
    assert observations >= result["accepted_closed"]
    # Re-arm also evaluates two preceding bars; those real calls must remain counted.
    assert trace["timing"]["PHASE:global:detector"]["calls"] >= observations * 5
    assert trace["timing"]["PHASE:global:encode"]["cpu_seconds"] > 0
    assert trace["agent_selections"]
    selections = [s for s in trace["agent_selections"] if "kind" in s]
    assert selections and all(
        s["kind"] in ("LIVE_EVENT", "EPISODE_UPDATE", "HISTORICAL_OUTCOME_UPDATE") for s in selections
    )
    assert all("event_live" in s and "fresh_until_ms" in s and "due_ms" in s for s in selections)
    raw = json.dumps(trace)
    assert len(raw.encode()) < MAX_REPORT
    assert "AGENT_HMAC_SECRET" not in raw and "Authorization" not in raw
    assert result["accepted_closed"] == result["offered_closed"] == 24
    # This8s fixture ends before the next closed1m wave; the full130s guard is unchanged.
    assert result["bounded_pool"]["refresh_receiver_checks"][0]["preserved"] is True
    assert result["direct_sqlite"]["foreign_key_violations"] == []
    assert result["producer_errors"] == result["worker_errors"] == result["receiver_errors"] == []
    assert sqlite3.connect is original_connect and EventEngine.observe is original_observe
    assert not (tmp_path / "aligned-oracle.sqlite3").exists()


def test_trace_failure_restores_sqlite_and_engine_and_bound_is_explicit():
    original_connect, original_observe = sqlite3.connect, EventEngine.observe
    trace = Trace(True)
    with pytest.raises(RuntimeError, match="DIAGNOSTIC_TRACE_BOUND_EXCEEDED"):
        with trace:
            trace.append(trace.actions, ("test",), 1)
            trace.append(trace.actions, ("test",), 1)
    assert trace.truncated and len(trace.actions) == 1
    assert sqlite3.connect is original_connect and EventEngine.observe is original_observe


def test_point_diff_keeps_missing_extra_and_multiplicity():
    result = point_difference(['{"id":1}', '{"id":1}', '{"id":2}'], ['{"id":1}', '{"id":3}', '{"id":3}'])
    assert result == {
        "missing_count": 2,
        "extra_count": 2,
        "missing": [{"count": 1, "point": {"id": 1}}, {"count": 1, "point": {"id": 2}}],
        "extra": [{"count": 2, "point": {"id": 3}}],
    }
