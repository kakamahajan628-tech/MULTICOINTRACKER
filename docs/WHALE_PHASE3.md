# Whale Watcher v0.3: liquidation proximity, loss exits and source recovery

This phase adds a dedicated **Liquidation proximity & loss exits** dashboard report, cohort subreports, JSON report payload and a fifth Telegram digest. Existing four cohort layouts and ten-minute cadence remain available. `/risk` retrieves the latest compact distress digest.

## Liquidation proximity

Long: `(mark_price - liquidation_price) / mark_price * 100`.
Short: `(liquidation_price - mark_price) / mark_price * 100`.

Defaults: critical distance **0-2%**, near distance **>2-5%**, outside band **>5%**. Zero/negative distance is **threshold crossed**, not confirmed liquidated. `WHALE_LIQ_CRITICAL_PCT` and `WHALE_LIQ_NEAR_PCT` change the bands; require `0 < critical <= near`. These are configurable monitoring cutoffs, not probabilities, recommendations or forecasts.

Each row includes trader label, cohort, coin, side, size, entry price, mark price, liquidation price, leverage, distance, risk band, source and observation timestamp. Both futures cohorts are included. Hedged accounts assess each leg and show the most exposed verified leg; unknown legs keep partial coverage visible. Missing/zero/nonfinite prices stay unknown. Nil positions have no applicable liquidation proximity. Unavailable current positions never enter the live near-risk list.

Hyperliquid supplies `liquidationPx` from positions and `markPx` from `metaAndAssetCtxs`, cached per dex per cycle. CEX adapters use normalized venue position mark/liquidation fields. Last-traded spot/DEX price is never substituted for futures mark price. Snapshot max age is 900 seconds (`WHALE_RISK_MAX_AGE_SECONDS`). Ten-minute polling cannot detect every intracycle approach to liquidation, and thresholds can change with collateral and other positions.

## Closed at loss

`closed_at_loss`, `partial_close_at_loss`, `full_close_at_loss` are separate subreports. Negative **realized closed-fill/order PnL** is required. Negative unrealized PnL alone is insufficient. Individual negative closes remain visible even when other positive fills make the reporting-window net PnL positive. Rows list per-close ID, realized loss, available exit price/closed size/time, source basis and fill coverage.

Net position reduction determines partial/full **snapshot comparison**; a close and reopen within a cycle can leave net size unchanged, so close evidence and net-position state remain separate. Bybit cumulative order updates spanning windows are excluded until precise attribution is available. Realized PnL may exclude fees/funding or include them differently by venue; it is not automatically net all-cost USD PnL. Spot cost-basis is still pending. Nil positions do not prove liquidation.

## Recovery and actual-attempt budgets

- Supported requests get bounded retries for transport errors, rate limits and 5xx responses. Access/request rejection does not endlessly retry.
- Failed sources enter a 120-second cooldown (`WHALE_SOURCE_COOLDOWN_SECONDS`), then are probed on later requests. Budget exhaustion waits until the local day/month reset. State and last success/failure persist in the database.
- RPC budgets now debit **each actual outbound attempt**, including retries and failed transport attempts, instead of reserving three units per successful call. Provider method costs and other apps' usage still need account checks.
- Spot fallback: Helius/configured RPC -> permitted same-chain fallback RPCs/public Solana -> Zerion -> Nansen, within configured access/budgets.
- Public Hyperliquid position fallback: Nansen `/api/v1/profiler/perp-positions`, same venue/address, only first dex and only with a valid fresh source timestamp. Nansen calls Hyperliquid upstream, so this is an alternate API access path, not independent chain redundancy. Private CEX positions are never substituted from another venue.
- Position and fill success are separate: fill/PnL outage preserves valid position data and sets realized PnL unknown. Mark outage leaves risk unknown. Total source failure preserves baseline and displays timestamped last-known data separately.
- Scheduler/report queue retry database errors instead of terminating their loops. No durable write is possible while PostgreSQL itself is unavailable; startup requires a working database. Render sleep and exhausted free quotas are not removed by retries.
- Source panel shows enabled/unverified, active after successful requests, cooldown, access pending or budget exhausted. Configured credentials do not prove live access. Raw evidence is on `/api/sources?include_evidence=true`; routine refresh returns lightweight health metadata.

## Sources that still require access or adapter work

Implemented: Helius, permitted EVM/Solana RPCs, Zerion, Nansen, Dune saved results, DEX Screener, Hyperliquid, authorized Binance/Bybit/OKX accounts. Keys/account mappings and live verification remain necessary. No credentials have been provisioned by this phase.

PDF vendors **Birdeye, Arkham, GMGN, Bubblemaps, DeBank, DEXCheck, Flipside and Chainalysis** remain adapter/access pending. Free UI access is not free automation entitlement. Unknown schemas, inaccessible documentation and licensed APIs cannot become live through retries alone. No account multiplication, payment signing, trading private key or unofficial scraper is added. A fallback must provide equivalent data for the same chain/asset or venue/account.

Official references checked 2026-10-08:

- https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals
- https://docs.nansen.ai/api/profiler/address-perp-positions
- https://www.helius.dev/docs/billing/plans
- https://docs.gmgn.ai/index/gmgn-agent-api (newer query interfaces exist; entitlement/schema still needed)

## Four practical ideas from the supplied platform guide

Suggestions, not silently activated features:

1. **Cohort consensus report:** count wallets accumulating/disposing of a coin in a window. Separate balance observations from verified swap/sell evidence. Reuse existing reports first; Birdeye or Dune rankings can add context when entitled.
2. **Exchange destination watch:** aggregate verified deposits into labeled exchange addresses, separate internal transfers from disposals. Use Helius evidence plus approved Arkham/Nansen labels or owner-verified addresses. Deposit is not proof of sale.
3. **Cluster-adjusted whale count:** flag linked wallets so 20 connected wallets do not automatically count as 20 independent traders. Bubblemaps/manual review or entitled cluster data can support this; a transfer link does not prove common ownership.
4. **Cohort quality score:** use Dune/verified historical closes to compare sample size, win rate, realized profit factor, holding duration and drawdown. Separate spot cost-basis coverage from futures fills; missing history is not zero losses. Update daily to conserve free credits.

## Verification

Three Whale Watcher test files cover mock long/short distance, invalid/stale fields, hedged legs, profitable net windows containing negative closes, retry/cooldown, actual attempt counting, persisted health, same-venue fallback and stale rejection. Live API entitlement, sustained Render capacity, complete liquidation backfill and every PDF vendor are not claimed verified.
