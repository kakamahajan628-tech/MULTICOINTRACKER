# Birdeye Standard/free integration — Whale Watcher v0.4

Implemented using current official documentation checked 2026-10-08. Birdeye's documentation has moved to `https://data.birdeye.so/docs`; several old `docs.birdeye.so/reference` links fail or redirect. The current package table explicitly lists the endpoints below for Standard. This supersedes earlier pending/unverified Birdeye status in phase guides.

## Supported endpoints

| Role | Endpoint | Local CU estimate per request | Usage |
| --- | --- | --- | --- |
| Current spot price | GET `/defi/price` | 3 | Automatic price source when key is set; DEX Screener fallback |
| Tracked token balance | POST `/wallet/v2/token-balance` | 10 for one token | Solana mint-balance fallback after RPC failures |
| Profit/loss candidates | GET `/defi/v2/tokens/top_traders` | 30 per page | Manual research, 10 traders per page, up to Top-100 |
| Holders | GET `/defi/v3/token/holder` | 30 | Solana owner-wallet mode, up to 100 rows |
| Token directory | GET `/defi/tokenlist` | 30 per page | Up to 100 rows, 50 per page |
| Wallet portfolio V2 | GET `/wallet/v2/current-net-worth` | 30 per page | Solana paginated research, up to 100 assets |

No arbitrary endpoint caller, paid-only multiprice endpoint, trading request, upgrade or extra account is added. This is a selected wallet-tracking integration, not every API in Birdeye's catalog. OHLCV, token stats, transfer-history/PnL enrichment and perps APIs are separate future extensions.

**Correction to earlier status:** current official Standard accessibility includes Top Traders, holders, wallet portfolio V2 and token balance V2. Earlier messages conservatively left this unverified; these adapters are now implemented. Legacy wallet token-list and V1 token-balance APIs have different entitlements, so this app uses the documented V2 balance/portfolio routes.

## Configure

Set in Render environment or local `.env.whale`:

```text
WHALE_BIRDEYE_KEY=YOUR_STANDARD_API_KEY
WHALE_BIRDEYE_MONTHLY_CU=27000
WHALE_BIRDEYE_DAILY_CU=1000
WHALE_BIRDEYE_PRICE_CACHE_SECONDS=600
WHALE_BIRDEYE_RESEARCH_CACHE_SECONDS=86400
WHALE_BIRDEYE_MAX_PRICE_AGE_SECONDS=900
```

Standard is documented as **$0, 30,000 CU/month, 1 request/second**. Local monthly allowance defaults to 27,000 CU to leave headroom; configuration cannot lift this adapter above 30,000 CU. Daily and monthly request caps are checked/debited atomically before HTTP. Each attempted call/retry is conservatively charged locally, even if provider billing treats failed requests differently. Actual provider account usage may include other applications and a different billing-cycle reset.

All requests use `X-API-KEY` and explicit `x-chain`, serialize at least 1.05 seconds apart, share the recovery/cooldown system, and preserve redacted health diagnostics. Key configuration alone does not prove entitlement: first successful requests establish active connectivity; account errors stay access pending. No live API-key test has occurred in this workspace.

## Automatic use and data rules

- Spot prices: Birdeye first when key configured; otherwise DEX Screener. Unknown, stale, invalid, or insufficient-liquidity Birdeye quotes fall through to DEX Screener. Prices are cached per chain/asset across wallets/cycles and use source update timestamps. EVM token contracts are EIP-55 checksummed; Solana mint case is preserved.
- Prices value current holdings. They are not execution prices, realized PnL or venue futures marks. Native spot valuation still needs an approved wrapped-token mapping.
- Balances: Helius/configured RPCs/public fallback first, then Birdeye for Solana token mints, then Zerion/Nansen if configured. Native SOL cannot be inferred from a wallet's wrapped-SOL token balance.
- Balance reads use a one-token V2 request, exact returned mint, matching decimals and raw integer `balance / 10^decimals`. This avoids scaled UI amount mixing. Missing/null rows are **unavailable**, never zero. Only an explicit valid zero balance can clear a holding.
- Research snapshots stay separate from imported cohorts and ten-minute booked-PnL reports. Rankings can change between pages; responses preserve per-page retrieval times. A Top-100 ranking is ten pages, **300 CU**, excluding retries. Ascending realized PnL returns loss candidates, which still need review; the app does not auto-label or import them.
- Holder mode is wallet, so multiple token accounts do not become separate owners. Portfolio reads include low-liquidity rows and indicate whether the returned list was exhausted; a 100-row cap is not a promise to enumerate every asset of larger wallets.

At two coins, one fresh 3-CU price call per coin every ten minutes costs about **25,920 CU over 30 days**, before research/balance fallback/retries. That leaves little budget for daily Top-100 lists. Rankings therefore run on demand with a 24-hour cache. Birdeye Standard is not a replacement for scanning hundreds of wallets every ten minutes. Limits trigger cooldown and alternative available sources, not a paid upgrade.

## Controls

Dashboard: **Birdeye research · Standard/free** panel selects kind, chain, token/wallet address and count; shows table and full JSON. Private API: `POST /api/birdeye/{kind}?chain=solana&address=...&count=100` using the existing dashboard authentication.

Telegram examples (replace placeholders with actual canonical addresses):

```text
/source birdeye price solana TOKEN_MINT
/source birdeye traders solana TOKEN_MINT 100
/source birdeye losers solana TOKEN_MINT 100
/source birdeye holders solana TOKEN_MINT 100
/source birdeye tokens solana
/source birdeye portfolio solana WALLET_ADDRESS 100
```

`/sources` and the dashboard show Birdeye key/access/health status. Telegram returns a shortened message when JSON exceeds message size; the dashboard/API exposes the complete result. Daily cached research may be historical by design; inspect retrieval timestamps.

## Official references

- [Current package accessibility](https://data.birdeye.so/docs/guides/data-accessibility-by-packages)
- [Standard pricing](https://data.birdeye.so/docs/guides/payment/pricing)
- [CU cost table](https://data.birdeye.so/docs/guides/what-is-compute-unit-cost)
- [Price](https://data.birdeye.so/docs/data-api/price-ohlcv/get-defi-price)
- [V2 tokens balance](https://data.birdeye.so/docs/data-api/balance-transfer/post-wallet-v2-token-balance)
- [Top traders](https://data.birdeye.so/docs/data-api/wallet-networth-pnl/get-defi-v2-tokens-top-traders)
- [Holders](https://data.birdeye.so/docs/data-api/holder/get-defi-v3-token-holder)
- [Token list](https://data.birdeye.so/docs/data-api/tokenmarket-list/get-defi-tokenlist)
- [Portfolio V2](https://data.birdeye.so/docs/data-api/wallet-networth-pnl/get-wallet-v2-current-net-worth)

The V2 balance page contains conflicting batch-size/cost wording (500 in schema, 50 in prose, wallet_count versus token_count). This adapter deliberately submits one token: its documented cost is 10 CU under either interpretation. It does not guess a large free batch allowance.

Mock tests cover authenticated headers, precision, checksums, caching, actual pagination/page costs, daily/monthly caps, rejection cooldown, balance identity and missing-data handling, and price fallback. Live responses and account entitlement remain unverified until a real API key is configured.
