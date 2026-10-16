# Synthetic market event v2

Этот JSON получен из текущего producer fixture. Он предназначен для изучения
контракта и изолированной schema validation. Это **не рыночное событие**:
контролируемое время — 2027-01-15, producer — `test-radar`, source SHA —
повторяющаяся `a`. Не отправлять в production и не использовать как live smoke.

[Граница ответственности, transport и acceptance checklist](../runbooks/agent-delivery.md#handoff-принимающему-проекту).

JSON внутри блока в UTF-8/LF, включая последний перевод строки: 7716 bytes,
SHA256 `2a65a5d0de157deb6039c0dfb9c4551ded95f89d00135604d36a483c88bc6bbf`.
Для сверки нормализовать переводы строк в LF; BOM отсутствует.
Это hash примера, не HMAC запроса. При отправке подпись вычисляется по точным
request bytes; credentials и подпись в пример не включены.

```json
{
  "completeness": "COMPLETE",
  "continuation_threshold_pct": 0.16666666666666669,
  "delivery_kind": "LIVE_EVENT",
  "detected_at": "2027-01-15T08:00:00.000+00:00",
  "detector_id": "price",
  "detector_version": "price.v1",
  "direction": "UP",
  "effective_parameters": {
    "breakout_buffer": 0.07500000000000001,
    "breakout_lookback": 20.0,
    "price_15m": 3.0,
    "price_1m": 0.75,
    "price_3m": 1.5,
    "price_5m": 1.875,
    "turnover_lookback": 12.0,
    "turnover_ratio": 2.25,
    "volatility_lookback": 20.0,
    "volatility_ratio": 1.5
  },
  "enrichment": {
    "detector_input": false,
    "mapping_source_sha256": null,
    "methodology_version": "1",
    "metrics": {},
    "schema_version": "cryptowatcher.derivatives_context.v1",
    "state": "UNAVAILABLE"
  },
  "episode": {
    "available_at": "2027-01-15T08:00:00.000+00:00",
    "close_reason": null,
    "closed_at": null,
    "confirmations": [
      {
        "detector_id": "breakout",
        "detector_version": "breakout.v1",
        "event_id": "a9100b0c-5c92-5ad3-b1c2-3f39ecbbd6a6",
        "expires_at": "2027-01-15T08:04:00.000+00:00",
        "fresh_until": "2027-01-15T08:02:00.000+00:00",
        "market_observed_at": "2027-01-15T08:00:00.000+00:00",
        "metric_units": {
          "atr14": "USDT",
          "breakout_buffer": "USDT",
          "breakout_distance": "USDT",
          "range_high": "USDT",
          "range_low": "USDT"
        },
        "metrics": {
          "atr14": 2.0,
          "breakout_buffer": 0.15000000000000002,
          "breakout_distance": 19.0,
          "range_high": 101.0,
          "range_low": 99.0
        },
        "settings_revision": 2,
        "settings_sha256": "05bcf00615dbdf43e619ada7ae2cdeb8a0d8b1a98adda1547a7bb8d990f8fdb2",
        "source_event_revision": 1,
        "strength": 126.66666666666664,
        "timeframe": "1m"
      },
      {
        "detector_id": "composite",
        "detector_version": "composite.v1",
        "event_id": "3ba3f529-fcdc-5fbd-b98f-6fa024aaebed",
        "expires_at": "2027-01-15T08:02:00.000+00:00",
        "fresh_until": "2027-01-15T08:01:00.000+00:00",
        "market_observed_at": "2027-01-15T08:00:00.000+00:00",
        "metric_units": {
          "liquidity_24h": "USDT",
          "liquidity_floor": "USDT",
          "price_return": "PERCENT",
          "price_threshold": "PERCENT",
          "turnover": "USDT",
          "turnover_mean": "USDT",
          "turnover_ratio": "RATIO",
          "turnover_threshold": "RATIO"
        },
        "metrics": {
          "liquidity_24h": 1000000.0,
          "liquidity_floor": 1000000.0,
          "price_return": 19.999999999999996,
          "price_threshold": 0.75,
          "turnover": 10000.0,
          "turnover_mean": 400.0,
          "turnover_ratio": 25.0,
          "turnover_threshold": 2.25
        },
        "settings_revision": 2,
        "settings_sha256": "05bcf00615dbdf43e619ada7ae2cdeb8a0d8b1a98adda1547a7bb8d990f8fdb2",
        "source_event_revision": 1,
        "strength": 11.11111111111111,
        "timeframe": "1m"
      },
      {
        "detector_id": "price",
        "detector_version": "price.v1",
        "event_id": "b3e2dabd-9b0c-5972-8b18-e43acc95ccd2",
        "expires_at": "2027-01-15T08:02:00.000+00:00",
        "fresh_until": "2027-01-15T08:01:00.000+00:00",
        "market_observed_at": "2027-01-15T08:00:00.000+00:00",
        "metric_units": {
          "price_return": "PERCENT",
          "price_threshold": "PERCENT"
        },
        "metrics": {
          "price_return": 19.999999999999996,
          "price_threshold": 0.75
        },
        "settings_revision": 2,
        "settings_sha256": "05bcf00615dbdf43e619ada7ae2cdeb8a0d8b1a98adda1547a7bb8d990f8fdb2",
        "source_event_revision": 1,
        "strength": 26.66666666666666,
        "timeframe": "1m"
      },
      {
        "detector_id": "price",
        "detector_version": "price.v1",
        "event_id": "daa7d586-4322-5a72-acb2-91596143b6b5",
        "expires_at": "2027-01-15T08:06:00.000+00:00",
        "fresh_until": "2027-01-15T08:03:00.000+00:00",
        "market_observed_at": "2027-01-15T08:00:00.000+00:00",
        "metric_units": {
          "price_return": "PERCENT",
          "price_threshold": "PERCENT"
        },
        "metrics": {
          "price_return": 19.999999999999996,
          "price_threshold": 1.5
        },
        "settings_revision": 2,
        "settings_sha256": "05bcf00615dbdf43e619ada7ae2cdeb8a0d8b1a98adda1547a7bb8d990f8fdb2",
        "source_event_revision": 1,
        "strength": 13.33333333333333,
        "timeframe": "3m"
      }
    ],
    "direction": "UP",
    "episode_id": "5bb27e1e-9055-5790-9369-e601ec188f27",
    "last_observation_at": "2027-01-15T08:00:00.000+00:00",
    "longest_timeframe": "3m",
    "market_type": "SPOT",
    "revision": 4,
    "started_at": "2027-01-15T08:00:00.000+00:00",
    "state": "ACTIVE",
    "strength": 126.66666666666664,
    "symbol": "BTCUSDT"
  },
  "episode_id": "5bb27e1e-9055-5790-9369-e601ec188f27",
  "event_id": "daa7d586-4322-5a72-acb2-91596143b6b5",
  "expires_at": "2027-01-15T08:06:00.000+00:00",
  "fresh_until": "2027-01-15T08:03:00.000+00:00",
  "freshness_policy_version": "1",
  "historical_outcomes": [],
  "interval_start": "2027-01-15T07:57:00.000+00:00",
  "market_observed_at": "2027-01-15T08:00:00.000+00:00",
  "market_type": "SPOT",
  "metric_units": {
    "price_return": "PERCENT",
    "price_threshold": "PERCENT"
  },
  "metrics": {
    "price_return": 19.999999999999996,
    "price_threshold": 1.5
  },
  "model_invoked": false,
  "produced_at": "2027-01-15T08:00:02.000+00:00",
  "producer_ref": "test-radar",
  "producer_source_revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "profile": "sensitive",
  "reference_at": "2027-01-15T08:00:00.000+00:00",
  "reference_atr14": 2.0,
  "reference_price": 120.0,
  "revision": 1,
  "schema_version": "cryptowatcher.market_event.v2",
  "settings_revision": 2,
  "settings_sha256": "05bcf00615dbdf43e619ada7ae2cdeb8a0d8b1a98adda1547a7bb8d990f8fdb2",
  "settings_snapshot": {
    "alert_detectors": [
      "composite"
    ],
    "alert_universe_mode": "top",
    "bybit_category": "spot",
    "candle_retention_days": 35,
    "cooldown_min": 10,
    "custom_pairs": [],
    "detail_retention_days": 30,
    "detector_parameters": [
      [
        "breakout_buffer",
        0.07500000000000001
      ],
      [
        "breakout_lookback",
        20.0
      ],
      [
        "price_15m",
        3.0
      ],
      [
        "price_1m",
        0.75
      ],
      [
        "price_3m",
        1.5
      ],
      [
        "price_5m",
        1.875
      ],
      [
        "turnover_lookback",
        12.0
      ],
      [
        "turnover_ratio",
        2.25
      ],
      [
        "volatility_lookback",
        20.0
      ],
      [
        "volatility_ratio",
        1.5
      ]
    ],
    "digest_interval_min": 60,
    "enrichment_enabled": false,
    "liquidity_floor_24h": 1000000.0,
    "outcome_horizons": [
      5,
      15,
      30,
      60
    ],
    "price_move_min": 2.0,
    "profile": "sensitive",
    "radar_poll_sec": 90,
    "signal_timeframe": "5m",
    "sma_periods": 12,
    "turnover_spike_min": 3.0,
    "watchlist": [],
    "watchlist_multiplier": 0.75
  },
  "source_event_revision": 1,
  "source_received_at": "2027-01-15T08:00:00.000+00:00",
  "source_window_sha256": "d733866755838b9419dcabb5c83749b8f4753b645bb22ba3cf74fb0b6c8d84b9",
  "strength": 13.33333333333333,
  "symbol": "BTCUSDT",
  "tick_size": 0.01,
  "timeframe": "3m",
  "trading_authorized": false,
  "trading_instruction": false,
  "venue": "BYBIT"
}
```
