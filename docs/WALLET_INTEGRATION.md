# Integration details

This package is a complete reviewable copy of the existing Whale Watcher v0.6-era
source plus the liquidity module. It is not an in-place change to the original
OneDrive project. The baseline file hashes are in reports/wallet-source-baseline.json.

Changed wallet files:

- app.py: collector lifecycle, authenticated liquidity API/dashboard, scan-start
  context capture, scan summaries, /liquidity commands.
- core.py: generic enqueue_report helper reusing the durable Telegram outbox.
- screening.py: bounded per-asset liquidity samples and safe 1h/4h summaries.
- screen_ai.py: additional constraints against attributing CEX orders or summing
  sampled flow into invented hourly totals.
- dashboard.html: shared-market panel, wallet/market associations and heatmap link.
- liquidity_service.py and liquidity_context.py: new collector and mapping/join layer.

The engine lives in liquidity/. Its ingestion, exact book state, metrics,
detectors, model, persistence, replay/backtest and web components are independent
of wallet RPC adapters. Future consumers such as the multi-coin indicator can
read its authenticated state without opening another connection per wallet.

## Scheduling and failure boundaries

The wallet scheduler keeps its existing cadence and provider budgets. The
continuous collector is a separate asyncio task on the same process. One shared
feed is created per mapped coin/venue; 1,000 identical asset watch entries yield
two venue feeds. The default pair budget is four, sorted deterministically by
symbol with Binance before Bybit for each coin. Deferred/unmapped markets are
reported explicitly.

Raw depth buffers and all dashboard/tape/candidate rings are bounded. A full raw
journal is optional and grows on disk; it is not a retention-managed time-series
database. Do not claim unlimited free hosting or unlimited coin coverage.

Wallet scan context is captured before wallet API calls. Asset joins reuse
market_targets() and its exact chain/contract/verified-symbol mapping. Token
tickers alone cannot attach a market. Wallet changes and public market statistics
remain separate evidence objects. Same-symbol wrapped or bridged assets still
need human verification of the mapping and unit meaning.

## Temporal and evidence limits

A scan's market context is a sample at its start, not a reconstruction of every
second during a ten-minute wallet window. Book/trade metrics are trailing
five/ten-second observations. Recent candidates cover only the retained last
120 seconds. Per-wallet observations retain their own times.

A wallet's balance increase does not establish a purchase. An association with
public absorption does not identify its orders or prove causal activity. Missing
book, stale tape, synthetic mode, missing asset mapping and changed wallet
sources prevent unsupported combined candidates.

A 1h/4h screen reports sample counts and the latest per-venue metrics; CVD/OFI
levels are not added together. Existing phase evidence gates and private account
access requirements are preserved. Liquidation scenarios are distinct from
verified venue liquidation events.

## Rollback

Stop this copy, keep a backup of its database, and restart the original source
with its original config. Existing scans receive additional JSON fields; no
destructive migration or credential transfer was performed. Deploying to the
current Render service is a separate operation.

