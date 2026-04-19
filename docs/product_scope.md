# CryptoWatcher — Product Scope

## 1. Purpose

CryptoWatcher is a Telegram bot for manual spot trading on Bybit.

The current product goal is not generic price monitoring and not portfolio tracking.
The bot must detect actionable short-term radar signals on Bybit Spot and send trader-friendly alerts to Telegram.

## 2. Current MVP scope

Current MVP = Bybit Spot Volume Radar.

Core user outcome:
the user opens Telegram, receives alerts about unusual short-term activity on liquid Bybit spot pairs, and can quickly jump into the Bybit terminal.

Current MVP includes:

- Bybit Spot as the primary and default product path.
- Composite radar signal based on short-term price move, turnover spike, and 24h liquidity floor.
- Default alert universe = Top 100 Bybit pairs by 24h turnover.
- Alternative alert universe = per-chat custom list.
- Persistent Telegram bottom keyboard as the main user interface.
- Russian Telegram UX texts.
- Quick symbol actions from alerts.
- Glossary / terms explanation inside Telegram.
- Per-chat persistent state for radar mode, custom list, mute state, and runtime workflow.

## 3. Non-goals for current MVP

The following are explicitly out of scope for the current MVP:

- CoinMarketCap as a primary product path.
- Old baseline-driven percent monitor as the main alerting model.
- Futures mode.
- Portfolio/account integration.
- Order execution from Telegram.
- Multi-exchange routing.
- Rich analytics dashboards.
- Returning to `.env` as the main user list management mechanism.

These ideas may be explored later, but they are not part of the current MVP contract.

## 4. Product principles

1. Main path first.
The main user path is Bybit Spot Volume Radar, not legacy monitor behavior.

2. Actionability over noise.
Signals should prioritize trader usefulness over raw message volume.

3. Telegram-first UX.
The bot should feel like a compact control deck, not a slash-command-only utility.

4. Per-chat state.
User-specific behavior belongs in persistent chat state, not in static server config.

5. Safe iteration.
New work must not silently break migrated legacy chats.

## 5. Signal contract

An alert is emitted only if all conditions are true:

1. `abs(price_change_5m) >= PRICE_MOVE_MIN`
2. `turnover_spike_ratio >= TURNOVER_SPIKE_MIN`
3. `turnover24h >= LIQUIDITY_FLOOR_24H`

Where:

- `price_change_5m` = move on the closed 5m candle
- `turnover_spike_ratio` = `current_5m_turnover / sma_5m_turnover`
- `sma_5m_turnover` = average turnover over the configured number of prior 5m candles
- `turnover24h` = Bybit 24h turnover for the symbol

Important product rule:
signal logic must use closed 5m candles only.
Do not build the main alert decision on still-forming candles.

## 6. Alert universe contract

Two supported alert universe modes exist:

### `top`
Default mode.

Behavior:
- Radar scans Top 100 Bybit spot pairs by 24h turnover.
- This is the default mode for fresh chats unless explicitly changed.

### `custom`
Alternative mode.

Behavior:
- Radar scans only the per-chat custom list.
- Custom list is managed from Telegram and stored in chat state.

Important rule:
`top` and `custom` are product-level modes.
Status, settings, help text, and alert behavior must all describe the same active mode truthfully.

## 7. Telegram UX contract

Main UX is a persistent bottom keyboard.

Expected main controls:

- `📊 Статус`
- `⚙️ Настройки`
- `Радар: Top 100`
- `Радар: Мой список`
- `Список`
- `Добавить монету`
- `Удалить монету`
- `Очистить список`
- `Термины`

Slash commands remain supported as a secondary interface.

Expected UX behavior:

- The bot should be usable without memorizing slash commands.
- User list operations should work through a simple conversational flow.
- Alerts should include a fast path to the Bybit trading screen.
- Terms and settings text should be understandable without reading source code or `.env`.

## 8. State ownership rules

`.env` is for defaults and technical parameters.

Persistent chat state is for active user behavior.

### `.env` owns:
- secrets and tokens
- technical polling intervals
- signal thresholds
- runtime infrastructure parameters
- default values

### chat state owns:
- `alert_universe_mode`
- `custom_pairs`
- per-chat mute state
- per-chat runtime workflow such as add/remove pending action
- other per-chat operational state

Important rule:
do not move active user list management back into `.env`.

## 9. Backward compatibility rules

Changes must preserve safe migration for legacy persisted chats.

When new state fields are introduced:
- old chats must continue to work after restart
- legacy list behavior must not be silently lost
- migration must use raw persisted legacy data, not accidentally overwritten default-merged values

## 10. Deployment readiness rules

A change is deployable only if:

- it preserves current MVP scope
- it does not reintroduce legacy CMC-first behavior into the main path
- state migration is safe
- `/status` reflects real radar behavior
- no-op settings interactions do not fail noisily
- Telegram help/settings text renders without parse errors
- runtime startup succeeds under the current Docker deployment path

## 11. Current accepted limitations

These are known and accepted for the MVP stage:

- the bot depends on server time being sane
- top-mode radar may put pressure on rate limits if scaled much further
- legacy dead code may still exist outside the main product path
- current architecture is intentionally simple and single-process

These are not blockers for the current MVP unless they cause user-visible incorrect behavior.

## 12. Change policy

Any new PR must preserve this scope unless it explicitly states a scope change.

In particular, do not:
- restore CMC as the main path
- restore the old percent/baseline monitor as the main product model
- move user list management back to `.env`
- ship status/help/settings text that contradict actual runtime behavior

If scope changes, this document must be updated in the same PR.
