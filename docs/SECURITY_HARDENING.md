# Review fixes and operating requirements

## Authentication and transport

Both dashboards require a configured 32–256 character printable ASCII password/token.
Generate at least 32 random bytes, for example:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Use that output as `WHALE_ADMIN_TOKEN` for integrated HTTP Basic, or `DASHBOARD_TOKEN`
for standalone Bearer auth. Empty/short/example-placeholder tokens refuse startup.
There is no production test-mode bypass. Standalone synthetic demo access requires
`--allow-unauthenticated-demo`, `--mode demo`, no token, and a loopback host. Replay
and live modes always require authentication. The standalone CLI already defaulted
to loopback before this review; the main module entry now also defaults to loopback.
The Docker image explicitly binds its internal interface and Compose publishes only
127.0.0.1. Keep one worker per collector/journal.

Failed authentication is limited to five attempts per peer per 60-second fixed
window. A bounded 10,000-peer map expires entries and does not evict active blocks.
Extra failures return 429 with Retry-After. Correct credentials remain usable behind
a shared NAT. This limiter is process-local; use reverse-proxy limits for internet
traffic. The app does not read arbitrary X-Forwarded-For itself. Configure trusted
proxy addresses correctly and replace untrusted forwarded headers at the proxy.
Non-ASCII/malformed Basic, Bearer and webhook secrets return rejection, never TypeError.

HTTPS is mandatory for every non-loopback client connection. Terminate TLS at a
trusted reverse proxy or managed HTTPS service, redirect/reject plaintext HTTP,
and keep the application listener private. HTTP Basic/Bearer are not encryption.
Do not publish an unauthenticated backend, reuse fixture credentials, or place keys
in URLs. Local loopback development is the sole plaintext exception.

HTTP client request INFO logging is suppressed. Telegram transport failures are
converted to safe status messages without chaining exceptions that contain bot URLs.
Tests check both log suppression and HTTP failure responses for token exposure.
The integrated liquidity page now uses only same-origin Basic credentials; the
unused sessionStorage/Bearer branch was removed. DOM text rendering is retained.

## Persistent imports and scan writes

Watch entries now live in `whale_wallets`, one row per stable watch ID. SQLite
BEGIN IMMEDIATE or PostgreSQL transaction advisory locks serialize append/replace,
capacity checks and revision changes. Each scan atomically captures membership with its revision, so a mid-scan append cannot contaminate a newer screening window. Concurrent appenders do not lose rows or
exceed 1,000 entries. Imports insert only new rows rather than rewriting one blob.

Stop all older app/import processes and back up the database before upgrading.
The legacy `whale_state.wallets` JSON is migrated transactionally and removed.
A stale old-version write after migration fails startup rather than silently
replacing normalized rows. To roll back to old code, restore the pre-upgrade DB
backup too. This is not a rolling mixed-version migration.

Successful scan snapshots now commit together using a bulk upsert. Holding events
keep their original exact Decimal evidence. Failed source rows retain their old
snapshot; invalid batch data does not partially commit. This removes the repeated
per-wallet filesystem commits that dominated the 1,000-wallet fixture. The real RPC
latency/cost of 1,000 wallets is a separate, unverified workload.

## Tests and timing

`python scripts/run_tests.py -q` writes JUnit, all durations, collection/setup/call/
teardown timing, CPU time and a parent-process record. Individual tests have a
120-second thread timeout; the wrapper has a 300-second whole-process deadline.
External DNS/connections are blocked in unit tests; loopback test servers are allowed.
Source failures cannot silently perform live API calls during the suite.

The old JUnit timing gap cannot be conclusively attributed from that report alone.
It was outside the summed testcase durations. Current instrumented runs record that
otherwise-hidden overhead; see historical-test-timing.json and test-timing.json.
One later pre-resume-check run recorded 973.6 seconds wall time, including a
924.7-second pure backtest call, but only 30.3 seconds CPU time for the process.
That localizes the interval and supports an execution/suspend-like pause rather
than a teardown wait; its exact external cause is not proven. Windows System event
logs were not accessible in this workspace. This captured run is retained as
`test-timing-long-pause.json`, rather than being presented as a timing success.

Windows kernel waits [exclude low-power time](https://learn.microsoft.com/en-us/windows/win32/api/synchapi/nf-synchapi-waitforsingleobject).
The runner now checks the measured wall deadline after every short wait AND after
child completion, so a late success after resume returns a nonzero deadline result.
Per-phase post-run checks also reject an over-budget phase. Heartbeats record
perf-counter, monotonic and CPU gaps and the active test/phase. No watchdog can
execute while its host is suspended; it must reject an overshoot when execution resumes.
No historical fixture/teardown hang is claimed fixed without evidence. Shutdown is
now bounded to ten seconds and unexpected adapter errors retain safe failure codes.
Retry handling re-raises programming/schema errors instead of renaming them outages.
Catch-all isolation remains at wallet/feed/optional-source boundaries so one failed
source does not erase successful observations. Raw exception strings are not diagnostics.

## Dependency and CI evidence

Install both exact files: `requirements.lock` and `requirements-dev.lock`.
The latter declares packaging, pytest-timeout, Ruff, pytest, pip-audit and their
closure. OSV audit reports bind every query name/version/marker/line to the SHA-256
of the exact input locks, including host-inactive markers. Pagination is followed
within a bounded budget. Non-== pins, incomplete responses and network failure
return a nonzero exit and an unverified report. Findings also fail the command.

CI uses contents:read, immutable Action SHAs, no persisted checkout credentials,
job deadlines, full-package E4/E7/E9/F/B lint, four OS/Python test jobs, and a separate
strict pip-audit + OSV job. Evidence is uploaded even on failure. This workflow is
configured; a GitHub-hosted run is not claimed from local tests.

These audits cover locked Python runtime/development distribution versions, not
container OS packages, unknown vulnerabilities or all build-isolation tools.
Docker, Linux runtime and PostgreSQL concurrency were not run locally. SQLite
migration/concurrent writes and Windows Python 3.12 behavior have actual tests.
