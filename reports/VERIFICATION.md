# Verification: Whale Wallet Intelligence v0.8.0

Checked 29 October 2026 on Windows 11, Python 3.12.14.

- **197 passed**, zero failures/errors/skips, 69.318 seconds in pytest.
- Parent runner completed within its 300-second deadline. No clock heartbeat gaps;
  unattributed session overhead 1.01 seconds.
- Full-package formatting and E4/E7/E9/F/B lint passed. Editable v0.8.0 build/install
  and pip check passed. One existing Starlette/httpx test-only deprecation warning.
- OSV queried **74 exact runtime/dev pins**, with input SHA-256 hashes, names,
  versions, markers and pagination evidence. No advisories returned. Strict
  pip-audit also returned no known vulnerabilities. See dependency-audit.json and pip-audit.json.
- Legacy watchlist migration preserved **51 entries exactly**, including watch IDs
  and metadata. A pre-upgrade SQLite backup remains in the local runtime folder.
- Live Hyperliquid order scan observed **50/50 ZEC account/coin pairs**, with
  **46 open orders** at 2026-10-29T15:40:56.023767+00:00.
  Existing position conditions, fill evidence and order evidence remain separate.
- Current order-status probe matched the exact account/order/coin and returned
  venue status `open`.
- Four SOL/ZEC Binance/Bybit books were fresh in the verified snapshot.
- Actual current-process journal replay: **34719 events**;
  all four top-20 books, update IDs, base CVD and quote CVD matched; zero rejections.
- Real main-app throttle returned five 401s then 429; valid credentials still
  returned 200. The standalone CLI returned 401 unauthenticated/invalid Unicode
  Bearer and 200 for its valid token on loopback.
- Both JavaScript files passed Node syntax checks. Authenticated wallet/liquidity
  pages, order API/history and commands have HTTP/integration tests.

The 1,000-wallet mocked scan test was measured at 35.1s before batching and 6.1s in
an affected-test run after batching. Repeat measurements vary with host load.
This is a local fixture measurement, not proof of 1,000-wallet RPC throughput.

A pre-fix instrumented run captured a long wall-time interval inside a pure
backtest phase with low CPU use. Its exact external cause is unproven. The new
runner and per-phase checks reject over-budget success after execution resumes;
heartbeat records preserve clock/CPU/active-phase gaps. Historical timing records
are retained instead of being silently relabeled as a fast success. See
historical-test-timing.json and docs/SECURITY_HARDENING.md.

Scope: watched account/coin pairs; no complete intracycle order-history claim.
Private CEX order access is mocked, not live credential-tested. Docker, Linux,
PostgreSQL and GitHub-hosted CI were not executed locally. No external Telegram
messages, orders, cancellations, signatures or withdrawals were sent.

See docs/ORDER_REPORTS.md, docs/SECURITY_HARDENING.md and LOCAL-LIVE.md.
