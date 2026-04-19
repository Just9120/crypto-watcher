# CryptoWatcher — State Contract

## 1. Purpose

This document defines what belongs in persistent chat state, what belongs in `.env`, and how migration must behave.

It exists to reduce accidental regressions during product iteration.

## 2. Ownership model

### `.env`
Used for:
- secrets
- default values
- infrastructure/runtime configuration
- signal thresholds
- polling cadence defaults
- deployment-specific parameters

### persistent chat state
Used for:
- actual per-chat behavior
- active radar mode
- custom symbol list
- mute data
- temporary chat workflow state
- per-chat runtime timestamps

## 3. Important state fields

### Product behavior
- `settings.alert_universe_mode`
  - allowed values: `top`, `custom`
  - defines which alert universe is active

- `settings.custom_pairs`
  - normalized Bybit symbols
  - per-chat active custom list

### Runtime
- `runtime.last_poll_ts`
- `runtime.pending_action`

### Operational
- `mutes`
- `baselines`

## 4. Legacy migration rules

Older persisted chats may not contain:
- `alert_universe_mode`
- `custom_pairs`

Migration rules:

1. If raw persisted `alert_universe_mode` exists, it is authoritative.
2. If raw persisted `alert_universe_mode` does not exist, derive mode from legacy behavior.
3. If raw persisted `custom_pairs` exists, it is authoritative, including an explicit empty list.
4. If `custom_pairs` does not exist, derive it from legacy raw fields in this order:
   - `bybit_pairs`
   - `watchlist`
   - otherwise empty list
5. Normalize symbols with `_normalize_bybit_pair`.
6. Preserve order while deduplicating.

Critical rule:
migration decisions must use raw persisted settings, not already default-merged settings.

## 5. Truthfulness rule

All user-facing surfaces must agree on active state:

- polling behavior
- `/status`
- settings text
- watchlist/list view
- help text where relevant

Example:
if alerts scan Top 100, status must not behave as if only the custom list exists.

## 6. Backward compatibility rule

A PR that changes state structure is incomplete unless it explicitly covers migration behavior.

At minimum, verify:
- legacy payload with old `bybit_pairs`
- legacy payload with old `watchlist`
- explicit empty `custom_pairs`
- explicit `alert_universe_mode`

## 7. Change policy

Any PR that adds or changes persistent state fields must update this document.
