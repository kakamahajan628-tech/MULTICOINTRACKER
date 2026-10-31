# Screening subreports — v0.5

`/screen` is a short name for the six-scan hourly screening report. `/screen4h` is the report over four saved hourly blocks. Alternative descriptive names could be Hourly Flow Screen and Four-Hour Phase Screen, while commands stay short.

## Commands and private dashboard

```text
/screen
/screen ETH
/screen ethereum:native
/screen solana:EXACT_TOKEN_MINT
/screen4h
/screen4h ETH
/report screen ETH
/report screen4h ethereum:native
```

Without a selector, show the available asset groups. A ticker can match multiple contracts; the report keeps them separate and names exact asset IDs. Use `chain:contract` for a specific token. Existing four cohort reports and risk reports remain separate.

Dashboard **Phase screening** has one-hour and four-hour buttons, optional asset selector, phase table, criteria/eligibility, heuristic scores, AI estimates/model votes, cohorts, coverage and gaps. Private API: `POST /api/screens/1h` or `/4h`, optional `selector`; `GET /api/screens` lists saved automatic reports.

## Counting and schedule

- Automatic: every **six completed scans** creates a non-overlapping hourly block; every **four completed automatic hourly blocks** creates a non-overlapping four-hour block. Reporting uses stored data, not six additional API scans.
- Command `/screen`: combine the **latest six completed scans** from the current watchlist revision, even between automatic block boundaries. `/screen4h`: combine the latest four completed automatic hourly blocks, not four overlapping rolling windows.
- Fewer inputs return progress (e.g. 4/6 or 2/4), not a fabricated completed report. First-baseline scans, failures and source gaps remain visible in metrics.
- Nominal 1h/4h is a scan-count hierarchy. Reports disclose actual start/end and span: manual scans can produce shorter spans, outages/slow scans longer spans. The initial baseline can mean the first six-scan block covers only about 50 minutes of observed changes. No missing intervals are invented.
- Window IDs, consumed cursors and reports persist in PostgreSQL/SQLite. Repeated commands reuse the same report/model result when inputs/configuration match. Restart does not recount consumed blocks. Screening runs in a separate async worker so model latency does not wait inside the wallet scan lock.
- Import or asset-link change advances watchlist revision; collect six fresh scans. Old cycles without the new identity schema are excluded. Criteria/model configuration is part of report identity.

## Coin/contract and futures mapping

Spot assets group by chain and exact mint/contract, including `chain:native`. Perpetuals group by venue and exact symbol until explicitly mapped. Same ticker does not prove the same asset.

To combine all four cohorts for a known underlying asset:

```text
/screenlink hyperliquid:ETH ethereum:native
/screenlink binance:ETH ethereum:native
```

This persists the owner-provided mapping and starts a fresh revision; repeating the same mapping is a no-op. Use the actual underlying contract for token perps, not a guessed ticker match. You may alternatively initialize `WHALE_SCREEN_ASSET_LINKS_JSON` with this mapping. Without a link, `/screen ETH` can show separate spot and venue-perp assessments and explicitly flag missing cohort linkage.

## Evidence and decision criteria

The report uses all four cohort data sources available for the mapped asset. It shows each cohort's unique traders, observation count, states and coverage; absence/unavailable data is not zero activity. It does not verify that Excel's profit/loss cohort labels are historically correct.

- Unique tracked addresses are counted once across scans; repeated transaction/fill IDs are deduplicated. One address with both spot and perp exposure retains both types of observations without becoming two independent people. Independent identities/sybil ownership remain unverified.
- Balance deltas with source changes or a previous observation outside the window are excluded. Balances do not prove buys/sells. Net quantity is summed only within the same asset identity; it is not summed across unlike token units.
- Price ranges/changes require comparable positive same-source/same-basis quotes. Spot reference and futures mark prices are not blindly mixed. Missing/inconsistent price series blocks price-dependent criteria.

| Phase | Initial eligibility criteria |
| --- | --- |
| Accumulation | Sufficient observed coverage, comparable spot wallets, at least 20% increasing, positive net quantity |
| Distribution | Sufficient coverage, at least 20% reducing, negative net quantity |
| Sideways | At least three comparable prices, observed range within configured 1.5% cutoff |
| Harvesting | Verified positive realized PnL alongside tracked position reduction |
| Liquidation | Venue-confirmed liquidation activity in the sample |
| Short squeeze | Price rise >=3%, >=2 short-covering wallets, confirmed short liquidation evidence |
| Long squeeze | Price fall <=-3%, >=2 long reductions, confirmed long liquidation evidence |
| Deleveraging | At least two observed futures exposure reductions |
| Markup/markdown | Comparable price move >=3% / <=-3%, corresponding spot accumulation/reduction |
| Manipulation | Unassessed: independent coordination/market evidence is missing |
| Rugpull | Unassessed: security/deployer/liquidity-control evidence is missing |

