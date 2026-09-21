# Verification report

Updated: September 21, 2026. Release candidate: 0.1.0.

## Executed automated checks

**103 pytest cases passed** in approximately two seconds on Python 3.14. The suite uses temporary state and local fixtures, not a production account. Python syntax checks and `node --check src/noreply_gateway/static/app.js` passed.

The tests cover normal/case-insensitive allowed senders; address and display-name NAT; marker case sensitivity; absent Subject; Subject-only marker rejection; duplicate/malformed sender headers; Resent-From safety; recipient union/Bcc; strict recipient policy; domain/count limits; body-byte preservation; stale authentication header removal; syntactically invalid and UTF-8 envelope addresses; passwordless smtplib reuse; empty envelope/RSET/SMTP sequencing; dot transparency; bare-LF rejection without command smuggling; buffer and queue backpressure; CIDR rejection; durable SQLite settings; capacity batching; reused Message-ID acceptance; tied-timestamp pagination; body cleanup; quota reservation/release; uncertain-attempt recovery; database commit failure; pause/backoff/age/history handling; and an indexed ready-queue lookup.

Upstream tests use real loopback HTTP with EWS and Graph request encoding. They exercise successful submissions, complete reading of fragmented/chunked responses, HTTP 401/403/429/400/500 classification, EWS busy/quota/access/recipient/timeout errors, lost-response uncertainty, report item class, SendOnly, and persistent dispatcher outcomes including a two-hour Retry-After.

OAuth tests use generated RSA keys and fake credential strings. They verify signatures, expected identity, nonce, audience, issuer, expiry, tenant format, forged-signature rejection, PKCE parameters, state/flow expiry, serialized concurrent refresh, encrypted rotated-token persistence, login validation before storage, concurrent rejection of one stale token generation, account-configuration invalidation, and refusal to use an unpersisted rotation.

The administration/API tests cover login, rate limiting, cookie flags, unauthenticated denial, same-origin/CSRF/Host checks, security response headers, stats without tokens, queue export/retry/cancel rules, per-session OAuth state consumption, health endpoints, properties parsing, selective EWS import, private initialization, password replacement, configuration round-trip, unsafe configuration rejection, exclusive instance locking, prefixed routes with a trusted proxy peer, and per-client password attempt limits.

## Installed-package smoke test

The built wheel was installed into a separate temporary target using `pip --no-deps --no-index`, with the already installed runtime dependencies. The wheel's own module/CLI initialized a new private state directory, started the complete service in a separate process, answered live health, accepted a passwordless SMTP message, served authenticated administration statistics, retained the queued message, and exited successfully on SIGTERM. No account credential was present and no upstream submission occurred. This verifies wheel content and service wiring in this environment, not a clean dependency install on every supported distribution.

## Browser / UI checks

The environment's managed Chromium blocked navigation to a loopback website (`ERR_BLOCKED_BY_ADMINISTRATOR`), so a real browser-to-server navigation was **not** validated here. That restriction was left intact.

The unchanged frontend HTML/CSS/JavaScript was then rendered in Chromium's blank document with **in-memory mock API responses**, without browser network access. Login-screen transition, statistics rendering, queue filtering, the Microsoft sign-in link, desktop layout and 390-pixel mobile layout passed, with zero JavaScript exceptions and no horizontal viewport overflow. `docs/ui-preview.png` and `docs/ui-mobile-preview.png` show synthetic data only. The real backend HTTP/API paths were separately covered by pytest. The optional `tools/render_preview.py` reproduces the offline frontend check and requires Playwright plus a local Chromium installation, not needed for the production package.

This separation does not establish real-browser OAuth callback, TLS-proxy, or tenant sign-in behavior. Those remain live deployment acceptance tests.

## Full-pipeline load runs

Real persistent SMTP clients sent into the real SMTP listener. Every message was rewritten through NAT, committed to SQLite WAL/FULL, claimed by the actual dispatcher, encoded by the actual EWS or Graph backend, posted over HTTP to a local mock, acknowledged, and moved to `submitted` in SQLite. The mock checked MIME sender rewriting and unique queue/request IDs. No Microsoft credentials or production configuration were loaded. The local daily recipient guard was explicitly disabled **only inside the mock benchmark**, and the synthetic upstream rate was set to 1,000/s to measure implementation capacity.

| Mock | Input bytes | Count | SMTP durable accepts/s | End-to-end mock submissions/s | SMTP p95 transaction ms |
|---|---:|---:|---:|---:|---:|
| EWS | 10,240 | 3,000 | 637.11 | 290.83 | 54.28 |
| GRAPH | 10,240 | 3,000 | 621.28 | 303.96 | 57.17 |
| EWS | 102,400 | 2,000 | 261.58 | 166.06 | 130.36 |

All 8,000 messages in the final three recorded runs were accepted locally and submitted to the mock, with zero observed duplicate request IDs and zero retained messages at completion. The raw JSON records include latency percentiles, timing, concurrency, WAL settings and process memory. These are single-run observations, not statistical confidence intervals.

The test process shared the gateway, producers and mock on Linux x86-64, with five logical CPUs visible, Python 3.13.5, 32 SMTP connections, four upstream workers and a 5 ms synthetic server delay. A configured SQLite FULL mode was verified; power-loss behavior or correctness of the underlying virtual storage's fsync was not tested. Peak RSS includes the producers and mock, not just production server overhead.

## Not established by these tests

No live Microsoft account login, refresh, consent, Conditional Access, EWS availability, real send, Sent Items creation, recipient receipt, Bcc privacy at final delivery, bounce handling, or 100/s Microsoft throughput was verified. No actual token was used or transmitted. Provider error handling was tested against controlled protocol responses, not tenant throttling in the wild.

No production Docker/systemd/nginx deployment, implicit-TLS SMTP handshake, fresh installation on every supported Python version, exhaustive SMTP fuzzing, external security audit, large-attachment upload, disk-power-loss recovery, sustained multi-hour load, very large historical database, or HA setup was tested. API and frontend checks are not a claim that the WebUI was exercised against Microsoft in a real browser.

An authorized, low-volume live acceptance test with controlled recipients is necessary before cutover. See DEPLOYMENT.md. Do not benchmark the actual mailbox at the mock rates.
