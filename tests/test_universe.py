from unittest.mock import AsyncMock

import pytest

from cryptowatcher.config import ConfigError, OwnerSettings
from cryptowatcher.market import MarketError
from cryptowatcher.universe import active_instruments, classify, discover


def metadata(symbol="BTCUSDT", market="spot", **changes):
    return {
        "symbol": symbol,
        "baseCoin": symbol[:-4],
        "quoteCoin": "USDT",
        "status": "Trading",
        "symbolType": "",
        "priceFilter": {"tickSize": "0.01"},
        **(
            {"contractType": "LinearPerpetual", "settleCoin": "USDT", "isPreListing": False}
            if market == "linear"
            else {}
        ),
        **changes,
    }


@pytest.mark.parametrize(
    "market,change,reason",
    [
        ("spot", {"quoteCoin": "BTC"}, "not_usdt_quote"),
        ("spot", {"status": "Closed"}, "not_trading"),
        ("spot", {"symbolType": "xstocks"}, "special_or_unknown_type"),
        ("spot", {"symbolType": "future_special_type"}, "special_or_unknown_type"),
        ("spot", {"baseCoin": "PAXG"}, "tokenized_gold"),
        ("linear", {"symbolType": "commodity"}, "special_or_unknown_type"),
        ("linear", {"symbolType": "stock"}, "special_or_unknown_type"),
        ("linear", {"isPreListing": True}, "not_live_usdt_perpetual"),
        ("linear", {"settleCoin": "USDC"}, "not_live_usdt_perpetual"),
        ("linear", {"contractType": "LinearFutures"}, "not_live_usdt_perpetual"),
        ("linear", {"underlyingTicker": "AAPL"}, "tradfi_underlying"),
        ("spot", {"priceFilter": {"tickSize": "NaN"}}, "invalid_metadata"),
    ],
)
def test_quarantine_reasons(market, change, reason):
    assert classify(market, metadata(market=market, **change)) == (None, reason)


async def test_full_linear_pagination_spot_no_cursor_and_more_than_100():
    adapter = AsyncMock()
    adapter._get.side_effect = [
        {"result": {"list": [metadata(f"C{i}USDT", "linear") for i in range(120)], "nextPageCursor": "next"}},
        {"result": {"list": [metadata("ONDOUSDT", "linear")], "nextPageCursor": ""}},
        {"result": {"list": [metadata("ONDOUSDT")], "nextPageCursor": "ignored-for-spot"}},
    ]
    linear, rejected = await discover(adapter, "linear")
    spot, _ = await discover(adapter, "spot")
    assert len(linear) == 121 and not rejected
    assert linear["linear/ONDOUSDT"].base == spot["spot/ONDOUSDT"].base == "ONDO"
    assert adapter._get.call_args_list[1].args[1]["cursor"] == "next"
    assert "cursor" not in adapter._get.call_args_list[2].args[1]
    assert "limit" not in adapter._get.call_args_list[2].args[1]


async def test_cursor_loop_or_duplicate_metadata_rejects_partial_universe():
    adapter = AsyncMock()
    adapter._get.side_effect = [{"result": {"list": [], "nextPageCursor": "loop"}}] * 2
    with pytest.raises(MarketError, match="repeated_metadata_cursor"):
        await discover(adapter, "linear")
    adapter._get.side_effect = [{"result": {"list": [metadata(), metadata()]}}]
    with pytest.raises(MarketError, match="duplicate_metadata_record"):
        await discover(adapter, "spot")


def test_liquidity_boundary_typed_watchlist_and_saved_sensitivity(store):
    instruments = {}
    for market in ("spot", "linear"):
        instrument, _ = classify(market, metadata(market=market))
        instruments[instrument.key] = instrument
    tickers = {key: {"turnover24h": "1000000", "lastPrice": "100"} for key in instruments}
    s = OwnerSettings()
    assert len(active_instruments(instruments, tickers, s)[0]) == 2
    assert not active_instruments(instruments, tickers, s.changed(liquidity_floor_24h=1000001))[0]
    s = s.changed(watchlist=("linear/BTCUSDT",), alert_universe_mode="custom", watchlist_multiplier=0.6)
    assert [i.key for i in active_instruments(instruments, tickers, s)[0]] == ["linear/BTCUSDT"]
    saved = store.update(store.snapshot().revision, s)
    assert store.snapshot().settings == saved.settings
    assert OwnerSettings.from_payload(s.payload()).watchlist_multiplier == 0.6
    assert active_instruments(instruments, tickers, s.changed(watchlist=(), custom_pairs=())) == ([], {})
    with pytest.raises(ConfigError):
        s.changed(watchlist=("unknown/BTCUSDT",))
    with pytest.raises(ConfigError):
        s.changed(watchlist_multiplier=float("inf"))
