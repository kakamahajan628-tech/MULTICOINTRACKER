# Public exchange markets — v0.6

Binance, Bybit and OKX public USDT perpetual data is enabled by default. No account API key, secret, trading permission or paid aggregator is needed for keyless routes. Calls use fixed official HTTPS hosts. No unofficial endpoints or regional-block bypasses are used. API availability and market-data entitlements can vary by exchange and hosting region; the runtime exposes partial failures.

## Commands and dashboard

```
/market ETH
/market BTC
/market                 # Saved public snapshots from the latest wallet scan
/report market ETH
/marketlink ethereum:0xYOUR_EXACT_TOKEN_CONTRACT PEPE
/marketlink perp:hyperliquid:ETH ETH
```

The dashboard's **Exchange market desk** displays separate venue rows, freshness timestamps, component errors and full snapshot JSON. `/sources` lists three public market adapters separately from private exchange account adapters. `GET /api/markets` reads latest scan context; `POST /api/markets/ETH` fetches/cache-reads the requested base symbol. All API endpoints require existing dashboard authentication; Telegram remains owner-only.

Native ETH/SOL/BNB/POL assets map automatically. Binance/Bybit/OKX futures rows use their exact venue base symbol. Spot token contracts and unmapped Hyperliquid symbols require an explicit owner-verified asset-to-market link. Tickers alone never merge different token contracts. `/marketlink` accepts existing watchlist asset IDs and starts a fresh screening revision. A prior `/screenlink hyperliquid:ETH ethereum:native` also makes the underlying native association available. `WHALE_MARKET_ASSET_LINKS_JSON` can initialize links. USDC/inverse/dated contracts and all exchange listings are outside this initial USDT perpetual scope. Multiplier contracts need their actual base symbol, e.g. `1000TOKEN`; no quantity conversion to a spot token is inferred.

## Official endpoints

| Venue | Path | Data |
|---|---|---|
| Binance | `/fapi/v1/premiumIndex` | Mark/index price, funding and next funding time |
| Binance | `/fapi/v1/ticker/24hr` | Last price, 24h change and quote turnover |
| Binance | `/fapi/v1/openInterest` | Current base-asset open interest |
| Binance | `/futures/data/globalLongShortAccountRatio` | Global account long/short ratio, 5m |
| Binance | `/futures/data/topLongShortAccountRatio` | Top-trader account ratio, 5m |
| Binance | `/futures/data/topLongShortPositionRatio` | Top-trader position ratio, 5m |
| Binance | `/futures/data/takerlongshortRatio` | Taker buy/sell ratio and base volumes, 5m |
| Bybit | `/v5/market/tickers` | Linear mark/index/last, funding interval, OI and quote turnover |
| Bybit | `/v5/market/account-ratio` | Account long/short proportions converted to a ratio, 5min |
| OKX | `/api/v5/market/ticker` | Last price, 24h change and base volume |
| OKX | `/api/v5/public/mark-price` | Mark price |
| OKX | `/api/v5/public/open-interest` | Base, contracts and USD OI, units kept separate |
| OKX | `/api/v5/public/funding-rate` | Funding rate, scheduled funding time and interval |
| OKX | `/api/v5/rubik/stat/contracts/long-short-account-ratio` | Coin-level contracts account ratio, broader than one USDT instrument |

All requests are GETs, unsigned and contain no wallet address. Funding rates retain both fractional and percentage fields. Bybit's OI convention is marked `base_asset_both_sides`; OI is never summed across venues. USDT is labeled USDT rather than assumed to be a dollar execution price. OKX derivative `volCcy24h` is base volume, not quote turnover. Ratios and 24h rolling volumes are never added across scans. Ratios cannot identify Top-100 individuals or their exact positions.

Binance's current documentation labels certain statistics routes `MARKET_DATA` and specifies an API-key header. The adapter tries public requests; a rejection leaves that component unavailable while core keyless routes and other venues continue. If your account requires a free market-data key, configure only `WHALE_BINANCE_MARKET_KEY`; no secret/signature is used and private account keys are never reused. The app cannot issue exchange API keys for you.

## Scheduling, fallback and persistence

Each completed wallet scan collects one snapshot per distinct linked base symbol, independent of the number of wallets. The same source snapshot is cached in PostgreSQL/SQLite for up to 600 seconds, including across restart. Concurrent requests for the same symbol are coalesced. Default maximum is 10 symbols per scan, configurable up to 50. Extra or timed-out symbols are explicitly deferred, never silently claimed scanned. Unmapped assets stay listed for owner mapping.

Requests are paced at 2/second/venue, use an 8-second HTTP timeout, bounded retry/cooldown and a 25-second venue deadline. Collection has a 60-second scan budget. The local 100,000 actual-attempt/month limit applies separately to each exchange and includes retries. This is an application cap, not a claim about provider quota or Render capacity. With all routes successful, one uncached symbol uses 7 Binance + 2 Bybit + 5 OKX calls. At an uninterrupted ten-minute cadence, that is approximately 30,240 / 8,640 / 21,600 calls per symbol over 30 days. The cap means 10 configured symbols is not a promise of uninterrupted free monitoring for all 10.

Venue fields are independent: a failed ratio must not erase a good price, OI or another exchange's data. Component health/cooldown is scoped by venue/endpoint/instrument, so one unlisted coin does not block other coins. Binance → Bybit → OKX selects the first available mark as a **reference only**, preserving source identity. An unavailable Binance account position remains unavailable even when a Bybit market price exists. Stale provider timestamps are rejected; prior available snapshots remain separately timestamped `last_known`, excluded from current metrics and AI screening. Keys/URLs/exceptions are not exposed in public reports.

`WHALE_MARKETS_ENABLED=false` disables all public market requests. `WHALE_MARKET_AUTO_REPORTS=false` suppresses the extra per-scan Telegram market digest while retaining collection. Other settings are documented in `.env.whale.example`. Render defaults enable this module without requiring keys. Deploying/updating a Render service remains a separate step.

## Screening and AI

Each scan saves associated public snapshots alongside the four cohort reports. Hourly and four-hourly screens retain same-venue time series, report base-OI and mark-price changes, latest funding and ratio scope, and provide aggregate context to configured Groq/Gemini/OpenRouter models. Repeated cached samples are deduplicated. Four-hour reports merge original samples, not averages of hourly percentage changes. Public metrics do not increase wallet coverage or unlock unsupported liquidation/squeeze/manipulation/rugpull conclusions. Individual realized profit/loss and near-liquidation prices still require account/on-chain evidence.

## Primary references

- [Binance USD-M public market API](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Mark-Price)
- [Bybit tickers](https://bybit-exchange.github.io/docs/v5/market/tickers), [account ratios](https://bybit-exchange.github.io/docs/v5/market/long-short-ratio)
- [OKX API documentation](https://www.okx.com/docs-v5/en/), [official funding interval notice](https://www.okx.com/docs-v5/log_en/)
