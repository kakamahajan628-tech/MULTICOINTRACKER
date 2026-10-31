> This guide describes the existing wallet-tracker base. For the integrated v0.7 build, start with README.md and reports/VERIFICATION.md.

# Whale Watcher Desk v0.6

**Public exchange market APIs are enabled by default:** [Binance, Bybit and OKX endpoints, commands and limits](docs/WHALE_PUBLIC_MARKETS.md). `/market ETH` and the Exchange market desk fetch unsigned public data without private account credentials. Per-coin cached snapshots join each scan and the 1h/4h AI context. Aggregate statistics cannot establish someone else's private exchange position or realized P&L.

**Hourly/four-hour screening:** [commands, evidence gates and three-provider AI setup](docs/WHALE_SCREENING.md). Six scan blocks and four hourly blocks persist across restart; `/screen`, `/screen4h`, `/report screen`, `/report screen4h` return the new subreports. AI explanations need separately configured provider keys/model IDs.

**Birdeye Standard/free is implemented:** see [endpoint coverage, setup and official references](docs/WHALE_BIRDEYE.md). Automatic price/balance fallback and dashboard/Telegram research are available; a real API key/live entitlement check is still needed. This supersedes older Birdeye pending descriptions below.

**Phase 3:** see [liquidation/loss-exit reporting and recovery](docs/WHALE_PHASE3.md). A dedicated risk report and fifth Telegram summary are added. Source health, cooldowns, actual-attempt RPC budgets and same-venue Nansen position fallback are implemented. The phase guide supersedes older budget/fallback descriptions below.

Independent Python service: `python -m whale_watcher`. Existing ATLAS and Square Desk remain separate. User requirements accepted: 10-minute scan cadence, four distinct cohort reports, professional dashboard and Telegram summaries, free sources prioritized.

