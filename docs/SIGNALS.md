# Data and inference contract

## Observable

Order-book price/absolute size updates, reported aggressor trades, mark price,
funding, open interest and reported liquidation events come from public endpoints.
Prices and base quantities are Decimal values validated against exchange tick
and lot grids. Original venue payloads for depth, metadata and snapshots are
journaled; normalized trade records retain venue IDs and exchange timestamps.
The journal is not a complete byte-for-byte capture of every WebSocket control
frame. Public depth is bounded, aggregate trades group executions, Binance
liquidations are sampled, and Bybit RPI orders are excluded.

Live feed status/reconnect events and metadata are also journaled. Each book
epoch begins with a snapshot, and Binance becomes ready only after the bridge.
On a gap the public view withholds book/metrics until resynchronization.
A stale book is excluded from venue aggregation and tape-dependent inference.

## Derived measurements

* CVD = sum(aggressor-buy base quantity minus aggressor-sell base quantity).
  Quote CVD uses each execution's price. IDs are deduplicated in a bounded 50,000
  ID cache per instrument. A market disconnect resets CVD and advances its segment
  counter; missing history is not fabricated. Book-only resets do not reset CVD.
* Best-quote OFI follows the bid/ask price-change indicator formulation:
  e = 1[b>=b_prev]q_b - 1[b<=b_prev]q_b_prev
      - 1[a<=a_prev]q_a + 1[a>=a_prev]q_a_prev.
  Last event and trailing ten-second sums are in base units. OFI is not the
  naive difference of summed top-N depth. Snapshot/reconnect resets its baseline.
* Imbalance uses top-20 base quantity. Microprice weights the opposite touch
  price by the touch quantity. Spread is in basis points.

## Candidate signals

* Wall: level notional exceeds both the configured minimum and a multiple of
  the same side's median of the top 100 visible levels. Persistence threshold
  defaults to 5 seconds. A wall dropping to <=20% of its peak is a vanishing
  candidate. Leaving visible coverage is not called a withdrawal.
* Spoof risk: at least three rapid (<5 second) low-observed-execution wall
  disappearances in 60 seconds, while the trade tape is fresh. This is a
  repricing/cancellation pattern, not proof of a trader's intent.
* Absorption: >=50,000 USDT aggressive execution in five seconds while the
  observed midpoint range stays <=2 bps after >=1 second of book observations.
  It is a coarse flow/price heuristic. A minimum directional price change is
  not required and unrelated flow can produce a candidate.
* Iceberg: absorption conditions plus executed/displayed base size ratio >=3
  at the same visible price and at least two observed increases after recent
  executions. Different participants can create the same pattern; no hidden
  quantity or order identity is estimated.

Every candidate includes evidence and a caveat. Score is deterministic heuristic
strength, never a calibrated likelihood. Book gaps reset wall/refill/price state.
Market-feed disconnects reset tape-dependent detector state. Configurable
cooldowns prevent repeated events every depth update. Storage contains signal
records linked to their triggering journal event.

## Liquidation scenario envelopes

Entries, leverage, collateral, cross-margin offsets and maintenance tiers are
not public. The independent model anchors initial OI at first observed mark,
adds cohorts on increases in observed OI quote notional, scales cohorts down on
decreases, and decays cohorts with leverage-specific half-lives.

For isolated linear scenarios, long price is approximately
entry*(1-1/leverage)/(1-MMR); short price is entry*(1+1/leverage)/(1+MMR).
The model uses leverage priors 5/10/25/50/100×, weights
0.15/0.25/0.30/0.20/0.10, MMR range 0.4%-1% and entry envelope +/-1.5%.
These ranges are assumptions, not fitted confidence intervals.

Each side is bounded by the one-sided OI budget; both sides refer to the same
contracts, so do not add them as unique exposure. OI quote changes can reflect
price changes as well as positions. An entire leverage/side envelope may combine
multiple cohorts: it is not a localized cluster estimate. The model suppresses
bands already intersecting current mark and reports context age/staleness.
It does not claim that crossing a modeled price proves a liquidation happened.

## Backtest interpretation

The example strategy reads only already-processed data, schedules an intent, then
fills at a later matching market's depth event after latency. Entry and exit walk
visible opposite-side depth. Incomplete fills fail; fees and adverse extra slippage
apply on both legs. An exit is requested after holding duration plus latency.
Market data are replayed in ingestion order, including failed sequencing events.

This research harness excludes unresolved positions at EOF or invalidated by a
book gap; those counts are prominent to expose selection bias. There is no
passive queue simulation, funding, market impact feedback, leverage/margin engine
or claim of profitable strategy performance. Fit thresholds on training data,
freeze them, then evaluate separate chronological periods and record all exclusions.

