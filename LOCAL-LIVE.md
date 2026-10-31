# Local live wallet setup

The configured instance is private to this computer at http://127.0.0.1:8002.
Use username `whale` and the value of `WHALE_ADMIN_TOKEN` in `.env.whale` as the password.
That local file contains a generated password and is excluded from source archives.

The active wallet database is `runtime/live-wallets.sqlite3`. The public depth/trade
journal is `runtime/live-liquidity-events.sqlite`. Neither is included in the ZIP.
A pre-CSV wallet database backup is saved at `runtime/pre-zec-wallets.sqlite3`.
Reports in this working copy record the real import and latest verified live scan.

## Restart

After installing requirements as described in README.md:

```powershell
.\scripts\start-local.ps1 -PythonPath .\.venv\Scripts\python.exe
```

For this desktop workspace the existing tested environment can also be used:

```powershell
.\scripts\start-local.ps1 -PythonPath ..\..\work\venv\Scripts\python.exe
```

The command runs in the foreground. Keep the terminal and computer running.
Use Ctrl+C to stop. Sleep, shutdown and process termination interrupt collection;
there is no automatic boot service or cloud deployment in this setup.
The desktop run's process details are in `runtime/local-server.json`.

## Public wallet and CSV imports

An existing normal Solana System Program account can be added offline using:

```powershell
python -m whale_watcher.wallet_setup PUBLIC_SOLANA_ADDRESS --env-file .env.whale
```

This creates a neutral SOL watch and opts that address into token inventory discovery.
Restart the app after an offline setup/import. Native SOL and token inventory are separate.
Two program queries cover SPL Token and Token-2022 mints once per wallet per scan.
Partial inventory failure preserves the last successful inventory and shows its age.
The authenticated `/api/portfolios/WATCH_ID` supports offset, limit (max 200),
positive and mint-address search. Inventory is not a portfolio valuation. Frozen assets,
unwanted airdrops, NFTs and extension-scaled amounts require separate interpretation.
DeFi positions, staking accounts and compressed assets are outside this inventory scope.

CSV headers must exactly match:

```text
address,label,coin,direction,position_value_usd,unrealized_pnl_usd,leverage
```

Use the dashboard's **Import Excel / CSV** control for an authenticated CSV append,
or the offline command:

```powershell
python -m whale_watcher.csv_import path/positions.csv
python -m whale_watcher.csv_import path/positions.csv --apply
```

Validation runs before any batch write. Reimporting the same candidate is idempotent.
CSV import appends Hyperliquid public account watches and preserves existing watches.
XLSX import retains the original replace-watchlist behavior; export/back up before replacing.

CSV position values, leverage, labels and unrealized P&L remain user-supplied historical
claims. Missing snapshot times stay unknown. Live size, direction, entry, notional,
unrealized P&L and liquidation thresholds come from Hyperliquid's public info API.
These EVM-format account identifiers do not represent native Zcash wallet balances.
An empty current position is not evidence of liquidation, sale or lifetime profitability.
The four profit/loss cohort screens exclude unclassified watches. Neutral futures still
participate in current liquidation-distance monitoring and wallet/liquidity context.

For a verified Hyperliquid-to-public-market context link, use e.g.:

```text
/marketlink perp:hyperliquid:ZEC ZEC
```

This is a market-context mapping, not attribution of any Binance/Bybit order to a wallet.
The configured four-feed budget covers SOL and ZEC on Binance Futures and Bybit.
Observed resting prices retain their wire precision separately from current new-order
filter ticks. No prices are rounded or discarded merely for being off the current order tick.

## Controls and limits

Wallet scans default to 10 minutes. `/scan` queues a new scan. `/pause` pauses wallet
scans; `/liquidity off` also stops continuous market collection. Controls persist.
Local Telegram credentials are absent and automatic messaging is disabled.
Configure the bot/owner settings only when external messages are wanted.
Public RPCs and exchange APIs are metered/retried, but availability is not guaranteed.
Unmapped tokens do not subscribe to a guessed exchange ticker. Tick recording grows
with activity; monitor disk and back up SQLite using its backup API while running.
See `docs/DEPLOYMENT.md` for Docker/Render and storage operations.

## v0.8 order study and upgrade

The 51-entry local watchlist is preserved through normalized-row migration. The
pre-upgrade database backup and current process/session details stay in runtime/.
Use the new Orders, fills & position conditions panel or authenticated /api/orders.
Only watched account/coin pairs are studied. See docs/ORDER_REPORTS.md for evidence
rules and /orders commands. See docs/SECURITY_HARDENING.md before external hosting
or rollback. Keep the local admin secret private; its value does not change on upgrade.
