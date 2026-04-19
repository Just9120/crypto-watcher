# CryptoWatcher — Open Issues

## Current pre-deploy issues to verify

### 1. Legacy state migration
Need to ensure that old persisted chats do not lose their list behavior after restart.

Risk:
default-merged values can accidentally overwrite the intended legacy fallback.

### 2. `/status` truthfulness in `top` mode
Need to ensure that status output reflects the actual radar universe.

Risk:
alerts may scan Top 100 while `/status` still behaves like custom-list-only mode.

### 3. No-op settings taps
Need to ensure repeated taps on already-selected settings do not fail with callback query errors.

Risk:
double callback acknowledgement may trigger Telegram-side errors.

## Already observed production-relevant regressions from earlier iterations

- help/settings parse error
- `Message is not modified`
- noisy CMC fallback log spam

These should remain fixed and must not be reintroduced.

## Next cleanup candidates after safe deploy

These are not current blockers, but they are good follow-up work:

- remove dead legacy code paths not used by the main radar product path
- split the large bot file into smaller modules
- add lightweight regression checks for state migration and top-mode status behavior
- validate server time sync assumptions
- review Bybit request pressure in top-mode polling
