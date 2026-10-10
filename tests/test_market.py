from dataclasses import replace

import httpx
import pytest

from cryptowatcher.config import OwnerSettings
from cryptowatcher.market import BybitREST, MarketError, evaluate_composite

NOW = 1800000000000
STEP = 300000


def rows():
    return [
        [
            NOW - (i + 1) * STEP,
            "100",
            "103",
            "97",
            "102" if i == 0 else "100",
            "1",
            "1200" if i == 0 else "400",
        ]
        for i in range(14)
    ]


def ticker():
    return {"lastPrice": "102", "turnover24h": "1000000"}


def test_composite_equality_closed_inputs_and_direction():
    s = OwnerSettings()
    result = evaluate_composite(rows(), ticker(), s, NOW)
    assert result and result["meets_signal"]
    assert result["turnover_spike_ratio"] == 3
    assert result["sma_tf_turnover"] == 400
    assert result["candle_end_ms"] == NOW
    open_row = [NOW, "100", "999", "1", "999", "1", "99999999"]
    assert evaluate_composite([open_row, *rows()], ticker(), s, NOW) == result
    down = rows()
    down[0][4] = "98"
    result = evaluate_composite(down, ticker(), s, NOW)
    assert result and result["meets_signal"] and result["price_change_tf"] < 0


@pytest.mark.parametrize(
    "changes", [{"price_move_min": 3}, {"turnover_spike_min": 4}, {"liquidity_floor_24h": 2000000}]
)
def test_each_composite_filter_blocks(changes):
    result = evaluate_composite(rows(), ticker(), OwnerSettings().changed(**changes), NOW)
    assert result and not result["meets_signal"]


def test_zero_incomplete_gap_and_invalid_windows():
    assert evaluate_composite(rows()[:3], ticker(), OwnerSettings(), NOW) is None
    zero = rows()
    for row in zero[1:]:
        row[6] = "0"
    assert evaluate_composite(zero, ticker(), OwnerSettings(), NOW) is None
    bad = rows()
    bad[3][0] -= STEP
    with pytest.raises(MarketError):
        evaluate_composite(bad, ticker(), OwnerSettings(), NOW)
    bad = rows()
    bad[0][4] = "nan"
    with pytest.raises(MarketError):
        evaluate_composite(bad, ticker(), OwnerSettings(), NOW)


async def test_rest_adapter_uses_bybit_clock_and_reports_missing_symbol():
    requests = []

    def respond(request):
        requests.append(request)
        payload = {
            "retCode": 0,
            "time": NOW,
            "result": {
                "list": [dict(ticker(), symbol="BTCUSDT"), dict(ticker(), symbol="ETHBTC")]
                if request.url.path.endswith("tickers")
                else rows()
            },
        }
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = BybitREST(client)
        result = await adapter.snapshot(
            replace(OwnerSettings(), alert_universe_mode="custom", custom_pairs=("BTCUSDT", "SOLUSDT"))
        )
        assert result.quotes["BTCUSDT"]["candle_end_ms"] == NOW
        assert result.errors == {"SOLUSDT": "instrument_not_in_ticker_snapshot"}
        assert all(request.url.host == "api.bybit.com" for request in requests)
