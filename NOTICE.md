This distribution contains independently authored Python software and these selective adaptations:

* liquidity/aggregation.py ports bid-floor / ask-ceiling bucketing from
  jose-donato/crypto-orderbook, internal/aggregation/aggregator.go,
  commit 51007178e5d62c7c8ee1df1568f0bab24f7bb3dc.
  Copyright (c) 2025 jose-donato, MIT.
  Changes: Python Decimal, sorted results, quote notional preservation,
  per-venue cross-market contributions.
* liquidity/metrics.py book_imbalance adapts bookImbalance from
  mamonet/orderbook-heatmap, index.html,
  commit 1219153a1a7c1b29a44e0a7beec1d1caa08bcc60.
  Copyright (c) 2026 mamonet, MIT.
  Changes: Python Decimal, engine-managed exact sorted L2 state.

The full upstream MIT notices are retained in licenses/.
No code from the other audited repositories is included.
Runtime dependency licenses are reported in reports/dependencies.json;
installed distributions supply their own license notices.

Repository selection, source hashes, compatibility findings and exclusions are in
docs/REPOSITORY_AUDIT.md and reports/source-provenance.json.


The integrated whale_watcher package is the user's existing wallet-tracker source,
modified in a separate reviewable copy. This notice does not relicense that
existing code or third-party dependencies.
