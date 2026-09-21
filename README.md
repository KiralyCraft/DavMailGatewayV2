# DavMailGatewayV2

DavMailGatewayV2 lets applications on a trusted network send email through Microsoft 365. An application speaks ordinary SMTP to this service; the service stores the message in a local queue and submits it to Microsoft using a connected account. The administrator signs in to Microsoft and chooses that account's own address or one delegated From address in the web UI. Existing deployments may also retain a separately configured legacy sender.

    Your application → SMTP → DavMailGatewayV2 → Microsoft 365 → recipients
                                  │
                                  └─ password-protected administration page

This is a **send-only bridge**, not an inbox or general mail server. It does not receive mail, offer IMAP/POP, or authenticate SMTP clients. Access to its SMTP port must be limited by both an IP allowlist and a firewall. The website has a separate administration password; signing in to Microsoft connects the sending account.

Messages are acknowledged to the SMTP client after they are committed to SQLite. A message marked “submitted” was accepted by Microsoft, which does not prove recipient delivery. The default total sending rate is 0.5 messages per second; this is not a high-volume transactional mail service.

The project began as a replacement for a patched DavMail sending setup. New deployments should use the Microsoft Graph backend with their own app registration. The EWS backend and migration tool remain for existing installations. No mailbox credentials or deployment state are included in this repository.

[WebUI preview with synthetic data](docs/ui-preview.png) · [Mobile preview](docs/ui-mobile-preview.png) · [Deployment guide](docs/DEPLOYMENT.md)

## What is included

| Component | Behavior |
|---|---|
| SMTP | Port 1025 by default; no AUTH advertisement or password; explicit client CIDR allowlist |
| Sender | The selected address must be the effective `From`; an optional legacy address remains accepted when configured |
| NAT | Case-sensitive `_NAT_` in **From**, not Subject; rewrite From and append original identity to Subject |
| Recipients | Preserves DavMail's union of To/Cc/Bcc and SMTP recipients; missing envelope recipients become Bcc |
| EWS compatibility | OAuth bearer authentication; MIME `CreateItem` / `SendAndSaveCopy`, or `SendOnly` |
| Graph migration | Delegated `Mail.Send`, base64 MIME `POST /me/sendMail`, Sent Items saving; optional second connection requests `Mail.Send.Shared` |
| Browser login | Microsoft authorization code, PKCE, state, nonce, and signature-validated ID token; no Microsoft password collected |
| Queue | MIME plus metadata committed together to SQLite WAL with `synchronous=FULL` before SMTP 250 |
| Administration | Login/disconnect account, pause/resume delivery, set rate, inspect/export/retry/cancel retained messages |
| Statistics | Lifetime accepts/submissions, queue states, failed/uncertain items, NAT count via API, rolling recipient budget, hourly activity, disk usage |
| Deployment | Python package and CLI; systemd, Docker and nginx examples; offline tests and benchmark tool |

Supported runtime: **Linux, Python 3.11 or newer**. Tested here with Python 3.13.5. POSIX locking and private directory permissions are required. Four runtime dependencies are declared in `pyproject.toml`.

## First-time setup

You need Linux, Python 3.11 or newer, a Microsoft 365 mailbox permitted to send mail, and a Microsoft Entra application registered for **delegated Microsoft Graph Mail.Send**. The tenant may require administrator consent. Keep the SMTP listener on loopback until you have restricted which applications may reach it.

From this source directory:

~~~bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .

noreply-gateway init --config ./gateway.toml \
    --backend graph \
    --tenant-id YOUR-TENANT-UUID \
    --client-id YOUR-APPLICATION-UUID \
    --redirect-uri https://login.microsoftonline.com/common/oauth2/nativeclient
noreply-gateway check --config ./gateway.toml
noreply-gateway serve --config ./gateway.toml
~~~