These are configurable/in-code starting heuristics, not a validated trading strategy. Default minimum observation coverage is 70% (`WHALE_SCREEN_MIN_COVERAGE_PCT`); price-range cutoff is `WHALE_SCREEN_SIDEWAYS_RANGE_PCT`. A report can have multiple plausible phases. Scores are **uncalibrated heuristic support**, not probabilities. Limited cohort presence is explicitly labeled. Squeeze-like evidence in a tracked sample does not prove a market-wide squeeze. Since v0.6, linked [Binance/Bybit/OKX public OI, funding and ratios](WHALE_PUBLIC_MARKETS.md) supply separate exchange context; they never fill missing wallet evidence. Manipulation/rugpull percentages remain null until proper independent evidence is available; null does not mean zero risk.

## AI: Groq, Gemini and OpenRouter

All three adapters are present; only explicitly configured keys/models run. Existing ATLAS/Square Desk credentials are not silently reused.

```text
WHALE_SCREEN_AI_MODE=consensus
WHALE_SCREEN_AI_TIMEOUT_SECONDS=25
WHALE_SCREEN_MAX_AI_ASSETS=8
WHALE_SCREEN_GROQ_KEY=YOUR_KEY
WHALE_SCREEN_GROQ_MODEL=YOUR_ACCOUNT_SUPPORTED_JSON_MODEL
WHALE_SCREEN_GROQ_DAILY_CALLS=60
WHALE_SCREEN_GEMINI_KEY=YOUR_KEY
WHALE_SCREEN_GEMINI_MODEL=YOUR_ACCOUNT_SUPPORTED_TEXT_MODEL
WHALE_SCREEN_GEMINI_DAILY_CALLS=60
WHALE_SCREEN_OPENROUTER_KEY=YOUR_KEY
WHALE_SCREEN_OPENROUTER_MODEL=openrouter/free
WHALE_SCREEN_OPENROUTER_DAILY_CALLS=20
```

Consensus mode independently queries all configured providers for each eligible asset; displays validated model votes, disagreement, model range and mean estimate. `fallback` mode tries configured providers in sequence until one valid answer succeeds. Free model access/quotas are account-specific and change; verify eligibility in the provider dashboard. OpenRouter accepts only its free router or `:free` model IDs here. Groq/Gemini need an explicitly selected model; no paid-plan upgrade is performed.

Only aggregate features, token/venue identity, evidence gates and gaps go to models. Watched wallet addresses, labels, transaction hashes, private deduplication IDs and API secrets are excluded. Inputs are treated as untrusted data; models receive no tools, trading action or permission to follow data-embedded instructions.

Answers must return all known phases, bounded finite estimates, known evidence references and an eligible dominant phase. Unsupported probabilities, malformed/truncated answers and unsupported accusations/trading-instruction patterns are rejected. Ineligible explanations are replaced with a fixed missing-evidence statement. This is structured validation, not proof every model sentence is factually correct; model opinions are labeled separately from measured facts.

**AI percentages are subjective, uncalibrated model estimates**, not calibrated event probabilities or financial advice. Model agreement is not evidence that an event will happen. Phases can coexist, so percentages do not have to sum to 100. Keys absent, quotas exhausted, provider failures or no eligible evidence preserve the deterministic report with explicit AI status; no model output is fabricated.

Each outbound attempt consumes a local provider daily-call allowance. API rate limits and model-token quotas still apply. With several coins, three-provider hourly + four-hour analysis may exceed free quotas (especially OpenRouter); limits are enforced and rule reports continue. Up to eight assets per report are analyzed by default; extras remain rule-only with a budget-limit label.

Official references checked 2026-10-09:

- https://console.groq.com/docs/structured-outputs
- https://console.groq.com/docs/api-reference
- https://ai.google.dev/gemini-api/docs/structured-output
- https://ai.google.dev/api/generate-content
- https://openrouter.ai/docs/guides/routing/model-variants/free
- https://openrouter.ai/openrouter/free

## Storage and testing

New `whale_screen_reports` table stores reports, kind, watchlist revision and automatic/manual origin; report/model results persist. Schema creation is additive. No historical data is deleted. Telegram outbox queues automatic screening messages using the existing delivery worker; command requests return current stored screening. Database/API/provider availability remains necessary; these reports do not remove Render free sleep/compute/storage limits.

Tests cover six-scan aggregation, four non-overlapping hourly blocks, 24 distinct scan IDs, restart idempotence, revision resets, legacy exclusion, contract separation, same-address spot/perp observation counting, event deduplication, source-change exclusions, private commands/API, three-provider headers/consensus, no-key fallback and unsupported probability rejection. Live provider access and forecast calibration have not been verified.
