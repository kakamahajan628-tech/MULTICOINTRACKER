# Orders and position-condition reports

This release extends the supplied Whale Watcher v0.6 conditions. Holding state,
position state and spot transaction-evidence functions retain the same AST logic.
See `reports/v06-compatibility.json` for the supplied archive hash and comparison.

## Evidence and scope

The watchlist defines the account/coin pairs to study. For the supplied ZEC list,
queries target Hyperliquid's public mainnet account data and ZEC perpetual orders.
Other coins in the same account are not silently added to the watchlist or market feeds.
Hyperliquid open orders are shared per account/dex within each scan. Authorized
Binance/Bybit/OKX accounts use read-only CCXT open-order queries when configured.
Their current position/closed-PnL adapters remain available. Detailed fill normalization
in this new ledger is currently Hyperliquid-specific; private CEX order access has
only mocked validation, not a live credential test.

| Data | Meaning |
|---|---|
| Current open order | Venue-reported instruction at its own retrieval timestamp |
| Order appeared/changed | Difference between complete polling snapshots |
| Order disappeared | Missing from the next complete snapshot; reason unresolved |
| Confirmed venue status | Exact account/order/coin matched `orderStatus` response |
| Executed fill | Venue-reported trade ID, order ID, quantity, price and time |
| Net position condition | Existing v0.6 comparison of signed position snapshots |
| Inferred order effect | Candidate increase/reduction/reversal based on side and current position |

An instruction is not an execution. A disappeared order is not assumed canceled
or filled. Status queries are bounded, so unresolved/deferred statuses stay unknown.
A `liquidatedCanceled` order status is retained as an order status and does not
set the account's liquidation flag. Anonymous Binance/Bybit L2 is contextual data;
it does not identify these wallets' exchange orders.

## Preserved conditions

- `baseline` / `nil_baseline`: first observation or noncomparable source/hedge context.
- `position_increase`: absolute signed exposure increased.
- `partial_close`: absolute exposure decreased but remained open.
- `full_close`: observed exposure reached zero.
- `direction_flip`: signed position crossed from long to short or vice versa.
- `holding` / `nil`: unchanged open/zero snapshot.

Fill-level effects additionally use Hyperliquid's reported `startPosition`, side
and executed quantity. A close and reopen can therefore remain visible even when
the net snapshot is unchanged. Missing or hedged context is not guessed. Existing
realized-loss close reports and the 2%/5% liquidation-distance bands are preserved.
Imported CSV unrealized P&L never establishes current realized P&L or lifetime skill.
Source closed P&L, fees grouped by currency, and funding remain separate concepts.
Zero/dynamic TP/SL size stays unknown rather than becoming a zero-notional certainty.

## Pages, commands and history

- `/`: Orders, fills & position conditions, plus the original cohort/risk/liquidity desks.
- `/orders [COIN]` and `/report orders [COIN]`: compact report through existing controls.
- `GET /api/orders?coin=ZEC`: authenticated current order report and persisted evidence.
- Optional `account`, `limit` (1–200), and `before_id` filter/cursor parameters.
  Follow `next_before_id` to page through older ledger rows.

`whale_order_events` stores deduplicated fills and order/position changes. Complete
open snapshots persist independently so restarts and endpoint outages retain their
last successful baseline. Identical trade IDs cannot double the persisted execution
ledger or Hyperliquid's realized-P&L sum. Successful position observations survive
order-endpoint failure. Successful fills survive open-order-endpoint failure.

Polling does not provide every intracycle order or a complete historical backfill.
Hyperliquid's fills-by-time endpoint returns at most 2,000 fills and only the latest
10,000 are available upstream. A capped/unavailable window stays incomplete. Open
order display/storage is bounded; truncated snapshots do not produce disappearance
claims. Fill display is limited to 50 per wallet/report, with totals and truncation
flags. The persisted ledger can be paged separately.

## Configuration and cost

| Variable | Default | Purpose |
|---|---|---|
| WHALE_ORDERS_ENABLED | true | Enable read-only order study during wallet scans |
| WHALE_MAX_ORDERS_PER_WALLET | 200 | Maximum retained open orders per watched account/coin |
| WHALE_ORDER_STATUS_BUDGET | 20 | Maximum status lookups per scan, hard maximum 100 |
| WHALE_ORDER_AUTO_REPORTS | true | Add compact order summary to an already-enabled Telegram outbox |

At most five vanished-order status lookups are attempted per wallet, within the
shared budget. A normal repeated Hyperliquid scan uses approximately one position,
one open-order and one fill request per distinct account, plus shared metadata and
bounded status requests. Multiple watched coins share account/dex requests where
possible. Existing metering, retry, cooldown and ten-minute scheduling apply.
No trading, cancellation, signing or withdrawal endpoints are called.

Telegram delivery remains disabled in this local setup because no bot/owner
credentials are configured. No messages were sent during verification.

Official schemas checked 29 October 2026:
https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint

Collector starts also journal an explicit disconnected stream boundary. Replaying
appended recordings therefore resets observed tape CVD at a new feed session,
even when contract metadata is identical to the preceding process.