## Quick start

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-whale.txt
Copy-Item .env.whale.example .env.whale
# Configure admin password, source keys/RPCs and Telegram values.
.\.venv\Scripts\python.exe -m whale_watcher
```

Dashboard: http://127.0.0.1:8002. Basic Auth username `whale`; password `WHALE_ADMIN_TOKEN`. Upload your Excel workbook. Valid nonempty imports activate tracking and schedule the first scan in 600 seconds; use **Scan & report now** for an immediate baseline. First observations are baselines, not new trades. Matching identities preserve existing baselines.

## Workbook

Use `whale_watcher/wallet_template.xlsx`. Sheets: `Spot Profit`, `Spot Loss`, `Futures Profit`, `Futures Loss`.
Headers: `address, chain, coin, label, rank, asset, decimals, platform`.

Paste from row 2. Maximum 1,000 combined watch entries. Four Top-100 sheets give 400 entries; multiple coins/groups for one address count as multiple entries. Enter chain (`ethereum`, `solana`, `arbitrum`, `base`, `polygon`, `bsc`), coin, rank and label. For spot, `asset=native` or exact contract/mint; token decimals are mandatory. Native coin must match chain. For futures use `platform=hyperliquid`, `binance`, `bybit` or `okx`, coin such as ETH or BTC, and `asset=native` as an unused placeholder. Hyperliquid HIP-3 coin names must include the dex prefix. Keep an EVM-format address for futures account mapping.

Exchange rows are **authorized account aliases supplied by you**, not proof that a blockchain address identifies a private exchange account. Invalid rows/formulas reject the entire import before replacement. Rankings and cohort profitability are user-supplied labels; this app does not automatically verify Top-100 profitability.

## Schedule, windows and delivery

- Cadence defaults/migrates to 600 seconds. Scan starts are scheduled from the preceding start, not 600 seconds after completion. A slow scan skips elapsed slots and reports overrun; scans never overlap. Hosting outages cannot be reconstructed into missed live scans.
- Four report snapshots are generated and stored after every completed scan, including unchanged cycles. Transaction windows run between scan-start cutoffs; observations retain their actual retrieval times. Events received during a scan enter a later cycle. Old late-arriving transactions remain raw source evidence and are not relabeled as current-window trades.
- Telegram gets four separately formatted summaries per cycle. A persistent outbox retries failed delivery every 30 seconds. After a crash between Telegram acknowledgment and saving delivery state, a duplicate can occur (at-least-once delivery).
- `/scan`, `/pause`, `/resume`, `/interval SECONDS`, `/status`, `/wallets`, `/reports`, `/sources`. Default remains ten minutes; owner can override interval. Pause finishes an already running scan. `/reports` returns cohort summary counts; dashboard/API contains all rows and subreports.
- `/api/reports/latest`: complete latest report JSON. `/api/reports`: last 12 stored cycles. Reports remain durable in the database; retention/archive policy is pending.

## Four different report formats

| Cohort | Dashboard format | Subreports |
| --- | --- | --- |
| Spot Profit | Disposal and booking ledger | Partial reductions, holdings cleared, accumulation, transaction activity, market disposal signals, verified profit booked, booking review, unavailable data |
| Spot Loss | Accumulation and exit matrix | Increasing holdings, partial reductions, holdings cleared, verified loss booked, loss/exit review, transaction activity, unavailable data |
| Futures Profit | Position lifecycle digest | Holding, partial/full close, direction flip/increase in full cohort rows, nil/clean positions, profit/loss digest, unavailable data |
| Futures Loss | Exposure and liquidation monitor | Holding, partial/full close, nil positions, realized loss digest, confirmed liquidation, unavailable data |

All reports include label, coin, address, before/current balance or position, state, source, observed time and per-coin counts. Transaction subreports list buy against USDT, sell against USDT, swap to USDT, swap to other coin, swap in, transfer in/out, transfer to known exchange, liquidity add/remove. Separate transaction counters count unique active wallet addresses; a wallet can appear in multiple actions. JSON also contains a per-coin breakdown.

**Accuracy rules:** a balance increase is accumulation until transaction evidence proves a buy; a decrease is reduction until evidence proves disposal/transfer. Zero holdings are cleared holdings and booking review, not automatic booked profit/loss. Spot booked-PnL lists require realized PnL/cost-basis integration and remain unconfirmed meanwhile. Zero futures positions are nil/clean or full close, not automatic liquidation. Cohort membership never determines realized PnL sign. Price quotes value current holdings, not historic execution proceeds. Source switches are flagged on observation rows.

## Free source priority and fallback

| Source | Implementation and scope | Requirements/limits |
| --- | --- | --- |
| Helius | Primary Solana RPC; native SOL batches up to 100 wallets/call; authenticated enhanced webhook receiver and evidence parser | `WHALE_HELIUS_KEY`, or configured Helius `WHALE_RPC_SOLANA`; conservative RPC cap 850,000 reserved units/month, allowance held aside for other usage |
| Configured free RPCs | Same-chain finalized balances; ERC-20 block/decimals request cache; fallback URL lists | User must supply legitimate endpoints and account quota; no account splitting or quota bypass |
| Public Solana RPC | Keyless fallback, subject to public rate limits/availability | Default local cap 150,000 reserved units/month; public service is not a production SLA |
| Zerion | Optional exact-contract wallet-position fallback | Free-start key, default cap 10 calls/day, account-specific entitlement; missing asset is never assumed zero |
| Nansen | Last balance fallback and on-demand balances/labels | Free daily top-up to 10 credits; default local cap 10/day; balances 1 credit/page, common labels 100 credits and therefore blocked by default cap |
| Dune | On-demand saved query results for research | Key/query ID, cap 5 result calls/day; no auto SQL execution and no raw-balance substitution |
| DEX Screener | Keyless current token reference USD quote; exact chain/contract, highest-liquidity eligible pair; per-token cycle cache | 0.3s request spacing, 100,000 calls/month local cap; minimum pair liquidity $1,000; native proxy mapping optional |
| Hyperliquid | Public positions, time-window fills, realized closedPnl | Explicit platform, keyless API; request pacing and cap; oversized fill response marks PnL unknown |
| Binance / Bybit / OKX | Authorized account positions and realized PnL read handlers | Read-only keys/account mapping; no access to arbitrary third-party private positions |

Budgets are local conservative controls, **not provider billing meters**. RPC calls reserve worst-case three retry units; method costs, webhooks, other apps and pre-existing usage still count at the provider. Configure caps below your account's remaining free allowance. No paid account or plan is created automatically. API limits can deny requests before the local budget runs out; failures remain unavailable and preserve baseline.

At 1,000 SPL wallets polled once every ten minutes, one call per wallet alone is about **4.32 million calls in 30 days**, exceeding Helius's 1M free monthly credits. Native SOL batching greatly reduces that count, but arbitrary-token/chain tracking for 1,000 wallets is not guaranteed completely free. Fallback coverage depends on legitimate quotas and available data; shortages are reported, not fabricated. Event-driven dirty-wallet reconciliation/backfill and full API coverage validation remain future work.

Sources mentioned in the supplied 21-page PDF remain in the registry with explicit status. Birdeye Standard is genuinely free but limited; broad top-trader/holder access is not assumed and its adapter remains pending verified endpoint access. Arkham, GMGN, Bubblemaps, DeBank, DEXCheck, Flipside and Chainalysis are not silently treated as free recurring APIs; access/schema/licensing must be established. No unofficial scraping endpoint is used.

Current official references (checked 2026-10-07):

- Helius Free: 1M monthly credits, 10 RPC requests/s: https://www.helius.dev/docs/billing/plans
- Solana batch size 100: https://solana.com/docs/rpc/http/getmultipleaccounts ; public RPC limitations: https://solana.com/docs/references/clusters
- Nansen Free: 100 trial credits, daily top-up to 10; current balances cost 1, common labels 100: https://docs.nansen.ai/getting-started/credits
- Zerion free-start and account-specific rate limits: https://developers.zerion.io/quickstart and https://developers.zerion.io/rate-limits
- Dune results: https://docs.dune.com/api-reference/executions/endpoint/get-query-result
- Token reference price API: https://docs.dexscreener.com/api/reference
- Hyperliquid public positions/fills: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals and https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint
- Bybit closed PnL/Trade history/execType: https://bybit-exchange.github.io/docs/v5/position/close-pnl and https://bybit-exchange.github.io/docs/v5/order/execution and https://bybit-exchange.github.io/docs/v5/enum
- OKX fillPnl: https://www.okx.com/docs-v5/en/

## Transaction evidence setup

Helius enhanced webhook: receiver `/helius/webhook`, authHeader exactly `WHALE_HELIUS_WEBHOOK_SECRET`. Register watched Solana addresses in your Helius account. Duplicate signatures are stored once; successful parsed swaps/transfers/liquidity activity are attributed only when tracked-wallet transfers corroborate them. Failed transactions are excluded. Register verified USDT mint/contract identities in `WHALE_QUOTE_ASSETS_JSON`, and verified exchange destination addresses in `WHALE_EXCHANGE_ADDRESSES_JSON`. Token ticker alone never proves USDT identity. Generic outgoing transfers do not become sells; parsed swaps to USDT remain labeled swaps rather than a separately invented direct sell. No EVM transaction classification adapter yet; EVM balance changes remain observed reductions/increases.

Helius registration and automatic address synchronization/backfill remain manual. Webhook delivery can be delayed or missed; parsed source evidence is not an unconditional no-missed-events guarantee. Liquidity add/remove is classified only with the provider type and matching wallet asset transfer, not from a holding drop alone.

## Futures account setup

`platform=hyperliquid` requires no secret and retrieves only that venue's publicly available address positions. Binance/Bybit/OKX use `WHALE_EXCHANGE_ACCOUNTS_JSON` mapping, for example:

```json
{"bybit:0xYOUR_AUTHORIZED_ACCOUNT_ALIAS":{"apiKey":"YOUR_KEY","secret":"YOUR_SECRET"},"okx:0xYOUR_AUTHORIZED_ACCOUNT_ALIAS":{"apiKey":"YOUR_KEY","secret":"YOUR_SECRET","password":"YOUR_API_PASSPHRASE"}}
```

Keep real credentials in Render environment, never in the workbook/repository. Only read methods are called; no trading/withdrawal request exists. Exchange mapping is owner-provided authorization, not derived account identity. Current CEX adapter covers USDT-settled perpetual symbols `COIN/USDT:USDT`. Hedged positions use gross exposure and preserve position legs; single shorts use negative size. Contract size converts to base units.

Hyperliquid closedPnl and Binance/OKX fill realized PnL are shown separately from unrealized PnL and may exclude fees/funding; they are not all-cost net profit. Bybit closed-order PnL that spans window boundaries is unknown rather than double-counted. API page caps/missing PnL mark coverage incomplete. Bybit `BustTrade` can confirm liquidation; nil position and liquidation price cannot. Binance/OKX/Hyperliquid liquidation-history reconciliation remains pending. Incomplete fill history never erases a valid position snapshot. Source fallback never substitutes another venue's positions for the requested venue.

## Render and Telegram

Deploy `render-whale.yaml`: free Python service, build `pip install -r requirements-whale.txt`, start `python -m whale_watcher`. Set `WHALE_DATABASE_URL` to durable external PostgreSQL; Render refuses local SQLite for live storage. Run one instance/worker. The app creates its own tables. Local SQLite is suitable for local verification.

Set Telegram token, owner numeric user ID, webhook secret and public URL. Official Telegram `setWebhook`: URL `https://YOUR-SERVICE.onrender.com/telegram/webhook`, `secret_token=WHALE_WEBHOOK_SECRET`. Only the authorized private chat can control/import; updates are deduplicated across restart. Reports are sent to the owner, not a public channel.

External uptime monitor: GET `/health` every five minutes. No monitor/account has been provisioned. Health checks HTTP process reachability, not all source freshness. Render Free can sleep after 15 minutes, restart/suspend, and shares 750 workspace hours/month. Free Render PostgreSQL expires after 30 days. Uptime pings do not guarantee uninterrupted ten-minute tracking. See https://render.com/docs/free.

## Validation and remaining work

Run `python -m pytest tests/test_whale_watcher.py tests/test_whale_reporting.py`. Tests use mock provider responses, including 1,000 entries, atomic imports, baselines, failures, classification, four layouts, outbox, quota caps, native batches, reference prices and futures fill PnL. They do not establish live API entitlement, sustained throughput or PostgreSQL production behavior.

Still pending: full portfolio enumeration/valuation, spot cost basis and realized PnL, EVM transaction parsing, supported vendor-specific free analytics adapters, complete pagination/backfill and liquidation histories, long-term report retention/export policy, and the next interaction's extra reporting conditions. Hosting/API/Telegram credentials and live deployment remain unconfigured.
