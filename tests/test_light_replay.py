"""The minimal complete path is a gate, not a capacity or live-transport claim."""

from .light_replay import replay


async def test_full_offered_source_episode_outcome_and_independent_delivery_path(tmp_path):
    result = await replay(tmp_path / "replay.sqlite3", minutes=15)
    assert result["offered_closed_candles"] == result["persisted_and_confirmed"]
    assert len(result["offers_by_stream"]) == 8
    assert result["families"] == ["breakout", "composite", "price", "turnover", "volatility"]
    assert result["outcome_states"] == {"COMPLETE": result["episodes"] * 4}
    assert result["comparison_jobs"] == 0 and result["comparison_states"] == {}
    assert all(p["due_outcomes"] == 0 for p in result["debt_checkpoints"])
    assert any(p["future_outcomes"] > 0 for p in result["debt_checkpoints"])
    assert result["source_freshness_at_offer_end"]["late_streams"] == 0
    assert result["source_freshness_at_offer_end"]["unknown_streams"] == 0
    assert result["source_freshness_at_offer_end"]["grace_ms"] == 30_000
    assert result["telegram_attempts"] > 0 and result["agent_receipts"] > 0
    assert result["initial_root_result_count"] == 16
    # Numeric facts of the first four roots match the identical replay on main
    # b254e6e, independently of later per-candle/event admissions in that version.
    assert (
        result["initial_root_numeric_sha256"]
        == "bf172fb0a0533ad4d74526a9b54f0e612d0045447ddd648711c34c63b8777559"
    )
