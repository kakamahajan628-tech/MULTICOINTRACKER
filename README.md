# Whale Wallet Intelligence v0.8.0

Your existing multi-chain Whale Watcher, integrated with a modular Python liquidity
engine for Binance USD-M Futures and Bybit USDT linear perpetuals.

The delivered copy contains the wallet tracker, Excel import and four cohorts,
Telegram control/outbox, public market snapshots, 1h/4h screening, continuous
sequenced L2, CVD/OFI, wall/absorption/iceberg candidates, uncertain liquidation
scenario bands, cross-venue comparison, recording/replay and taker backtesting.

This update adds read-only open-order study, executed-fill evidence, position-condition
reports, normalized/atomic watch imports, failed-login throttling, fail-closed auth,
lock-bound audits and bounded offline test runs. See [order reports](docs/ORDER_REPORTS.md)
and [review fixes](docs/SECURITY_HARDENING.md).

## Quick preview: no accounts, no network

Python 3.12 or 3.13:

~~~powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.lock
.venv\Scripts\python.exe -m pip install --no-deps -e .
.venv\Scripts\python.exe scripts\demo_wallet_desk.py
~~~

Open http://127.0.0.1:8341 and follow "Open heatmap & evidence desk".
The loopback demo uses synthetic wallets and synthetic exchange events.
Synthetic observations cannot generate live wallet/market associations or Telegram alerts.
The demo is deliberately separate from production credentials and databases.

macOS/Linux: use .venv/bin/python in place of .venv\Scripts\python.exe.

## Run your real tracker

1. Copy .env.whale.example to .env.whale.
2. Set a random WHALE_ADMIN_TOKEN of at least 32 printable ASCII characters and your database/provider settings.
3. Run python -m uvicorn whale_watcher.app:app --host 127.0.0.1 --port 8002 --env-file .env.whale --workers 1.
4. Open http://127.0.0.1:8002. Username: whale; password: WHALE_ADMIN_TOKEN.
5. Import your normal wallet Excel. Existing source access/budget requirements remain.

Native coins map automatically. Token contracts require an explicit verified
mapping, for example:

~~~text
/marketlink ethereum:0xYOUR_CONTRACT PEPE
/screenlink hyperliquid:ETH ethereum:native
~~~

Mappings attach market context; they do not prove identical wrappers, bridge
custody, order identities or fill attribution. Coins without a supported Binance/
Bybit perpetual remain unavailable for this module.

The collector is enabled by default and follows mapped imported assets.
Default limit: four venue/coin pairs (usually two coins × two venues), shared by
all 1,000 watch entries. Additional markets are visibly deferred.
WHALE_LIQUIDITY_MAX_MARKETS configures this budget. A 1,000-entry import limit is
not a verified 24/7 hosting or API entitlement.

## Neutral public-wallet imports

See [LOCAL-LIVE.md](LOCAL-LIVE.md) for Solana inventory discovery, authenticated
append-only CSV imports, neutral Hyperliquid watches and the local launch helper.
CSV P&L stays historical; current account state is fetched separately. Unclassified
watches never become a profit/loss cohort solely from an imported unrealized figure.

## Commands and pages

~~~text
/liquidity
/liquidity ETH
/report liquidity ETH
/liquidity off
/liquidity on
~~~

- /: wallet reports plus shared liquidity and latest wallet/market associations.
- /liquidity: heatmap, CVD, ladder, candidate evidence and modeled liquidation bands.
- /api/liquidity: authenticated current state and quality.
- /api/liquidity/state?market=binance:ETHUSDT: selected chart history.
- Existing /market, /screen, /screen4h and four cohort reports remain available.

On/off controls persist. Existing /pause and /resume control wallet scan scheduling;
use /liquidity off to stop continuous market feeds. A single server worker owns
the shared collector; do not start multiple web workers against the same journal.

## What is joined

A bounded market sample is captured at wallet scan START, before wallet API calls.
The scan stores visible observations, derived flow metrics, inferred candidates,
modeled bands and wallet associations separately. Cohort reports and Telegram
summaries include that context. Six-scan and four-hour screens summarize samples;
they never add CVD levels or sampled OFI into invented hourly executed volume.

Wallet increases are balance changes, not confirmed purchases. A candidate such
as wallet increase + bid absorption is an association, not proof that a tracked
wallet placed a particular Binance/Bybit order. Exact contract identity, stale
data, source changes and synthetic exclusion have explicit checks.

## Recording, replay and backtest

Local embedded runs record raw normalized events to runtime/liquidity-events.sqlite.
Set WHALE_LIQUIDITY_JOURNAL to another writable durable location; set it empty to
disable tick recording. Compact scan records persist in the existing wallet DB.

~~~sh
python -m liquidity.cli replay runtime/liquidity-events.sqlite --output runtime/replayed.json
python -m liquidity.cli export runtime/liquidity-events.sqlite --output runtime/events.jsonl
# Standalone replay requires a separate token (32+ random characters):
$env:DASHBOARD_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
python -m liquidity.cli serve --mode replay --input runtime/events.jsonl --port 8080
python -m liquidity.cli backtest runtime/events.jsonl --market binance:ETHUSDT --output runtime/backtest.json
python -m liquidity.cli backtest examples/demo.jsonl --allow-without-flow --imbalance-threshold 0 --holding-ms 1000 --output runtime/cost-sanity.json
~~~

The included backtester uses future observed books after latency, full taker depth
walks, both-leg fees and adverse slippage. It reports gap/insufficient-depth
exclusions and open-at-end positions. Passive queue fills, market-impact feedback,
funding and private margin accounts are outside its scope.

## Telegram

Existing WHALE_TELEGRAM_TOKEN and WHALE_TELEGRAM_OWNER_ID serve wallet, market and
liquidity summaries through the durable outbox. Extra candidate alerts require
WHALE_LIQUIDITY_ALERTS=true; they are score-filtered, bounded and deduplicated.
No real message was sent during development. Keep the existing webhook secret and
owner authentication settings. Delivery is at least once; an acknowledgment/crash
boundary can produce a duplicate.

## Verification and deployment

See reports/VERIFICATION.md for actual test results, live checks and limitations.
See docs/DEPLOYMENT.md for Docker, Render, persistence, resource budgets and rollback.
Docker/Render deployment was not performed on this host.

The uploaded ZIPs were inspected as source data, not executed. The two small MIT
adaptations are attributed in NOTICE.md; all four archive fingerprints and licenses
are in reports/uploaded-archives.json. FlowMap is a reference; hftbacktest remains
an optional external research tool. No Rust/Tauri/React server is required at runtime.

## Tests

~~~sh
python -m pip install -r requirements.lock -r requirements-dev.lock
python -m pip install --no-deps -e .
python scripts/run_tests.py -q --junitxml=reports/tests.xml
python -m ruff check liquidity whale_watcher scripts tests
python scripts/audit_dependencies.py
python -m liquidity.cli probe --output reports/live-probe-current.json
~~~

Tests use synthetic wallet fixtures and mocked account/provider APIs. The bounded
real-feed smoke uses only public exchange data and synthetic watch entries.

