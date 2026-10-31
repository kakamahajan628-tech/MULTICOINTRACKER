# Deployment and operations

## Preserve the existing installation

This is a reviewable integrated copy, not an in-place change to the original
OneDrive PYTHON BUILDER checkout. reports/wallet-source-baseline.json records
the source version used. The package excludes existing credentials, wallet data,
runtime databases, provider keys and unrelated Square Desk/indicator apps.

Back up your existing wallet database and configuration before switching versions.
Start this copy with a separate test database first. Database tables and report
formats are additive: older scans without liquidity context remain readable.

## Local production process

Use Python 3.12/3.13, requirements.lock and one uvicorn worker.
python -m whale_watcher loads .env.whale and listens on PORT (default 8002).
All data/command endpoints retain the tracker's Basic authentication and Telegram
owner/secret controls. HTTPS is mandatory for non-loopback clients. Terminate TLS at a trusted reverse proxy, keep the app listener private, and restrict access. Plaintext HTTP is allowed only for local loopback development.

Configuration additions:

| Variable | Default | Purpose |
|---|---|---|
| WHALE_LIQUIDITY_ENABLED | true | Initial collector switch; persisted /liquidity on/off overrides it |
| WHALE_LIQUIDITY_MODE | live | live or synthetic demo |
| WHALE_LIQUIDITY_MAX_MARKETS | 4 | Shared venue/coin pair budget, hard maximum 20 |
| WHALE_LIQUIDITY_JOURNAL | runtime/liquidity-events.sqlite locally | Tick journal path; empty disables |
| WHALE_LIQUIDITY_CONFIG | empty | TOML thresholds file, e.g. config.example.toml |
| WHALE_LIQUIDITY_ALERTS | false | Explicit extra candidate-alert opt-in |
| WHALE_LIQUIDITY_AUTO_REPORTS | true | Include liquidity context with scan Telegram summaries |

Only correctly mapped imported assets are subscribed. Subscription changes reconcile
within about 15 seconds. Removed assets lose active state; missing market data are
not substituted with another venue. A feed error resets reconstruction and starts
a fresh snapshot synchronization. Binance uses a bounded bootstrap queue; queue
overflow or lag >3 seconds aborts the session rather than dropping deltas silently.

CVD resets when a market stream disconnects, including Bybit's periodic refresh.
Book snapshot refresh resets OFI/wall history without inventing executions.
Dashboard quality shows epochs, ages and failures. Tick recorder write/flush failure
stops market collection and withholds fresh derived context; wallet processing is
separate. Inspect fatal_error in /api/liquidity.

## Docker

~~~sh
cp .env.whale.example .env.whale
# Replace WHALE_ADMIN_TOKEN and configure required wallet sources.
mkdir -p runtime
# On Linux, UID 10001 must be able to write the bind mount:
# sudo chown 10001:10001 runtime
docker compose up --build -d
~~~

The image runs as UID 10001. Compose binds only 127.0.0.1:8002 and mounts runtime/.
The environment file is not copied into the image. Docker was unavailable in the
development environment, so files were reviewed but the image was not built/run.

## Render

The included render-whale.yaml is a starting configuration. On Render, the existing
wallet tracker requires external durable PostgreSQL via WHALE_DATABASE_URL.
Compact wallet scans/screens persist there. Tick journaling defaults OFF on Render's
ephemeral filesystem unless you explicitly configure a path on a durable disk.

[Render Free documentation](https://render.com/docs/free) describes idle spin-down,
ephemeral local storage and limited resources. A sleeping service cannot collect
missed order-book history, and an uptime request is not a durability guarantee.
For continuous history, use an always-on instance/worker with durable storage.
If resources are constrained, set max markets to 2 or disable continuous feeds.
Full raw tick recording plus 1,000-wallet polling was not load-tested on Render Free.

## Storage, backup and recovery

SQLite WAL batches commit every 100 events or roughly once per second in the
embedded service. A hard crash can lose the current uncommitted batch; normal
shutdown flushes it. synchronous=FULL protects committed transactions, not
events that never reached the journal.

Raw tick history grows continuously and is not automatically pruned. Monitor disk
usage and archive by stopping /liquidity, stopping the process, backing up the
closed SQLite file, then starting with a new journal path. Do not delete a live
journal or its WAL/SHM sidecars. Each new collector session records metadata and
snapshots. Keep archives separate and replay in their original ingestion order.

Compact scan/screen/outbox records use the existing wallet database. Do not call
them a complete continuous market history. Threshold settings should be retained
with each research run; replay defaults do not recover custom model settings from
the event journal automatically.

## Operational limits

Short public-feed runs passed for BTCUSDT and ETHUSDT at the recorded dates.
No private account API, wallet RPC entitlement, Telegram delivery, long-duration
uptime, Render deployment, Linux container or 1,000-wallet RPC throughput was
verified in the new run. The 1,000-entry integration smoke validates shared feed
selection, not the cost/latency of polling 1,000 real wallets.

Python and dependency versions are pinned. CCXT was updated from 4.5.84 to 4.5.90
after OSV flagged its old certifi/multidict/urllib3 pins. The final audit report
records the exact versions and advisory response. Re-run audits after updates.


## Dashboard transport

Selected chart history uses a since cursor and a bounded browser cache; book
epoch changes reset chart history. Recent candidate payloads are capped, and
FastAPI responses use gzip. The measured synthetic idle refresh is recorded in
reports/dashboard-efficiency.json; it is not a bandwidth guarantee. Venue price
buckets and displayed precision adapt to low-priced tokens.

## v0.8 upgrade and review fixes

Stop older app/import processes and back up both databases first. Startup migrates
legacy watchlist JSON into normalized rows transactionally. Keep the pre-upgrade DB
for rollback; older code cannot read the new watchlist table. See SECURITY_HARDENING.md
for authentication, proxy limits, minimum secrets, offline tests and CI evidence.
Read ORDER_REPORTS.md for order polling, history cursors, limits and API budgets.