Replace the example UUIDs with your own values. The initialization command asks for an **administration website password** and creates private local state. It does not ask for a Microsoft password or sender address. Open http://127.0.0.1:8080 on the server, log in, and choose **Connect Microsoft account**. After sign-in, choose the read-only default address returned by Microsoft or enter a custom address the account may send from. With the native-client redirect shown above, paste the final redirect URL into the administration page. Never share that URL: it contains a short-lived authorization code.

Only applications on the gateway host can use the default SMTP listener. To add another trusted application, configure its exact address in the SMTP allowlist and firewall as described below. Once the Microsoft account is connected, use the [normal client example](#normal-client-example) to send a controlled test message. Check the queue and recipient mailbox separately; a local SMTP success only confirms durable acceptance by the gateway.

For headless initialization, `--admin-password-env` reads the website password from an environment variable. Do not put passwords directly in shell arguments. The `check` command validates local configuration and state; it does not verify tenant consent or send mail. The [deployment guide](docs/DEPLOYMENT.md) covers systemd, private state, backups, and a TLS reverse proxy. The web UI can also be served under a URL path such as `/internal/mailgateway/` when the proxy forwards the same path and `web.base_url` matches the public URL.

### Optional migration of the existing refresh token

Prefer a fresh Microsoft login. Some patched DavMail configurations stored refresh tokens as **base64, not encryption**. Treat any such legacy credential as exposed and replace or revoke it through the tenant account-security procedure.

When explicitly retaining a still-valid local credential during migration, add `--import-token` to the **initial** initialization command:

```bash
noreply-gateway init --config ./gateway.toml \
    --from-davmail /path/to/noreply.davmail --import-token
```

This decodes the patched format locally and immediately stores it in the encrypted vault. It does not print the token, copy the source properties file, use the obsolete embedded SMTP password, or make a network request. Imported credentials use the original v1 resource-based EWS refresh grant. They cannot be imported into Graph mode. Stop the old DavMail instance before live cutover so the old and new gateways do not compete for the same submission traffic or rotate credentials concurrently.

**No real refresh token, account password, or original properties file is included in this distribution.**

## Microsoft account login

### EWS compatibility path

The generated EWS configuration retains the source's default DavMail public application ID, `common` tenant selector, and native-client redirect. Existing token import therefore has a direct migration path. A fresh browser login asks for delegated `EWS.AccessAsUser.All`, `openid`, `profile`, and `offline_access`; tenant consent and Conditional Access policy still apply. The source's current sign-in acceptance and future EWS availability cannot be established offline.

For the default native-client redirect, Microsoft navigates to:

```text
https://login.microsoftonline.com/common/oauth2/nativeclient?code=...&state=...
```

Copy the **entire final address** from that tab into the WebUI's Final redirect URL field. A blank or informational native-client page is not itself an error. The code is protected by a per-session PKCE verifier and state. Do not share this address, include it in screenshots, or paste it into logs. Login attempts expire after ten minutes and are consumed once. Start a new flow after an error.

### Graph path for continued operation

**EWS disablement in Exchange Online begins in October 2026, with full retirement in 2027.** Keep EWS only as a compatibility/migration option. Graph is included for continued use; this is an addition to, not a claim about, the supplied Java source. See the [Microsoft Exchange Team announcement](https://techcommunity.microsoft.com/blog/exchange/exchange-online-ews-your-time-is-almost-up/4492361), checked September 10, 2026.

Register an application under your tenant's control. Grant **delegated Microsoft Graph `Mail.Send`**, permit the required interactive account login, and obtain any required tenant consent. Configure a matching redirect registration. Two supported configurations are:

- A native/public-client application with the Microsoft native-client redirect, using the manual paste-back flow above. A public client does not need an application secret.
- An application registered with the exact WebUI callback, such as `https://gateway-admin.example.org/oauth/callback`. For a confidential **Web** application, also set `account.client_secret_env` to the name of the environment variable containing its client secret. The HTTPS site must reverse-proxy to this service. Local loopback HTTP callbacks are also supported with a compatible registered public-client redirect.

Use your own application client ID for Graph; do not assume the DavMail application has Graph consent. Example initialization:

```bash
noreply-gateway init --config ./gateway.toml \
    --backend graph \
    --sender noreply@example.org \
    --tenant-id YOUR-TENANT-UUID \
    --client-id YOUR-APPLICATION-UUID \
    --redirect-uri https://login.microsoftonline.com/common/oauth2/nativeclient
```

The two UUID arguments above must be replaced with actual UUIDs. Keep `save_in_sent = true`: this package's Graph backend uses MIME submission, not the JSON `saveToSentItems=false` path. It deliberately does not implement Graph draft creation or large-attachment upload sessions.

To switch a legacy configured account, pause delivery, let active attempts finish, stop the service, change `account.backend`, tenant, client, and redirect settings, then restart and sign in again. Changing the credential identity invalidates the cached credential. Changing the configured mailbox itself is refused while any bodies remain retained.

### Microsoft sending account and optional legacy route

In the administration page, choose **Connect Microsoft account**. This Graph connection is stored separately from an optional legacy sender. By default it uses the application's tenant, client ID and redirect settings in `[account]`. The application must be allowed delegated `Mail.Send`, `Mail.Send.Shared`, and `User.Read`; tenant consent may be required.

For automatic return from Microsoft without pasting a URL, register the exact gateway URL ending in `/oauth/callback` as a **Web** redirect in a Microsoft Entra application you control. Set `[additional].client_id` and `[additional].redirect_uri` to that application's values, and set `[additional].client_secret_env` to the name of an environment variable holding its client secret. The browser then returns to the signed-in administration page automatically. Use `[additional].tenant_id` only if this account needs a different tenant. These fields are optional and leave an existing legacy account's OAuth settings unchanged. The exact redirect registration, tenant consent and client secret must exist before enabling it; a `nativeclient` redirect continues to use the manual paste flow. Keep the secret out of the TOML file and source control.

After Microsoft sign-in, the gateway reads the signed-in account's `mail` address from Graph `/me` (or its `userPrincipalName` when `mail` is empty). The sender dialog offers **Use default address**, displayed read-only, or **Use a custom address**, entered by the administrator. The gateway cannot list delegated mailboxes. Microsoft requires the signed-in user to have Exchange **Send As** or **Send on Behalf** rights for a custom address, and checks those rights when mail is submitted. A rejected Send As attempt is reported as failed in the queue. [Microsoft's delegated sending guidance](https://learn.microsoft.com/en-us/graph/outlook-send-mail-from-other-user) explains the permission model.

SMTP clients select the connected account by putting the exact selected address in the message's `From` header. If an older installation has `account.sender` configured, that address remains accepted and uses its own separate Microsoft connection; the web UI shows it inside **Legacy configured sender**. Other From addresses are rejected. Changing the selected address affects new SMTP submissions; already queued messages retain their From header and remain routed through the same connection. Send a controlled message and check the received From address before changing an automated application's mail settings.

## Configure trusted SMTP clients

By default only loopback clients can connect. Edit the existing `[smtp]` section, using the real server address and **only the actual trusted client IPs/subnets**:

```toml
[smtp]
host = "10.20.30.10"
port = 1025
allowed_networks = ["10.20.30.21/32", "10.20.30.22/32", "127.0.0.1/32"]
```

Restart after editing configuration. Do not append duplicate TOML sections. These example addresses must be replaced. Enforce the same boundary with a firewall. World-wide `/0` allowlists are rejected; that check is not a substitute for reviewing the CIDRs you choose.

Clients are passwordless, so **any allowed client can submit as an enabled sender**. A correct `From` is a routing/policy requirement, not proof of user identity. Do not expose this listener to the public Internet. When Docker, a proxy, or NAT changes the observed peer IP, review the resulting trust boundary rather than broadly trusting a bridge address without understanding who can reach it.

Plain SMTP is intended for a trusted host/network. Optional `smtp.tls_cert` and `smtp.tls_key` enable **implicit TLS on that listener**; use `smtplib.SMTP_SSL` in that case. STARTTLS is not implemented. Neither TLS mode adds SMTP AUTH.

### Normal client example

```python
import smtplib
from email.message import EmailMessage

message = EmailMessage()
message["From"] = "selected-address@example.org"  # Match the web UI's selected From address.
message["To"] = "recipient@example.org"
message["Subject"] = "Gateway test"
message.set_content("A test sent through the standalone gateway.")

with smtplib.SMTP("127.0.0.1", 1025, timeout=30) as smtp:
    smtp.send_message(message)  # Deliberately no smtp.login(...).
```

No Microsoft password or access token is needed on the client. In compatibility with the Java source, SMTP `MAIL FROM` is not used as the final sending identity; **the effective MIME From is validated**. A syntactically valid empty envelope sender is accepted. Replies are not automatically redirected to the original envelope sender. The package does not observe bounces or generate asynchronous DSNs.

### Preserved NAT example for legacy senders

Submit:

```text
From: _NAT_alice@example.org
To: recipient@example.org
Subject: Build completed
```

The queued MIME sent to Microsoft becomes:

```text
From: noreply@example.org
To: recipient@example.org
Subject: Build completed (Sender: alice@example.org)
```

This behavior requires an optional configured legacy sender such as `noreply@example.org`; senderless installations reject NAT-marked messages. The marker is case-sensitive, can also occur in the From display name, and all its occurrences are removed from the identity appended to Subject. NAT does not change recipients, the MIME body, attachments, or an existing Reply-To. Putting `_NAT_` in Subject alone does nothing. When Subject is absent, the source's literal `null (Sender: ...)` behavior is retained. Header whitespace/encoding is normalized by Python; byte-for-byte preservation of Java header serialization is not promised.

For safety, the final effective From is resolved **after applying Resent-From but before NAT and validation**. This prevents Resent-From from undoing the sender rewrite. Duplicate/conflicting sender headers are rejected.

### Recipient behavior deserves special attention

The supplied DavMail path sends to the **union** of MIME To/Cc/Bcc and SMTP `RCPT TO`, adding envelope-only recipients as Bcc. The default `account.recipient_policy = "davmail_union"` preserves that behavior, including header-only recipients. This is different from a normal SMTP relay that treats only its envelope as authoritative.

Set `recipient_policy = "envelope_strict"` to require every header recipient to also appear in the SMTP envelope. Optional `allowed_recipient_domains` is enforced on the entire effective set, not just RCPT commands. The count limit also applies to that entire set. Validate To/Cc/Bcc behavior in the live tenant before using production recipients.

## Queue semantics and administration

```text
trusted application -> SMTP policy/NAT -> SQLite durable commit -> SMTP 250
                                               |
                                               v
                                 rate/quota-controlled workers
                                               |
                                      EWS or Microsoft Graph
                                               |
                                  submitted / retry / held state
```

**SMTP 250 means locally committed**, not Microsoft acceptance and not final delivery. This is an intentional change from DavMail's synchronous upstream send, needed to absorb bursts without holding every application connection open. The gateway assumes responsibility for retained mail after its 250 reply.

| State | Meaning / operator action |
|---|---|
| `queued` | Stored, waiting for a permit and recipient budget |
| `sending` | An upstream attempt is in flight; it cannot be cancelled locally mid-send |
| `submitted` | Upstream reported acceptance; local body removed, metadata retained |
| `retry` | Definite transient/authentication rejection; retry/backoff or operator login needed |
| `failed` | Permanent rejection, attempt limit, or queue age exceeded; body retained for review |
| `uncertain` | Response lost, ambiguous server outcome, or interrupted attempt at restart; **no automatic retry** |
| `cancelled` | Operator removed the retained body; does not recall already submitted mail |

HTTP 429 and explicit EWS throttling errors schedule backoff. `Retry-After` and EWS `BackOffMilliseconds` are respected; a provider delay is not truncated to the local exponential-backoff maximum. Authentication failures pause submission. An operator retry resets that message's retry age/count budget but retains its original acceptance timestamp.

At restart, previously `sending` messages become `uncertain`. An explicit retry of one of these requires a duplicate-risk acknowledgement. Check Microsoft message trace/Sent Items or the recipient before making that decision. Stable Message-ID and request correlation ID assist investigation, but **neither is an exactly-once idempotency key**. A connection can also disappear after queue commit but before the SMTP 250 reaches its client; resubmission then may duplicate the message. The source's single-last-Message-ID suppression is deliberately not copied because it could silently lose legitimate messages or retries.

Default limits: 100,000 retained messages, 5 GiB logical retained MIME, 256 MiB free-disk floor, 2,000,000 bytes per rewritten message, 500 effective recipients per message, 128 SMTP connections, 64 MiB aggregate received DATA buffering, 20 automatic attempts, 72-hour retry age. Rewriting can increase header size, so leave room below the message-size limit. Received buffers are bounded; total process memory also includes parsed headers, rewritten MIME, SQLite and HTTP buffers.

Failed and uncertain bodies remain until explicitly retried or cancelled; they consume queue capacity. Submitted/cancelled metadata expires after seven days; lifetime counters remain. SMTP rejection counters reset on restart. SQLite file size does not immediately shrink when a body is removed; freed pages are reused. Provision capacity for the database, WAL, and metadata **in addition to** the logical MIME quota. Do not set the free-disk floor to zero in production.

When capacity/disk/commit is unavailable, SMTP returns a temporary failure instead of acknowledging uncommitted mail. Queue faults halt intake and sending. Monitor the WebUI or authenticated `/api/stats`, especially failed/uncertain counts, queue age, disk space and remaining recipient budget. Pausing delivery intentionally does **not** pause SMTP intake.

`/health/live` indicates the HTTP process is reachable. `/health/ready` additionally checks local queue health/capacity, an available local credential and unpaused state; it is not a live Microsoft probe and does not establish remaining upstream quota or deliverability. It can be healthy while a provider backoff is active. HTTP health requests must use the configured Host header.

## Performance and Microsoft limits

Microsoft documents an Exchange Online mailbox message rate of **30/minute**, with a **10,000-recipient/day** limit and additional service/tenant restrictions. Graph also applies **four concurrent requests** and **10,000 requests per ten minutes** per app/mailbox, plus upload-volume limits. Graph acceptance is HTTP 202, not final delivery. Sources: [Exchange Online limits](https://learn.microsoft.com/en-us/office365/servicedescriptions/exchange-online-service-description/exchange-online-limits), [Graph throttling](https://learn.microsoft.com/en-us/graph/throttling-limits), [Graph sendMail](https://learn.microsoft.com/en-us/graph/api/user-sendmail?view=graph-rest-1.0), checked September 10, 2026.

100 messages/second is 6,000/minute: **200 times** the documented per-mailbox message rate. At one recipient per message it is 8.64 million recipients/day. Switching from EWS to Graph does not remove mailbox transport limits. Other applications using the mailbox also consume the same upstream allowance, which this gateway's local ledger cannot observe. Directory group expansion and other tenant rules can differ from the gateway's simple count of distinct effective addresses. The guard is an operational budget, not an authoritative Microsoft quota meter.

At sustained intake of 100/s with 0.5/s upstream, a 100,000-message queue would fill in roughly 16.8 minutes, assuming its byte limit or provider daily cap did not bind first. Queuing absorbs **bursts**, not an indefinite capacity mismatch. At a four-request concurrency limit, reaching 100/s would also require average upstream request latency below roughly 40 ms, even with all quota restrictions removed.

Measured offline in this container, using 32 persistent SMTP clients, four HTTP workers, SQLite WAL/FULL, all messages exercising NAT, and a loopback mock with a 5 ms response delay:

| Backend mock | Input size | Messages | Durable SMTP accepts/s | End-to-end mock submissions/s |
|---|---:|---:|---:|---:|
| EWS | 10 KiB | 3,000 | 637.11 | 290.83 |
| Graph | 10 KiB | 3,000 | 621.28 | 303.96 |
| EWS | 100 KiB | 2,000 | 261.58 | 166.06 |

All 8,000 mock submissions completed with zero observed duplicate queue IDs and no retained messages at the end. These are **single-run synthetic results**, not production guarantees. The clients, mock, and gateway shared a process/container. Real CPU, storage, fsync behavior, message size, TLS, network latency, and Microsoft policy change performance. No live sending rate, power-loss durability, or final recipient delivery was measured. Raw JSON results and methodology are in `docs/`.

For a hard requirement of **sustained 100 actual sends/second**, replace the upstream with an approved high-volume transactional-mail transport. That is a different delivery backend/contract, not a rate-control adjustment. The current package implements EWS and Graph only; it does not bypass Microsoft restrictions, rotate through accounts, or claim that raising its rate setting grants additional capacity.

## Security and deployment

Use the WebUI over loopback/SSH or a TLS reverse proxy. The UI requires a separate scrypt-hashed password, session cookie, same-origin requests and CSRF token for mutations; it validates Host, forbids framing, and does not load third-party scripts. Set `web.base_url` to the exact browser URL, including a path prefix when the proxy serves it under one. The Python web listener itself is HTTP; `https://` in `base_url` requires a correctly configured external reverse proxy. Never publish the underlying HTTP listener publicly. `web.trusted_proxy_ips` may name the exact reverse-proxy peers whose final `X-Forwarded-For` address is used for per-client login limits. The proxy must append its actual client address after any client-supplied values. Password attempts are limited to five per client and forty overall in fifteen minutes, with a Retry-After response; these in-memory counters reset when the service restarts. Proxy access logs must not capture OAuth query strings; the provided nginx example disables them.

Microsoft refresh tokens are encrypted with a generated Fernet key; rotated credentials are persisted before use. The private state directory must be 0700 and the key 0600. **The key and ciphertext are accessible to the service account**: this protects against accidental disclosure of the token file alone, not a compromised service user/root or a stolen complete state directory. Message bodies and metadata in SQLite are not encrypted by the application. Use encrypted storage and protected backups where required.

Run one process per state directory, on a local filesystem. There is no multi-host/active-active mode or queue over NFS. Do not point two instances at the same mailbox/credential as a throughput workaround. Back up while the service is stopped, including `queue.sqlite3`, any remaining `-wal`/`-shm` companions, encrypted records, and `vault.key`. Restoring an older queue snapshot can replay already submitted messages; pause and reconcile it before enabling delivery. Never delete retained queue data as routine troubleshooting.

See `deploy/noreply-gateway.service`, `deploy/Dockerfile`, `deploy/compose.yaml`, `deploy/nginx.conf`, and [Deployment guide](docs/DEPLOYMENT.md). Docker/systemd examples require environment-specific paths, addresses and permissions; they were not executed against a production host here.

## Testing and boundaries

```bash
python -m pip install '.[test]'
python -m pytest -q
python tools/benchmark.py --backend ews --messages 3000 --message-bytes 10240
python tools/benchmark.py --backend graph --messages 3000 --message-bytes 10240
```

The benchmark creates only fresh temporary state and a loopback mock. It never reads production configuration or credentials. See [test report](docs/TEST_REPORT.md) and [source parity](docs/SOURCE_PARITY.md).

Deliberate non-features: SMTP AUTH, STARTTLS upgrade, SMTPUTF8 addresses, DSN extension, LMTP, inbound mail/bounce processing, Microsoft password login, multi-mailbox routing, application-only Graph auth, draft/large-attachment upload sessions, mailbox/calendar synchronization, automatic live-delivery verification, and HA. SMTP requires CRLF framing and standard line lengths; encode binary attachments using normal MIME transfer encodings. Preserve a separate route for monitoring the account's received bounces.

The source is supplied under GPL-2.0-or-later with DavMail attribution in `NOTICE`; no Java source/binaries or user credential files are bundled. See `LICENSE`.
