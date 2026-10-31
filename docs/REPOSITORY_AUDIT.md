# Selective repository audit

Audited 27 October 2026. Exact inspected commits and SHA-256 file evidence are in
[the provenance manifest](../reports/source-provenance.json). Names without owners
were resolved to the candidates below; they are not proof of the original chat's intended URLs.
The prior chat supplied no recoverable repository links. No upstream application
was installed or represented as production-tested.

| Requested project / selected identity | License evidence | Dependencies and API findings | Decision |
|---|---|---|---|
| [Order-Book-Stream-Visualization](https://github.com/aryan1803/Order-Book-Stream-Visualization) · ac8bf6a | No tracked license/notice found | Python, unpinned websockets/httpx/sortedcontainers/FastAPI/Dash/psutil. config.py uses legacy fstream /ws. sequence_validator mixes snapshot+1 with futures bridge comments. | Exclude all code. Implement official futures sequencing independently. |
| [FlowMap](https://github.com/nazmiefearmutcu/flowmap) · ffa9ce7 | Apache-2.0 root LICENSE | Python >=3.13, React/Tauri, numpy/polars/msgspec, FastAPI, bundled crocodile wheel 0.3.2. Native connectors and grid tightly coupled to its binary protocol. | No code copied. Integrating this full desktop stack would increase maintenance and obscure feed guarantees. Its feature set informed evaluation priorities only. |
| [orderbook-heatmap](https://github.com/mamonet/orderbook-heatmap) · 1219153 | MIT root LICENSE, copyright 2026 mamonet | Single browser HTML/Canvas, no runtime Python dependencies; spot combined depth/trade feed. Local map updates are useful, but not a substitute for futures REST/sequence synchronization. | Adapt bookImbalance only into metrics.py, with full notice. Own dashboard, detectors and feed state. |
| [LiqMap](https://github.com/bendizziesmith/liqmap) · a85c5b8 | No tracked license/notice found | React 19/Vite 6/TypeScript/Vitest. Public Bybit observations drive a client-side estimation model. Repository visibility is not redistribution permission. | Exclude all source, model code and UI assets. Independently implement a simpler documented OI/mark scenario model. |
| [order-flow-screener](https://github.com/zadescoxp/order-flow-screener) · 0591ea4 | Apache-2.0 root LICENSE | Next 16.4/React 19.3/Zustand/ws. Binance Futures adapter contains legacy fstream /ws. Frontend pipeline includes multiple market types, including quote proxies. | No copied code. Avoid a second runtime and do not treat quote proxies as observed L2. |
| [hft](https://github.com/arvin-shafiei/hft) · be01154 | Cargo.toml declares MIT; no tracked license text/notice found | Rust/Tokio/tungstenite/reqwest/rust_decimal; arbitrage/trading scope; manifest repository URL is a placeholder. Identity remains ambiguous. | Conservative exclusion: no code copied. Do not claim this is the original chat's hft. |
| [crypto-orderbook](https://github.com/jose-donato/crypto-orderbook) · 5100717 | MIT LICENSE, copyright 2025 jose-donato | Go module requires 1.24/toolchain 1.24.6 (README says 1.22+); gorilla WS, shopspring Decimal, React/Vite frontend. Its exchange clients are not imported. | Port small bid-floor / ask-ceiling aggregation component to Python Decimal. Preserve original-price quote notional and per-venue contributions. |
| [hftbacktest](https://github.com/nkaz001/hftbacktest) · 5f3ec40 (additional candidate) | MIT LICENSE | Python >=3.11, Rust/maturin, numpy >=2,<2.3, numba ~0.61, polars/matplotlib/holoviews. Detailed queue/latency research engine. | Audited as an alternative interpretation of hft, not silently substituted. Deferred heavy integration; included taker-only causal harness does not simulate passive fills. |

Alternative names also exist: Elenchev/order-book-heatmap (different spelling)
and JuaanM0/orderbook-heatmap (README calls it a dummy prototype). They were not
silently mixed into this delivery.

## Actual reuse boundary

Only two named-repository components are adapted, both MIT. Their original
license texts are in licenses/ and file-level provenance is in NOTICE.md.
The official exchange protocol is implemented afresh. No unlicensed code is
vendored. No source was copied from LiqMap, Order-Book-Stream-Visualization,
hft, FlowMap, order-flow-screener or hftbacktest.

The isolated liquidity engine uses aiohttp and sortedcontainers. The integrated
wallet distribution additionally includes the existing FastAPI/SQLAlchemy/CCXT
wallet stack; its full tested closure is in requirements.lock. reports/dependencies.json
records installed license metadata. pip-audit results apply to this application's
locked runtime closure, not to all upstream repos' npm/Go/Rust trees. The audit
of excluded upstream dependency files is static; it is not a vulnerability
certification or successful build claim.

## Current API contract used

* [Binance USD-M local order book](https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams/How-to-manage-a-local-order-book-correctly):
  open depth stream, buffer, obtain REST snapshot, discard u < snapshot ID,
  bridge with U <= snapshot ID <= u, then require pu == previous u.
* [Binance WebSocket migration](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/websocket-market-streams/Important-WebSocket-Change-Notice):
  separate /public for depth and /market for trades/liquidations.
* [Bybit orderbook](https://bybit-exchange.github.io/docs/v5/websocket/public/orderbook):
  snapshot replaces state; delta quantities are absolute; u=1 marks restart.
  seq compares generation order across book depths. Neither seq nor u is
  documented as consecutive, so numeric jumps are accepted and no impossible
  perfect gap guarantee is advertised. TCP/WS ordering, regressions, reconnect
  snapshots, age checks and ten-minute refreshes provide the implemented controls.
  RPI orders are outside this feed's coverage.
* [Bybit allLiquidation](https://bybit-exchange.github.io/docs/v5/websocket/public/all-liquidation):
  S=Buy identifies a liquidated long; p is bankruptcy price. This differs from
  interpreting S as the liquidation order's aggressor side.
* Binance forceOrder is sampled latest-per-symbol per 1000 ms, not a complete
  liquidation tape. Aggregate trades also are not individual order identities.

## Evidence levels

reports/live-probe.json contains actual bounded HTTP/WebSocket checks.
reports/live-smoke.json contains a 45-second real BTCUSDT reconstruction run.
Both venues synchronized, 2,285 events were journaled, no reconstruction gaps
occurred, and replay recovered matching books/update IDs/CVD.
This proves a short live run at the recorded endpoint versions and network
location; it does not prove long-term uptime, exchange completeness, other
symbols or future API compatibility. Failure is surfaced rather than replaced
with simulated live data. Demo mode is explicitly marked synthetic.


## Uploaded archives recheck: 28 October 2026

The four user-supplied ZIPs were inspected without executing their programs.
Their root licenses are MIT (hftbacktest, crypto-orderbook, orderbook-heatmap)
and Apache-2.0 (FlowMap). Uploaded selected source files match the previously
audited files after line-ending normalization. Exact archive and selected-file
hashes are in reports/uploaded-archives.json. No additional upstream runtime
was introduced merely because its archive was provided.

The integrated wallet collector's new live evidence is in
reports/integrated-live.json: two shared ETH feeds selected for 1,000 synthetic
watch entries, 1,773 events in 45 seconds, both books fresh, zero replay gaps,
and matching book/CVD state. reports/live-probe-current.json rechecks the seven
public REST/WS cases after the dependency security update.
